"""Mock PLC manager for simulation without a real Modbus device.

Drop-in replacement for `plc.manager.PLCManager` backed by an in-memory
state. Result writes are recorded in `results_log` so tests can assert on
what TaskManager produced. `set_camera_status()` lets a test driver flip
state-machine values that would normally come from the PLC ladder.

Use together with `camera.mock.MockCameraManager` to exercise the full
TaskManager pipeline without hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from core import log_config
from plc.enums import (
    CameraResult,
    CameraStatus,
    CameraTriggerStatus,
    ProductType,
    SystemStatus,
)

logger = log_config.setup_logging()


@dataclass
class MockCameraConfig:
    """Per-camera state read by TaskManager via read_camera_settings()."""

    status: CameraStatus = CameraStatus.IDLE
    trigger_mode: CameraTriggerStatus = CameraTriggerStatus.SOFTWARE_TRIGGER
    exposure_time: int = 5000
    pixel_distance: float = 1.0
    product_type: ProductType = ProductType.NONE
    raw_config: tuple[int, ...] = field(default_factory=lambda: tuple([0] * 18))


@dataclass
class ResultRecord:
    camera_num: int
    result: CameraResult


class MockPLCManager:
    """In-memory PLCManager replacement.

    Public surface mirrors plc.manager.PLCManager — TaskManager interacts
    only through these methods, so no inheritance is needed.
    """

    def __init__(self, configs: dict[int, MockCameraConfig] | None = None) -> None:
        self._configs: dict[int, MockCameraConfig] = configs or {}
        self.results_log: list[ResultRecord] = []
        self.system_status: SystemStatus = SystemStatus.IDLE
        self.error_code: int = 0
        self.system_heartbeat: int = 0
        self._lock = Lock()

    # ------------------------------------------------------------------
    # Test driver helpers (not part of the real PLCManager API)
    # ------------------------------------------------------------------
    def set_camera_config(self, camera_num: int, cfg: MockCameraConfig) -> None:
        with self._lock:
            self._configs[camera_num] = cfg

    def set_camera_status_value(self, camera_num: int, status: CameraStatus) -> None:
        """Flip just the status word, keeping other fields. Used by test scripts."""
        with self._lock:
            cfg = self._configs.setdefault(camera_num, MockCameraConfig())
            cfg.status = status

    def reset_results(self) -> None:
        with self._lock:
            self.results_log.clear()

    # ------------------------------------------------------------------
    # PLCManager-compatible read / write methods
    # ------------------------------------------------------------------
    def read_camera_settings(self, camera_num: int) -> dict[str, Any]:
        with self._lock:
            cfg = self._configs.get(camera_num)
            if cfg is None:
                return {}
            return {
                "status": cfg.status,
                "trigger_mode": cfg.trigger_mode,
                "exposure_time": cfg.exposure_time,
                "pixel_distance": cfg.pixel_distance,
                "product_type": cfg.product_type,
                "raw_config": cfg.raw_config,
            }

    def write_camera_result(self, camera_num: int, result: CameraResult) -> None:
        with self._lock:
            self.results_log.append(ResultRecord(camera_num=camera_num, result=result))
        logger.debug(
            f"[MockPLC] cam{camera_num} result x={result.x:.1f} y={result.y:.1f} "
            f"angle={result.angle:.2f} ok={result.result}"
        )

    def write_camera_status(self, camera_num: int, status: CameraStatus) -> None:
        with self._lock:
            cfg = self._configs.setdefault(camera_num, MockCameraConfig())
            cfg.status = status

    def write_system_status(self, status: SystemStatus) -> None:
        with self._lock:
            self.system_status = status

    def write_error_code(self, code: int) -> None:
        with self._lock:
            self.error_code = code

    def write_system_heartbeat(self, value: int) -> None:
        with self._lock:
            self.system_heartbeat = value

    def toggle_system_heartbeat(self) -> None:
        with self._lock:
            self.system_heartbeat = 1 - self.system_heartbeat

    def read_plc_heartbeat(self) -> int:
        return self.system_heartbeat

    def close(self) -> None:
        pass
