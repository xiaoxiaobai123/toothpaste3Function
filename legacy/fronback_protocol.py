"""Modbus TCP layer for the legacy fronback protocol.

Register map matches the original toothpastefronback program byte-for-byte
on every value that affects PLC-observable behaviour. See
`docs/PLC寄存器手册.md` (Legacy section) for the address-by-address spec.

PLC writes (we read):
    D1   capture trigger   (10 = capture, 11 = LOOP, anything else = idle)
    D2   workcamera_count  (1 = dual-cam frontback, 0 = single-cam height,
                            2 = single-cam brush_head — additive extension)
    D10  cam1 exposure µs              (frontback mode + brush_head mode)
    D11  cam2 exposure µs              (frontback mode)
    D12  brush_head dot_area_min       (0 = use config.json default)
    D13  brush_head dot_area_max       (0 = use default)
    D14  brush_head ratio_min × 10     (0 = use default; 15 = 1.5 ratio)
    D15  brush_head ratio_max × 10     (0 = use default; 35 = 3.5 ratio)
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
    D42  brush_head dot count (additive extension; 0 = unset)
    D43  brush_head detected area / 100 (additive extension)

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
# Brush-head parameter block (D2=2 mode). 0 in any slot = use config default.
REG_BRUSH_DOT_AREA_MIN = 12
REG_BRUSH_DOT_AREA_MAX = 13
REG_BRUSH_RATIO_MIN_X10 = 14
REG_BRUSH_RATIO_MAX_X10 = 15
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
# Brush-head output diagnostics (D2=2 mode). Optional — clients that don't
# read these still get OK/NG via D0.
REG_BRUSH_DOT_COUNT = 42
REG_BRUSH_AREA_X100 = 43

# Sentinel values.
#
# TRIGGER_FIRE / IDLE / DONE are byte-compat with the original
# toothpastefronback program (it only ever wrote 10 to fire and observed
# our 0/1 ack/done writes back). TRIGGER_LOOP was NOT in the original —
# it's an additive extension lifted from the head/display source repos
# (where it lives as CameraStatus.START_LOOP=11) so legacy customers
# whose PLC ladder has been updated for continuous capture can use the
# same dispatch path. Picking 11 specifically (not e.g. 12 or 99) keeps
# alignment with v2's CameraStatus.START_LOOP, so a future migration
# legacy → v2 doesn't need PLC value remaps.
TRIGGER_FIRE = 10
TRIGGER_IDLE = 0
TRIGGER_DONE = 1
TRIGGER_LOOP = 11

MODE_HEIGHT = 0  # single-camera height detection
MODE_FRONTBACK = 1  # dual-camera front/back detection
MODE_BRUSH_HEAD = 2  # single-camera brush-head detection (cam1, additive extension)

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
class LoopBlock:
    """Single-shot D1-D15 read used by the LOOP hot path.

    Bundles trigger + mode + frontback exposures + brush-head parameters
    so one Modbus round-trip serves all three modes (height needs nothing
    here, frontback needs D10/D11, brush_head needs D10 + D12-D15). D3-D9
    are read as part of the contiguous span and discarded — they're values
    we write (cam status / unused), so reading them echoes our own state.
    """

    trigger: int
    mode: int
    cam1_exposure: int
    cam2_exposure: int
    # Brush-head extension fields (D12-D15). 0 = "use config default" per slot.
    brush_dot_area_min: int
    brush_dot_area_max: int
    brush_ratio_min_x10: int
    brush_ratio_max_x10: int


@dataclass(frozen=True)
class BrushHeadSettings:
    """Brush-head detection parameters from PLC.

    Each field is a raw PLC word; 0 means "use the config.json default for
    this parameter" — the adapter in `legacy/fronback_brush_head.py` does
    the merge with config defaults before calling the v2 BrushHeadProcessor.

    Sharing cam1_exposure with frontback's D10 lets the LOOP block read
    cover both modes without a separate request.
    """

    cam1_exposure: int       # D10
    dot_area_min: int        # D12 (raw uint16; 0 = default)
    dot_area_max: int        # D13
    ratio_min_x10: int       # D14 (15 = ratio 1.5)
    ratio_max_x10: int       # D15


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

    def read_loop_block(self) -> LoopBlock | None:
        """Block-read D1-D15 in one Modbus request: trigger, mode, frontback
        exposures, and brush-head parameters bundled together. Saves
        round-trips vs per-mode `read_*_settings` calls inside the LOOP,
        where the per-iteration savings compound. Extending the read from
        11 to 15 words costs ~1 ms for the wire payload but saves a full
        round-trip whenever brush-head mode is dispatched.

        D3-D9 are read as part of the contiguous span (Modbus reads must
        be contiguous) but discarded — they're our own writes echoed back.
        """
        with self._lock:
            words = self.plc.read_status(REG_CAPTURE_TRIGGER, count=15)
        if not isinstance(words, list) or len(words) < 15:
            return None
        return LoopBlock(
            trigger=words[0],                 # D1
            mode=words[1],                    # D2
            cam1_exposure=words[9],           # D10
            cam2_exposure=words[10],          # D11
            brush_dot_area_min=words[11],     # D12
            brush_dot_area_max=words[12],     # D13
            brush_ratio_min_x10=words[13],    # D14
            brush_ratio_max_x10=words[14],    # D15
        )

    def read_brush_head_settings(self) -> BrushHeadSettings | None:
        """Block-read D10-D15 (6 words) for the FIRE-mode brush_head path.

        LOOP path uses `read_loop_block` instead so it shares the trigger
        read; this method is only hit by the single-shot FIRE dispatch.
        """
        with self._lock:
            words = self.plc.read_status(REG_CAM1_EXPOSURE, count=6)
        if not isinstance(words, list) or len(words) < 6:
            return None
        return BrushHeadSettings(
            cam1_exposure=words[0],          # D10
            dot_area_min=words[2],           # D12
            dot_area_max=words[3],           # D13
            ratio_min_x10=words[4],          # D14
            ratio_max_x10=words[5],          # D15
        )

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

    def write_camera_statuses(self, cam1_online: bool, cam2_online: bool) -> None:
        """Block-write D3+D4 in one Modbus request — half the round-trips of
        two separate `write_camera_status` calls. LOOP path uses this; the
        single-cam height path keeps `write_camera_status` for cam2 alone.
        """
        with self._lock:
            self.plc.write_multiple_registers(
                REG_CAM1_STATUS,
                [1 if cam1_online else 0, 1 if cam2_online else 0],
            )

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

    def write_brush_head_result(self, dot_count: int, area: int) -> None:
        """Write D42 (dot count) + D43 (detected area / 100) as a 2-word
        block — used by the BRUSH_HEAD mode for diagnostic output.

        Both values are clamped to uint16 range. The area is stored
        scaled-down because typical bristle-head ROIs are 50k-500k pixels,
        which would overflow a single 16-bit register; /100 keeps a useful
        resolution while fitting.
        """
        words = [
            max(0, min(65535, int(dot_count))),
            max(0, min(65535, int(area) // 100)),
        ]
        with self._lock:
            self.plc.write_multiple_registers(REG_BRUSH_DOT_COUNT, words)

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
