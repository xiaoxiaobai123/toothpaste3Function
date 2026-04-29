"""End-to-end test: LegacyFronbackOrchestrator + MockCameraManager + fake PLC.

Drives the full poll-trigger-capture-write loop and asserts on the
exact sequence of D-register writes the orchestrator produced.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import cv2
import numpy as np
import pytest

from camera.mock import MockCameraManager
from legacy.fronback_orchestrator import (
    LegacyFronbackOrchestrator,
    make_file_roi_provider,
)
from legacy.fronback_protocol import (
    REG_CAM1_EXPOSURE,
    REG_CAM1_STATUS,
    REG_CAM2_STATUS,
    REG_CAPTURE_TRIGGER,
    REG_EDGE1_LOW,
    REG_HEIGHT_CAM2_EXPOSURE,
    REG_HEIGHT_RESULT,
    REG_RECOGNITION_RESULT,
    RESULT_BACK_OR_NG,
    RESULT_FRONT_OR_OK,
    TRIGGER_DONE,
    TRIGGER_FIRE,
    TRIGGER_IDLE,
    LegacyFronbackPLC,
)


# ----------------------------------------------------------------------
# A scripted PLCBase that simulates D-register state transitions over time.
# ----------------------------------------------------------------------
class ScriptedPLC:
    """In-memory Modbus-server-like state."""

    def __init__(self) -> None:
        self.regs: dict[int, int] = {addr: 0 for addr in range(0, 50)}
        self.writes: list[tuple[int, list[int]]] = []  # (start_addr, words)
        self._lock = asyncio.Lock()

    def read_status(self, address: int, count: int = 1) -> int | list[int] | None:
        if count == 1:
            return self.regs.get(address, 0)
        return [self.regs.get(address + i, 0) for i in range(count)]

    def write_status(self, address: int, value: int) -> bool:
        self.regs[address] = value
        self.writes.append((address, [value]))
        return True

    def write_multiple_registers(self, address: int, values: list[int]) -> bool:
        for i, v in enumerate(values):
            self.regs[address + i] = v
        self.writes.append((address, list(values)))
        return True

    def close(self) -> None:
        pass


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture
def cam_dir(tmp_path: Path) -> tuple[Path, Path]:
    """Two folders of synthetic images: one with many edges (cam1), one with few."""
    d1 = tmp_path / "cam1"
    d2 = tmp_path / "cam2"
    d1.mkdir()
    d2.mkdir()

    # cam1: lots of vertical stripes → many edges
    img1 = np.full((400, 600, 3), 220, dtype=np.uint8)
    for x in range(20, 580, 10):
        cv2.line(img1, (x, 50), (x, 350), (40, 40, 40), 2)
    cv2.imwrite(str(d1 / "frame.png"), img1)

    # cam2: few stripes → few edges
    img2 = np.full((400, 600, 3), 220, dtype=np.uint8)
    for x in (200, 400):
        cv2.line(img2, (x, 100), (x, 300), (40, 40, 40), 1)
    cv2.imwrite(str(d2 / "frame.png"), img2)

    return d1, d2


@pytest.fixture
def height_dir(tmp_path: Path) -> Path:
    """A single-image folder for cam2 in height mode (red-channel fill)."""
    d = tmp_path / "height_cam"
    d.mkdir()
    img = np.zeros((500, 600, 3), dtype=np.uint8)
    img[100:250, :, 2] = 200  # red channel filled to y=249
    cv2.imwrite(str(d / "frame.png"), img)
    return d


def _logger() -> logging.Logger:
    log = logging.getLogger("legacy_test")
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    return log


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_frontback_one_cycle_writes_d0_and_edge_counts(
    cam_dir: tuple[Path, Path], tmp_path: Path
) -> None:
    """D2=1 + D1=10 → orchestrator writes D0 (1 or 2) and D20-D23 edge counts."""
    d1, d2 = cam_dir

    plc = ScriptedPLC()
    plc.regs[REG_CAPTURE_TRIGGER] = TRIGGER_FIRE
    plc.regs[2] = 1  # MODE_FRONTBACK
    plc.regs[REG_CAM1_EXPOSURE] = 5000
    plc.regs[REG_CAM1_EXPOSURE + 1] = 5000

    legacy_plc = LegacyFronbackPLC(plc_base=plc)
    cam = MockCameraManager({1: d1, 2: d2})
    roi = make_file_roi_provider(cam, base_dir=tmp_path, logger=_logger())

    png_path = tmp_path / "processed_image.png"
    rgb565_path = tmp_path / "output_image.rgb565"
    orchestrator = LegacyFronbackOrchestrator(
        legacy_plc, cam, roi, _logger(), png_path=png_path, rgb565_path=rgb565_path
    )
    task = asyncio.create_task(orchestrator.run())

    # Wait for both the D0 write AND the display files. PLC writes finish
    # before the display thread does — cancelling on D0 alone races with
    # the rgb565 write completing in its asyncio.to_thread worker.
    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        d0_done = any(addr == REG_RECOGNITION_RESULT for addr, _ in plc.writes)
        files_done = png_path.is_file() and rgb565_path.is_file()
        if d0_done and files_done:
            break
        await asyncio.sleep(0.05)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    addrs_written = [addr for addr, _ in plc.writes]
    assert REG_RECOGNITION_RESULT in addrs_written, "D0 not written"
    assert REG_EDGE1_LOW in addrs_written, "D20-D23 edge counts not written"
    assert REG_CAM1_STATUS in addrs_written, "D3 cam1 status not written"
    assert REG_CAM2_STATUS in addrs_written, "D4 cam2 status not written"

    # cam1 has many more edges, expect FRONT (D0=1).
    d0_writes = [v[0] for addr, v in plc.writes if addr == REG_RECOGNITION_RESULT]
    assert d0_writes[-1] == RESULT_FRONT_OR_OK

    # Both display sinks should be written:
    # - PNG for older sites still using feh/fbi
    # - rgb565 for sites using image_updater + /dev/fb0
    assert png_path.is_file(), "display PNG not produced"
    assert png_path.stat().st_size > 0
    assert rgb565_path.is_file(), "rgb565 file not produced for image_updater"
    # Header (8 bytes) + at least one frame of pixels.
    assert rgb565_path.stat().st_size > 8


@pytest.mark.asyncio
async def test_frontback_swaps_result_when_cam2_has_more_edges(
    cam_dir: tuple[Path, Path], tmp_path: Path
) -> None:
    """Same setup but with cam folders swapped → expect BACK (D0=2)."""
    d1, d2 = cam_dir

    plc = ScriptedPLC()
    plc.regs[REG_CAPTURE_TRIGGER] = TRIGGER_FIRE
    plc.regs[2] = 1

    legacy_plc = LegacyFronbackPLC(plc_base=plc)
    # Swapped: cam1 gets the low-edge folder, cam2 gets the high-edge folder.
    cam = MockCameraManager({1: d2, 2: d1})
    roi = make_file_roi_provider(cam, base_dir=tmp_path, logger=_logger())

    orchestrator = LegacyFronbackOrchestrator(legacy_plc, cam, roi, _logger())
    task = asyncio.create_task(orchestrator.run())

    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        if any(addr == REG_RECOGNITION_RESULT for addr, _ in plc.writes):
            break
        await asyncio.sleep(0.05)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    d0_writes = [v[0] for addr, v in plc.writes if addr == REG_RECOGNITION_RESULT]
    assert d0_writes[-1] == RESULT_BACK_OR_NG


@pytest.mark.asyncio
async def test_height_one_cycle_writes_d0_and_d40(height_dir: Path, tmp_path: Path) -> None:
    """D2=0 + D1=10 → orchestrator writes D0 + D40, doesn't touch D3/D20-23."""
    plc = ScriptedPLC()
    plc.regs[REG_CAPTURE_TRIGGER] = TRIGGER_FIRE
    plc.regs[2] = 0  # MODE_HEIGHT
    plc.regs[REG_HEIGHT_CAM2_EXPOSURE] = 4000
    plc.regs[31] = 100  # brightness
    plc.regs[32] = 50  # min_height
    plc.regs[35] = 300  # decision

    legacy_plc = LegacyFronbackPLC(plc_base=plc)
    # Only cam2 needed in height mode.
    cam = MockCameraManager({2: height_dir})
    roi = make_file_roi_provider(cam, base_dir=tmp_path, logger=_logger())

    orchestrator = LegacyFronbackOrchestrator(legacy_plc, cam, roi, _logger())
    task = asyncio.create_task(orchestrator.run())

    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        if any(addr == REG_HEIGHT_RESULT for addr, _ in plc.writes):
            break
        await asyncio.sleep(0.05)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    addrs_written = [addr for addr, _ in plc.writes]
    assert REG_RECOGNITION_RESULT in addrs_written
    assert REG_HEIGHT_RESULT in addrs_written
    # Height mode must NOT write cam1 status or edge counts.
    assert REG_CAM1_STATUS not in addrs_written
    assert REG_EDGE1_LOW not in addrs_written

    # Image fills y=100..249 → max_y_avg ≈ 249, < decision 300 → state 1 (OK)
    d0_writes = [v[0] for addr, v in plc.writes if addr == REG_RECOGNITION_RESULT]
    assert d0_writes[-1] == RESULT_FRONT_OR_OK
    d40_writes = [v[0] for addr, v in plc.writes if addr == REG_HEIGHT_RESULT]
    assert 240 <= d40_writes[-1] <= 260


