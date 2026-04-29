"""Async main loop for the legacy fronback protocol.

Replaces TaskManager when `config.json` selects `plc_protocol == "legacy_fronback"`.

Polls D1 (capture trigger) and D2 (mode) every ~50 ms — same cadence as
the original program. When D1 == 10:

    D2 == 1 → dual-camera frontback comparison (cam1 + cam2)
    D2 == 0 → single-camera height check (cam2 only)

Per-camera ROI is loaded from `roi_coordinates_<ip-with-underscores>.json`
in the working directory (matches the original's path convention) — the
field deployment files for camera1/camera2 carry over unchanged.

Uses asyncio.gather for parallel I/O (set_exposure on both cameras,
write_camera_status writes, capture_image on both cameras simultaneously)
where the original program ran them serially. This is the only place
the legacy path diverges from the original — purely a performance win
that does not change observable PLC behaviour.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from legacy.fronback_algorithms import compute_frontback, compute_height
from legacy.fronback_display import (
    DEFAULT_DISPLAY_PATH,
    render_frontback,
    render_height,
)
from legacy.fronback_protocol import (
    MODE_FRONTBACK,
    MODE_HEIGHT,
    RESULT_BACK_OR_NG,
    RESULT_EMPTY,
    RESULT_FRONT_OR_OK,
    TRIGGER_DONE,
    TRIGGER_FIRE,
    TRIGGER_IDLE,
    LegacyFronbackPLC,
)

POLL_INTERVAL_S = 0.05  # matches the original program's 50 ms loop.


# --------------------------------------------------------------------------- #
# ROI provider — file-based, matches the original `load_roi_coordinates_from_file`.
# --------------------------------------------------------------------------- #
def make_file_roi_provider(
    camera_manager: Any,
    base_dir: Path | str = ".",
    logger: logging.Logger | None = None,
) -> Callable[[int], dict[str, int]]:
    """Return a `get_roi(camera_num)` function that loads from disk on first
    access and caches thereafter.

    File naming follows the original program's convention:
        roi_coordinates_<ip-with-dots-as-underscores>.json
    e.g. `roi_coordinates_192_168_2_10.json` for cam1 @ 192.168.2.10.

    A missing file returns a "full frame" ROI rather than crashing — this
    matches the v2 path's tolerant behaviour and is friendlier than the
    original program's hard `FileNotFoundError`.
    """
    log = logger or logging.getLogger(__name__)
    base = Path(base_dir)
    cache: dict[int, dict[str, int]] = {}
    full_frame: dict[str, int] = {"x1": 0, "y1": 0, "x2": 99999, "y2": 99999}

    def get_roi(camera_num: int) -> dict[str, int]:
        if camera_num in cache:
            return cache[camera_num]
        info = camera_manager.get_camera_info(camera_num)
        if not info:
            log.error(f"[Legacy] no camera info for cam{camera_num}, using full frame")
            cache[camera_num] = full_frame
            return cache[camera_num]
        ip = info["device_ip"]
        path = base / f"roi_coordinates_{ip.replace('.', '_')}.json"
        if not path.is_file():
            log.warning(f"[Legacy] ROI file missing: {path}, using full frame")
            cache[camera_num] = full_frame
            return cache[camera_num]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            roi = {k: int(float(data[k])) for k in ("x1", "y1", "x2", "y2")}
            cache[camera_num] = roi
            log.info(f"[Legacy] cam{camera_num} ROI loaded from {path}: {roi}")
            return roi
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            log.error(f"[Legacy] failed to parse {path}: {e}; using full frame")
            cache[camera_num] = full_frame
            return cache[camera_num]

    return get_roi


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
class LegacyFronbackOrchestrator:
    """Single-loop orchestrator wiring PLC <-> CameraManager <-> algorithms."""

    def __init__(
        self,
        plc: LegacyFronbackPLC,
        camera_manager: Any,
        roi_provider: Callable[[int], dict[str, int]],
        logger: logging.Logger,
        display_path: str | Path = DEFAULT_DISPLAY_PATH,
    ) -> None:
        self.plc = plc
        self.cam = camera_manager
        self.get_roi = roi_provider
        self.logger = logger
        self.display_path = str(display_path)

        self._exposure_cache: dict[int, int] = {}

    # ---------------------------------------------------------------- run loop

    async def run(self) -> None:
        active = self.cam.active_camera_nums()
        if not active:
            self.logger.error("[Legacy] no cameras initialized; orchestrator not starting")
            return

        self.logger.info(f"[Legacy] orchestrator started (active cameras: {active})")
        while True:
            try:
                state = await asyncio.to_thread(self.plc.read_trigger_and_mode)
                if state is None:
                    await asyncio.sleep(POLL_INTERVAL_S)
                    continue
                if state.trigger == TRIGGER_FIRE:
                    await self._handle_capture(state.mode)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(f"[Legacy] loop exception: {e}")
                await asyncio.sleep(1.0)
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _handle_capture(self, mode: int) -> None:
        # Acknowledge: set trigger -> 0 so PLC sees we accepted the command.
        await asyncio.to_thread(self.plc.write_trigger, TRIGGER_IDLE)

        if mode == MODE_FRONTBACK:
            await self._do_frontback()
        elif mode == MODE_HEIGHT:
            await self._do_height()
        else:
            self.logger.warning(f"[Legacy] unknown mode D2={mode}, skipping")

        # Mark done.
        await asyncio.to_thread(self.plc.write_trigger, TRIGGER_DONE)

    # ------------------------------------------------------------ frontback

    async def _do_frontback(self) -> None:
        settings = await asyncio.to_thread(self.plc.read_frontback_settings)
        if settings is None:
            self.logger.error("[Legacy] frontback: failed to read settings")
            return

        # Apply exposures only when changed (camera ASICs snap values to
        # legal steps; setting the same value re-flushes the buffer for
        # nothing — wastes ~50 ms in the original).
        await asyncio.gather(
            self._apply_exposure_if_changed(1, settings.cam1_exposure),
            self._apply_exposure_if_changed(2, settings.cam2_exposure),
        )

        # Capture both cameras in parallel.
        img1, img2 = await asyncio.gather(
            asyncio.to_thread(self.cam.capture_image, 1),
            asyncio.to_thread(self.cam.capture_image, 2),
        )

        # Report camera status to PLC (parallel writes).
        await asyncio.gather(
            asyncio.to_thread(self.plc.write_camera_status, 1, img1 is not None),
            asyncio.to_thread(self.plc.write_camera_status, 2, img2 is not None),
        )

        if img1 is None or img2 is None:
            self.logger.error(
                f"[Legacy] frontback skipped: cam1={'ok' if img1 is not None else 'offline'}, "
                f"cam2={'ok' if img2 is not None else 'offline'}"
            )
            return

        roi1 = self.get_roi(1)
        roi2 = self.get_roi(2)

        result = await asyncio.to_thread(compute_frontback, img1, img2, roi1, roi2)
        d0_value = RESULT_FRONT_OR_OK if result.is_front else RESULT_BACK_OR_NG

        self.logger.info(
            f"[Legacy] frontback: edge1={result.edge1_count} edge2={result.edge2_count} "
            f"-> D0={d0_value} ({'FRONT' if result.is_front else 'BACK'})"
        )

        # Write D0 + D20-D23 to the PLC in parallel with rendering the
        # operator-screen image. The display write is local-disk I/O so
        # it doesn't block the PLC ack — keeps the cycle time tight.
        await asyncio.gather(
            asyncio.to_thread(self.plc.write_recognition_result, d0_value),
            asyncio.to_thread(self.plc.write_edge_counts, result.edge1_count, result.edge2_count),
            asyncio.to_thread(self._render_frontback_display, img1, img2, result.is_front),
        )

    # -------------------------------------------------------------- height

    async def _do_height(self) -> None:
        settings = await asyncio.to_thread(self.plc.read_height_settings)
        if settings is None:
            self.logger.error("[Legacy] height: failed to read settings")
            return

        await self._apply_exposure_if_changed(2, settings.cam2_exposure)
        img = await asyncio.to_thread(self.cam.capture_image, 2)
        await asyncio.to_thread(self.plc.write_camera_status, 2, img is not None)

        if img is None:
            self.logger.error("[Legacy] height skipped: cam2 offline")
            return

        result = await asyncio.to_thread(
            compute_height,
            img,
            settings.brightness_threshold,
            settings.min_height,
            settings.height_comparison,
        )

        # Map the algorithm's state back to the PLC code (already aligned).
        d0_value = result.state
        if d0_value not in (RESULT_FRONT_OR_OK, RESULT_BACK_OR_NG, RESULT_EMPTY):
            self.logger.warning(f"[Legacy] height: unexpected state {d0_value}, forcing EMPTY")
            d0_value = RESULT_EMPTY

        self.logger.info(
            f"[Legacy] height: state={result.state} max_y_avg={result.max_y_avg} -> D0={d0_value}"
        )

        await asyncio.gather(
            asyncio.to_thread(self.plc.write_recognition_result, d0_value),
            asyncio.to_thread(self.plc.write_height_result, result.max_y_avg),
            asyncio.to_thread(self._render_height_display, img),
        )

    # -------------------------------------------------------------- helpers

    def _render_frontback_display(self, img1: np.ndarray, img2: np.ndarray, is_front: bool) -> None:
        """Wrapper that swallows render errors so display problems never
        block PLC writes — the production line keeps running even if the
        operator screen fails."""
        try:
            render_frontback(img1, img2, is_front, self.display_path)
        except Exception as e:
            self.logger.error(f"[Legacy] display render failed: {e}")

    def _render_height_display(self, image: np.ndarray) -> None:
        try:
            render_height(image, self.display_path)
        except Exception as e:
            self.logger.error(f"[Legacy] display render failed: {e}")

    async def _apply_exposure_if_changed(self, camera_num: int, exposure: int) -> None:
        if exposure <= 0:
            return  # 0 is "leave alone" sentinel — matches v2 path.
        cached = self._exposure_cache.get(camera_num)
        if cached == exposure:
            return
        ok = await asyncio.to_thread(self.cam.set_exposure, camera_num, exposure)
        if ok:
            self._exposure_cache[camera_num] = exposure


# Keep numpy reachable via this module for static-analysis hidden imports
# (PyInstaller misses numpy for type hints in some configurations).
_ = np
