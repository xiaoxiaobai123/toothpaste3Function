"""Synthetic-image tests for HEIGHT_CHECK detection.

We construct images with bright pixels filling up to a known Y line and
verify the processor reports the right state code (1=OK, 2=high, 3=empty)
plus a max-Y average within tolerance.
"""

from __future__ import annotations

import numpy as np

from plc.enums import Endian
from processing.height_check import HeightCheckProcessor
from processing.result import ProcessResult


def _settings(overrides: dict[int, int] | None = None) -> dict:
    raw = [0] * 18
    raw[4] = 2  # ProductType.HEIGHT_CHECK
    raw[5] = 2  # channel = B (BGR index 0)
    raw[6] = 100  # pixel_threshold
    raw[7] = 50  # min_height
    raw[8] = 300  # decision_threshold
    if overrides:
        for idx, val in overrides.items():
            raw[idx] = val
    return {"raw_config": tuple(raw), "endian": Endian.LITTLE}


def _image_with_bright_below(fill_to_y: int, width: int = 600, height: int = 500) -> np.ndarray:
    """A black image with bright-blue pixels in rows [fill_to_y, height)."""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[fill_to_y:, :, 0] = 200  # blue channel only
    return img


def test_height_ok_when_avg_below_decision() -> None:
    """Fill goes from y=350 down → max_y per column ≈ 499 (rows 350..499 are bright)."""
    # Wait: bright pixels span [fill_to_y, height). So max-Y of any column = height-1.
    # That makes max_y_avg = height-1 = 499 which is >= decision (300) → state 2 (HIGH).
    # To get state 1 (OK), we want max_y_avg < 300, i.e. fill must STOP before y=300.
    img = np.zeros((500, 600, 3), dtype=np.uint8)
    img[100:250, :, 0] = 200  # bright between y=100 and y=250
    outcome = HeightCheckProcessor().process(img, _settings())
    assert outcome.result == ProcessResult.OK
    assert int(outcome.center[0]) == 1  # OK
    assert 240 <= outcome.center[1] <= 260  # max_y_avg ≈ 249


def test_height_high_when_avg_at_or_above_decision() -> None:
    """Fill reaches into the lower half → max_y_avg >= 300 → state 2 (HIGH)."""
    img = _image_with_bright_below(fill_to_y=100)  # fills y=100..499
    outcome = HeightCheckProcessor().process(img, _settings())
    assert outcome.result == ProcessResult.OK
    assert int(outcome.center[0]) == 2  # HIGH
    assert outcome.center[1] >= 300


def test_height_empty_when_no_pixel_above_min_height() -> None:
    """Solid black image: no column reaches min_height → state 3 (EMPTY)."""
    img = np.zeros((500, 600, 3), dtype=np.uint8)
    outcome = HeightCheckProcessor().process(img, _settings())
    assert outcome.result == ProcessResult.NG
    assert int(outcome.center[0]) == 3
    assert outcome.center[1] == 0


def test_height_channel_selectable() -> None:
    """Switching to the green channel should detect green-only fill."""
    img = np.zeros((500, 600, 3), dtype=np.uint8)
    img[100:250, :, 1] = 200  # green channel only
    # channel=1 → green
    outcome = HeightCheckProcessor().process(img, _settings({5: 1}))
    assert outcome.result == ProcessResult.OK
    assert int(outcome.center[0]) == 1


def test_height_respects_roi() -> None:
    """ROI excludes the bright region → no column above min_height → state 3."""
    img = np.zeros((500, 600, 3), dtype=np.uint8)
    img[100:250, 400:600, 0] = 200  # bright only on the right side
    # ROI = left half (0,0)-(300,500); does not contain the bright region.
    overrides = {9: 0, 10: 0, 11: 300, 12: 500}
    outcome = HeightCheckProcessor().process(img, _settings(overrides))
    assert outcome.result == ProcessResult.NG
    assert int(outcome.center[0]) == 3
