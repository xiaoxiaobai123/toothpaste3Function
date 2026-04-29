#!/usr/bin/env python3
"""
显示链路测试脚本（不含 halcon / 相机 SDK / PLC 依赖）

用途
----
在新测试主机上验证:
  Python 生成 rgb565 文件 → C image_updater (inotify) → framebuffer (/dev/fb0)
整条链路是否正常工作。

运行
----
  python3 test_display.py

依赖
----
  pip install numpy opencv-python-headless

前提
----
  已跑过 deploy/install.sh 建立软链接和 tmpfs 文件:
    /home/pi/output_image.rgb565 -> /dev/shm/output_image.rgb565
  并且 image_updater.service 在跑.

可选参数
--------
  --path PATH       输出文件路径 (默认 /home/pi/output_image.rgb565)
  --interval SEC    每帧间隔秒数 (默认 1.0)
  --count N         只跑 N 帧后退出 (默认无限循环)
  --size WxH        单相机图尺寸 (默认 1024x1280)
"""

import argparse
import time
from enum import Enum
from pathlib import Path

import cv2
import numpy as np


# ---------- 和生产代码一致的枚举 ----------
class ProcessResult(Enum):
    OK = 1
    NG = 2
    EXCEPTION = 3


# ---------- 类级缓存（和生产版本一致）----------
_company_bar_cache = {}
_result_bar_cache = {}

_BAR_COLORS = {
    ProcessResult.OK: (0, 255, 0),
    ProcessResult.NG: (0, 0, 255),
    ProcessResult.EXCEPTION: (128, 128, 128),
}
_BAR_HEIGHT = 90


