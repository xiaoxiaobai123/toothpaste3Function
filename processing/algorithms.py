"""Cross-algorithm helper functions.

Generic utilities shared by every Processor. Algorithm-specific helpers
live in their own module (e.g. brush-head ROI detection lives entirely
inside processing/brush_head.py).
"""

from __future__ import annotations

from core import log_config

logger = log_config.setup_logging()


def validate_and_adjust_param(value: float, default: float, min_val: float, max_val: float) -> float:
    """Clamp a PLC-supplied parameter to its valid range.

    `value == 0` is treated as "use default" (PLC sentinel for unset
    fields). Out-of-range values are clamped with a warning so a single
    typo on the HMI side does not silently change algorithm behavior.
    """
    if value == 0:
        return default
    if min_val <= value <= max_val:
        return value
    logger.warning(f"Parameter {value} outside [{min_val}, {max_val}], clamping.")
    return max(min_val, min(value, max_val))


def adjust_bounds(
    lower: float, upper: float, name: str, default_pair: tuple[float, float]
) -> tuple[float, float]:
    """Use defaults when PLC supplies an inverted (lower > upper) pair."""
    if lower > upper:
        logger.warning(f"{name.capitalize()} lower ({lower}) > upper ({upper}); using defaults.")
        return default_pair
    return lower, upper


def convert_to_center_coordinates(
    point: tuple[float, float], image_size: tuple[int, int]
) -> tuple[float, float]:
    """Move origin from top-left to image center; flip the y axis.

    image_size is (width, height). Used by every Processor to express
    output coordinates relative to the operator's view of the workpiece.
    """
    cx = image_size[0] / 2
    cy = image_size[1] / 2
    return point[0] - cx, cy - point[1]
