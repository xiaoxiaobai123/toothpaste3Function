"""Tests for legacy/fronback_protocol.py — the Modbus address layer.

Uses a fake PLCBase that records every read/write so we can assert
exactly which D-registers the adapter touches and in what order.
"""

from __future__ import annotations

import pytest

from legacy.fronback_protocol import (
    REG_CAM1_EXPOSURE,
    REG_CAM1_STATUS,
    REG_CAM2_STATUS,
    REG_CAPTURE_TRIGGER,
    REG_EDGE1_LOW,
    REG_HEIGHT_CAM2_EXPOSURE,
    REG_HEIGHT_RESULT,
    REG_RECOGNITION_RESULT,
    LegacyFronbackPLC,
)


class FakePLCBase:
    """Records every read/write for assertion."""

    def __init__(self, scripted_reads: dict[tuple[int, int], list[int]] | None = None):
        # Map (address, count) -> list of words the next read will return.
        self.scripted_reads = scripted_reads or {}
        self.reads: list[tuple[int, int]] = []
        self.writes_single: list[tuple[int, int]] = []
        self.writes_block: list[tuple[int, list[int]]] = []
        self.closed = False

    def read_status(self, address: int, count: int = 1) -> int | list[int] | None:
        self.reads.append((address, count))
        words = self.scripted_reads.get((address, count))
        if words is None:
            return None
        return words[0] if count == 1 else list(words)

    def write_status(self, address: int, value: int) -> bool:
        self.writes_single.append((address, value))
        return True

    def write_multiple_registers(self, address: int, values: list[int]) -> bool:
        self.writes_block.append((address, list(values)))
        return True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake() -> FakePLCBase:
    return FakePLCBase()


@pytest.fixture
def legacy(fake: FakePLCBase) -> LegacyFronbackPLC:
    return LegacyFronbackPLC(plc_base=fake)


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------
def test_read_trigger_and_mode_uses_one_block_read(fake: FakePLCBase) -> None:
    fake.scripted_reads = {(REG_CAPTURE_TRIGGER, 2): [10, 1]}
    legacy = LegacyFronbackPLC(plc_base=fake)

    state = legacy.read_trigger_and_mode()
    assert state is not None
    assert state.trigger == 10
    assert state.mode == 1
    # Critically: ONE Modbus request, not two separate reads.
    assert fake.reads == [(REG_CAPTURE_TRIGGER, 2)]


def test_read_trigger_and_mode_returns_none_on_failure(fake: FakePLCBase) -> None:
    legacy = LegacyFronbackPLC(plc_base=fake)
    assert legacy.read_trigger_and_mode() is None


def test_read_frontback_settings_uses_block_read(fake: FakePLCBase) -> None:
    fake.scripted_reads = {(REG_CAM1_EXPOSURE, 2): [5000, 6000]}
    legacy = LegacyFronbackPLC(plc_base=fake)

    settings = legacy.read_frontback_settings()
    assert settings is not None
    assert settings.cam1_exposure == 5000
    assert settings.cam2_exposure == 6000
    assert fake.reads == [(REG_CAM1_EXPOSURE, 2)]


def test_read_height_settings_uses_seven_word_block_read(fake: FakePLCBase) -> None:
    fake.scripted_reads = {(REG_HEIGHT_CAM2_EXPOSURE, 7): [4000, 100, 50, 0, 600, 300, 0]}
    legacy = LegacyFronbackPLC(plc_base=fake)

    settings = legacy.read_height_settings()
    assert settings is not None
    assert settings.cam2_exposure == 4000
    assert settings.brightness_threshold == 100
    assert settings.min_height == 50
    assert settings.height_comparison == 300
    # All seven registers fetched in one Modbus request.
    assert fake.reads == [(REG_HEIGHT_CAM2_EXPOSURE, 7)]


def test_read_loop_block_uses_one_eleven_word_request(fake: FakePLCBase) -> None:
    """LOOP path bundles D1-D11 (trigger + mode + frontback exposures) into
    one Modbus read. Brush-head params live at D50-D63 and are read
    separately when mode == BRUSH_HEAD (avoids padding 39 useless words)."""
    fake.scripted_reads = {(REG_CAPTURE_TRIGGER, 11): [11, 1, 0, 0, 0, 0, 0, 0, 0, 5000, 6000]}
    legacy = LegacyFronbackPLC(plc_base=fake)

    block = legacy.read_loop_block()
    assert block is not None
    assert block.trigger == 11
    assert block.mode == 1
    assert block.cam1_exposure == 5000
    assert block.cam2_exposure == 6000
    # Single Modbus round-trip, not multiple.
    assert fake.reads == [(REG_CAPTURE_TRIGGER, 11)]


def test_read_loop_block_returns_none_on_failure(fake: FakePLCBase) -> None:
    """Mirrors the other read_* helpers — None on transient PLC failure."""
    legacy = LegacyFronbackPLC(plc_base=fake)
    assert legacy.read_loop_block() is None


