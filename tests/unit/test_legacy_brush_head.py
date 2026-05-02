"""Tests for legacy/fronback_brush_head — the v2 BrushHeadProcessor adapter.

Two layers covered:
1. _merge_with_defaults: pure parameter-shape translation (PLC raw words +
   config defaults → v2 raw_config + manual_roi). Default-substitution
   rules (PLC 0 → use config default) are the bug-prone bit; explicit
   per-field tests below.
2. run_brush_head: end-to-end through a stubbed BrushHeadProcessor so we
   verify the Outcome → BrushHeadCycleResult mapping (D0 / D42 / D43 /
   display_image) without needing the full algorithm to run.
"""

from __future__ import annotations

import numpy as np

from legacy.fronback_brush_head import (
    BrushHeadCycleResult,
    _merge_with_defaults,
    run_brush_head,
)
from legacy.fronback_protocol import (
    RESULT_BACK_OR_NG,
    RESULT_FRONT_OR_OK,
    BrushHeadSettings,
)
from plc.codec import words_to_uint32
from processing.result import Outcome, ProcessResult

# Mirrors the hard-coded fallback in core.config_manager.get_legacy_brush_head_defaults.
_DEFAULTS = {
    "exposure": 5000,
    "shrink_pct": 15,
    "adapt_block": 31,
    "adapt_C": 8,
    "dot_area_min": 20,
    "dot_area_max": 500,
    "roi_area_min": 50000,
    "roi_area_max": 500000,
    "ratio_min": 1.5,
    "ratio_max": 3.5,
}


def _zero_settings() -> BrushHeadSettings:
    """A 'PLC didn't write any params' settings — every field 0 means defaults
    and manual_roi (0,0,0,0) means auto-detect."""
    return BrushHeadSettings(
        cam1_exposure=0,
        shrink_pct=0,
        adapt_block=0,
        dot_area_min=0,
        dot_area_max=0,
        roi_area_min_x100=0,
        roi_area_max_x100=0,
        ratio_min_x10=0,
        ratio_max_x10=0,
        manual_roi=(0, 0, 0, 0),
    )


# --------------------------------------------------------------------------- #
# Parameter-merge: 0-PLC slots fall through to config defaults.
# --------------------------------------------------------------------------- #
def test_merge_uses_defaults_when_plc_writes_zero() -> None:
    out = _merge_with_defaults(_zero_settings(), _DEFAULTS)
    raw = out["raw_config"]
    assert raw[5] == 15  # default shrink_pct
    assert raw[6] == 31  # default adapt_block
    assert raw[7] == 8  # default adapt_C (never PLC-overridable)
    assert raw[8] == 20  # default dot_area_min
    assert raw[9] == 500  # default dot_area_max
    # roi_area_min/max are uint32 split into 2 words each.
    assert words_to_uint32(raw[10], raw[11]) == 50000
    assert words_to_uint32(raw[12], raw[13]) == 500000
    assert raw[14] == 15  # default ratio_min × 10
    assert raw[15] == 35  # default ratio_max × 10


def test_merge_uses_plc_value_when_nonzero() -> None:
    settings = BrushHeadSettings(
        cam1_exposure=0,
        shrink_pct=12,
        adapt_block=29,
        dot_area_min=99,
        dot_area_max=999,
        roi_area_min_x100=400,  # → 40000 px
        roi_area_max_x100=6000,  # → 600000 px
        ratio_min_x10=18,
        ratio_max_x10=32,
        manual_roi=(0, 0, 0, 0),
    )
    out = _merge_with_defaults(settings, _DEFAULTS)
    raw = out["raw_config"]
    assert raw[5] == 12
    assert raw[6] == 29
    assert raw[8] == 99
    assert raw[9] == 999
    assert words_to_uint32(raw[10], raw[11]) == 40000
    assert words_to_uint32(raw[12], raw[13]) == 600000
    assert raw[14] == 18
    assert raw[15] == 32


def test_merge_mixes_plc_and_defaults_per_slot() -> None:
    """Some PLC fields set, others 0 — should produce a hybrid raw_config."""
    settings = BrushHeadSettings(
        cam1_exposure=0,
        shrink_pct=0,  # default
        adapt_block=0,  # default
        dot_area_min=77,  # custom
        dot_area_max=0,  # default
        roi_area_min_x100=0,  # default
        roi_area_max_x100=4000,  # custom (400000)
        ratio_min_x10=0,  # default
        ratio_max_x10=40,  # custom (4.0)
        manual_roi=(0, 0, 0, 0),
    )
    out = _merge_with_defaults(settings, _DEFAULTS)
    raw = out["raw_config"]
    assert raw[5] == 15  # default
    assert raw[6] == 31  # default
    assert raw[8] == 77  # PLC
    assert raw[9] == 500  # default
    assert words_to_uint32(raw[10], raw[11]) == 50000  # default
    assert words_to_uint32(raw[12], raw[13]) == 400000  # PLC
    assert raw[14] == 15  # default
    assert raw[15] == 40  # PLC


def test_merge_pads_raw_config_to_18_words() -> None:
    """v2 BrushHeadProcessor reads up to raw[15]. The dict must always be
    18 words so its `len(raw) < 16` defense check passes."""
    out = _merge_with_defaults(_zero_settings(), _DEFAULTS)
    assert len(out["raw_config"]) == 18


