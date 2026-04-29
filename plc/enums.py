"""Enumerations and named tuples for PLC communication.

ProductType is the dispatch key picked up by TaskManager; the registry in
processing/registry.py maps each value to a Processor implementation.
"""

from __future__ import annotations

from enum import Enum
from typing import NamedTuple


class CameraStatus(Enum):
    IDLE = 0
    READING_DATA = 1
    PROCESSING_DATA = 2
    TASK_COMPLETED = 3
    START_TASK = 10
    START_LOOP = 11


class ProductType(Enum):
    """Detection algorithms selectable per camera via PLC register D14/D34.

    Each value maps to one Processor in processing/registry.py. Adding a
    new value requires:
        1. Add the enum value here.
        2. Implement processing/<name>.py with a Processor subclass.
        3. Register in processing/registry.py.
        4. Document the +5..+17 register layout in docs/PLC_REGISTERS.md.
    """

    NONE = 0
    TOOTHPASTE_FRONTBACK = 1
    HEIGHT_CHECK = 2
    BRUSH_HEAD = 3


class SystemStatus(Enum):
    STARTING = 0
    IDLE = 1
    PROCESSING = 2
    ERROR = 3


class CameraTriggerStatus(Enum):
    DISCONNECTED = 0
    HARDWARE_TRIGGER = 1
    SOFTWARE_TRIGGER = 2


class Endian(Enum):
    LITTLE = "little"
    BIG = "big"


class CameraResult(NamedTuple):
    """Result block written back to PLC per camera (17 words).

    For algorithms that return a discrete classification (e.g. BRUSH_HEAD's
    front=1 / back=2), encode it in `x` and leave `y` / `angle` at 0.
    """

    x: float
    y: float
    angle: float
    result: bool
    area: int
    circularity: float
