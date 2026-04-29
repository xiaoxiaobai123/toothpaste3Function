"""Tests for legacy/fronback_display.py — verifies the operator-screen
output file matches the original fronback program's shape and content
markers (color bar at the bottom, dimensions matching expected resize).
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from legacy.fronback_display import render_frontback, render_height


def _solid(width: int, height: int, color: tuple[int, int, int]) -> np.ndarray:
    return np.full((height, width, 3), color, dtype=np.uint8)


# ----------------------------------------------------------------------
# render_frontback
# ----------------------------------------------------------------------
def test_frontback_writes_png_to_target_path(tmp_path: Path) -> None:
    out = tmp_path / "processed.png"
    img1 = _solid(1024, 1280, (10, 10, 10))
    img2 = _solid(1024, 1280, (200, 200, 200))

    written = render_frontback(img1, img2, is_front=True, output_path=out)

    assert written == out
    assert out.is_file()
    assert out.stat().st_size > 0


def test_frontback_dimensions_match_original_layout(tmp_path: Path) -> None:
    """Original layout:
        each panel resized to 0.4x → (1024*0.4, 1280*0.4) = (409, 512)
        + color bar 25 high
        + 2px white border each side
        Then hconcat with 2px separator.

    Total width = 2*(409 + 4) + 2 = 828
    Total height = 512 + 25 + 4 = 541
    """
    out = tmp_path / "out.png"
    img1 = _solid(1024, 1280, (50, 50, 50))
    img2 = _solid(1024, 1280, (100, 100, 100))

    render_frontback(img1, img2, is_front=False, output_path=out)
    rendered = cv2.imread(str(out))

    assert rendered is not None
    h, w = rendered.shape[:2]
    # Allow ±2px slack for the integer-rounding in the resize step.
    assert abs(w - 828) <= 4, f"unexpected width {w}"
    assert abs(h - 541) <= 4, f"unexpected height {h}"


def test_frontback_color_bar_swaps_with_winner(tmp_path: Path) -> None:
    """Inspecting the bottom row: should be blue (255,0,0 BGR) on the
    losing side and grey (128,128,128) on the winning side. Original
    program's mapping:
        is_front = True  -> cam1 grey, cam2 blue
        is_front = False -> cam1 blue, cam2 grey
    """
    img1 = _solid(1024, 1280, (255, 255, 255))
    img2 = _solid(1024, 1280, (255, 255, 255))

    out_front = tmp_path / "front.png"
    render_frontback(img1, img2, is_front=True, output_path=out_front)
    front = cv2.imread(str(out_front))

    out_back = tmp_path / "back.png"
    render_frontback(img1, img2, is_front=False, output_path=out_back)
    back = cv2.imread(str(out_back))

    # Sample the bottom-most row of pixels in each panel.
    h_front, w_front = front.shape[:2]
    bottom_row_front = front[h_front - 5, :, :]
    # Left third = cam1 panel, right third = cam2 panel.
    left_pixel = bottom_row_front[w_front // 4]
    right_pixel = bottom_row_front[3 * w_front // 4]

    # is_front=True: cam1 grey, cam2 blue.
    assert tuple(int(c) for c in left_pixel) == (128, 128, 128)
    assert tuple(int(c) for c in right_pixel) == (255, 0, 0)

    # is_front=False: swap.
    h_back, w_back = back.shape[:2]
    bottom_row_back = back[h_back - 5, :, :]
    left_pixel_b = bottom_row_back[w_back // 4]
    right_pixel_b = bottom_row_back[3 * w_back // 4]
    assert tuple(int(c) for c in left_pixel_b) == (255, 0, 0)
    assert tuple(int(c) for c in right_pixel_b) == (128, 128, 128)


def test_frontback_creates_parent_directory(tmp_path: Path) -> None:
    out = tmp_path / "deep" / "nested" / "dir" / "result.png"
    img = _solid(640, 480, (100, 100, 100))
    render_frontback(img, img, is_front=True, output_path=out)
    assert out.is_file()


# ----------------------------------------------------------------------
# render_height
# ----------------------------------------------------------------------
def test_height_writes_raw_image(tmp_path: Path) -> None:
    """Original height path just dumps the captured image unchanged."""
    out = tmp_path / "height.png"
    img = _solid(640, 480, (123, 45, 67))

    render_height(img, output_path=out)

    rendered = cv2.imread(str(out))
    assert rendered is not None
    # Identical dimensions (no resize, no overlay).
    assert rendered.shape == img.shape
    # Pixel-level equality (PNG is lossless).
    assert np.array_equal(rendered, img)
