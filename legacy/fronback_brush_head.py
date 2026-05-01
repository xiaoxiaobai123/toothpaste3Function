"""Legacy-protocol adapter for the v2 BrushHeadProcessor.

Handles the D2=2 dispatch path: takes BrushHeadSettings (raw PLC words,
0 means "use config default") + the operator's config.json defaults,
shapes them into the 18-word raw_config dict that BrushHeadProcessor
consumes, runs the same algorithm v2 uses for ProductType.BRUSH_HEAD,
and maps the Outcome back to legacy's D0/D42/D43 register layout.

We deliberately reuse the v2 BrushHeadProcessor verbatim — no
algorithm divergence between v2 brush_head and legacy brush_head —
so a future port of customers from legacy → v2 produces identical
detection behaviour. The adapter is purely a parameter+result shape
translation.
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
from processing.brush_head import BrushHeadProcessor
from processing.result import ProcessResult


@dataclass(frozen=True)
class BrushHeadCycleResult:
    """One brush-head cycle's result, ready for the orchestrator to write
    to PLC + display."""

    plc_result: int                  # D0: 1=OK, 2=NG
    dot_count: int                   # D42 (clamped to uint16 by writer)
    area: int                        # D43 (raw; writer divides by 100)
    display_image: np.ndarray        # Visualization for the rgb565 sink


# A single processor instance is reused across cycles — it's stateless
# (.process() takes the image + settings and returns an Outcome) so this
# is safe and avoids reconstructing OpenCV state every iteration.
_PROCESSOR = BrushHeadProcessor()


def _merge_with_defaults(
    legacy: BrushHeadSettings, defaults: dict[str, Any]
) -> dict[str, Any]:
    """Build the v2-shaped raw_config dict, swapping in defaults for any
    legacy slot the PLC left at 0.

    raw_config is an 18-word array; BrushHeadProcessor reads indices 5-15.
    We only populate the four slots legacy exposes (8/9/14/15); the rest
    stay 0 so BrushHeadProcessor's own per-field "0 → DEFAULTS[...]"
    fallback fills them in.

    Ratios are stored × 10 in PLC for uint16 fit (15 = 1.5). Defaults are
    stored as floats in config.json — convert before mixing.
    """
    raw = [0] * 18

    raw[8] = (
        legacy.dot_area_min
        if legacy.dot_area_min != 0
        else int(defaults["dot_area_min"])
    )
    raw[9] = (
        legacy.dot_area_max
        if legacy.dot_area_max != 0
        else int(defaults["dot_area_max"])
    )
    raw[14] = (
        legacy.ratio_min_x10
        if legacy.ratio_min_x10 != 0
        else int(round(float(defaults["ratio_min"]) * 10))
    )
    raw[15] = (
        legacy.ratio_max_x10
        if legacy.ratio_max_x10 != 0
        else int(round(float(defaults["ratio_max"]) * 10))
    )

    return {
        "raw_config": raw,
        # Legacy brush_head doesn't expose manual pre-crop ROI to PLC
        # — the PLC parameter budget is tight. (0,0,0,0) means
        # "auto-detect on full frame" inside BrushHeadProcessor.
        "manual_roi": (0, 0, 0, 0),
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

    plc_result = (
        RESULT_FRONT_OR_OK if outcome.result == ProcessResult.OK else RESULT_BACK_OR_NG
    )

    return BrushHeadCycleResult(
        plc_result=plc_result,
        dot_count=0,   # TODO(brush_head): expose via Outcome side-channel
        area=0,
        display_image=outcome.image,
    )
