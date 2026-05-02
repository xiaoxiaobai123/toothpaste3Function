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
import contextlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from core.log_throttle import LogThrottle
from legacy.fronback_algorithms import (
    compute_frontback_parallel,
    compute_height,
)
from legacy.fronback_brush_head import run_brush_head
from legacy.fronback_display import (
    DEFAULT_PNG_PATH,
    DEFAULT_RGB565_PATH,
    render_frontback,
    render_height,
)
from legacy.fronback_protocol import (
    MODE_BRUSH_HEAD,
    MODE_FRONTBACK,
    MODE_HEIGHT,
    RESULT_BACK_OR_NG,
    RESULT_EMPTY,
    RESULT_FRONT_OR_OK,
    TRIGGER_DONE,
    TRIGGER_FIRE,
    TRIGGER_IDLE,
    TRIGGER_LOOP,
    BrushHeadSettings,
    FrontbackSettings,
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
        png_path: str | Path | None = DEFAULT_PNG_PATH,
        rgb565_path: str | Path | None = DEFAULT_RGB565_PATH,
    ) -> None:
        self.plc = plc
        self.cam = camera_manager
        self.get_roi = roi_provider
        self.logger = logger
        # Throttled wrapper for the 50 ms poll loop — see core/log_throttle.py.
        # The orchestrator's main `while True` would otherwise log the same
        # PLC/camera fault 20×/s when something's broken upstream.
        self.throttled = LogThrottle(logger)
        # Either path may be None to disable that sink (e.g., test harnesses
        # that don't want to touch /tmp or /home/pi).
        self.png_path = str(png_path) if png_path is not None else None
        self.rgb565_path = str(rgb565_path) if rgb565_path is not None else None

        self._exposure_cache: dict[int, int] = {}

    # ---------------------------------------------------------------- run loop

    async def run(self) -> None:
        active = self.cam.active_camera_nums()
        if not active:
            self.logger.error("[Legacy] no cameras initialized; orchestrator not starting")
            return

        self.logger.info(f"[Legacy] orchestrator started (active cameras: {active})")

        # Spawn the system-heartbeat task — toggles D45 once per second so
        # PLC's watchdog can detect a hung / crashed vision binary even
        # while no FIRE / LOOP is happening. Cancelled below if `run()` is
        # itself cancelled.
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            while True:
                try:
                    state = await asyncio.to_thread(self.plc.read_trigger_and_mode)
                    if state is None:
                        await asyncio.sleep(POLL_INTERVAL_S)
                        continue
                    if state.trigger == TRIGGER_FIRE:
                        await self._handle_capture(state.mode)
                    elif state.trigger == TRIGGER_LOOP:
                        await self._do_loop()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # Throttled — same exception per-iteration would flood the log.
                    self.throttled.error(f"[Legacy] loop exception: {e}")
                    await asyncio.sleep(1.0)
                await asyncio.sleep(POLL_INTERVAL_S)
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _heartbeat_loop(self) -> None:
        """Toggle D45 once per second so PLC's watchdog can verify the
        vision binary is alive (Modbus reachable AND program running).
        Failures are throttled — a transient PLC blip shouldn't flood
        the log with one warning per second.
        """
        toggle = 0
        while True:
            try:
                await asyncio.to_thread(self.plc.write_system_heartbeat, toggle)
                toggle ^= 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.throttled.warning(f"[Legacy] heartbeat write failed: {e}")
            await asyncio.sleep(1.0)

    async def _handle_capture(self, mode: int) -> None:
        # Acknowledge: set trigger -> 0 so PLC sees we accepted the command.
        await asyncio.to_thread(self.plc.write_trigger, TRIGGER_IDLE)

        if mode == MODE_FRONTBACK:
            await self._do_frontback()
        elif mode == MODE_HEIGHT:
            await self._do_height()
        elif mode == MODE_BRUSH_HEAD:
            await self._do_brush_head()
        else:
            self.logger.warning(f"[Legacy] unknown mode D2={mode}, skipping")

        # Mark done.
        await asyncio.to_thread(self.plc.write_trigger, TRIGGER_DONE)

    async def _do_loop(self) -> None:
        """Continuous-capture loop, runs until PLC writes D1 != TRIGGER_LOOP.

        Mirrors the START_LOOP behaviour from the head/display source repos
        and v2's TaskManager.process_continuous_capture: each iteration
        re-reads D1+D2, dispatches by mode, then immediately starts the
        next iteration. Operator can flip D2 mid-loop to switch between
        frontback and height without stopping.

        Unlike _handle_capture, this does NOT touch D1 itself. The PLC
        owns D1 throughout the loop — it sets 11 to start, anything else
        (typically 0 IDLE) to stop. If we wrote IDLE/DONE inside the loop
        we'd either flip ourselves out prematurely or signal "single
        capture complete" semantics that don't apply here.

        Result registers (D0, D20-D23, D40) are still written every cycle
        by _do_frontback / _do_height — same as in single-fire mode, just
        much more frequently. Customer's PLC ladder must be ready for that
        write rate (typically <= 1 Hz given capture+algorithm cycle time).
        """
        self.logger.info("[Legacy] LOOP started")
        while True:
            try:
                # One Modbus round-trip pulls D1-D11 (trigger, mode, frontback
                # exposures) in a single block read — saves ~10-15 ms per
                # iteration vs separate read_trigger_and_mode + read_frontback_settings.
                block = await asyncio.to_thread(self.plc.read_loop_block)
                if block is None:
                    await asyncio.sleep(POLL_INTERVAL_S)
                    continue
                if block.trigger != TRIGGER_LOOP:
                    self.logger.info(f"[Legacy] LOOP stopping (D1={block.trigger})")
                    return
                # Re-dispatch on each iteration so operator can flip D2 mid-loop.
                if block.mode == MODE_FRONTBACK:
                    # Hand the already-read exposures to _do_frontback so it
                    # doesn't redundantly re-read D10+D11.
                    preread = FrontbackSettings(
                        cam1_exposure=block.cam1_exposure,
                        cam2_exposure=block.cam2_exposure,
                    )
                    await self._do_frontback(preread_settings=preread)
                elif block.mode == MODE_HEIGHT:
                    await self._do_height()
                elif block.mode == MODE_BRUSH_HEAD:
                    # Brush params live in D50-D63, far from the loop block;
                    # _do_brush_head reads them itself. Adds ~1 round-trip
                    # per brush_head cycle but keeps the loop block tight
                    # for frontback/height (the common cases).
                    await self._do_brush_head()
                else:
                    self.logger.warning(f"[Legacy] LOOP: unknown mode D2={block.mode}, skipping cycle")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.throttled.error(f"[Legacy] LOOP iteration exception: {e}")
                await asyncio.sleep(1.0)
            # No trailing sleep here — the per-iteration work
            # (capture + algorithm + PLC writes + display) takes 200-400 ms,
            # which is already plenty of "yield" for the asyncio loop. The
            # 50 ms POLL_INTERVAL_S sleep that exists in run() and in the
            # `state is None` branch above is the right rate-limit for
            # PLC polling when the orchestrator is idle; inside an active
            # loop it just adds 12-15% dead time per cycle. (v0.3.9.)

    # ------------------------------------------------------------ frontback

    async def _do_frontback(self, preread_settings: FrontbackSettings | None = None) -> None:
        # LOOP path passes settings already extracted from the bundled
        # D1-D11 read so we skip a redundant Modbus round-trip. FIRE
        # (single-shot) path leaves it None and we read here.
        if preread_settings is not None:
            settings = preread_settings
        else:
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

        # Report camera status to PLC — single block-write of D3+D4 instead
        # of two separate single-register writes (one round-trip vs two).
        await asyncio.to_thread(self.plc.write_camera_statuses, img1 is not None, img2 is not None)

        # Compute ROIs upfront so both the algorithm AND the display
        # (including the OFFLINE-camera path) can see them.
        roi1 = self.get_roi(1)
        roi2 = self.get_roi(2)

        if img1 is None or img2 is None:
            self.logger.error(
                f"[Legacy] frontback skipped: cam1={'ok' if img1 is not None else 'offline'}, "
                f"cam2={'ok' if img2 is not None else 'offline'}"
            )
            # Render the display anyway so the operator sees which camera
            # dropped (with a CAM N OFFLINE placeholder) rather than a
            # frozen old frame. Algorithm did not run, so D0/D20-D23 are
            # left untouched — `is_front` is a don't-care for the placeholder
            # path (compose_frontback ignores it when one image is None).
            # Pass ROIs so the present camera still shows its ROI overlay.
            await asyncio.to_thread(self._render_frontback_display, img1, img2, False, roi1, roi2)
            return

        # Run the two per-camera Sobel computations concurrently in worker
        # threads — cv2 releases the GIL so the NanoPi's quad-core actually
        # parallelises. Cuts ~50% off the algorithm portion of the cycle.
        result = await compute_frontback_parallel(img1, img2, roi1, roi2)
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
            asyncio.to_thread(self._render_frontback_display, img1, img2, result.is_front, roi1, roi2),
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
            settings.left_limit,
            settings.right_limit,
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
            asyncio.to_thread(
                self._render_height_display,
                img,
                left_limit=settings.left_limit,
                right_limit=settings.right_limit,
                comparison=settings.height_comparison,
                top_columns=result.top_columns,
                state=d0_value,
                max_y_avg=result.max_y_avg,
                brightness_threshold=settings.brightness_threshold,
                min_height=settings.min_height,
            ),
        )

    # ----------------------------------------------------------- brush_head

    async def _do_brush_head(self, preread_settings: BrushHeadSettings | None = None) -> None:
        """Single-camera (cam1) brush-head detection using the v2 algorithm.

        Mirrors the _do_height shape: read settings, apply exposure, capture,
        run algorithm via the brush_head adapter, write D0 + D42/D43 + render.
        Defaults for any PLC slot left at 0 come from
        config.json:legacy_brush_head_defaults — see core/config_manager.py.
        """
        if preread_settings is not None:
            settings = preread_settings
        else:
            settings = await asyncio.to_thread(self.plc.read_brush_head_settings)
            if settings is None:
                self.logger.error("[Legacy] brush_head: failed to read settings")
                return

        await self._apply_exposure_if_changed(1, settings.cam1_exposure)
        img = await asyncio.to_thread(self.cam.capture_image, 1)
        await asyncio.to_thread(self.plc.write_camera_status, 1, img is not None)

        if img is None:
            self.logger.error("[Legacy] brush_head skipped: cam1 offline")
            return

        defaults = self._brush_head_defaults()
        cycle = await asyncio.to_thread(run_brush_head, img, settings, defaults)

        self.logger.info(
            f"[Legacy] brush_head: D0={cycle.plc_result} side={cycle.side_code}"
        )

        await asyncio.gather(
            asyncio.to_thread(self.plc.write_recognition_result, cycle.plc_result),
            asyncio.to_thread(self.plc.write_brush_side_code, cycle.side_code),
            asyncio.to_thread(self._render_brush_head_display, cycle.display_image),
        )

    def _brush_head_defaults(self) -> dict:
        """Pull legacy brush-head defaults from the config singleton.

        Wrapped in a method so tests can monkeypatch this on the
        orchestrator instance instead of the singleton (avoids leaking
        test-only config into other tests via shared module state).
        """
        # Local import: orchestrator is imported at module load time on
        # NanoPi, before config.json existence checks pass. Keep config
        # access lazy to mirror how the rest of the codebase handles it.
        from core.config_manager import config

        return config.get_legacy_brush_head_defaults()

    # -------------------------------------------------------------- helpers

    def _render_frontback_display(
        self,
        img1: np.ndarray | None,
        img2: np.ndarray | None,
        is_front: bool,
        roi1: dict[str, int] | None = None,
        roi2: dict[str, int] | None = None,
    ) -> None:
        """Wrapper that swallows render errors so display problems never
        block PLC writes — the production line keeps running even if the
        operator screen fails. Either image may be None when a camera is
        offline; `render_frontback` handles that with a placeholder.

        Optional `roi1` / `roi2` overlay yellow rectangles on each camera
        panel showing the algorithm's region of interest (v0.3.9+).
        """
        try:
            render_frontback(
                img1,
                img2,
                is_front,
                self.png_path,
                self.rgb565_path,
                roi1=roi1,
                roi2=roi2,
            )
        except Exception as e:
            self.logger.error(f"[Legacy] display render failed: {e}")

    def _render_height_display(
        self,
        image: np.ndarray,
        *,
        left_limit: int = 0,
        right_limit: int = 0,
        comparison: int = 0,
        top_columns: tuple = (),
        state: int = 0,
        max_y_avg: int = 0,
        brightness_threshold: int = 0,
        min_height: int = 0,
    ) -> None:
        """Forward overlays + algorithm result through to render_height.
        Defaults preserve raw-frame behaviour for callers that don't
        pass them (existing tests, manual REPL invocations, brush_head
        which goes through this same render path but doesn't have a
        height-mode `state` value)."""
        try:
            render_height(
                image,
                self.png_path,
                self.rgb565_path,
                left_limit=left_limit,
                right_limit=right_limit,
                comparison=comparison,
                top_columns=top_columns,
                state=state,
                max_y_avg=max_y_avg,
                brightness_threshold=brightness_threshold,
                min_height=min_height,
            )
        except Exception as e:
            self.logger.error(f"[Legacy] display render failed: {e}")

    def _render_brush_head_display(self, image: np.ndarray) -> None:
        """BrushHeadProcessor returns its own visualization image (with ROI
        rectangle, dot overlays, side label) — we just need to fit it to
        the framebuffer and ship it through the same single-camera render
        path height uses.
        """
        try:
            render_height(image, self.png_path, self.rgb565_path)
        except Exception as e:
            self.logger.error(f"[Legacy] brush_head display render failed: {e}")

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
