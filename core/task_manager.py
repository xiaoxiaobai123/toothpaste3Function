"""Async orchestration: PLC polling, capture, processing, display, write-back.

Per-camera asyncio task loop:
    1. Block-read PLC config (atomic snapshot of D1+D10..D27 / D2+D30..D47)
    2. Match trigger mode (hardware vs software) to PLC status
    3. Apply PLC-pushed exposure (with read-back tolerance)
    4. On START_TASK: single capture + process; on START_LOOP: continuous
    5. Dispatch image through processing.registry by ProductType
    6. Parallel: write result to PLC + combine images + save RGB565

Single-camera mode skips the combine step (~40 ms/frame saved). The
asyncio.gather() of write_result_to_plc and process_combined_results
saves another ~25 ms/frame on dual-camera.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from camera.manager import CameraManager
from plc.enums import (
    CameraResult,
    CameraStatus,
    CameraTriggerStatus,
    SystemStatus,
)
from plc.manager import PLCManager
from processing import dispatch
from processing.display_utils import (
    convert_to_rgb565,
    process_and_combine_images,
    save_rgb565_with_header,
)
from processing.result import Outcome, ProcessResult


class TaskManager:
    """Per-camera asyncio loop coordinating PLC, camera, processor."""

    OUTPUT_FILE = "output_image.rgb565"
    REPORT_INTERVAL = 2.0  # seconds between continuous-mode FPS reports

    HW_TRIGGER_SOURCE = 0
    SW_TRIGGER_SOURCE = 7

    def __init__(
        self,
        plc_manager: PLCManager,
        camera_manager: CameraManager,
        config: Any,
        logger: Any,
    ) -> None:
        self.plc_manager = plc_manager
        self.camera_manager = camera_manager
        self.config = config
        self.logger = logger

        active = camera_manager.active_camera_nums() if camera_manager else [1, 2]
        # Only allocate slots for cameras that actually came up.
        self.camera_results: dict[int, Outcome | None] = {n: None for n in active} or {1: None, 2: None}
        self.logger.info(f"[SYS] camera_results initialized: {list(self.camera_results.keys())}")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        active = self.camera_manager.active_camera_nums()
        if not active:
            self.logger.error("[SYS] No camera initialized; task manager not starting")
            return
        self.logger.info(f"[SYS] Task Manager started (active cameras: {active})")
        tasks = [asyncio.create_task(self.camera_task(n)) for n in active]
        await asyncio.gather(*tasks)

    async def camera_task(self, camera_num: int) -> None:
        while True:
            try:
                await self.process_camera(camera_num)
            except Exception as e:
                self.logger.error(f"[Cam{camera_num}] task loop exception: {e}")
            await asyncio.sleep(0.1)

    async def process_camera(self, camera_num: int) -> None:
        settings = await self.read_plc_settings(camera_num)
        if not settings:
            return  # transient PLC failure; retry next tick
        status: CameraStatus = settings["status"]

        current_trigger = self.camera_manager.get_trigger_source(camera_num)

        if status == CameraStatus.START_LOOP:
            # Loop mode is always software triggered.
            if current_trigger != self.SW_TRIGGER_SOURCE:
                await self.update_camera_trigger_mode(camera_num, is_hardware_trigger=False)
            is_hardware_trigger = False
        elif status == CameraStatus.START_TASK:
            is_hardware_trigger = settings.get("trigger_mode") == CameraTriggerStatus.HARDWARE_TRIGGER
            new_source = self.HW_TRIGGER_SOURCE if is_hardware_trigger else self.SW_TRIGGER_SOURCE
            if current_trigger != new_source:
                await self.update_camera_trigger_mode(camera_num, is_hardware_trigger)
        else:
            # IDLE: do not change trigger mode, but allow exposure pre-push.
            is_hardware_trigger = current_trigger == self.HW_TRIGGER_SOURCE

        await self._ensure_exposure(camera_num, settings, is_hardware_trigger)

        if status == CameraStatus.START_TASK:
            await self.process_single_capture(camera_num, settings, is_hardware_trigger)
        elif status == CameraStatus.START_LOOP:
            await self.process_continuous_capture(camera_num, settings, is_hardware_trigger)

    async def _ensure_exposure(
        self, camera_num: int, settings: dict[str, Any], is_hardware_trigger: bool
    ) -> None:
        new_exposure = settings.get("exposure_time")
        if not new_exposure:
            return

        current = self.camera_manager.get_exposure_time(camera_num)
        # Cameras snap requested values to legal steps; strict != would
        # cause repeated re-sets on every tick.
        if current is not None and abs(current - new_exposure) <= 1.0:
            return

        await self.set_camera_exposure(camera_num, new_exposure)
        # In software-trigger mode, drop one stale frame so the next
        # algorithm pass does not see a transitional exposure.
        if not is_hardware_trigger:
            await asyncio.to_thread(self.camera_manager.flush_one_frame, camera_num)

    async def read_plc_settings(self, camera_num: int) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self.plc_manager.read_camera_settings, camera_num)
        except Exception as e:
            self.logger.error(f"[Cam{camera_num}] PLC read exception: {e}")
            return {}

    # ------------------------------------------------------------------
    # Single capture
    # ------------------------------------------------------------------
    async def process_single_capture(
        self, camera_num: int, settings: dict[str, Any], is_hardware_trigger: bool
    ) -> None:
        self.logger.info(f"[Cam{camera_num}] capture start")
        await asyncio.to_thread(self.plc_manager.write_camera_status, camera_num, CameraStatus.IDLE)

        outcome = await self.capture_and_process_image(camera_num, settings, is_hardware_trigger)
        self.camera_results[camera_num] = outcome

        # Write to PLC and combine+save in parallel — independent IO paths.
        await asyncio.gather(
            self.write_result_to_plc(camera_num, outcome),
            self.process_combined_results(),
        )
        await asyncio.to_thread(self.plc_manager.write_camera_status, camera_num, CameraStatus.TASK_COMPLETED)
        self.logger.info(f"[Cam{camera_num}] capture done")

    # ------------------------------------------------------------------
    # Continuous capture
    # ------------------------------------------------------------------
    async def process_continuous_capture(
        self, camera_num: int, settings: dict[str, Any], is_hardware_trigger: bool
    ) -> None:
        self.logger.info(f"[Cam{camera_num}] continuous capture start")

        loop_start = time.time()
        frame_count = 0
        last_report_time = loop_start
        last_report_frames = 0

        while True:
            try:
                t0 = time.time()

                new_settings = await self.read_plc_settings(camera_num)
                if not new_settings:
                    await asyncio.sleep(0.1)
                    continue

                if new_settings["status"] != CameraStatus.START_LOOP:
                    self.logger.info(f"[Cam{camera_num}] continuous capture stopping")
                    await asyncio.to_thread(
                        self.plc_manager.write_camera_status, camera_num, CameraStatus.IDLE
                    )
                    break

                t_plc = time.time()
                await self._ensure_exposure(camera_num, new_settings, is_hardware_trigger)
                t_exp = time.time()
                outcome = await self.capture_and_process_image(camera_num, new_settings, is_hardware_trigger)
                t_cap = time.time()

                self.camera_results[camera_num] = outcome
                await asyncio.gather(
                    self.write_result_to_plc(camera_num, outcome),
                    self.process_combined_results(),
                )
                t_out = time.time()

                frame_count += 1
                total_ms = (t_out - t0) * 1000

                now = time.time()
                if now - last_report_time >= self.REPORT_INTERVAL:
                    fps = (frame_count - last_report_frames) / (now - last_report_time)
                    self.logger.info(
                        f"[Cam{camera_num}] FPS {fps:4.1f} frame#{frame_count:04d} | "
                        f"plc={(t_plc - t0) * 1000:3.0f}ms "
                        f"exp={(t_exp - t_plc) * 1000:3.0f}ms "
                        f"cap+algo={(t_cap - t_exp) * 1000:3.0f}ms "
                        f"write+combine={(t_out - t_cap) * 1000:3.0f}ms "
                        f"total={total_ms:3.0f}ms"
                    )
                    last_report_time = now
                    last_report_frames = frame_count

                await asyncio.sleep(0.01)

            except Exception as e:
                self.logger.error(f"[Cam{camera_num}] continuous capture exception: {e}")
                await asyncio.sleep(1)

        total_run = time.time() - loop_start
        avg_fps = frame_count / total_run if total_run > 0 else 0
        self.logger.info(
            f"[Cam{camera_num}] continuous capture ended | "
            f"{frame_count} frames / {total_run:.1f}s = avg {avg_fps:.1f} FPS"
        )

    # ------------------------------------------------------------------
    # Capture + dispatch
    # ------------------------------------------------------------------
    async def capture_and_process_image(
        self, camera_num: int, settings: dict[str, Any], is_hardware_trigger: bool
    ) -> Outcome | None:
        image = await asyncio.to_thread(self.camera_manager.capture_image, camera_num, is_hardware_trigger)
        if image is None:
            self.logger.error(f"[Cam{camera_num}] no image captured")
            return None

        product_type = settings["product_type"]
        processor = dispatch(product_type)
        if processor is None:
            self.logger.error(f"[Cam{camera_num}] no processor registered for {product_type}")
            return None

        self.logger.info(f"[Cam{camera_num}] algo: {processor.name}")
        return await asyncio.to_thread(processor.process, image, settings)

    # ------------------------------------------------------------------
    # Result write-back
    # ------------------------------------------------------------------
    async def write_result_to_plc(self, camera_num: int, outcome: Outcome | None) -> None:
        if outcome is None:
            self.logger.error(f"[Cam{camera_num}] no valid result to write")
            return

        camera_result = CameraResult(
            x=outcome.center[0],
            y=outcome.center[1],
            angle=outcome.angle,
            result=outcome.result == ProcessResult.OK,
            area=0,
            circularity=0.0,
        )

        try:
            await asyncio.to_thread(self.plc_manager.write_camera_result, camera_num, camera_result)
            self.logger.info(
                f"[Cam{camera_num}] result={outcome.result.name} "
                f"x={outcome.center[0]:.1f} y={outcome.center[1]:.1f} "
                f"angle={outcome.angle:.2f}"
            )
        except Exception as e:
            self.logger.error(f"[Cam{camera_num}] write result exception: {e}")

    async def update_camera_trigger_mode(self, camera_num: int, is_hardware_trigger: bool) -> None:
        try:
            await asyncio.to_thread(self.camera_manager.update_trigger_mode, camera_num, is_hardware_trigger)
        except Exception as e:
            self.logger.error(f"[Cam{camera_num}] trigger mode update exception: {e}")

    async def set_camera_exposure(self, camera_num: int, exposure_time: float) -> None:
        try:
            await asyncio.to_thread(self.camera_manager.set_exposure, camera_num, exposure_time)
        except Exception as e:
            self.logger.error(f"[Cam{camera_num}] set exposure exception: {e}")

    # ------------------------------------------------------------------
    # Display pipeline
    # ------------------------------------------------------------------
    async def process_combined_results(self) -> None:
        # Combine + RGB565 + save are CPU/IO bound; threading keeps the
        # asyncio event loop free for the other camera task to progress.
        await asyncio.to_thread(self._combine_and_save, self.camera_results)

    def _combine_and_save(self, results: dict[int, Outcome | None]) -> None:
        combined = process_and_combine_images(results)
        if combined is None:
            return
        rgb565 = convert_to_rgb565(combined)
        if rgb565 is None:
            return
        save_rgb565_with_header(rgb565, self.OUTPUT_FILE)

    async def update_system_status(self, status: SystemStatus) -> None:
        try:
            await asyncio.to_thread(self.plc_manager.write_system_status, status)
            self.logger.info(f"System status updated to {status.name}")
        except Exception as e:
            self.logger.error(f"Error updating system status: {e}")

    async def handle_error(self, error_code: int) -> None:
        try:
            await asyncio.to_thread(self.plc_manager.write_error_code, error_code)
            await self.update_system_status(SystemStatus.ERROR)
            self.logger.error(f"System error: {error_code}")
        except Exception as e:
            self.logger.error(f"Error handling system error: {e}")
