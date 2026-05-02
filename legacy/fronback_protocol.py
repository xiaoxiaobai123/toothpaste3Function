"""Modbus TCP layer for the legacy fronback protocol.

Register map matches the original toothpastefronback program byte-for-byte
on every value that affects PLC-observable behaviour. See
`docs/PLC寄存器手册.md` (Legacy section) for the address-by-address spec.

PLC writes (we read):
    D1   capture trigger   (10 = capture, 11 = LOOP, anything else = idle)
    D2   workcamera_count  (mode selector: 1 = dual-cam frontback,
                            0 = single-cam height, 2 = single-cam brush_head)
    D10  cam1 exposure µs              (frontback mode)
    D11  cam2 exposure µs              (frontback mode)
    D30  cam2 exposure µs              (height mode)
    D31  height brightness threshold   (0-255)
    D32  height min-Y filter
    D33  height detect left limit      (v0.3.15+ honored as algorithm ROI)
    D34  height detect right limit     (v0.3.15+ honored as algorithm ROI)
    D35  height comparison threshold
    D36  height width comparison       (read but unused — kept for compat)
    D50  brush_head cam1 exposure µs   (independent from D10 frontback)
    D51  brush_head shrink_pct         (0 = use config default)
    D52  brush_head adapt_block        (0 = use default; sanitized to odd ≥3)
    D53  reserved (v0.3.16+; future adapt_C slot)
    D54  brush_head dot_area_min       (0 = default)
    D55  brush_head dot_area_max       (0 = default)
    D56  brush_head roi_area_min ÷ 100 (0 = default; 500 = 50000 px)
    D57  brush_head roi_area_max ÷ 100 (0 = default; 5000 = 500000 px)
    D58  brush_head ratio_min × 10     (0 = default; 15 = 1.5 ratio)
    D59  brush_head ratio_max × 10     (0 = default; 35 = 3.5 ratio)
    D60-D63 brush_head manual ROI (x1, y1, x2, y2; all-0 = auto-detect)

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
    * D12-D15 were briefly used in v0.3.14/15 as a brush_head parameter
      block, but the customer asked for full physical isolation between
      brush_head and frontback registers — so v0.3.16 moved all brush
      params to D50-D63. D12-D15 are reserved again. The brush_head
      isolation only excepts D0/D1/D2/D3/D4 (system-level mode selector
      and result/status registers), which are intrinsically shared.
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
# D12-D15 reserved (briefly held brush_head params in v0.3.14/15;
# moved to D50-D63 in v0.3.16 for clean separation from frontback).
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

# Brush-head parameter block (D2=2 mode). v0.3.16+: physically separated
# from frontback / height registers so the customer's PLC ladder for the
# three modes never overlap (except the system-level D0-D4 + D2 selector).
# Each slot at 0 means "use the config.json default for this field" — the
# adapter in legacy/fronback_brush_head.py merges PLC + defaults per cycle.
REG_BRUSH_CAM1_EXPOSURE = 50
REG_BRUSH_SHRINK_PCT = 51
REG_BRUSH_ADAPT_BLOCK = 52
REG_BRUSH_RESERVED_53 = 53  # reserved for future adapt_C
REG_BRUSH_DOT_AREA_MIN = 54
REG_BRUSH_DOT_AREA_MAX = 55
REG_BRUSH_ROI_AREA_MIN_X100 = 56  # PLC value × 100 = pixel area (uint16 fit)
REG_BRUSH_ROI_AREA_MAX_X100 = 57
REG_BRUSH_RATIO_MIN_X10 = 58  # PLC value / 10 = ratio (15 = 1.5)
REG_BRUSH_RATIO_MAX_X10 = 59
REG_BRUSH_MANUAL_ROI_X1 = 60  # all four 0 = auto-detect (no manual crop)
REG_BRUSH_MANUAL_ROI_Y1 = 61
REG_BRUSH_MANUAL_ROI_X2 = 62
REG_BRUSH_MANUAL_ROI_Y2 = 63
BRUSH_PARAM_BLOCK_SIZE = 14  # D50..D63 inclusive

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
    """Single-shot D1-D11 read used by the LOOP hot path.

    Bundles trigger + mode + frontback exposures so one Modbus round-trip
    replaces the two separate `read_trigger_and_mode` + `read_frontback_settings`
    calls. D3-D9 are read as part of the contiguous span and discarded
    — they're our own writes (cam status) echoed back.

    LOOP path for brush_head mode (D2=2) follows up with a separate
    `read_brush_head_settings` call against D50-D63; bundling that into
    the loop block would cost a 51-word read every iteration even for
    frontback / height cycles, which is wasteful.
    """

    trigger: int
    mode: int
    cam1_exposure: int
    cam2_exposure: int


@dataclass(frozen=True)
class BrushHeadSettings:
    """Brush-head detection parameters from PLC (D50-D63 block).

    Each scalar field is a raw PLC word; 0 means "use the config.json
    default for this parameter" — the adapter in
    `legacy/fronback_brush_head.py` does the merge with config defaults
    before calling the v2 BrushHeadProcessor. roi_area is stored as
    PLC-value × 100 to fit pixel ranges in uint16 (e.g. PLC 500 → 50000
    pixel area). Ratios are × 10 (e.g. PLC 15 → 1.5).

    `manual_roi` is a 4-tuple (x1, y1, x2, y2) in full-image pixel
    coordinates. (0, 0, 0, 0) means "auto-detect on full frame" —
    matches the v2 BrushHeadProcessor's MANUAL_ROI_DEFAULT semantics.
    """

    cam1_exposure: int  # D50
    shrink_pct: int  # D51 (0 = default)
    adapt_block: int  # D52 (0 = default; sanitized to odd ≥3 by processor)
    dot_area_min: int  # D54
    dot_area_max: int  # D55
    roi_area_min_x100: int  # D56 (PLC value × 100 = pixel area)
    roi_area_max_x100: int  # D57
    ratio_min_x10: int  # D58 (15 = ratio 1.5)
    ratio_max_x10: int  # D59
    manual_roi: tuple[int, int, int, int]  # D60-D63 (0,0,0,0 = auto)


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
        """Block-read D1-D11 in one Modbus request: trigger, mode, frontback
        exposures bundled together. Saves a round-trip vs separate
        `read_trigger_and_mode` + `read_frontback_settings` calls inside the
        LOOP, where the per-iteration savings compound.

        D3-D9 are read as part of the contiguous span (Modbus reads must
        be contiguous) but discarded — they're our own writes echoed back.

        Brush-head mode (D2=2) uses this for trigger detection and then
        does its own `read_brush_head_settings` against the dedicated
        D50-D63 block — that block is too far from D11 to share a read
        without padding 39 useless words every cycle.
        """
        with self._lock:
            words = self.plc.read_status(REG_CAPTURE_TRIGGER, count=11)
        if not isinstance(words, list) or len(words) < 11:
            return None
        return LoopBlock(
            trigger=words[0],  # D1
            mode=words[1],  # D2
            cam1_exposure=words[9],  # D10
            cam2_exposure=words[10],  # D11
        )

    def read_brush_head_settings(self) -> BrushHeadSettings | None:
        """Block-read D50-D63 (14 words): every brush-head parameter.

        Used by both LOOP and FIRE paths after `read_loop_block` (or
        `read_trigger_and_mode`) returns mode == MODE_BRUSH_HEAD. The
        whole brush-head register block lives here, physically separated
        from the frontback / height registers per customer spec.
        """
        with self._lock:
            words = self.plc.read_status(REG_BRUSH_CAM1_EXPOSURE, count=BRUSH_PARAM_BLOCK_SIZE)
        if not isinstance(words, list) or len(words) < BRUSH_PARAM_BLOCK_SIZE:
            return None
        return BrushHeadSettings(
            cam1_exposure=words[0],  # D50
            shrink_pct=words[1],  # D51
            adapt_block=words[2],  # D52
            # words[3] = D53 reserved
            dot_area_min=words[4],  # D54
            dot_area_max=words[5],  # D55
            roi_area_min_x100=words[6],  # D56
            roi_area_max_x100=words[7],  # D57
            ratio_min_x10=words[8],  # D58
            ratio_max_x10=words[9],  # D59
            manual_roi=(
                int(words[10]),  # D60 x1
                int(words[11]),  # D61 y1
                int(words[12]),  # D62 x2
                int(words[13]),  # D63 y2
            ),
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
