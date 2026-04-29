"""Tests for legacy/fronback_display.py.

Two output sinks now:
    PNG    -> /tmp/processed_image.png  (feh/fbi sites)
    RGB565 -> /home/pi/output_image.rgb565  (image_updater sites)

Both can be redirected per-call (None disables that sink). Tests below
exercise each one independently and the byte-faithful colour-bar mapping
that mirrors the original fronback program.
"""

from __future__ import annotations

import struct
from pathlib import Path

import cv2
import numpy as np

from legacy.fronback_display import (
    compose_frontback,
    render_frontback,
    render_height,
)


def _solid(width: int, height: int, color: tuple[int, int, int]) -> np.ndarray:
    return np.full((height, width, 3), color, dtype=np.uint8)


# ----------------------------------------------------------------------
# render_frontback — PNG output
# ----------------------------------------------------------------------
def test_frontback_writes_png_to_target_path(tmp_path: Path) -> None:
    out = tmp_path / "processed.png"
    img1 = _solid(1024, 1280, (10, 10, 10))
    img2 = _solid(1024, 1280, (200, 200, 200))

    render_frontback(img1, img2, is_front=True, png_path=out, rgb565_path=None)

    assert out.is_file()
    assert out.stat().st_size > 0


def test_frontback_dimensions_match_original_layout(tmp_path: Path) -> None:
    """Original layout:
        each panel resized to 0.4x → (1024*0.4, 1280*0.4) = (409, 512)
        + colour bar 25 high
        + 2px white border each side
        Then hconcat with 2px separator.

    Total width = 2*(409 + 4) + 2 = 828
    Total height = 512 + 25 + 4 = 541
    """
    out = tmp_path / "out.png"
    img1 = _solid(1024, 1280, (50, 50, 50))
    img2 = _solid(1024, 1280, (100, 100, 100))

    render_frontback(img1, img2, is_front=False, png_path=out, rgb565_path=None)
    rendered = cv2.imread(str(out))

    assert rendered is not None
    h, w = rendered.shape[:2]
    # Allow ±4px slack for the integer-rounding in the resize step.
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
    render_frontback(img1, img2, is_front=True, png_path=out_front, rgb565_path=None)
    front = cv2.imread(str(out_front))

    out_back = tmp_path / "back.png"
    render_frontback(img1, img2, is_front=False, png_path=out_back, rgb565_path=None)
    back = cv2.imread(str(out_back))

    h_front, w_front = front.shape[:2]
    bottom_row_front = front[h_front - 5, :, :]
    left_pixel = bottom_row_front[w_front // 4]
    right_pixel = bottom_row_front[3 * w_front // 4]
    assert tuple(int(c) for c in left_pixel) == (128, 128, 128)
    assert tuple(int(c) for c in right_pixel) == (255, 0, 0)

    h_back, w_back = back.shape[:2]
    bottom_row_back = back[h_back - 5, :, :]
    left_pixel_b = bottom_row_back[w_back // 4]
    right_pixel_b = bottom_row_back[3 * w_back // 4]
    assert tuple(int(c) for c in left_pixel_b) == (255, 0, 0)
    assert tuple(int(c) for c in right_pixel_b) == (128, 128, 128)


def test_frontback_creates_parent_directory(tmp_path: Path) -> None:
    out = tmp_path / "deep" / "nested" / "dir" / "result.png"
    img = _solid(640, 480, (100, 100, 100))
    render_frontback(img, img, is_front=True, png_path=out, rgb565_path=None)
    assert out.is_file()


# ----------------------------------------------------------------------
# render_frontback — RGB565 output (image_updater sink)
# ----------------------------------------------------------------------
def test_frontback_writes_rgb565_with_header(tmp_path: Path) -> None:
    """The rgb565 sink must produce: int32 width, int32 height, then pixels.
    image_updater.c parses exactly this format."""
    rgb565_out = tmp_path / "output_image.rgb565"
    img1 = _solid(640, 480, (10, 10, 10))
    img2 = _solid(640, 480, (200, 200, 200))

    render_frontback(img1, img2, is_front=True, png_path=None, rgb565_path=rgb565_out)

    assert rgb565_out.is_file()
    data = rgb565_out.read_bytes()
    # Header: 2 × int32 little-endian.
    width, height = struct.unpack("<ii", data[:8])
    assert width > 0 and height > 0
    # Pixel payload: width × height × 2 bytes (RGB565).
    expected_pixels = width * height * 2
    assert len(data) - 8 == expected_pixels


def test_frontback_writes_both_sinks_atomically(tmp_path: Path) -> None:
    """Both sinks write; rgb565 uses tmp+rename to avoid torn reads."""
    png_out = tmp_path / "processed.png"
    rgb565_out = tmp_path / "output_image.rgb565"
    img = _solid(800, 600, (128, 128, 128))

    render_frontback(img, img, is_front=False, png_path=png_out, rgb565_path=rgb565_out)

    assert png_out.is_file()
    assert rgb565_out.is_file()
    # No leftover .tmp file from the atomic rename trick.
    assert not (tmp_path / "output_image.rgb565.tmp").exists()


def test_frontback_can_disable_either_sink(tmp_path: Path) -> None:
    """Passing None for one sink writes only the other."""
    png_only = tmp_path / "only.png"
    render_frontback(
        _solid(640, 480, (50, 50, 50)),
        _solid(640, 480, (100, 100, 100)),
        is_front=True,
        png_path=png_only,
        rgb565_path=None,
    )
    assert png_only.is_file()

    rgb565_only = tmp_path / "only.rgb565"
    render_frontback(
        _solid(640, 480, (50, 50, 50)),
        _solid(640, 480, (100, 100, 100)),
        is_front=True,
        png_path=None,
        rgb565_path=rgb565_only,
    )
    assert rgb565_only.is_file()


# ----------------------------------------------------------------------
# render_height
# ----------------------------------------------------------------------
def test_height_writes_raw_image_to_png(tmp_path: Path) -> None:
    """Original height path just dumps the captured image unchanged."""
    out = tmp_path / "height.png"
    img = _solid(640, 480, (123, 45, 67))

    render_height(img, png_path=out, rgb565_path=None)

    rendered = cv2.imread(str(out))
    assert rendered is not None
    assert rendered.shape == img.shape
    assert np.array_equal(rendered, img)


def test_height_writes_rgb565_dimensions_match_input(tmp_path: Path) -> None:
    out = tmp_path / "height.rgb565"
    img = _solid(640, 480, (123, 45, 67))

    render_height(img, png_path=None, rgb565_path=out)

    data = out.read_bytes()
    width, height = struct.unpack("<ii", data[:8])
    assert width == 640
    assert height == 480


# ----------------------------------------------------------------------
# compose_frontback (pure CPU helper, no I/O)
# ----------------------------------------------------------------------
def test_compose_frontback_returns_array_without_writing(tmp_path: Path) -> None:
    img = _solid(640, 480, (100, 100, 100))
    composed = compose_frontback(img, img, is_front=True)
    assert isinstance(composed, np.ndarray)
    assert composed.ndim == 3
    assert composed.shape[2] == 3
    # No files created in tmp_path.
    assert list(tmp_path.iterdir()) == []


# ----------------------------------------------------------------------
# compose_frontback — offline camera handling (one image is None)
# ----------------------------------------------------------------------
def test_compose_frontback_handles_missing_cam1() -> None:
    """When cam1 is offline, compose succeeds and the left half of the
    composition is a dark OFFLINE placeholder while the right half shows
    the bright cam2 image."""
    img2 = _solid(640, 480, (180, 180, 180))  # bright grey
    composed = compose_frontback(None, img2, is_front=False)
    assert isinstance(composed, np.ndarray)
    h, w = composed.shape[:2]

    # Compare panel-wide brightness rather than spot pixels (would collide
    # with the green crosshair or text strokes).
    left_panel = composed[:, : w // 2 - 2]   # exclude central white separator
    right_panel = composed[:, w // 2 + 2 :]
    assert left_panel.mean() < right_panel.mean() - 80, (
        f"left panel should be dark OFFLINE placeholder; got "
        f"left_mean={left_panel.mean():.1f}, right_mean={right_panel.mean():.1f}"
    )


def test_compose_frontback_handles_missing_cam2() -> None:
    img1 = _solid(640, 480, (180, 180, 180))
    composed = compose_frontback(img1, None, is_front=True)
    h, w = composed.shape[:2]
    left_panel = composed[:, : w // 2 - 2]
    right_panel = composed[:, w // 2 + 2 :]
    assert right_panel.mean() < left_panel.mean() - 80, (
        f"right panel should be dark OFFLINE placeholder; got "
        f"left_mean={left_panel.mean():.1f}, right_mean={right_panel.mean():.1f}"
    )


def test_compose_frontback_handles_both_missing() -> None:
    """Edge case: both cameras dropped. Compose still produces a frame
    (so the display loop doesn't crash) — just two dark placeholders."""
    composed = compose_frontback(None, None, is_front=False)
    assert isinstance(composed, np.ndarray)
    # The composition is dominated by black placeholder pixels; allow
    # plenty of slack for white borders / red title text / hint text.
    assert composed.mean() < 60, (
        "both-offline composition should be dominated by dark placeholder"
    )


def test_compose_frontback_uses_loser_color_when_one_offline() -> None:
    """When one camera is offline the algorithm doesn't run, so neither
    panel should get the winner-grey bar — both are loser-blue to avoid
    implying a pass/fail on a stale or absent capture."""
    img2 = _solid(640, 480, (255, 255, 255))
    composed = compose_frontback(None, img2, is_front=True)  # is_front ignored
    h, w = composed.shape[:2]

    # Bottom row sits inside the colour bar (bar height = 25, plus white
    # 2px border at bottom — sample a few pixels up to clear the border).
    bar_row = composed[h - 5, :, :]
    left_bar_pixel = bar_row[w // 4]
    right_bar_pixel = bar_row[3 * w // 4]
    # Both should be _COLOR_LOSER (255, 0, 0) BGR.
    assert tuple(int(c) for c in left_bar_pixel) == (255, 0, 0)
    assert tuple(int(c) for c in right_bar_pixel) == (255, 0, 0)


def test_render_frontback_writes_when_cam1_missing(tmp_path: Path) -> None:
    """Even with cam1 None, both display sinks still get written so the
    operator screen reflects the missing camera every cycle."""
    out_png = tmp_path / "p.png"
    out_rgb565 = tmp_path / "p.rgb565"
    render_frontback(
        None,
        _solid(640, 480, (200, 200, 200)),
        is_front=False,
        png_path=out_png,
        rgb565_path=out_rgb565,
    )
    assert out_png.is_file()
    assert out_rgb565.is_file()
    # rgb565 header must still be valid.
    data = out_rgb565.read_bytes()
    width, height = struct.unpack("<ii", data[:8])
    assert width > 0 and height > 0
