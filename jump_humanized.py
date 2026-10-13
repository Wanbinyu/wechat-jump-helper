#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信【跳一跳】拟人化辅助（PC + ADB，免 root）

原理：ADB 截图 -> OpenCV式识别小人与目标块 -> 计算按压时长 -> ADB 模拟按压
拟人化：落点正态偏移 / 按压时长抖动 / 随机节奏 / 疲劳后主动失误结束本轮

使用前提：
  1. 手机打开「USB 调试」并连接电脑，手机上弹窗点允许
  2. 手机先进入跳一跳游戏界面，看到小人和方块后运行本脚本
  3. pip install pillow （已装则跳过）

运行：
  python jump_humanized.py                    # 自动选择唯一设备
  python jump_humanized.py --serial <设备号>   # 多台设备时指定
  python jump_humanized.py --speed 0.7179     # 手动指定速度系数(px/ms)
  每轮回车开始，跳完一轮自动结束；Ctrl+C 退出

参数调整：
  如果总是跳过头 -> 调小 --speed；总是跳不到 -> 调大 --speed
"""

import argparse
import io
import math
import random
import subprocess
import sys
import time
from collections import deque

try:
    from PIL import Image
except ImportError:
    print("缺少 Pillow：请先执行  pip install pillow")
    sys.exit(1)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ==================== 拟人化模块（与 APK 版 Humanizer 一致） ====================

class Humanizer:
    """拟人化随机扰动：让落点、按压、节奏带"人味"，疲劳后主动失误"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.jump_count = 0
        self.finished = False
        # 每轮随机期望跳数（无连击时约等于分数），默认不求高分
        self.fatigue_threshold = random.randint(18, 40)

    def offset_landing(self, cx, cy, half_width):
        """在目标块范围内按近似正态分布随机偏移落点"""
        self.jump_count += 1
        over = self.jump_count - self.fatigue_threshold
        if over >= 2:
            # 明显失误一跳：瞄向块边缘之外，自然结束本轮
            self.finished = True
            angle = random.random() * math.pi
            return (int(cx + math.cos(angle) * half_width * 1.8),
                    int(cy + math.sin(angle) * half_width * 0.9))
        if over > 0:
            # "手感变差"：偏移幅度逐渐放大
            half_width = int(max(half_width, 24) * (1 + over * 0.5))
        sigma = max(3, int(half_width * 0.12)) if half_width > 0 else 6
        dx = int(round(random.gauss(0, sigma)))
        dy = int(round(random.gauss(0, sigma / 2.0)))
        return cx + dx, cy + dy

    def jitter_press_time(self, time_ms):
        """按压时长 ±1.5% 高斯抖动，模拟手指误差"""
        factor = max(0.985, min(1.015, 1 + random.gauss(0, 0.008)))
        return max(80, int(round(time_ms * factor)))

    def next_wait_ms(self, base_ms):
        """两跳之间的等待：基础时长 + 随机停顿，偶尔"犹豫"几秒"""
        wait = base_ms + 400 + random.randint(0, 1800)
        if random.randint(0, 99) < 6:
            wait += 3000 + random.randint(0, 5000)
        return wait

    @staticmethod
    def screenshot_wait_s():
        """截图后的等待随机化（原版固定 1233ms）"""
        return (850 + random.randint(0, 700)) / 1000.0


# ==================== ADB 封装 ====================

def run_adb(adb, serial, args, timeout=20):
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def pick_device(adb, serial):
    """无 serial 时自动挑选唯一在线设备"""
    r = run_adb(adb, None, ["devices"])
    lines = r.stdout.decode("utf-8", "replace").splitlines()
    devices = [l.split("\t")[0] for l in lines[1:] if "\tdevice" in l]
    if serial:
        if serial in devices:
            return serial
        print("指定的设备不在线，当前在线设备：%s" % devices)
        sys.exit(1)
    if len(devices) == 1:
        return devices[0]
    if not devices:
        print("未检测到设备：请打开手机 USB 调试并连接电脑，允许授权弹窗后重试")
    else:
        print("检测到多台设备，请用 --serial 指定：%s" % devices)
    sys.exit(1)


