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
    """LOOP path bundles D1-D11 into one Modbus read instead of two."""
    # 11 words: D1 D2 D3 D4 D5 D6 D7 D8 D9 D10 D11
    fake.scripted_reads = {(REG_CAPTURE_TRIGGER, 11): [11, 1, 0, 0, 0, 0, 0, 0, 0, 5000, 6000]}
    legacy = LegacyFronbackPLC(plc_base=fake)

    block = legacy.read_loop_block()
    assert block is not None
    assert block.trigger == 11
    assert block.mode == 1
    assert block.cam1_exposure == 5000
    assert block.cam2_exposure == 6000
    # Single Modbus round-trip, not two.
    assert fake.reads == [(REG_CAPTURE_TRIGGER, 11)]


def test_read_loop_block_returns_none_on_failure(fake: FakePLCBase) -> None:
    """Mirrors the other read_* helpers — None on transient PLC failure."""
    legacy = LegacyFronbackPLC(plc_base=fake)
    assert legacy.read_loop_block() is None


def test_read_loop_block_extracts_correct_word_offsets(fake: FakePLCBase) -> None:
    """Word indices: trigger=0, mode=1, cam1_exp=9, cam2_exp=10. A wrong
    offset would silently swap exposures with garbage like D5/D6."""
    fake.scripted_reads = {
        (REG_CAPTURE_TRIGGER, 11): [
            10,    # D1 trigger
            0,     # D2 mode
            99,    # D3 (cam1 status — our own write echoed back)
            99,    # D4
            99,    # D5
            99,    # D6
            99,    # D7
            99,    # D8
            99,    # D9
            7777,  # D10 cam1 exposure
            8888,  # D11 cam2 exposure
        ]
    }
    legacy = LegacyFronbackPLC(plc_base=fake)
    block = legacy.read_loop_block()
    assert block is not None
    assert block.trigger == 10
    assert block.mode == 0
    assert block.cam1_exposure == 7777
    assert block.cam2_exposure == 8888


def test_does_not_read_d12_d13_unrecognized_threshold(fake: FakePLCBase) -> None:
    """The original program's dead D12/D13 reads are dropped here.

    If a future change accidentally re-introduces them, this test fails
    and forces a conscious decision.
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
