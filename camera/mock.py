"""Mock camera manager for simulation without hardware.

Drop-in replacement for `camera.manager.CameraManager` that serves images
from configured directories instead of capturing from a real Hikvision
camera. All trigger/exposure operations are accepted as no-ops.

Use when:
    - Running unit / integration tests that exercise TaskManager.
    - Reproducing field issues from saved images.
    - Demoing the system on a laptop without an MVS-licensed host.

Image discovery:
    Each `image_dirs[camera_num]` is scanned for *.png / *.jpg / *.jpeg
    files (case-insensitive). On every `capture_image()` call the next
    image in lexical order is returned, cycling indefinitely.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import cv2
import numpy as np

from core import log_config

logger = log_config.setup_logging()

_IMAGE_GLOBS = ("*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG", "*.JPEG")


class MockCameraManager:
    """File-folder backed CameraManager replacement.

    Mirrors the public API consumed by TaskManager:
        active_camera_nums, get_camera, get_camera_info,
        capture_image, set_exposure, flush_one_frame,
        update_trigger_mode, get_trigger_source, get_exposure_time,
        reinitialize_camera, close_all_cameras, start_grabbing, stop_grabbing.
    """

    SOFTWARE_TRIGGER_SOURCE = 7

    def __init__(self, image_dirs: dict[int, Path | str]) -> None:
        self._dirs = {n: Path(d) for n, d in image_dirs.items()}
        self._paths: dict[int, list[Path]] = {}
        self._cycles: dict[int, itertools.cycle[Path]] = {}
        self._exposures: dict[int, float] = {}
        self._trigger_sources: dict[int, int] = {}

        for num, directory in self._dirs.items():
            paths: list[Path] = []
            for pattern in _IMAGE_GLOBS:
                paths.extend(directory.glob(pattern))
            paths = sorted(set(paths))
            if not paths:
                logger.warning(f"[MockCam{num}] no images found in {directory}")
                continue
            self._paths[num] = paths
            self._cycles[num] = itertools.cycle(paths)
            self._exposures[num] = 5000.0
            self._trigger_sources[num] = self.SOFTWARE_TRIGGER_SOURCE
            logger.info(f"[MockCam{num}] loaded {len(paths)} images from {directory}")

    # ------------------------------------------------------------------
    # Identity / discovery
    # ------------------------------------------------------------------
    def active_camera_nums(self) -> list[int]:
        return sorted(self._cycles.keys())

    def get_camera(self, camera_num: int) -> object | None:
        # The real CameraManager returns a CameraBase; we return self as a
        # marker so existence checks pass. TaskManager never inspects it.
        return self if camera_num in self._cycles else None

    def get_camera_info(self, camera_num: int) -> dict[str, str] | None:
        if camera_num not in self._cycles:
            return None
        return {"device_ip": "mock", "net_ip": "mock"}

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------
    def capture_image(
        self,
        camera_num: int,
        is_hardware_trigger: bool = False,  # noqa: ARG002 — kept for signature parity
        max_retries: int = 3,  # noqa: ARG002
    ) -> np.ndarray | None:
        cycle = self._cycles.get(camera_num)
        if cycle is None:
            logger.error(f"[MockCam{camera_num}] not found")
            return None
        path = next(cycle)
        img = cv2.imread(str(path))
        if img is None:
            logger.error(f"[MockCam{camera_num}] failed to load {path}")
            return None
        logger.debug(f"[MockCam{camera_num}] capture {path.name}")
        return img

    # ------------------------------------------------------------------
    # No-op control surface (returns success values).
    # ------------------------------------------------------------------
    def set_exposure(self, camera_num: int, exposure_time: float) -> bool:
        if camera_num not in self._cycles:
            return False
        self._exposures[camera_num] = float(exposure_time)
        logger.debug(f"[MockCam{camera_num}] exposure → {int(exposure_time)}us")
        return True

    def flush_one_frame(self, camera_num: int) -> bool:
        return camera_num in self._cycles

    def update_trigger_mode(self, camera_num: int, is_hardware_trigger: bool) -> bool:
        if camera_num not in self._cycles:
            return False
        self._trigger_sources[camera_num] = 0 if is_hardware_trigger else self.SOFTWARE_TRIGGER_SOURCE
        return True

    def get_trigger_source(self, camera_num: int) -> int | None:
        return self._trigger_sources.get(camera_num)

    def get_exposure_time(self, camera_num: int) -> float | None:
        return self._exposures.get(camera_num)

    def reinitialize_camera(self, camera_num: int) -> bool:
        return camera_num in self._cycles

    def start_grabbing(self, camera_num: int) -> bool:
        return camera_num in self._cycles

    def stop_grabbing(self, camera_num: int) -> bool:
        return camera_num in self._cycles

    def close_all_cameras(self) -> None:
        self._cycles.clear()
        self._exposures.clear()
        self._trigger_sources.clear()