def screenshot(adb, serial):
    """exec-out screencap 直接读 PNG 字节流"""
    r = run_adb(adb, serial, ["exec-out", "screencap", "-p"], timeout=15)
    if r.returncode != 0 or not r.stdout.startswith(b"\x89PNG"):
        return None
    try:
        return Image.open(io.BytesIO(r.stdout)).convert("RGB")
    except Exception:
        return None


def tap(adb, serial, x, y, ms):
    cmd = ["shell", "input", "swipe", str(x), str(y), str(x), str(y), str(ms)]
    run_adb(adb, serial, cmd, timeout=15)


# ==================== 图像识别 ====================

def color_dist(c1, c2):
    return abs(c1[0] - c2[0]) + abs(c1[1] - c2[1]) + abs(c1[2] - c2[2])


def find_piece(im):
    """识别小人：头部深色低饱和顶点 + 紫色底座最低行"""
    px = im.load()
    w, h = im.size
    x0, x1 = int(w * 0.1), int(w * 0.9)
    y0, y1 = int(h * 0.3), int(h * 0.65)

    top = None
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b = px[x, y]
            # 头部：深色且低饱和（排除深色方块）
            if r < 70 and g < 70 and b < 70 and max(r, g, b) - min(r, g, b) < 16:
                top = (x, y)
                break
        if top:
            break
    if not top:
        return None

    # 底座：从头顶向下找紫色底盘的最后一行
    bottom_y = None
    for y in range(top[1], int(h * 0.75)):
        r, g, b = px[top[0], y]
        if 45 < r < 140 and 45 < g < 140 and 80 < b < 175 and b - r > 18:
            bottom_y = y
        elif bottom_y is not None and y - bottom_y > 8:
            break
    py = bottom_y if bottom_y else top[1] + int(w * 0.09)
    return (top[0], py)


