"""Singleton configuration loader, reading config.json at startup.

Camera entries are keyed `camera1`, `camera2`, ... and may set:
    enabled    bool, default True (omit field to enable)
    ip         camera device IP
    host_lan   host NIC IP on the same subnet as the camera
    roi        optional hardware-ROI dict {width, height, offset_x, offset_y}
               width / height usually must be multiples of 4 (Hikvision GigE).

The PLC entry holds {ip}.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ConfigManager:
    """Singleton with lazy load: config.json is read on first attribute access.

    Importing this module never touches the filesystem, so unit tests and
    `python -c 'import core'` work without a config file present. Production
    callers either call `config.load_config()` explicitly at startup or rely
    on the `_ensure_loaded()` guard in each accessor.
    """

    _instance: ConfigManager | None = None
    _config: dict[str, Any] | None = None
    _config_path: str = "config.json"

    def __new__(cls) -> ConfigManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load_config(self, config_file: str | None = None) -> None:
        """Load config.json. Errors if the file is missing or malformed."""
        path = Path(config_file or self._config_path)
        if not path.is_file():
            raise FileNotFoundError(f"Config file not found: {path.resolve()}")
        with path.open("r", encoding="utf-8") as f:
            self._config = json.load(f)
        self._config_path = str(path)

    def _ensure_loaded(self) -> None:
        if self._config is None:
            self.load_config()

    @property
    def config(self) -> dict[str, Any]:
        self._ensure_loaded()
        assert self._config is not None
        return self._config

    # ------------------------------------------------------------------
    # Camera helpers
    # ------------------------------------------------------------------
    def _camera_entry(self, camera_num: int) -> dict[str, Any]:
        return self.config["cameras"][f"camera{camera_num}"]

    def get_camera_ip(self, camera_num: int) -> str:
        return self._camera_entry(camera_num)["ip"]

    def get_camera_host_lan(self, camera_num: int) -> str:
        return self._camera_entry(camera_num)["host_lan"]

    def is_camera_enabled(self, camera_num: int) -> bool:
        """Default True (omitting the `enabled` field means enabled)."""
        try:
            return bool(self._camera_entry(camera_num).get("enabled", True))
        except KeyError:
            return False

    def get_camera_roi(self, camera_num: int) -> dict[str, int] | None:
        """Optional hardware ROI. None means use full frame."""
        roi = self._camera_entry(camera_num).get("roi")
        if roi is None:
            return None
        if "width" not in roi or "height" not in roi:
            raise ValueError(f"camera{camera_num}.roi must include width and height")
        return {
            "width": int(roi["width"]),
            "height": int(roi["height"]),
            "offset_x": int(roi.get("offset_x", 0)),
            "offset_y": int(roi.get("offset_y", 0)),
        }

    def configured_camera_nums(self) -> list[int]:
        """Return sorted camera numbers explicitly listed in config (1-based)."""
        nums: list[int] = []
        for key in self.config.get("cameras", {}):
            if key.startswith("camera"):
                try:
                    nums.append(int(key.removeprefix("camera")))
                except ValueError:
                    continue
        return sorted(nums)

    # ------------------------------------------------------------------
    # PLC
    # ------------------------------------------------------------------
    def get_plc_ip(self) -> str:
        return self.config["plc"]["ip"]


# Module-level singleton, mirroring the original display layout.
config = ConfigManager()
