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

    def get_plc_protocol(self) -> str:
        """Return the PLC protocol selector.

        Values:
          "v2_unified"      — default. Per-camera ProductType in 18-word
                              config block (D10..D27 / D30..D47), result
                              block at D70.. — used by new sites.
          "legacy_fronback" — drop-in for the original toothpastefronback
                              program: D1 trigger, D2 mode, D0 result,
                              D20-D23 edge counts, D40 height result.
                              Routed through legacy/ subpackage.

        Unknown values fall back to "v2_unified" with a warning logged
        elsewhere — never raise here, since config_manager is touched
        during every test.
        """
        value = self.config.get("plc_protocol", "v2_unified")
        return value if value in {"v2_unified", "legacy_fronback"} else "v2_unified"

    # ------------------------------------------------------------------
    # Legacy brush-head defaults (D2=2 mode parameters that PLC didn't write)
    # ------------------------------------------------------------------
    def get_legacy_brush_head_defaults(self) -> dict[str, Any]:
        """Defaults applied when PLC writes 0 to a brush-head parameter.

        Lets `legacy_fronback` brush_head mode (D2=2) work without the
        customer adding every parameter to their PLC ladder right away
        — they just write D2=2 to trigger and we fill in any 0-valued
        slot from config.json (or hardcoded sensible values from
        BrushHeadProcessor.DEFAULTS). Each non-zero PLC word overrides
        the matching default per cycle (see
        `legacy/fronback_brush_head._merge_with_defaults`).

        Field set mirrors BrushHeadProcessor.DEFAULTS plus an `exposure`
        entry for the camera. `adapt_C` is included even though no PLC
        register exposes it — the customer can still tune it via this
        config when the auto-detected adaptive threshold drifts.

        Numeric coercion is forgiving so a stray string like "1.5" in
        the config file still works.
        """
        cfg = self.config.get("legacy_brush_head_defaults", {})
        return {
            "exposure": int(cfg.get("exposure", 5000)),
            "shrink_pct": int(cfg.get("shrink_pct", 15)),
            "adapt_block": int(cfg.get("adapt_block", 31)),
            "adapt_C": int(cfg.get("adapt_C", 8)),
            "dot_area_min": int(cfg.get("dot_area_min", 20)),
            "dot_area_max": int(cfg.get("dot_area_max", 500)),
            "roi_area_min": int(cfg.get("roi_area_min", 50000)),
            "roi_area_max": int(cfg.get("roi_area_max", 500000)),
            "ratio_min": float(cfg.get("ratio_min", 1.5)),
            "ratio_max": float(cfg.get("ratio_max", 3.5)),
        }


# Module-level singleton, mirroring the original display layout.
config = ConfigManager()