def test_merge_zero_manual_roi_propagates_as_auto_detect() -> None:
    """(0,0,0,0) PLC manual_roi → (0,0,0,0) in settings → auto-detect inside
    BrushHeadProcessor."""
    out = _merge_with_defaults(_zero_settings(), _DEFAULTS)
    assert out["manual_roi"] == (0, 0, 0, 0)


def test_merge_nonzero_manual_roi_propagates_unchanged() -> None:
    """A PLC-supplied manual_roi rectangle should reach BrushHeadProcessor
    verbatim — no scaling, no offset."""
    settings = BrushHeadSettings(
        cam1_exposure=0,
        shrink_pct=0,
        adapt_block=0,
        dot_area_min=0,
        dot_area_max=0,
        roi_area_min_x100=0,
        roi_area_max_x100=0,
        ratio_min_x10=0,
        ratio_max_x10=0,
        manual_roi=(150, 250, 850, 750),
    )
    out = _merge_with_defaults(settings, _DEFAULTS)
    assert out["manual_roi"] == (150, 250, 850, 750)


def test_merge_floats_in_defaults_round_to_int_ratio() -> None:
    """Config defaults can carry float ratio (1.5), but PLC raw is uint16
    × 10. Defaults must be coerced via int(round(x * 10))."""
    custom = {**_DEFAULTS, "ratio_min": 2.7, "ratio_max": 4.3}
    out = _merge_with_defaults(_zero_settings(), custom)
    raw = out["raw_config"]
    assert raw[14] == 27  # 2.7 × 10 = 27
    assert raw[15] == 43  # 4.3 × 10 = 43


def test_merge_preserves_adapt_C_default_from_config() -> None:
    """adapt_C is never PLC-overridable in this protocol layer — config
    is the only path. A custom default must reach the processor at raw[7]."""
    custom = {**_DEFAULTS, "adapt_C": -3}
    out = _merge_with_defaults(_zero_settings(), custom)
    assert out["raw_config"][7] == -3


# --------------------------------------------------------------------------- #
# run_brush_head: Outcome → BrushHeadCycleResult mapping.
# --------------------------------------------------------------------------- #
def test_run_brush_head_maps_ok_to_recognition_result_1(monkeypatch) -> None:
    """OK Outcome → D0 = RESULT_FRONT_OR_OK (1)."""
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    fake_outcome = Outcome(
        result=ProcessResult.OK,
        image=np.full((100, 100, 3), 200, dtype=np.uint8),  # marker
        center=(1.0, 0.0),  # side=1 (Front)
        angle=0.0,
    )

    # Patch the module-level processor's process method.
    from legacy import fronback_brush_head

    monkeypatch.setattr(
        fronback_brush_head._PROCESSOR,
        "process",
        lambda image, settings: fake_outcome,
    )

    result = run_brush_head(img, _zero_settings(), _DEFAULTS)
    assert isinstance(result, BrushHeadCycleResult)
    assert result.plc_result == RESULT_FRONT_OR_OK
    assert result.display_image is fake_outcome.image


def test_run_brush_head_maps_ng_to_recognition_result_2(monkeypatch) -> None:
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    fake_outcome = Outcome(
        result=ProcessResult.NG,
        image=np.full((50, 50, 3), 100, dtype=np.uint8),
        center=(0.0, 0.0),
        angle=0.0,
    )
    from legacy import fronback_brush_head

    monkeypatch.setattr(
        fronback_brush_head._PROCESSOR,
        "process",
        lambda image, settings: fake_outcome,
    )

    result = run_brush_head(img, _zero_settings(), _DEFAULTS)
    assert result.plc_result == RESULT_BACK_OR_NG


def test_run_brush_head_maps_exception_to_ng(monkeypatch) -> None:
    """ProcessResult.EXCEPTION → D0 = RESULT_BACK_OR_NG (treat as failure)."""
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    fake_outcome = Outcome(
        result=ProcessResult.EXCEPTION,
        image=np.zeros((50, 50, 3), dtype=np.uint8),
        center=(0.0, 0.0),
        angle=0.0,
    )
    from legacy import fronback_brush_head

    monkeypatch.setattr(
        fronback_brush_head._PROCESSOR,
        "process",
        lambda image, settings: fake_outcome,
    )

    result = run_brush_head(img, _zero_settings(), _DEFAULTS)
    assert result.plc_result == RESULT_BACK_OR_NG


def test_run_brush_head_passes_merged_settings_to_processor(monkeypatch) -> None:
    """The processor must receive the dict produced by _merge_with_defaults
    — i.e. defaults filled in for any 0 PLC slot."""
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    captured: dict = {}

    def _spy_process(image, settings):
        captured.update(settings)
        return Outcome(ProcessResult.OK, image, (1.0, 0.0), 0.0)

    from legacy import fronback_brush_head

    monkeypatch.setattr(fronback_brush_head._PROCESSOR, "process", _spy_process)

    run_brush_head(img, _zero_settings(), _DEFAULTS)
    assert "raw_config" in captured
    raw = captured["raw_config"]
    # Sanity: defaults made it through to the processor.
    assert raw[8] == 20
    assert raw[9] == 500
    assert words_to_uint32(raw[10], raw[11]) == 50000
