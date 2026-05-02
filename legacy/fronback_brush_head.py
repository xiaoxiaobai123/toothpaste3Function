"""Legacy-protocol adapter for the v2 BrushHeadProcessor.

Handles the D2=2 dispatch path: takes the 14-word BrushHeadSettings
block (D50-D63 — physically separated from frontback / height registers
per customer spec) + the operator's config.json defaults, shapes them
into the 18-word raw_config dict that BrushHeadProcessor consumes, runs
the same algorithm v2 uses for ProductType.BRUSH_HEAD, and maps the
Outcome back to legacy's D0/D42/D43 register layout.

We deliberately reuse the v2 BrushHeadProcessor verbatim — no
algorithm divergence between v2 brush_head and legacy brush_head — so
a future port of customers from legacy → v2 produces identical
detection behaviour. The adapter is purely a parameter+result shape
translation.

Per-slot fallback: each PLC field at 0 means "use the config.json
default for this parameter", so the customer can ship a minimal PLC
ladder (just D2=2 dispatch + a few fields they care about) and tune the
rest from config. Field-by-field: writing 0 to a slot the customer
*does* want at the literal value 0 is impossible — that's an inherent
limitation of the sentinel scheme. None of the brush_head parameters
have 0 as a meaningful operating value, so this is fine in practice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from legacy.fronback_protocol import (
    RESULT_BACK_OR_NG,
    RESULT_FRONT_OR_OK,
    BrushHeadSettings,
)
from plc.codec import uint32_to_words
from processing.brush_head import BrushHeadProcessor
from processing.result import ProcessResult


@dataclass(frozen=True)
class BrushHeadCycleResult:
    """One brush-head cycle's result, ready for the orchestrator to write
    to PLC + display."""

    plc_result: int  # D0: 1=OK, 2=NG
    dot_count: int  # D42 (clamped to uint16 by writer)
    area: int  # D43 (raw; writer divides by 100)
    display_image: np.ndarray  # Visualization for the rgb565 sink


# A single processor instance is reused across cycles — it's stateless
# (.process() takes the image + settings and returns an Outcome) so this
# is safe and avoids reconstructing OpenCV state every iteration.
_PROCESSOR = BrushHeadProcessor()


def _merge_with_defaults(legacy: BrushHeadSettings, defaults: dict[str, Any]) -> dict[str, Any]:
    """Build the v2-shaped settings dict from PLC values + config defaults.

    Builds an 18-word raw_config array (BrushHeadProcessor reads indices
    5-15) where each populated slot is either the PLC value (when
    non-zero) or the config default. Slot layout:

        raw[5]      shrink_pct          (D51 / defaults["shrink_pct"])
        raw[6]      adapt_block         (D52 / defaults["adapt_block"])
        raw[7]      adapt_C             (NOT exposed via PLC; defaults only)
        raw[8]      dot_area_min        (D54 / defaults["dot_area_min"])
        raw[9]      dot_area_max        (D55 / defaults["dot_area_max"])
        raw[10..11] roi_area_min uint32 (D56 × 100 / defaults["roi_area_min"])
        raw[12..13] roi_area_max uint32 (D57 × 100 / defaults["roi_area_max"])
        raw[14]     ratio_min × 10      (D58 / defaults["ratio_min"] × 10)
        raw[15]     ratio_max × 10      (D59 / defaults["ratio_max"] × 10)

    `manual_roi` is the D60-D63 4-tuple; (0,0,0,0) means "auto-detect on
    full frame", same as the v2 default.
    """
    raw = [0] * 18

    raw[5] = legacy.shrink_pct if legacy.shrink_pct != 0 else int(defaults["shrink_pct"])
    raw[6] = legacy.adapt_block if legacy.adapt_block != 0 else int(defaults["adapt_block"])
    raw[7] = int(defaults["adapt_C"])  # never PLC-overridable in this protocol layer
    raw[8] = legacy.dot_area_min if legacy.dot_area_min != 0 else int(defaults["dot_area_min"])
    raw[9] = legacy.dot_area_max if legacy.dot_area_max != 0 else int(defaults["dot_area_max"])

    roi_area_min = (
        legacy.roi_area_min_x100 * 100 if legacy.roi_area_min_x100 != 0 else int(defaults["roi_area_min"])
    )
    roi_area_max = (
        legacy.roi_area_max_x100 * 100 if legacy.roi_area_max_x100 != 0 else int(defaults["roi_area_max"])
    )
    # uint32 → 2-word little-endian split (raw[10]=low, raw[11]=high) — matches
    # what BrushHeadProcessor reconstructs via words_to_uint32(raw[10], raw[11]).
    lo, hi = uint32_to_words(roi_area_min)
    raw[10], raw[11] = lo, hi
    lo, hi = uint32_to_words(roi_area_max)
    raw[12], raw[13] = lo, hi

    raw[14] = (
        legacy.ratio_min_x10 if legacy.ratio_min_x10 != 0 else int(round(float(defaults["ratio_min"]) * 10))
    )
    raw[15] = (
        legacy.ratio_max_x10 if legacy.ratio_max_x10 != 0 else int(round(float(defaults["ratio_max"]) * 10))
    )

    return {
        "raw_config": raw,
        # manual_roi falls through unchanged. (0,0,0,0) = auto-detect inside
        # BrushHeadProcessor; non-zero rectangle pre-crops the image.
        "manual_roi": legacy.manual_roi,
    }


def run_brush_head(
    image: np.ndarray,
    legacy_settings: BrushHeadSettings,
    defaults: dict[str, Any],
) -> BrushHeadCycleResult:
    """Run BrushHeadProcessor on `image` and translate the Outcome into
    legacy D0/D42/D43 + a display-ready BGR image.

    BrushHeadProcessor.Outcome.center.x carries the side code
    (1=Front, 2=Back, 0=NG) — but legacy's D0 alphabet is OK/NG/EMPTY
    (1/2/3). We collapse Front/Back into a single OK because legacy
    customers aren't running differential front/back logic on this mode
    (that's what frontback D2=1 is for). Side detail is still visible
    on the operator screen via the visualization image.

    dot_count and area aren't currently exposed by Outcome; they're
    written to logs inside BrushHeadProcessor. For D42/D43 we report 0
    until BrushHeadProcessor exposes them as fields (deliberately not
    parsing log strings — too fragile). Customer PLCs that don't read
    D42/D43 see no behavior change.
    """
    settings = _merge_with_defaults(legacy_settings, defaults)
    outcome = _PROCESSOR.process(image, settings)

    plc_result = RESULT_FRONT_OR_OK if outcome.result == ProcessResult.OK else RESULT_BACK_OR_NG

    return BrushHeadCycleResult(
        plc_result=plc_result,
        dot_count=0,  # TODO(brush_head): expose via Outcome side-channel
        area=0,
        display_image=outcome.image,
    )
