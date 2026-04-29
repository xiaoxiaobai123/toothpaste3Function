"""High-level PLC operations: register layout, type marshalling, atomic block reads.

Per-camera config block (18 words, addresses are camera 1 / camera 2):
    +0   trigger        D10 / D30
    +1   exposure       D11 / D31
    +2-3 pixel_distance D12-13 / D32-33   (float32)
    +4   product_type   D14 / D34
    +5..+17  algorithm-specific parameters — interpreted by each Processor

The system status word lives at D1 / D2 alongside the camera blocks; the
read covers status + config in one atomic Modbus request, eliminating the
"status fresh, config stale" race.
"""

from __future__ import annotations

import threading
from typing import Any

from core import log_config
from plc.base import PLCBase
from plc.codec import (
    double_to_words,
    float32_to_words,
    uint32_to_words,
    words_to_float32,
)
from plc.enums import (
    CameraResult,
    CameraStatus,
    CameraTriggerStatus,
    Endian,
    ProductType,
    SystemStatus,
)

logger = log_config.setup_logging()


class PLCManager:
    """Modbus register layout + atomic block read/write for camera settings."""

    CONFIG_SIZE = 18
    READ_LAYOUT = {
        # camera_num: (status_register, config_start_register)
        1: (1, 10),  # status D1, config D10..D27
        2: (2, 30),  # status D2, config D30..D47
    }

    WRITE_REGISTERS = {
        1: {
            "output_x": 70,
            "output_y": 74,
            "output_angle": 78,
            "result": 82,
            "area": 83,
            "circularity": 85,
        },
        2: {
            "output_x": 90,
            "output_y": 94,
            "output_angle": 98,
            "result": 102,
            "area": 103,
            "circularity": 105,
        },
    }

    SYSTEM_REGISTERS = {
        "plc_heartbeat": 50,
        "system_status": 120,
        "error_code": 121,
        "system_heartbeat": 122,
        "camera1_trigger_status": 123,
        "camera2_trigger_status": 124,
        "camera1_status": 1,
        "camera2_status": 2,
    }

    def __init__(self, ip: str, port: int = 502, endian: Endian = Endian.LITTLE) -> None:
        self.plc = PLCBase(ip, port)
        self.endian = endian
        self.lock = threading.Lock()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def read_camera_settings(self, camera_num: int) -> dict[str, Any]:
        """Atomic block read of status + config for a single camera.

        Returns generic fields (status / trigger / exposure / pixel_distance /
        product_type) decoded into Python types, plus the full 18-word config
        block as `raw_config`. Each Processor decodes raw_config[5..17] into
        its own algorithm parameters — semantics differ per ProductType
        (see docs/PLC_REGISTERS.md).
        """
        if camera_num not in self.READ_LAYOUT:
            logger.error(f"Unsupported camera_num: {camera_num}")
            return {}

        status_addr, config_addr = self.READ_LAYOUT[camera_num]
        block_start = status_addr
        block_size = (config_addr + self.CONFIG_SIZE) - block_start
        status_idx = status_addr - block_start
        config_idx = config_addr - block_start

        with self.lock:
            block = self.plc.read_status(block_start, count=block_size)
            if block is None or not isinstance(block, list) or len(block) < block_size:
                logger.error(
                    f"[PLC] atomic-read failed for cam{camera_num} "
                    f"D{block_start}..D{block_start + block_size - 1}, got: {block}"
                )
                return {}

            c = config_idx
            return {
                "status": CameraStatus(block[status_idx]),
                "trigger_mode": CameraTriggerStatus(block[c + 0]),
                "exposure_time": block[c + 1],
                "pixel_distance": words_to_float32(block[c + 2], block[c + 3], self.endian),
                "product_type": ProductType(block[c + 4]),
                # Raw 18-word block — Processor reads raw_config[5..17] for
                # algorithm-specific parameters.
                "raw_config": tuple(block[config_idx : config_idx + self.CONFIG_SIZE]),
            }

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def write_camera_result(self, camera_num: int, result: CameraResult) -> None:
        """Block-write 17 result registers per camera in one Modbus call."""
        with self.lock:
            registers = self.WRITE_REGISTERS[camera_num]
            start_addr = registers["output_x"]

            words: list[int] = []
            words.extend(double_to_words(result.x))  # 4 words: output_x
            words.extend(double_to_words(result.y))  # 4 words: output_y
            words.extend(double_to_words(result.angle))  # 4 words: output_angle
            words.append(1 if result.result else 2)  # 1 word : result
            words.extend(uint32_to_words(result.area, self.endian))  # 2 words: area
            words.extend(float32_to_words(result.circularity))  # 2 words: circularity

            ok = self.plc.write_multiple_registers(start_addr, words)
            if not ok:
                logger.error(f"[PLC] cam{camera_num} block write failed (addr={start_addr}, n={len(words)})")

    # ------------------------------------------------------------------
    # System registers
    # ------------------------------------------------------------------
    def read_plc_heartbeat(self) -> int | None:
        v = self.plc.read_status(self.SYSTEM_REGISTERS["plc_heartbeat"])
        return v if isinstance(v, int) else None

    def write_system_status(self, status: SystemStatus) -> None:
        self.plc.write_status(self.SYSTEM_REGISTERS["system_status"], status.value)

    def write_error_code(self, code: int) -> None:
        self.plc.write_status(self.SYSTEM_REGISTERS["error_code"], code)

    def write_system_heartbeat(self, value: int) -> None:
        self.plc.write_status(self.SYSTEM_REGISTERS["system_heartbeat"], value)

    def write_camera_status(self, camera_num: int, status: CameraStatus) -> None:
        with self.lock:
            register = self.SYSTEM_REGISTERS[f"camera{camera_num}_status"]
            self.plc.write_status(register, status.value)

    def toggle_system_heartbeat(self) -> None:
        current = self.plc.read_status(self.SYSTEM_REGISTERS["system_heartbeat"])
        if isinstance(current, int):
            self.write_system_heartbeat(1 - current)

    def close(self) -> None:
        self.plc.close()