def test_read_loop_block_extracts_correct_word_offsets(fake: FakePLCBase) -> None:
    """Word indices: trigger=0, mode=1, cam1_exp=9, cam2_exp=10. A wrong
    offset would silently swap exposures with cam status echoes."""
    fake.scripted_reads = {
        (REG_CAPTURE_TRIGGER, 11): [
            10,  # D1 trigger
            2,  # D2 mode (BRUSH_HEAD)
            99,  # D3 (cam1 status — our own write echoed back)
            99,  # D4
            99,  # D5
            99,  # D6
            99,  # D7
            99,  # D8
            99,  # D9
            7777,  # D10 cam1 exposure
            8888,  # D11 cam2 exposure
        ]
    }
    legacy = LegacyFronbackPLC(plc_base=fake)
    block = legacy.read_loop_block()
    assert block is not None
    assert block.trigger == 10
    assert block.mode == 2
    assert block.cam1_exposure == 7777
    assert block.cam2_exposure == 8888


def test_does_not_read_d12_d13_unrecognized_threshold(fake: FakePLCBase) -> None:
    """No code path touches D12-D15 since v0.3.16.

    History: the original toothpastefronback program read D12/D13 as
    `unrecognized_threshold` but never used the values. v0.3.14 briefly
    repurposed D12-D15 for brush_head parameters, but v0.3.16 moved
    those to D50-D63 for full physical isolation between modes per
    customer spec. D12-D15 are now reserved (no reads, no writes).
    """
    fake.scripted_reads = {
        (REG_CAPTURE_TRIGGER, 2): [10, 1],
        (REG_CAM1_EXPOSURE, 2): [5000, 6000],
    }
    legacy = LegacyFronbackPLC(plc_base=fake)

    legacy.read_trigger_and_mode()
    legacy.read_frontback_settings()

    accessed_addresses = {addr for addr, _ in fake.reads}
    assert 12 not in accessed_addresses
    assert 13 not in accessed_addresses


# ----------------------------------------------------------------------
# Writes
# ----------------------------------------------------------------------
def test_write_trigger_targets_d1(fake: FakePLCBase) -> None:
    legacy = LegacyFronbackPLC(plc_base=fake)
    legacy.write_trigger(0)
    legacy.write_trigger(1)
    assert fake.writes_single == [(REG_CAPTURE_TRIGGER, 0), (REG_CAPTURE_TRIGGER, 1)]


def test_write_recognition_result_targets_d0(fake: FakePLCBase) -> None:
    legacy = LegacyFronbackPLC(plc_base=fake)
    legacy.write_recognition_result(2)
    assert fake.writes_single == [(REG_RECOGNITION_RESULT, 2)]


def test_write_camera_status_targets_d3_or_d4(fake: FakePLCBase) -> None:
    legacy = LegacyFronbackPLC(plc_base=fake)
    legacy.write_camera_status(1, online=True)
    legacy.write_camera_status(2, online=False)
    assert fake.writes_single == [
        (REG_CAM1_STATUS, 1),
        (REG_CAM2_STATUS, 0),
    ]


def test_write_camera_status_ignores_unsupported_camera(fake: FakePLCBase) -> None:
    legacy = LegacyFronbackPLC(plc_base=fake)
    legacy.write_camera_status(7, online=True)  # not 1 or 2
    assert fake.writes_single == []


def test_write_camera_statuses_uses_block_write_at_d3(fake: FakePLCBase) -> None:
    """LOOP path writes D3+D4 in one Modbus block instead of two singles."""
    legacy = LegacyFronbackPLC(plc_base=fake)
    legacy.write_camera_statuses(cam1_online=True, cam2_online=False)
    assert fake.writes_block == [(REG_CAM1_STATUS, [1, 0])]
    # And nothing leaked to single writes.
    assert fake.writes_single == []


def test_write_camera_statuses_both_offline(fake: FakePLCBase) -> None:
    legacy = LegacyFronbackPLC(plc_base=fake)
    legacy.write_camera_statuses(cam1_online=False, cam2_online=False)
    assert fake.writes_block == [(REG_CAM1_STATUS, [0, 0])]


def test_write_camera_statuses_both_online(fake: FakePLCBase) -> None:
    legacy = LegacyFronbackPLC(plc_base=fake)
    legacy.write_camera_statuses(cam1_online=True, cam2_online=True)
    assert fake.writes_block == [(REG_CAM1_STATUS, [1, 1])]


