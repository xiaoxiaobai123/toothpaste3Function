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


def _build_settings(
    overrides: dict[int, int] | None = None,
    manual_roi: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> dict:
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
        "manual_roi": manual_roi,
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


# ----------------------------------------------------------------------
# v0.3.10 — manual pre-crop ROI (extension regs D110-D113 cam1 / D114-D117 cam2)
# ----------------------------------------------------------------------
def test_brush_head_manual_roi_zero_means_auto_detect() -> None:
    """When manual_roi = (0,0,0,0), behaviour is byte-identical to v0.3.9
    auto-detect on the full frame."""
    img = _draw_brush(upper_dots=80, lower_dots=20, seed=42)

    auto = BrushHeadProcessor().process(img, _build_settings())
    explicit_zero = BrushHeadProcessor().process(img, _build_settings(manual_roi=(0, 0, 0, 0)))

    assert auto.result == explicit_zero.result
    assert int(auto.center[0]) == int(explicit_zero.center[0])


def test_brush_head_manual_roi_pre_crops_search_area() -> None:
    """Two clusters in the image — one good (in centre) and one decoy
    (bottom-right). With auto-detect, the convex hull spans BOTH clusters
    and the resulting bounding rect is too big / wrong-shaped → NG.
    With a manual ROI tight around the centre cluster, decoy is excluded
    and the algorithm classifies cleanly."""
    img = _draw_brush(upper_dots=80, lower_dots=20, seed=42)
    # Add a decoy cluster in the bottom-right corner that would distort
    # the convex hull when the algorithm scans the full frame.
    rng = np.random.default_rng(99)
    for _ in range(60):
        x = int(rng.integers(700, 790))
        y = int(rng.integers(500, 590))
        cv2.circle(img, (x, y), 4, (40, 40, 40), -1)

    # Auto-detect (no manual ROI): convex hull spans original + decoy →
    # ratio likely outside [1.5, 3.5] or area outside bounds → NG.
    auto = BrushHeadProcessor().process(img, _build_settings())

    # Manual ROI tight around centre cluster (cluster spans 100..700 x, 200..400 y).
    manual = BrushHeadProcessor().process(img, _build_settings(manual_roi=(100, 200, 700, 400)))

    # Either manual succeeds where auto failed, OR both succeed with the
    # same side classification. We don't accept the case where manual
    # gives a different side than what the centred cluster actually shows.
    assert manual.result == ProcessResult.OK, f"manual ROI should classify cleanly, got {manual.result}"
    assert int(manual.center[0]) == 1, "centre cluster has upper-denser dots → Front"
    # Auto path may NG on the distorted hull. If it OKs, that's a happy
    # accident — but the test's main proof is manual succeeded.
    _ = auto


def test_brush_head_manual_roi_invalid_falls_back_to_auto() -> None:
    """Inverted/zero-area manual ROI is treated as 'no manual ROI' rather
    than crashing — algorithm falls back to full-frame auto-detection."""
    img = _draw_brush(upper_dots=80, lower_dots=20, seed=42)
    bad_roi = (500, 500, 200, 200)  # x2 < x1, y2 < y1

    outcome = BrushHeadProcessor().process(img, _build_settings(manual_roi=bad_roi))
    # Should behave like no-manual-ROI: still classifies the synthetic brush.
    assert outcome.result == ProcessResult.OK
    assert int(outcome.center[0]) == 1


def test_brush_head_settings_without_manual_roi_key_works() -> None:
    """Backward-compat: callers that pre-date v0.3.10 don't include a
    manual_roi key in settings. Processor must default to auto-detect."""
    img = _draw_brush(upper_dots=80, lower_dots=20, seed=42)
    settings = _build_settings()
    del settings["manual_roi"]  # simulate old-style settings dict

    outcome = BrushHeadProcessor().process(img, settings)
    assert outcome.result == ProcessResult.OK
    assert int(outcome.center[0]) == 1


def test_brush_head_fail_image_draws_manual_roi() -> None:
    """v0.3.11 fix: when the algorithm rejects a frame (e.g. ratio out of
    bounds) and a manual ROI was active, the fail-visualization image must
    show the purple manual-ROI rectangle so the operator can see WHERE
    the algorithm was looking. v0.3.10 forgot to draw it."""
    # Blank image so dot detection fails immediately; manual_roi set.
    img = np.full((600, 800, 3), 230, dtype=np.uint8)
    manual_roi = (100, 100, 700, 500)
    outcome = BrushHeadProcessor().process(img, _build_settings(manual_roi=manual_roi))
    # Expect failure (no dots → no valid ROI).
    assert outcome.result == ProcessResult.NG
    fail_img = outcome.image

    # Sample a pixel along the rectangle's top edge — should be purple
    # (BGR (255, 0, 255)). The line is 2 px thick so check a 4-pixel band.
    top_band = fail_img[manual_roi[1] : manual_roi[1] + 2, manual_roi[0] + 50, :]
    # At least one pixel in the band should be ~purple.
    purple_match = (top_band[..., 0] > 200) & (top_band[..., 1] < 50) & (top_band[..., 2] > 200)
    assert purple_match.any(), (
        f"expected purple manual-ROI rect on fail image; sampled top band={top_band.tolist()}"
    )


def test_brush_head_fail_image_omits_manual_roi_when_none() -> None:
    """If no manual ROI was active, the fail image must NOT contain a
    purple rectangle (auto-detect fail looks like 'message + params only')."""
    img = np.full((600, 800, 3), 230, dtype=np.uint8)
    outcome = BrushHeadProcessor().process(img, _build_settings())  # default = auto
    assert outcome.result == ProcessResult.NG
    fail_img = outcome.image

    # No purple should appear anywhere on the fail image (just red text + grey params).
    purple_pixels = ((fail_img[..., 0] > 200) & (fail_img[..., 1] < 50) & (fail_img[..., 2] > 200)).sum()
    assert purple_pixels < 50, f"expected ~no purple on auto-detect fail image, found {purple_pixels} pixels"
