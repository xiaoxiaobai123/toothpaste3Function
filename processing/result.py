"""Result enum and Outcome data class shared by all processors."""

from __future__ import annotations

from enum import Enum
from typing import NamedTuple

import numpy as np


class ProcessResult(Enum):
    OK = 1
    NG = 2
    EXCEPTION = 3


class Outcome(NamedTuple):
    """Standard return shape from every Processor.process() call.

    result        OK / NG / EXCEPTION
    image         BGR numpy array with overlays drawn (the image displayed
                  on the operator screen for this camera)
    center        (x, y) in image-centered coordinates after pixel_distance
                  scaling, or (999, 999) on NG/EXCEPTION
    angle         orientation in degrees, 0 if not applicable
    """

    result: ProcessResult
    image: np.ndarray
    center: tuple[float, float]
    angle: float
