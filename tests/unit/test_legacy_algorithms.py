"""Synthetic-image tests for the byte-faithful legacy frontback + height algorithms.

These verify the algorithms reproduce the original program's logic:
    - frontback: relative edge-count comparison between two cameras
    - height:    per-column max-Y of red-channel threshold mask
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from legacy.fronback_algorithms import (
    EDGE_INTENSITY_THRESHOLD,
    HEIGHT_CHANNEL_INDEX,
    compute_frontback,
    compute_frontback_parallel,
    compute_height,
)


# ----------------------------------------------------------------------
# Frontback
# ----------------------------------------------------------------------
def _striped_image(width: int, height: int, n_stripes: int) -> np.ndarray:
    img = np.full((height, width, 3), 220, dtype=np.uint8)
    if n_stripes <= 0:
        return img
    step = width // (n_stripes + 1)
    for i in range(1, n_stripes + 1):
        x = i * step
        cv2.line(img, (x, 50), (x, height - 50), (40, 40, 40), 2)
    return img


def _full_roi(w: int, h: int) -> dict[str, int]:
    return {"x1": 0, "y1": 0, "x2": w, "y2": h}


def test_frontback_cam1_more_edges_returns_front() -> None:
    img1 = _striped_image(600, 400, n_stripes=40)
    img2 = _striped_image(600, 400, n_stripes=5)
    roi = _full_roi(600, 400)

    result = compute_frontback(img1, img2, roi, roi)
    assert result.is_front is True
    assert result.edge1_count > result.edge2_count


def test_frontback_cam2_more_edges_returns_back() -> None:
    img1 = _striped_image(600, 400, n_stripes=5)
    img2 = _striped_image(600, 400, n_stripes=40)
    roi = _full_roi(600, 400)

    result = compute_frontback(img1, img2, roi, roi)
    assert result.is_front is False
    assert result.edge2_count > result.edge1_count


def test_frontback_equal_edges_classified_as_back() -> None:
    """Original semantics: `edge1 > edge2` ⇒ Front. Equal counts mean Back."""
    img1 = _striped_image(600, 400, n_stripes=10)
    img2 = _striped_image(600, 400, n_stripes=10)
    roi = _full_roi(600, 400)

    result = compute_frontback(img1, img2, roi, roi)
    assert result.is_front is False
    assert result.edge1_count == result.edge2_count


def test_frontback_respects_per_camera_roi() -> None:
    """ROI restriction should be applied per camera independently."""
    img = _striped_image(600, 400, n_stripes=40)
    full = _full_roi(600, 400)
    # cam2 ROI excludes the stripes (top-left corner of image)
    roi_corner = {"x1": 0, "y1": 0, "x2": 50, "y2": 50}

    result = compute_frontback(img, img, full, roi_corner)
    assert result.is_front is True  # cam1 sees all stripes, cam2 sees none
    assert result.edge1_count > 0
    assert result.edge2_count < 100  # very few edges in 50x50 corner


def test_frontback_threshold_constant_matches_original() -> None:
    """Sanity check: original program hard-codes 30."""
    assert EDGE_INTENSITY_THRESHOLD == 30


@pytest.mark.asyncio
async def test_frontback_parallel_matches_sequential() -> None:
    """compute_frontback_parallel must produce byte-identical output to
    the sequential version — same Sobel calls, same threshold, same
    is_front comparison, just run in worker threads. Drift between the
    two versions would mean OK/NG boundaries shift in the field."""
    img1 = _striped_image(600, 400, n_stripes=40)
    img2 = _striped_image(600, 400, n_stripes=8)
    roi = _full_roi(600, 400)

    seq = compute_frontback(img1, img2, roi, roi)
    par = await compute_frontback_parallel(img1, img2, roi, roi)

    assert par.is_front == seq.is_front
    assert par.edge1_count == seq.edge1_count
    assert par.edge2_count == seq.edge2_count


def test_frontback_handles_empty_roi() -> None:
    img = _striped_image(600, 400, n_stripes=40)
    bad_roi = {"x1": 100, "y1": 100, "x2": 100, "y2": 100}  # zero-area
    result = compute_frontback(img, img, bad_roi, bad_roi)
    assert result.edge1_count == 0
    assert result.edge2_count == 0


# ----------------------------------------------------------------------
# Height
# ----------------------------------------------------------------------
def _height_image(fill_top_y: int, fill_bottom_y: int, width: int = 600, height: int = 500) -> np.ndarray:
    """Black background; bright red between fill_top_y..fill_bottom_y."""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    # Red channel = BGR index 2.
    img[fill_top_y:fill_bottom_y, :, HEIGHT_CHANNEL_INDEX] = 200
    return img


def test_height_empty_below_min_returns_state_3() -> None:
    img = np.zeros((500, 600, 3), dtype=np.uint8)
    result = compute_height(img, brightness_threshold=100, min_height=50, height_comparison=300)
    assert result.state == 3
    assert result.max_y_avg == 0


def test_height_signal_above_min_below_decision_returns_state_1() -> None:
    """max-Y average is around (fill_bottom_y - 1); want it < height_comparison."""
    img = _height_image(fill_top_y=100, fill_bottom_y=250)
    # max_y_avg ≈ 249, height_comparison = 300 → state = 1 (OK)
    result = compute_height(img, brightness_threshold=100, min_height=50, height_comparison=300)
    assert result.state == 1
    assert 240 <= result.max_y_avg <= 260


def test_height_signal_at_or_above_decision_returns_state_2() -> None:
    img = _height_image(fill_top_y=200, fill_bottom_y=499)
    # max_y_avg ≈ 498, height_comparison = 300 → state = 2 (NG/overfill)
    result = compute_height(img, brightness_threshold=100, min_height=50, height_comparison=300)
    assert result.state == 2
    assert result.max_y_avg >= 300


def test_height_min_height_acts_as_floor() -> None:
    """If max-Y ≤ min_height for every column, treat as empty (state 3)."""
    img = _height_image(fill_top_y=10, fill_bottom_y=30)  # max-Y ≈ 29
    # min_height = 50 → no column qualifies → state 3
    result = compute_height(img, brightness_threshold=100, min_height=50, height_comparison=300)
    assert result.state == 3


def test_height_uses_red_channel_only() -> None:
    """A pure-blue fill must NOT trigger detection."""
    img = np.zeros((500, 600, 3), dtype=np.uint8)
    img[100:300, :, 0] = 200  # blue channel only — should be invisible to algorithm
    result = compute_height(img, brightness_threshold=100, min_height=50, height_comparison=300)
    assert result.state == 3
    assert result.max_y_avg == 0


def test_height_grayscale_input_returns_empty_gracefully() -> None:
    """Non-3-channel inputs should not crash; algorithm needs RGB."""
    gray = np.full((500, 600), 200, dtype=np.uint8)
    result = compute_height(gray, brightness_threshold=100, min_height=50, height_comparison=300)
    assert result.state == 3