@pytest.mark.asyncio
async def test_trigger_acknowledged_then_done(cam_dir: tuple[Path, Path], tmp_path: Path) -> None:
    """D1 must be written 0 (ack) before processing, then 1 (done) after."""
    d1, d2 = cam_dir

    plc = ScriptedPLC()
    plc.regs[REG_CAPTURE_TRIGGER] = TRIGGER_FIRE
    plc.regs[2] = 1

    legacy_plc = LegacyFronbackPLC(plc_base=plc)
    cam = MockCameraManager({1: d1, 2: d2})
    roi = make_file_roi_provider(cam, base_dir=tmp_path, logger=_logger())
    orchestrator = LegacyFronbackOrchestrator(legacy_plc, cam, roi, _logger())
    task = asyncio.create_task(orchestrator.run())

    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        d1_writes = [v[0] for addr, v in plc.writes if addr == REG_CAPTURE_TRIGGER]
        if TRIGGER_DONE in d1_writes:
            break
        await asyncio.sleep(0.05)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    d1_writes = [v[0] for addr, v in plc.writes if addr == REG_CAPTURE_TRIGGER]
    # Order: TRIGGER_IDLE (ack) appears before TRIGGER_DONE.
    assert TRIGGER_IDLE in d1_writes
    assert TRIGGER_DONE in d1_writes
    assert d1_writes.index(TRIGGER_IDLE) < d1_writes.index(TRIGGER_DONE)