# ---------- 显示管线函数（从生产代码抽出，无 halcon）----------
def get_company_bar(width):
    if width in _company_bar_cache:
        return _company_bar_cache[width]

    current_dir = Path(__file__).parent
    company_png = current_dir / "company_name.png"
    if company_png.exists():
        bar = cv2.imread(str(company_png))
        if bar is not None and bar.shape[1] != width:
            scale = width / bar.shape[1]
            new_height = int(bar.shape[0] * scale)
            bar = cv2.resize(bar, (width, new_height), interpolation=cv2.INTER_AREA)
    else:
        bar = None

    if bar is None:
        # 没 png 就生成一个 fallback 条
        bar = np.full((60, width, 3), (142, 25, 7), dtype=np.uint8)  # 深蓝(BGR)
        cv2.putText(
            bar,
            "TEST MODE - NO HALCON",
            (max(20, width // 2 - 250), 42),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (255, 255, 255),
            2,
        )

    _company_bar_cache[width] = bar
    return bar


def get_result_bar(width, dtype, result):
    color = _BAR_COLORS.get(result, (128, 128, 128))
    key = (width, dtype.str, result)
    bar = _result_bar_cache.get(key)
    if bar is None:
        bar = np.full((_BAR_HEIGHT, width, 3), color, dtype=dtype)
        _result_bar_cache[key] = bar
    return bar


def add_result_bar(image, result):
    h, w = image.shape[:2]
    if not isinstance(result, ProcessResult):
        result = ProcessResult.EXCEPTION
    bar = get_result_bar(w, image.dtype, result)
    return cv2.vconcat([image, bar])


def add_company_name(image):
    bar = get_company_bar(image.shape[1])
    return cv2.vconcat([bar, image])


def combine_images(images):
    """两路相机图横拼.  canvas 复用, 省掉每帧 np.zeros 的大内存分配."""
    assert len(images) == 2, "expected two images"
    h, w = images[0].shape[:2]
    out_w = w * 2 + 10

    canvas = _canvas_cache.get((h, out_w))
    if canvas is None:
        canvas = np.empty((h, out_w, 3), dtype=np.uint8)
        canvas[:, w : w + 10] = (255, 255, 255)  # 白色分隔线只画一次
        _canvas_cache[(h, out_w)] = canvas

    canvas[:, :w] = images[0]
    canvas[:, w + 10 :] = images[1]
    return canvas


def convert_to_rgb565(image):
    """BGR → RGB565.  优先用 OpenCV 原生 C 实现, 失败回退到 numpy 位运算."""
    if image is None:
        return None
    try:
        # OpenCV 的 COLOR_BGR2BGR565 = 标准 5-6-5 (高 5 位 R, 中 6 位 G, 低 5 位 B)
        # 内部 C + SIMD 实现, 比 numpy 位运算快 3~5 倍
        # 输出 shape=(H, W, 2), dtype=uint8; 按字节写盘即可
        out = _rgb565_cache.get(image.shape)
        if out is None:
            out = np.empty((image.shape[0], image.shape[1], 2), dtype=np.uint8)
            _rgb565_cache[image.shape] = out
        cv2.cvtColor(image, cv2.COLOR_BGR2BGR565, dst=out)
        return out
    except cv2.error:
        # 退化: numpy 实现 (第一次跑或 OpenCV 版本太老时兜底)
        b = (image[:, :, 0] >> 3).astype(np.uint16)
        g = (image[:, :, 1] >> 2).astype(np.uint16)
        r = (image[:, :, 2] >> 3).astype(np.uint16)
        return (r << 11) | (g << 5) | b


_canvas_cache = {}
_rgb565_cache = {}


def save_rgb565_with_header(image, filename):
    """Supports both shapes:
    (H, W) uint16  -- numpy fallback path
    (H, W, 2) uint8 -- OpenCV BGR565 path (each pixel 2 bytes)
    """
    if image.ndim == 3:
        height, width, _ = image.shape
    else:
        height, width = image.shape
    header = np.array([width, height], dtype=np.int32)
    with open(filename, "wb") as f:
        f.write(header.tobytes())
        f.write(image.tobytes())


# ---------- 测试驱动：生成假"相机图" ----------
def make_fake_camera_image(cam_num, tick, result, width=1024, height=1280):
    """用 OpenCV 画一张带变化的测试图."""
    # 用 tick 调一个渐变背景色
    hue = (tick * 5) % 180
    bg_hsv = np.full((height, width, 3), (hue, 80, 30), dtype=np.uint8)
    img = cv2.cvtColor(bg_hsv, cv2.COLOR_HSV2BGR)

    # 画一个变化的圆（模拟工件检测）
    center = (width // 2, height // 2)
    radius = 180 + (tick * 7) % 120
    color = (100, 255, 100) if result == ProcessResult.OK else (100, 100, 255)
    cv2.circle(img, center, radius, color, 10)
    cv2.circle(img, center, radius // 2, (255, 200, 100), 4)

    # 画文字标识
    cv2.putText(img, f"Cam {cam_num}", (50, 90), cv2.FONT_HERSHEY_SIMPLEX, 2.5, (255, 255, 255), 4)
    cv2.putText(img, f"Tick {tick:04d}", (50, 170), cv2.FONT_HERSHEY_SIMPLEX, 1.8, (255, 255, 255), 3)
    cv2.putText(img, result.name, (50, height - 50), cv2.FONT_HERSHEY_SIMPLEX, 2.5, (255, 255, 0), 4)

    # 十字准星
    cv2.line(img, (center[0] - 30, center[1]), (center[0] + 30, center[1]), (255, 255, 255), 2)
    cv2.line(img, (center[0], center[1] - 30), (center[0], center[1] + 30), (255, 255, 255), 2)

    return img


def main():
    parser = argparse.ArgumentParser(
        description="显示链路测试 (无 halcon 依赖)", formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--path", default="/home/pi/output_image.rgb565", help="rgb565 输出文件路径")
    parser.add_argument("--interval", type=float, default=1.0, help="每帧间隔秒数")
    parser.add_argument("--count", type=int, default=0, help="跑 N 帧后退出, 0 = 无限循环")
    parser.add_argument("--size", default="1024x1280", help="单相机图尺寸, 格式 WxH")
    parser.add_argument("--profile", action="store_true", help="每帧打印各环节耗时细分, 方便定位瓶颈")
    args = parser.parse_args()

    width, height = map(int, args.size.lower().split("x"))

    # 前置检查
    output_path = Path(args.path)
    if not output_path.exists() and not output_path.is_symlink():
        print(f"⚠️  警告: {args.path} 不存在")
        print("    建议先跑 deploy/install.sh 建立软链接和 tmpfs 文件")
        print()

    print("=" * 68)
    print("  显示链路测试 (无 halcon / 无相机 / 无 PLC)")
    print("=" * 68)
    print(f"  输出路径:  {args.path}")
    print(f"  帧尺寸:    {width} x {height} (单相机)")
    print(f"  更新间隔:  {args.interval}s")
    print(f"  总帧数:    {'无限' if args.count == 0 else args.count}")
    print("  按 Ctrl-C 停止")
    print("=" * 68)
    print()

    tick = 0
    try:
        while args.count == 0 or tick < args.count:
            t0 = time.time()

            # 模拟 OK/NG 变化，让画面更丰富
            result1 = ProcessResult.OK if (tick // 5) % 2 == 0 else ProcessResult.NG
            result2 = ProcessResult.OK if (tick // 7) % 2 == 0 else ProcessResult.NG

            # 生成两路假图
            img1 = make_fake_camera_image(1, tick, result1, width, height)
            img2 = make_fake_camera_image(2, tick, result2, width, height)
            t_fake = time.time()

            # 完整显示管线
            img1 = add_result_bar(img1, result1)
            img2 = add_result_bar(img2, result2)
            combined = combine_images([img1, img2])
            final = add_company_name(combined)
            t_compose = time.time()

            # RGB565 转换
            rgb565 = convert_to_rgb565(final)
            t_convert = time.time()

            # 存盘
            save_rgb565_with_header(rgb565, args.path)
            t_save = time.time()

            total_ms = (t_save - t0) * 1000
            size_kb = rgb565.nbytes / 1024
            print(
                f"  tick={tick:04d}  "
                f"{result1.name}/{result2.name:3s}  "
                f"{final.shape[1]}x{final.shape[0]}  "
                f"{size_kb:5.0f}KB  "
                f"took {total_ms:5.1f}ms"
            )

            if args.profile:
                print(
                    f"         fake={(t_fake - t0) * 1000:5.1f}ms  "
                    f"compose={(t_compose - t_fake) * 1000:5.1f}ms  "
                    f"rgb565={(t_convert - t_compose) * 1000:5.1f}ms  "
                    f"save={(t_save - t_convert) * 1000:5.1f}ms"
                )

            tick += 1
            sleep_s = args.interval - (time.time() - t0)
            if sleep_s > 0:
                time.sleep(sleep_s)
    except KeyboardInterrupt:
        print("\n停止.")
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        raise


if __name__ == "__main__":
    main()
