"""Modbus TCP layer for the legacy fronback protocol.

Register map matches the original toothpastefronback program byte-for-byte
on every value that affects PLC-observable behaviour. See
`docs/PLC寄存器手册.md` (Legacy section) for the address-by-address spec.

PLC writes (we read):
    D1   capture trigger   (10 = capture, anything else = idle)
    D2   workcamera_count  (1 = dual-cam frontback, 0 = single-cam height)
    D10  cam1 exposure µs              (frontback mode)
    D11  cam2 exposure µs              (frontback mode)
    D30  cam2 exposure µs              (height mode)
    D31  height brightness threshold   (0-255)
    D32  height min-Y filter
    D33  height detect left limit      (read but unused — kept for compat)
    D34  height detect right limit     (read but unused — kept for compat)
    D35  height comparison threshold
    D36  height width comparison       (read but unused — kept for compat)

We write (PLC reads):
    D0   recognition result (1=front/OK, 2=back/NG, 3=empty)
    D1   capture trigger ack (0=processing, 1=done)
    D3   cam1 online status (0/1)
    D4   cam2 online status (0/1)
    D20-D23  edge1/edge2 count, each split into low+high uint16 words
    D40  height result (top-10 max-Y average)
    D41  height width result (kept at 0 — placeholder)

Notes:
    * D12/D13 (`unrecognized_threshold`) are *not* read by this adapter.
      The original program read them then never used the value (verified
      by exhaustive grep — see commit message of this PR). Skipping them
      saves 2 Modbus round-trips per poll cycle.
    * Block reads/writes are used wherever consecutive registers allow
      (D1+D2 in one read, D30..D36 in one read, D20..D23 in one write),
      mirroring the optimisation done in the v2 path's plc/manager.py.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from core import log_config
from plc.base import PLCBase

logger = log_config.setup_logging()

# --------------------------------------------------------------------------- #
# Register addresses — frozen to match the original program.
# --------------------------------------------------------------------------- #
REG_RECOGNITION_RESULT = 0
REG_CAPTURE_TRIGGER = 1
REG_WORKCAMERA_COUNT = 2
REG_CAM1_STATUS = 3
REG_CAM2_STATUS = 4
REG_CAM1_EXPOSURE = 10
REG_CAM2_EXPOSURE = 11
REG_EDGE1_LOW = 20
REG_EDGE1_HIGH = 21
REG_EDGE2_LOW = 22
REG_EDGE2_HIGH = 23
REG_HEIGHT_CAM2_EXPOSURE = 30
REG_HEIGHT_BRIGHTNESS = 31
REG_HEIGHT_MIN_Y = 32
REG_HEIGHT_LEFT_LIMIT = 33  # read but unused, kept for compat
REG_HEIGHT_RIGHT_LIMIT = 34  # read but unused, kept for compat
REG_HEIGHT_COMPARISON = 35
REG_HEIGHT_WIDTH_COMP = 36  # read but unused, kept for compat
REG_HEIGHT_RESULT = 40
REG_HEIGHT_WIDTH_RESULT = 41

# Sentinel values from the original program.
TRIGGER_FIRE = 10
TRIGGER_IDLE = 0
TRIGGER_DONE = 1

MODE_HEIGHT = 0  # single-camera height detection
MODE_FRONTBACK = 1  # dual-camera front/back detection

RESULT_FRONT_OR_OK = 1
RESULT_BACK_OR_NG = 2
RESULT_EMPTY = 3


# --------------------------------------------------------------------------- #
# Decoded read groups (typed, immutable).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TriggerState:
    """Snapshot of D1 (trigger) + D2 (mode) — read together atomically."""

    trigger: int
    mode: int


@dataclass(frozen=True)
class FrontbackSettings:
    """Decoded D10 + D11 — frontback mode parameters."""

    cam1_exposure: int
    cam2_exposure: int


@dataclass(frozen=True)
class HeightSettings:
    """Decoded D30..D36 — height mode parameters.

    `left_limit`, `right_limit`, and `width_comparison` are read for
    protocol fidelity but the algorithm currently ignores them (matches
    the original program's behaviour).
    """

    cam2_exposure: int
    brightness_threshold: int
    min_height: int
    left_limit: int
    right_limit: int
    height_comparison: int
    width_comparison: int


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class LegacyFronbackPLC:
    """Modbus TCP wrapper for the legacy fronback register layout.

    Construct either with an IP (creates its own PLCBase) or with a
    pre-built `plc_base` (used by tests with mock backends).
    """

    def __init__(
        self,
        ip: str | None = None,
        port: int = 502,
        plc_base: PLCBase | None = None,
    ) -> None:
        if plc_base is None:
            if ip is None:
                raise ValueError("LegacyFronbackPLC needs an ip or a plc_base")
            plc_base = PLCBase(ip, port)
        self.plc = plc_base
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ Reads

    def read_trigger_and_mode(self) -> TriggerState | None:
        """Block-read D1+D2 in one Modbus request. None on transient failure."""
        with self._lock:
            words = self.plc.read_status(REG_CAPTURE_TRIGGER, count=2)
        if not isinstance(words, list) or len(words) < 2:
            return None
        return TriggerState(trigger=words[0], mode=words[1])

    def read_frontback_settings(self) -> FrontbackSettings | None:
        """Block-read D10+D11."""
        with self._lock:
            words = self.plc.read_status(REG_CAM1_EXPOSURE, count=2)
        if not isinstance(words, list) or len(words) < 2:
            return None
        return FrontbackSettings(cam1_exposure=words[0], cam2_exposure=words[1])

    def read_height_settings(self) -> HeightSettings | None:
        """Block-read D30..D36 (7 registers in one request)."""
        with self._lock:
            words = self.plc.read_status(REG_HEIGHT_CAM2_EXPOSURE, count=7)
        if not isinstance(words, list) or len(words) < 7:
            return None
        return HeightSettings(
            cam2_exposure=words[0],
            brightness_threshold=words[1],
            min_height=words[2],
            left_limit=words[3],
            right_limit=words[4],
            height_comparison=words[5],
            width_comparison=words[6],
        )

    # ----------------------------------------------------------------- Writes

    def write_trigger(self, value: int) -> None:
        with self._lock:
            self.plc.write_status(REG_CAPTURE_TRIGGER, value)

    def write_recognition_result(self, value: int) -> None:
        with self._lock:
            self.plc.write_status(REG_RECOGNITION_RESULT, value)

    def write_camera_status(self, camera_num: int, online: bool) -> None:
        if camera_num not in (1, 2):
            logger.warning(f"[Legacy] write_camera_status: unsupported cam {camera_num}")
            return
        reg = REG_CAM1_STATUS if camera_num == 1 else REG_CAM2_STATUS
        with self._lock:
            self.plc.write_status(reg, 1 if online else 0)

    def write_edge_counts(self, edge1: int, edge2: int) -> None:
        """Block-write D20-D23 (2x uint32, low word first per original)."""
        words = [
            edge1 & 0xFFFF,
            (edge1 >> 16) & 0xFFFF,
            edge2 & 0xFFFF,
            (edge2 >> 16) & 0xFFFF,
        ]
        with self._lock:
            self.plc.write_multiple_registers(REG_EDGE1_LOW, words)

    def write_height_result(self, max_y_avg: int) -> None:
        """Write D40. D41 (width result) is left at whatever PLC last wrote;
        the original program also did not write it in the height path.
        """
        # Clamp to uint16 range — max_y can technically be larger than 65535
        # if the camera image is huge, but in practice it isn't.
        clamped = max(0, min(65535, int(max_y_avg)))
        with self._lock:
            self.plc.write_status(REG_HEIGHT_RESULT, clamped)

    def close(self) -> None:
        self.plc.close()