@pytest.mark.asyncio
async def test_frontback_renders_offline_placeholder_when_cam1_missing(
    cam_dir: tuple[Path, Path], tmp_path: Path
) -> None:
    """When cam1 fails to capture during frontback mode, the orchestrator
    still writes both display sinks (operator sees CAM 1 OFFLINE) but
    skips the D0 / edge-count writes since the algorithm did not run."""
    _, d2 = cam_dir  # use only cam2's dir; cam1 is intentionally absent

    plc = ScriptedPLC()
    plc.regs[REG_CAPTURE_TRIGGER] = TRIGGER_FIRE
    plc.regs[2] = 1  # MODE_FRONTBACK

    legacy_plc = LegacyFronbackPLC(plc_base=plc)
    # MockCameraManager returns None from capture_image(1) when cam1 is
    # not configured — same code path as a real camera being offline at
    # capture time (the cam crashed mid-run rather than at startup).
    cam = MockCameraManager({2: d2})
    roi = make_file_roi_provider(cam, base_dir=tmp_path, logger=_logger())

    png_path = tmp_path / "processed_image.png"
    rgb565_path = tmp_path / "output_image.rgb565"
    orchestrator = LegacyFronbackOrchestrator(
        legacy_plc, cam, roi, _logger(), png_path=png_path, rgb565_path=rgb565_path
    )
    task = asyncio.create_task(orchestrator.run())

    # Wait for: display sinks written + TRIGGER_DONE handshake. The
    # display write happens inside _do_frontback; TRIGGER_DONE is written
    # after _do_frontback returns. Bail on display-files-only would race
    # the trigger-done assertion below.
    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline:
        files_done = png_path.is_file() and rgb565_path.is_file()
        trigger_writes = [v[0] for addr, v in plc.writes if addr == REG_CAPTURE_TRIGGER]
        if files_done and TRIGGER_DONE in trigger_writes:
            break
        await asyncio.sleep(0.05)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # Display sinks are written even though cam1 was offline — operator
    # screen shows the OFFLINE placeholder instead of a frozen old frame.
    assert png_path.is_file(), "PNG should be written with placeholder when cam1 offline"
    assert rgb565_path.is_file(), "rgb565 should be written with placeholder when cam1 offline"

    addrs_written = [addr for addr, _ in plc.writes]
    # Camera-status writes are still made so the PLC knows which camera
    # dropped (matches every other frontback cycle).
    assert REG_CAM1_STATUS in addrs_written
    assert REG_CAM2_STATUS in addrs_written
    # But D0 (recognition result) and D20-D23 (edge counts) MUST NOT be
    # written — the algorithm did not run on a half-blind frame, so any
    # value here would be a lie to the PLC.
    assert REG_RECOGNITION_RESULT not in addrs_written, (
        "D0 should NOT be written when cam1 is offline (algorithm didn't run)"
    )
    assert REG_EDGE1_LOW not in addrs_written, (
        "edge counts should NOT be written when cam1 is offline"
    )
    # Trigger handshake still completes so the PLC doesn't time out.
    d1_writes = [v[0] for addr, v in plc.writes if addr == REG_CAPTURE_TRIGGER]
    assert TRIGGER_IDLE in d1_writes
    assert TRIGGER_DONE in d1_writes


@pytest.mark.asyncio
async def test_no_capture_when_trigger_idle(cam_dir: tuple[Path, Path], tmp_path: Path) -> None:
    """If D1 != 10 throughout, no D0 / edge writes happen."""
    d1, d2 = cam_dir

    plc = ScriptedPLC()
    plc.regs[REG_CAPTURE_TRIGGER] = 0  # NEVER fires
    plc.regs[2] = 1

    legacy_plc = LegacyFronbackPLC(plc_base=plc)
    cam = MockCameraManager({1: d1, 2: d2})
    roi = make_file_roi_provider(cam, base_dir=tmp_path, logger=_logger())
    orchestrator = LegacyFronbackOrchestrator(legacy_plc, cam, roi, _logger())
    task = asyncio.create_task(orchestrator.run())

    await asyncio.sleep(0.5)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    addrs_written = [addr for addr, _ in plc.writes]
    assert REG_RECOGNITION_RESULT not in addrs_written
    assert REG_EDGE1_LOW not in addrs_written