# ----------------------------------------------------------------------
# Brush-head reads + writes (D2=2 mode added in v0.3.14)
# ----------------------------------------------------------------------
def test_read_brush_head_settings_uses_one_fourteen_word_block_read(
    fake: FakePLCBase,
) -> None:
    """Brush_head reads its own D50-D63 14-word block, separate from the
    frontback / height registers. v0.3.16+ layout:
        D50 cam1_exposure
        D51 shrink_pct
        D52 adapt_block
        D53 reserved
        D54 dot_area_min
        D55 dot_area_max
        D56 roi_area_min ÷ 100
        D57 roi_area_max ÷ 100
        D58 ratio_min × 10
        D59 ratio_max × 10
        D60-D63 manual_roi (x1, y1, x2, y2)
    """
    from legacy.fronback_protocol import REG_BRUSH_CAM1_EXPOSURE

    fake.scripted_reads = {
        (REG_BRUSH_CAM1_EXPOSURE, 14): [
            5000,  # D50 cam1_exposure
            12,  # D51 shrink_pct
            29,  # D52 adapt_block
            0,  # D53 reserved
            100,  # D54 dot_area_min
            800,  # D55 dot_area_max
            500,  # D56 roi_area_min ÷ 100  -> 50000 px
            5000,  # D57 roi_area_max ÷ 100 -> 500000 px
            18,  # D58 ratio_min × 10  -> 1.8
            32,  # D59 ratio_max × 10  -> 3.2
            100,
            200,
            900,
            700,  # D60-D63 manual_roi
        ]
    }
    legacy = LegacyFronbackPLC(plc_base=fake)

    settings = legacy.read_brush_head_settings()
    assert settings is not None
    assert settings.cam1_exposure == 5000
    assert settings.shrink_pct == 12
    assert settings.adapt_block == 29
    assert settings.dot_area_min == 100
    assert settings.dot_area_max == 800
    assert settings.roi_area_min_x100 == 500
    assert settings.roi_area_max_x100 == 5000
    assert settings.ratio_min_x10 == 18
    assert settings.ratio_max_x10 == 32
    assert settings.manual_roi == (100, 200, 900, 700)
    # Single Modbus round-trip for the whole block.
    assert fake.reads == [(REG_BRUSH_CAM1_EXPOSURE, 14)]


def test_read_brush_head_settings_returns_none_on_failure(fake: FakePLCBase) -> None:
    legacy = LegacyFronbackPLC(plc_base=fake)
    assert legacy.read_brush_head_settings() is None


def test_write_brush_side_code_uses_single_register_at_d70(fake: FakePLCBase) -> None:
    """v0.3.24+: front/back classification at D70, distinct from D0
    OK/NG. 1=Front, 2=Back, 0=UNKNOWN."""
    legacy = LegacyFronbackPLC(plc_base=fake)
    legacy.write_brush_side_code(1)
    legacy.write_brush_side_code(2)
    legacy.write_brush_side_code(0)
    legacy.write_brush_side_code(99999)  # over uint16 → clamp to 65535
    legacy.write_brush_side_code(-1)  # negative → clamp to 0
    assert fake.writes_single == [(70, 1), (70, 2), (70, 0), (70, 65535), (70, 0)]


def test_write_system_heartbeat_uses_single_register_at_d9(fake: FakePLCBase) -> None:
    """v0.3.25+: heartbeat moved D6 → D9 per customer request. Single-
    register write, system-area placement so all three modes share one
    watchdog address. Background task in the orchestrator alternates
    0/1 each second."""
    legacy = LegacyFronbackPLC(plc_base=fake)
    legacy.write_system_heartbeat(0)
    legacy.write_system_heartbeat(1)
    legacy.write_system_heartbeat(70000)  # over uint16 → clamp to 65535
    legacy.write_system_heartbeat(-1)  # negative → clamp to 0
    assert fake.writes_single == [(9, 0), (9, 1), (9, 65535), (9, 0)]


def test_write_edge_counts_uses_block_write_at_d20(fake: FakePLCBase) -> None:
    legacy = LegacyFronbackPLC(plc_base=fake)
    legacy.write_edge_counts(0x12345678, 0xABCDEF01)
    # Low word first (matches original split):
    expected_words = [0x5678, 0x1234, 0xEF01, 0xABCD]
    assert fake.writes_block == [(REG_EDGE1_LOW, expected_words)]


def test_write_height_result_clamps_to_uint16(fake: FakePLCBase) -> None:
    legacy = LegacyFronbackPLC(plc_base=fake)
    legacy.write_height_result(70000)  # > 65535
    legacy.write_height_result(-5)
    legacy.write_height_result(300)
    assert fake.writes_single == [
        (REG_HEIGHT_RESULT, 65535),
        (REG_HEIGHT_RESULT, 0),
        (REG_HEIGHT_RESULT, 300),
    ]


def test_close_propagates_to_plc_base(fake: FakePLCBase) -> None:
    legacy = LegacyFronbackPLC(plc_base=fake)
    legacy.close()
    assert fake.closed is True


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------
def test_constructor_requires_ip_or_plc_base() -> None:
    with pytest.raises(ValueError, match="needs an ip or a plc_base"):
        LegacyFronbackPLC()  # neither provided
