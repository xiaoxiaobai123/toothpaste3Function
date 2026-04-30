"""Multi-camera lifecycle manager (dynamic 1-N).

Iterates over cameras configured in config.json (camera1, camera2, ...) and
initializes each that is `enabled`. Failed initializations are logged but
do not abort the manager: the system runs with whatever cameras came up,
and TaskManager.active_camera_nums() drives the per-camera asyncio tasks.
"""

from __future__ import annotations

import threading
import time

import numpy as np

from camera.base import CameraBase
from core import log_config
from core.config_manager import config

logger = log_config.setup_logging()


class CameraManager:
    # Auto-reinit policy: after this many consecutive capture failures on the
    # same camera, the manager attempts to close + reopen the MVS device handle.
    # Real-world cause: GigE camera "half-dead" state where ping still works but
    # the stream channel hangs (network blip, firmware glitch, switch buffer
    # overrun). MV_E_NER_TIMEOUT (0x80000206) and similar error codes resolve
    # ~90% of the time after a fresh handle.
    #
    # Cooldown prevents pathological reinit-storms when the camera is genuinely
    # offline (e.g. unplugged) — we attempt at most one reinit per cooldown
    # window, then back off and let the orchestrator render OFFLINE placeholders
    # until the network actually comes back.
    AUTO_REINIT_THRESHOLD = 3
    AUTO_REINIT_COOLDOWN_S = 30.0

    def __init__(self) -> None:
        self.cameras: dict[int, CameraBase] = {}
        self.camera_locks: dict[int, threading.Lock] = {}
        # Per-camera failure tracking for auto-reinit decisions.
        self._consecutive_failures: dict[int, int] = {}
        self._last_reinit_at: dict[int, float] = {}
        self._initialize_cameras()

    def _initialize_cameras(self) -> None:
        for i in config.configured_camera_nums():
            self.camera_locks[i] = threading.Lock()
            if not config.is_camera_enabled(i):
                logger.info(f"[Cam{i}] disabled in config.json, skipping")
                continue
            device_ip = config.get_camera_ip(i)
            net_ip = config.get_camera_host_lan(i)
            roi = config.get_camera_roi(i)
            try:
                camera = CameraBase(device_ip, net_ip, camera_num=i, roi=roi)
                if camera.init_camera():
                    self.cameras[i] = camera
                    self.start_grabbing(i)
                else:
                    logger.error(f"[Cam{i}] init failed (offline / wrong IP / occupied?)")
            except Exception as e:
                logger.error(f"[Cam{i}] init exception: {e}")

    def active_camera_nums(self) -> list[int]:
        """Return numbers of cameras successfully initialized."""
        return sorted(self.cameras.keys())

    def get_camera(self, camera_num: int) -> CameraBase | None:
        return self.cameras.get(camera_num)

    def get_camera_info(self, camera_num: int) -> dict[str, str] | None:
        camera = self.get_camera(camera_num)
        if camera:
            return {"device_ip": camera.device_ip, "net_ip": camera.net_ip}
        return None

    # ------------------------------------------------------------------
    # Capture / config wrappers — all serialize through per-camera locks.
    # ------------------------------------------------------------------
    def capture_image(
        self,
        camera_num: int,
        is_hardware_trigger: bool = False,
        max_retries: int = 3,
    ) -> np.ndarray | None:
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return None
        with self.camera_locks[camera_num]:
            try:
                image = camera.capture_image(is_hardware_trigger=is_hardware_trigger, max_retries=max_retries)
            except Exception as e:
                logger.error(f"[Cam{camera_num}] capture exception: {e}")
                image = None

        if image is None:
            logger.error(f"[Cam{camera_num}] capture returned no image")
            # Auto-reinit decision is outside the per-camera lock because
            # reinitialize_camera() pops + recreates the camera entry; holding
            # the old camera's lock during that swap would be a use-after-free.
            self._maybe_auto_reinit(camera_num)
        else:
            # Successful capture clears the failure counter — only consecutive
            # failures count toward the auto-reinit threshold.
            self._consecutive_failures.pop(camera_num, None)
        return image

    def _maybe_auto_reinit(self, camera_num: int) -> None:
        """Track consecutive failures and attempt a fresh init at threshold.

        Cooldown prevents reinit storms when the camera is genuinely offline:
        we try once, wait COOLDOWN_S, try again. Between attempts the
        orchestrator's normal "cam offline" path keeps the line running with
        OFFLINE placeholder frames.
        """
        n = self._consecutive_failures.get(camera_num, 0) + 1
        self._consecutive_failures[camera_num] = n
        if n < self.AUTO_REINIT_THRESHOLD:
            return
        now = time.monotonic()
        if now - self._last_reinit_at.get(camera_num, 0.0) < self.AUTO_REINIT_COOLDOWN_S:
            return
        self._last_reinit_at[camera_num] = now
        logger.warning(
            f"[Cam{camera_num}] {n} consecutive capture failures — auto-reinit attempt"
        )
        if self.reinitialize_camera(camera_num):
            logger.info(f"[Cam{camera_num}] auto-reinit succeeded")
            self._consecutive_failures.pop(camera_num, None)
        else:
            logger.error(
                f"[Cam{camera_num}] auto-reinit failed; will retry after "
                f"{self.AUTO_REINIT_COOLDOWN_S:.0f}s cooldown"
            )

    def set_exposure(self, camera_num: int, exposure_time: float) -> bool:
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return False
        with self.camera_locks[camera_num]:
            try:
                return camera.write_exposure_time(exposure_time)
            except Exception as e:
                logger.error(f"[Cam{camera_num}] set exposure exception: {e}")
                return False

    def flush_one_frame(self, camera_num: int) -> bool:
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return False
        with self.camera_locks[camera_num]:
            try:
                return camera.flush_one_frame()
            except Exception as e:
                logger.error(f"[Cam{camera_num}] flush exception: {e}")
                return False

    def start_grabbing(self, camera_num: int) -> bool:
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return False
        try:
            return camera.start_grabbing()
        except Exception as e:
            logger.error(f"[Cam{camera_num}] start grabbing exception: {e}")
            return False

    def stop_grabbing(self, camera_num: int) -> bool:
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return False
        with self.camera_locks[camera_num]:
            try:
                return camera.stop_grabbing()
            except Exception as e:
                logger.error(f"[Cam{camera_num}] stop grabbing exception: {e}")
                return False

    def update_trigger_mode(self, camera_num: int, is_hardware_trigger: bool) -> bool:
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return False
        with self.camera_locks[camera_num]:
            try:
                return camera.update_trigger_mode(is_hardware_trigger)
            except Exception as e:
                logger.error(f"[Cam{camera_num}] trigger mode exception: {e}")
                return False

    def reinitialize_camera(self, camera_num: int) -> bool:
        try:
            self.cameras.pop(camera_num, None)
            camera_ip = config.get_camera_ip(camera_num)
            host_lan = config.get_camera_host_lan(camera_num)
            roi = config.get_camera_roi(camera_num)
            new_camera = CameraBase(camera_ip, host_lan, camera_num=camera_num, roi=roi)
            if new_camera.init_camera():
                self.cameras[camera_num] = new_camera
                self.start_grabbing(camera_num)
                return True
            logger.error(f"[Cam{camera_num}] reinitialize failed")
            return False
        except Exception as e:
            logger.error(f"[Cam{camera_num}] reinitialize exception: {e}")
            return False

    def close_all_cameras(self) -> None:
        for camera_num, camera in list(self.cameras.items()):
            with self.camera_locks[camera_num]:
                try:
                    camera.close_camera()
                except Exception as e:
                    logger.error(f"[Cam{camera_num}] close exception: {e}")
        self.cameras.clear()
        self.camera_locks.clear()

    def get_trigger_source(self, camera_num: int) -> int | None:
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return None
        with self.camera_locks[camera_num]:
            try:
                source = camera.get_trigger_source()
                if source is None:
                    logger.error(f"[Cam{camera_num}] get trigger source failed")
                return source
            except Exception as e:
                logger.error(f"[Cam{camera_num}] get trigger source exception: {e}")
                return None

    def get_exposure_time(self, camera_num: int) -> float | None:
        with self.camera_locks[camera_num]:
            camera = self.get_camera(camera_num)

            if camera is None:
                logger.warning(f"[Cam{camera_num}] not found, attempting reinit")
                if not self.reinitialize_camera(camera_num):
                    return None
                camera = self.get_camera(camera_num)

            try:
                exposure_time = camera.get_exposure_time()
                if exposure_time is not None:
                    return exposure_time
            except Exception as e:
                logger.warning(f"[Cam{camera_num}] read exposure exception: {e}")

            if camera is not None:
                logger.warning(f"[Cam{camera_num}] read exposure failed, attempting reinit")
                if self.reinitialize_camera(camera_num):
                    camera = self.get_camera(camera_num)
                    try:
                        exposure_time = camera.get_exposure_time()
                        if exposure_time is not None:
                            return exposure_time
                    except Exception as e:
                        logger.error(f"[Cam{camera_num}] read exposure after reinit failed: {e}")

            logger.error(f"[Cam{camera_num}] read exposure gave up")
            return None
