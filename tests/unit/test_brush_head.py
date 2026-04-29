"""Synthetic-image tests for BRUSH_HEAD detection.

We draw a controlled "brush head" — an oblong cluster of dark dots on a
light background — with the upper half denser than the lower half (or vice
versa) and verify the processor classifies it correctly.

The synthetic geometry matches the algorithm's default ROI thresholds
(area ≥ 50000 pixels², aspect ratio in [1.5, 3.5]) so the same defaults
that ship with the processor can be exercised directly.
"""

from __future__ import annotations

import cv2
import numpy as np

from plc.codec import double_to_words, float32_to_words, uint32_to_words
from plc.enums import Endian
from processing.brush_head import BrushHeadProcessor
from processing.result import ProcessResult


def _build_settings(overrides: dict[str, int] | None = None) -> dict:
    """Build a settings dict mimicking PLCManager.read_camera_settings()."""
    raw = [0] * 18
    raw[1] = 5000  # exposure (irrelevant)
    pd_words = float32_to_words(1.0)  # pixel_distance = 1.0
    raw[2], raw[3] = pd_words[0], pd_words[1]
    raw[4] = 3  # ProductType.BRUSH_HEAD
    if overrides:
        for idx, val in overrides.items():
            raw[idx] = val
    return {
        "raw_config": tuple(raw),
        "pixel_distance": 1.0,
        "endian": Endian.LITTLE,
    }


def _draw_brush(
    width: int = 800,
    height: int = 600,
    upper_dots: int = 80,
    lower_dots: int = 20,
    seed: int = 42,
) -> np.ndarray:
    """Draw a synthetic brush head: oblong dot cluster on a light background.

    The cluster spans the centered 600x200 region; within it, dots are
    distributed unequally between the upper and lower halves so the
    processor can compare densities and pick a side.
    """
    rng = np.random.default_rng(seed)
    img = np.full((height, width, 3), 230, dtype=np.uint8)

    cluster_x_lo = (width - 600) // 2
    cluster_x_hi = cluster_x_lo + 600
    cluster_y_lo = (height - 200) // 2
    cluster_y_mid = cluster_y_lo + 100
    cluster_y_hi = cluster_y_lo + 200

    def _scatter(n: int, y_lo: int, y_hi: int) -> None:
        for _ in range(n):
            x = int(rng.integers(cluster_x_lo + 10, cluster_x_hi - 10))
            y = int(rng.integers(y_lo + 5, y_hi - 5))
            radius = int(rng.integers(3, 6))
            cv2.circle(img, (x, y), radius, (40, 40, 40), -1)

    _scatter(upper_dots, cluster_y_lo, cluster_y_mid)
    _scatter(lower_dots, cluster_y_mid, cluster_y_hi)
    return img


def test_brush_head_detects_front_when_upper_denser() -> None:
    img = _draw_brush(upper_dots=80, lower_dots=20, seed=42)
    outcome = BrushHeadProcessor().process(img, _build_settings())

    assert outcome.result == ProcessResult.OK
    # side_code is encoded in Outcome.center.x; 1 = Front (upper denser).
    assert int(outcome.center[0]) == 1
    assert outcome.angle == 0.0


def test_brush_head_detects_back_when_lower_denser() -> None:
    img = _draw_brush(upper_dots=20, lower_dots=80, seed=43)
    outcome = BrushHeadProcessor().process(img, _build_settings())

    assert outcome.result == ProcessResult.OK
    # 2 = Back (lower denser).
    assert int(outcome.center[0]) == 2


def test_brush_head_returns_ng_on_blank_image() -> None:
    img = np.full((600, 800, 3), 230, dtype=np.uint8)
    outcome = BrushHeadProcessor().process(img, _build_settings())

    assert outcome.result == ProcessResult.NG
    assert int(outcome.center[0]) == 0


def test_brush_head_uses_plc_overrides() -> None:
    """When the PLC supplies non-zero parameters, they override defaults."""
    img = _draw_brush(upper_dots=60, lower_dots=20, seed=44)

    # Set shrink_pct=20 (was default 15) — algorithm should still classify OK.
    overrides = {5: 20}
    # roi_area_min = 30000 (LE uint32)
    lo_words = uint32_to_words(30000, Endian.LITTLE)
    overrides[10], overrides[11] = lo_words[0], lo_words[1]

    outcome = BrushHeadProcessor().process(img, _build_settings(overrides))
    assert outcome.result == ProcessResult.OK
    assert int(outcome.center[0]) == 1


def test_brush_head_is_robust_to_camera_result_packing() -> None:
    """Sanity check: encoding the side code into a CameraResult works."""
    from plc.enums import CameraResult

    img = _draw_brush(upper_dots=60, lower_dots=15, seed=45)
    outcome = BrushHeadProcessor().process(img, _build_settings())
    assert outcome.result == ProcessResult.OK

    # Mimic TaskManager.write_result_to_plc — would write side code as x.
    cr = CameraResult(
        x=outcome.center[0],
        y=outcome.center[1],
        angle=outcome.angle,
        result=outcome.result == ProcessResult.OK,
        area=0,
        circularity=0.0,
    )
    # The packed double for x = 1.0 should round-trip.
    words = double_to_words(cr.x)
    assert len(words) == 4
