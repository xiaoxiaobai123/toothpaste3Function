"""Application entry point.

Boot sequence:
    1. Init logging (rotating file with millisecond timestamps).
    2. Print version banner (git branch+commit if dev, else build-time string).
    3. Validate hardware-bound license; abort if invalid.
    4. Instantiate PLC, camera, and task managers.
    5. Run the asyncio event loop until interrupted.
"""

from __future__ import annotations

import asyncio
import sys

from core import license_utils, log_config, version
from core.config_manager import config
from core.task_manager import TaskManager


async def _main() -> None:
    logger = log_config.setup_logging()

    logger.info("[SYS] ======== program start ========")
    logger.info(f"[SYS] version: {version.get_version_info()}")
    logger.info(f"[SYS] python: {sys.version.split()[0]}  argv: {sys.argv}")
    logger.info(f"[SYS] workdir: {version.workdir()}")

    if not license_utils.validate_license():
        logger.error("Invalid license. Exiting.")
        sys.exit(1)

    # Imported here so test_display.py and unit tests can import this
    # module without requiring MVS SDK / pyModbusTCP.
    from camera.manager import CameraManager
    from plc.manager import PLCManager

    plc_manager = PLCManager(config.get_plc_ip())
    camera_manager = CameraManager()

    task_manager = TaskManager(plc_manager, camera_manager, config, logger)
    await task_manager.run()


if __name__ == "__main__":
    asyncio.run(_main())
