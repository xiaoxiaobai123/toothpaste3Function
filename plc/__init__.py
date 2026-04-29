"""PLC communication layer (Modbus TCP)."""

from plc.enums import (
    CameraResult,
    CameraStatus,
    CameraTriggerStatus,
    Endian,
    ProductType,
    SystemStatus,
)
from plc.manager import PLCManager

__all__ = [
    "CameraResult",
    "CameraStatus",
    "CameraTriggerStatus",
    "Endian",
    "PLCManager",
    "ProductType",
    "SystemStatus",
]