def find_target(im, piece):
    """识别目标块：找到第一个非背景像素后区域生长，返回 (中心x, 中心y, 半宽)"""
    px = im.load()
    w, h = im.size

    # 背景取样：顶部渐变 + 底部一行主色
    bg_top = px[w // 2, int(h * 0.12)]
    hist = {}
    for x in range(0, w, 2):
        c = px[x, h - 2]
        hist[c] = hist.get(c, 0) + 1
    bg_bottom = max(hist, key=hist.get)

    def is_bg(c):
        return (max(abs(c[i] - bg_top[i]) for i in range(3)) < 20 or
                max(abs(c[i] - bg_bottom[i]) for i in range(3)) < 20)

    limit = piece[1] - int(w * 0.1)
    seed = None
    for y in range(int(h * 0.22), limit):
        for x in range(0, w):
            if not is_bg(px[x, y]):
                seed = (x, y)
                break
        if seed:
            break
    if not seed:
        return None

    # 区域生长（与 GameHelper 的 BFS 逻辑一致），边界限制避免溢出
    sx, sy = seed
    target_color = px[sx, sy]
    xmin_b, xmax_b = max(0, sx - int(w * 0.3)), min(w - 1, sx + int(w * 0.3))
    ymin_b, ymax_b = max(0, sy - int(w * 0.4)), min(limit - 1, sy + int(w * 0.4))

    visited = set()
    xmin = xmax = sx
    ymin = ymax = sy
    q = deque([seed])
    while q:
        x, y = q.popleft()
        if not (xmin_b <= x <= xmax_b and ymin_b <= y <= ymax_b):
            continue
        if (x, y) in visited:
            continue
        visited.add((x, y))
        c = px[x, y]
        if max(abs(c[i] - target_color[i]) for i in range(3)) > 16:
            continue
        xmin, xmax = min(xmin, x), max(xmax, x)
        ymin, ymax = min(ymin, y), max(ymax, y)
        q.extend([(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)])

    half = (xmax - xmin) // 2
    return ((xmin + xmax) // 2, (ymin + ymax) // 2, half)


def find_white_center(im, cx, cy, half):
    """目标块中心白色圆点修正（白色方块会整块命中，质心即中心，同样正确）"""
    px = im.load()
    w, h = im.size
    x0, x1 = max(0, cx - half), min(w - 1, cx + half)
    y0, y1 = max(0, cy - half), min(h - 1, cy + half)
    sx = sy = n = 0
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            r, g, b = px[x, y]
            if r >= 240 and g >= 240 and b >= 240:
                sx += x
                sy += y
                n += 1
    if n > 30:
        return (sx // n, sy // n)
    return None


def default_speed(width):
    """按分辨率给出默认速度系数(px/ms)，来自原 App 适配值"""
    if width <= 720:
        return 0.479
    if width <= 1080:
        return 0.7179
    return 1.7114  # 2K（红米 K80 走这里）


# ==================== 主流程 ====================

def jump_once(adb, serial, speed, humanizer):
    """执行一次"截图-识别-跳跃"，返回 True 表示继续，False 表示结束"""
    time.sleep(Humanizer.screenshot_wait_s())
    im = screenshot(adb, serial)
    if im is None:
        return "fail", 0

    piece = find_piece(im)
    target = find_target(im, piece)
    if not piece or not target:
        return "fail", 0

    cx, cy, half = target
    white = find_white_center(im, cx, cy, max(half, 20))
    if white:
        cx, cy = white

    # 拟人化：随机偏移落点（疲劳阈值后偏移放大，最终主动失误）
    lx, ly = humanizer.offset_landing(cx, cy, max(half, 22))
    dis = math.hypot(lx - piece[0], ly - piece[1])
    press_ms = humanizer.jitter_press_time(int(dis / speed))

    w, h = im.size
    tx = random.randint(int(w * 0.1), int(w * 0.4))
    ty = random.randint(int(h * 0.72), int(h * 0.9))
    tap(adb, serial, tx, ty, press_ms)

    print("  第 %3d 跳 | 距离 %5d px | 按压 %4d ms" %
          (humanizer.jump_count, int(dis), press_ms))

    if humanizer.finished:
        return "miss", humanizer.jump_count
    wait = humanizer.next_wait_ms(max(press_ms * 5, 3333)) / 1000.0
    time.sleep(wait)
    return "ok", humanizer.jump_count


def run_round(adb, serial, speed):
    """一轮自动跳：到疲劳目标后主动失误结束"""
    humanizer = Humanizer()
    print("  本轮预计 %d~%d 跳后自然结束（按 Ctrl+C 可随时中断）" %
          (humanizer.fatigue_threshold, humanizer.fatigue_threshold + 2))
    fails = 0
    while True:
        try:
            result, count = jump_once(adb, serial, speed, humanizer)
        except (subprocess.SubprocessError, OSError) as e:
            print("  ADB 执行异常：%s" % e)
            result, count = "fail", 0

        if result == "fail":
            fails += 1
            if fails >= 2:
                print("  连续识别失败（可能已结束游戏或画面异常），本轮停止")
                return
            time.sleep(2)
            continue
        fails = 0

        if result == "miss":
            time.sleep(3)  # 等待失误落地动画
            print("  本轮结束，共 %d 跳（约 %d 分）" % (count, count))
            return


def main():
    parser = argparse.ArgumentParser(description="微信跳一跳 拟人化辅助（免root）")
    parser.add_argument("--adb", default="adb", help="adb 可执行文件路径")
    parser.add_argument("--serial", default=None, help="设备序列号（多设备时指定）")
    parser.add_argument("--speed", type=float, default=None,
                        help="速度系数 px/ms，默认按分辨率自动（720p:0.479 1080p:0.7179 2K:1.7114）")
    args = parser.parse_args()

    serial = pick_device(args.adb, args.serial)
    probe = screenshot(args.adb, serial)
    if probe is None:
        print("截图失败：请检查 USB 调试授权")
        sys.exit(1)
    speed = args.speed or default_speed(probe.size[0])
    print("设备：%s | 分辨率：%d x %d | 速度系数：%.4f" %
          (serial, probe.size[0], probe.size[1], speed))
    print("请切换到微信跳一跳游戏界面后操作。")

    try:
        while True:
            input("\n===== 按回车开始一轮自动跳，Ctrl+C 退出 =====")
            run_round(args.adb, serial, speed)
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
