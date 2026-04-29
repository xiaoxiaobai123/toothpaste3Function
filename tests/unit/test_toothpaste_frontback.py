"""Synthetic-image tests for TOOTHPASTE_FRONTBACK detection.

We build images with controlled edge content:
    - "front" image: many vertical edges (Sobel X yields high edge count)
    - "back"  image: few vertical edges
    - "empty" image: solid color (no edges → EXCEPTION)
"""

from __future__ import annotations

import cv2
import numpy as np

from plc.codec import uint32_to_words
from plc.enums import Endian
from processing.result import ProcessResult
from processing.toothpaste_frontback import ToothpasteFrontBackProcessor


def _settings(overrides: dict[int, int] | None = None) -> dict:
    raw = [0] * 18
    raw[4] = 1  # ProductType.TOOTHPASTE_FRONTBACK
    raw[5] = 30  # edge_intensity_threshold
    front_words = uint32_to_words(5000, Endian.LITTLE)
    raw[6], raw[7] = front_words[0], front_words[1]
    back_words = uint32_to_words(500, Endian.LITTLE)
    raw[8], raw[9] = back_words[0], back_words[1]
    if overrides:
        for idx, val in overrides.items():
            raw[idx] = val
    return {"raw_config": tuple(raw), "endian": Endian.LITTLE}


def _front_image() -> np.ndarray:
    """Many vertical stripes → high Sobel X edge count."""
    img = np.full((400, 600, 3), 220, dtype=np.uint8)
    for x in range(20, 580, 12):
        cv2.line(img, (x, 50), (x, 350), (40, 40, 40), 2)
    return img


def _back_image() -> np.ndarray:
    """A few short vertical stripes → moderate edge count between back and front thresholds."""
    img = np.full((400, 600, 3), 220, dtype=np.uint8)
    for x in (150, 300, 450):
        cv2.line(img, (x, 180), (x, 220), (40, 40, 40), 1)
    return img


def _empty_image() -> np.ndarray:
    return np.full((400, 600, 3), 220, dtype=np.uint8)


def test_toothpaste_classifies_front_when_edges_high() -> None:
    outcome = ToothpasteFrontBackProcessor().process(_front_image(), _settings())
    assert outcome.result == ProcessResult.OK
    assert int(outcome.center[0]) == 1  # Front
    assert outcome.center[1] >= 5000  # edge count above threshold


def test_toothpaste_classifies_back_when_edges_moderate() -> None:
    outcome = ToothpasteFrontBackProcessor().process(_back_image(), _settings())
    assert outcome.result == ProcessResult.OK
    assert int(outcome.center[0]) == 2  # Back
    assert 500 <= outcome.center[1] < 5000


def test_toothpaste_returns_exception_on_empty_image() -> None:
    outcome = ToothpasteFrontBackProcessor().process(_empty_image(), _settings())
    assert outcome.result == ProcessResult.EXCEPTION
    assert int(outcome.center[0]) == 0


def test_toothpaste_respects_roi() -> None:
    """When ROI excludes the stripes, edge count drops below back_threshold."""
    img = _front_image()
    # Restrict ROI to top-left corner where there are no stripes.
    overrides = {10: 0, 11: 0, 12: 100, 13: 30}
    outcome = ToothpasteFrontBackProcessor().process(img, _settings(overrides))
    assert outcome.result == ProcessResult.EXCEPTION
    assert int(outcome.center[0]) == 0


def test_toothpaste_thresholds_are_overridable() -> None:
    """Lowering front_count_threshold should re-classify the back image as front."""
    img = _back_image()
    # Drop both thresholds: back<10, front<=200 — moderate edge count now counts as Front.
    front_words = uint32_to_words(200, Endian.LITTLE)
    back_words = uint32_to_words(10, Endian.LITTLE)
    overrides = {
        6: front_words[0],
        7: front_words[1],
        8: back_words[0],
        9: back_words[1],
    }
    outcome = ToothpasteFrontBackProcessor().process(img, _settings(overrides))
    assert outcome.result == ProcessResult.OK
    assert int(outcome.center[0]) == 1
