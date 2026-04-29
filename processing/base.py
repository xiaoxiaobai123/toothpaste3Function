"""Abstract Processor base for all detection algorithms.

Each ProductType maps to one Processor in processing/registry.py.
TaskManager dispatches captured images through that registry, so adding
a new detection mode is one new file + one line in the registry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from processing.result import Outcome


class Processor(ABC):
    """Each subclass implements a single detection algorithm."""

    name: str = "Processor"

    @abstractmethod
    def process(self, image: np.ndarray, settings: dict[str, Any]) -> Outcome:
        """Run detection on a captured BGR image.

        `settings` is the dict returned by PLCManager.read_camera_settings():
            status, trigger_mode, exposure_time, pixel_distance, product_type,
            gray_upper, gray_lower, area_upper, area_lower,
            circularity_upper, circularity_lower,
            roi_x, roi_y, roi_diameter,
            raw_config (tuple of 18 raw words for processor-specific decoding)

        Implementations must always return a valid Outcome — they should
        catch their own exceptions and report Outcome(EXCEPTION, ...).
        """
        ...
