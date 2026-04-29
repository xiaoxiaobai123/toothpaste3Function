"""Hikvision MVS SDK path setup.

Adds the platform-specific MvImport directory to sys.path and configures
LD_LIBRARY_PATH on Linux. Must be called before importing
MvCameraControl_class.

Supported platforms:
    Windows  x86_64   (development hosts with MVS installed in
                       C:\\Program Files (x86)\\MVS\\)
    Linux    aarch64  (production target: NanoPi-R5S / RK3568, and the
                       GitHub-hosted ubuntu-24.04-arm CI runner)
"""

from __future__ import annotations

import os
import platform
import sys

from core import log_config

logger = log_config.setup_logging()


def setup_camera_environment() -> bool:
    os_type = platform.system()
    arch_type = platform.machine()

    if os_type == "Windows":
        lib_path = r"C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport"
        sys.path.append(lib_path)
    elif os_type == "Linux" and arch_type == "aarch64":
        lib_path = "/opt/MVS/Samples/aarch64/Python/MvImport"
        sys.path.append(lib_path)
        os.environ["LD_LIBRARY_PATH"] = lib_path + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")
    else:
        logger.error(f"Unsupported platform: {os_type} on {arch_type} (expected Windows or Linux aarch64)")
        return False

    logger.info(f"Camera environment set up for {os_type} on {arch_type}")
    logger.info(f"Python path: {sys.path}")
    return True
