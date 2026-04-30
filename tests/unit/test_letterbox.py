"""Tests for processing.display_utils.fit_to_framebuffer.

Pre-scaling makes image_updater's NEON fast path possible — verify the
output is exactly fb-sized, preserves source aspect ratio, and uses
black for the letterbox bands (not orange or random uninitialised data).
"""

from __future__ import annotations

import numpy as np

from processing.display_utils import fit_to_framebuffer


def _solid(width: int, height: int, color: tuple[int, int, int]) -> np.ndarray:
    return np.full((height, width, 3), color, dtype=np.uint8)


def test_output_matches_fb_size_exactly() -> None:
    src = _solid(800, 600, (100, 100, 100))
    out = fit_to_framebuffer(src, fb_size=(1920, 1080))
    h, w = out.shape[:2]
    assert (w, h) == (1920, 1080)


def test_already_correct_size_returns_unchanged() -> None:
    src = _solid(1920, 1080, (50, 50, 50))
    out = fit_to_framebuffer(src, fb_size=(1920, 1080))
    # Identity — no resize, no letterbox.
    assert out is src or np.array_equal(out, src)


def test_letterbox_bands_are_black() -> None:
    """A 4:3 source on a 16:9 fb gets vertical letterbox bands on the
    sides. Sample a column near the edge — should be black (0,0,0)."""
    src = _solid(800, 600, (200, 200, 200))  # bright grey, 4:3
    out = fit_to_framebuffer(src, fb_size=(1920, 1080))
    h, w = out.shape[:2]
    # Far-left column — outside the scaled image, in the letterbox band.
    left_col = out[:, 5, :]
    assert np.all(left_col == 0), f"left letterbox should be black, got mean={left_col.mean():.1f}"


def test_aspect_ratio_preserved_letterboxed_horizontally() -> None:
    """4:3 (1.33) source on 16:9 (1.78) fb fits to height (1080), width
    becomes ~1440 with 240 px of letterbox on each side."""
    src = _solid(800, 600, (180, 180, 180))
    out = fit_to_framebuffer(src, fb_size=(1920, 1080))
    # Find the bright region in a horizontal scan of the middle row.
    mid_row = out[540, :, 0]  # blue channel of middle row
    bright_indices = np.where(mid_row > 100)[0]
    assert len(bright_indices) > 0, "image content not found in middle row"
    bright_w = bright_indices[-1] - bright_indices[0] + 1
    # Expected width = 1080 * (800/600) = 1440 (±slack for resize rounding)
    assert abs(bright_w - 1440) <= 10, f"expected ~1440 px wide image area, got {bright_w}"


def test_aspect_ratio_preserved_letterboxed_vertically() -> None:
    """16:10 (1.6) source on a 5:4 (1.25) fb fits to width, with vertical
    letterbox bands top + bottom."""
    src = _solid(1600, 1000, (180, 180, 180))
    out = fit_to_framebuffer(src, fb_size=(1280, 1024))
    # Far-top row should be black letterbox.
    top_row = out[5, :, :]
    assert np.all(top_row == 0), f"top letterbox should be black, got mean={top_row.mean():.1f}"
    # Middle row should contain the bright image content.
    mid_row = out[512, :, 0]
    assert mid_row.mean() > 100, "image content not found in middle row"


def test_centered_image_within_canvas() -> None:
    """The scaled image should be centred — equal letterbox width on
    both sides (within ±1 px for odd-pixel splits)."""
    src = _solid(800, 600, (255, 255, 255))
    out = fit_to_framebuffer(src, fb_size=(1920, 1080))
    mid_row = out[540, :, 0]
    bright_indices = np.where(mid_row > 100)[0]
    left_band = bright_indices[0]
    right_band = out.shape[1] - bright_indices[-1] - 1
    assert abs(left_band - right_band) <= 1, (
        f"image not centred: left letterbox={left_band}, right letterbox={right_band}"
    )
