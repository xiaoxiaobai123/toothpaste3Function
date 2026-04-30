"""Application entry point.

Boot sequence:
    1. Init logging (rotating file with millisecond timestamps).
    2. Print version banner (git branch+commit if dev, else build-time string).
    3. Validate hardware-bound license; abort if invalid.
    4. Instantiate camera manager.
    5. Pick the orchestrator based on config.json `plc_protocol`:
         - "legacy_fronback" -> LegacyFronbackOrchestrator (drop-in for the
           original toothpastefronback program, byte-compatible PLC layout)
         - "v2_unified"      -> TaskManager (new abstract Processor +
           18-word config block protocol; default for new sites)
    6. Run the asyncio event loop until interrupted.
"""

from __future__ import annotations

import asyncio
import sys

from core import license_utils, log_config, version
from core.config_manager import config


async def _main() -> None:
    logger = log_config.setup_logging()

    logger.info("[SYS] ======== program start ========")
    logger.info(f"[SYS] version: {version.get_version_info()}")
    logger.info(f"[SYS] python: {sys.version.split()[0]}  argv: {sys.argv}")
    logger.info(f"[SYS] workdir: {version.workdir()}")

    if not license_utils.validate_license():
        logger.error("Invalid license. Exiting.")
        sys.exit(1)

    # Imported here so unit tests can import this module without requiring
    # MVS SDK / pyModbusTCP.
    from camera.manager import CameraManager

    camera_manager = CameraManager()

    plc_protocol = config.get_plc_protocol()
    logger.info(f"[SYS] plc_protocol: {plc_protocol}")

    if plc_protocol == "legacy_fronback":
        from legacy.fronback_orchestrator import (
            LegacyFronbackOrchestrator,
            make_file_roi_provider,
        )
        from legacy.fronback_protocol import LegacyFronbackPLC

        plc = LegacyFronbackPLC(config.get_plc_ip())
        roi_provider = make_file_roi_provider(camera_manager, base_dir=".", logger=logger)
        # png_path=None: production deployments display via image_updater
        # (which reads the rgb565 sink). PNG is a leftover sink for sites
        # using feh/fbi instead — none of the live customers do, so writing
        # it every cycle just burns ~10-30 ms encoding to a file no one
        # reads. Tests pass an explicit png_path when they need it.
        orchestrator = LegacyFronbackOrchestrator(
            plc, camera_manager, roi_provider, logger, png_path=None
        )
        await orchestrator.run()
    else:
        from core.task_manager import TaskManager
        from plc.manager import PLCManager

        plc_manager = PLCManager(config.get_plc_ip())
        task_manager = TaskManager(plc_manager, camera_manager, config, logger)
        await task_manager.run()


if __name__ == "__main__":
    asyncio.run(_main())
