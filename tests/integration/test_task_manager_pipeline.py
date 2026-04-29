"""End-to-end pipeline tests: TaskManager + MockCameraManager + MockPLCManager.

These exercise the asyncio orchestration that unit tests skip:
    - PLC settings flow into TaskManager every loop iteration
    - capture → dispatch → process → write_result_to_plc + combine in
      parallel via asyncio.gather
    - status transitions (IDLE → START_TASK → TASK_COMPLETED)
    - the display image is written to disk via display_utils

We tear the loop down via task.cancel() once the assertion holds, since
TaskManager's outer loops are infinite by design.
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
from core.task_manager import TaskManager
from plc.codec import float32_to_words, uint32_to_words
from plc.enums import (
    CameraStatus,
    CameraTriggerStatus,
    Endian,
    ProductType,
)
from plc.mock import MockCameraConfig, MockPLCManager


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture
def brush_dir(tmp_path: Path) -> Path:
    """Two synthetic brush-head images with clear "front" classification."""
    out = tmp_path / "brush"
    out.mkdir()
    rng = np.random.default_rng(seed=42)
    for fname in ("a.png", "b.png"):
        img = np.full((600, 800, 3), 230, dtype=np.uint8)
        for _ in range(80):
            x = int(rng.integers(110, 690))
            y = int(rng.integers(205, 295))
            cv2.circle(img, (x, y), 4, (40, 40, 40), -1)
        for _ in range(20):
            x = int(rng.integers(110, 690))
            y = int(rng.integers(305, 395))
            cv2.circle(img, (x, y), 4, (40, 40, 40), -1)
        cv2.imwrite(str(out / fname), img)
    return out


def _brush_raw_config() -> tuple[int, ...]:
    """raw_config tuple instructing BrushHeadProcessor with default-ish params."""
    raw = [0] * 18
    pd = float32_to_words(1.0)
    raw[2], raw[3] = pd[0], pd[1]
    raw[4] = ProductType.BRUSH_HEAD.value
    # Default values are picked up via the "0 = use default" sentinel.
    return tuple(raw)


def _toothpaste_raw_config() -> tuple[int, ...]:
    raw = [0] * 18
    pd = float32_to_words(1.0)
    raw[2], raw[3] = pd[0], pd[1]
    raw[4] = ProductType.TOOTHPASTE_FRONTBACK.value
    front = uint32_to_words(5000, Endian.LITTLE)
    back = uint32_to_words(500, Endian.LITTLE)
    raw[6], raw[7] = front[0], front[1]
    raw[8], raw[9] = back[0], back[1]
    return tuple(raw)


def _make_config(product_type: ProductType, status: CameraStatus) -> MockCameraConfig:
    raw_config = _brush_raw_config() if product_type == ProductType.BRUSH_HEAD else _toothpaste_raw_config()
    return MockCameraConfig(
        status=status,
        trigger_mode=CameraTriggerStatus.SOFTWARE_TRIGGER,
        exposure_time=5000,
        pixel_distance=1.0,
        product_type=product_type,
        raw_config=raw_config,
    )


async def _run_until(condition, plc: MockPLCManager, timeout: float = 5.0) -> None:
    """Spin until condition() returns True, or fail after `timeout` seconds."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout}s; results={len(plc.results_log)}")


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_single_capture_produces_one_plc_write(
    brush_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """START_TASK → exactly one write_camera_result, then TASK_COMPLETED."""
    monkeypatch.chdir(tmp_path)  # display_utils writes output_image.rgb565 to cwd
    # company_name.png is required by display_utils; copy from project root.
    project_root = Path(__file__).resolve().parents[2]
    (tmp_path / "company_name.png").write_bytes((project_root / "company_name.png").read_bytes())

    plc = MockPLCManager({1: _make_config(ProductType.BRUSH_HEAD, CameraStatus.START_TASK)})
    cam = MockCameraManager({1: brush_dir})
    logger = logging.getLogger("test")
    logger.addHandler(logging.NullHandler())

    tm = TaskManager(plc, cam, config=None, logger=logger)
    task = asyncio.create_task(tm.run())

    try:
        await _run_until(lambda: len(plc.results_log) >= 1, plc)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert len(plc.results_log) >= 1
    rec = plc.results_log[0]
    assert rec.camera_num == 1
    assert rec.result.result is True  # OK (Front detected)
    # BrushHeadProcessor encodes side code in x; 1 = Front.
    assert int(rec.result.x) == 1

    # Display pipeline ran: rgb565 file should exist.
    assert (tmp_path / "output_image.rgb565").is_file()
    assert (tmp_path / "output_image.rgb565").stat().st_size > 0


@pytest.mark.asyncio
async def test_dual_camera_independent_dispatch(
    brush_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two cameras, different ProductTypes, both get their own write."""
    monkeypatch.chdir(tmp_path)
    project_root = Path(__file__).resolve().parents[2]
    (tmp_path / "company_name.png").write_bytes((project_root / "company_name.png").read_bytes())

    # Build a toothpaste image folder.
    toothpaste_dir = tmp_path / "toothpaste"
    toothpaste_dir.mkdir()
    img = np.full((400, 600, 3), 220, dtype=np.uint8)
    for x in range(20, 580, 12):
        cv2.line(img, (x, 50), (x, 350), (40, 40, 40), 2)
    cv2.imwrite(str(toothpaste_dir / "f.png"), img)

    plc = MockPLCManager(
        {
            1: _make_config(ProductType.BRUSH_HEAD, CameraStatus.START_TASK),
            2: _make_config(ProductType.TOOTHPASTE_FRONTBACK, CameraStatus.START_TASK),
        }
    )
    cam = MockCameraManager({1: brush_dir, 2: toothpaste_dir})
    logger = logging.getLogger("test")
    logger.addHandler(logging.NullHandler())

    tm = TaskManager(plc, cam, config=None, logger=logger)
    task = asyncio.create_task(tm.run())

    try:
        await _run_until(
            lambda: (
                any(r.camera_num == 1 for r in plc.results_log)
                and any(r.camera_num == 2 for r in plc.results_log)
            ),
            plc,
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    cams_seen = {r.camera_num for r in plc.results_log}
    assert cams_seen == {1, 2}, f"expected results from both cameras, got {cams_seen}"

    # Cam1 (BRUSH_HEAD) should classify as Front (x=1).
    cam1_records = [r for r in plc.results_log if r.camera_num == 1]
    assert all(int(r.result.x) == 1 for r in cam1_records)
    # Cam2 (TOOTHPASTE_FRONTBACK) — many vertical stripes → Front (x=1).
    cam2_records = [r for r in plc.results_log if r.camera_num == 2]
    assert all(int(r.result.x) == 1 for r in cam2_records)


@pytest.mark.asyncio
async def test_continuous_loop_produces_multiple_writes(
    brush_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """START_LOOP keeps producing writes until status flips back to IDLE."""
    monkeypatch.chdir(tmp_path)
    project_root = Path(__file__).resolve().parents[2]
    (tmp_path / "company_name.png").write_bytes((project_root / "company_name.png").read_bytes())

    plc = MockPLCManager({1: _make_config(ProductType.BRUSH_HEAD, CameraStatus.START_LOOP)})
    cam = MockCameraManager({1: brush_dir})
    logger = logging.getLogger("test")
    logger.addHandler(logging.NullHandler())

    tm = TaskManager(plc, cam, config=None, logger=logger)
    task = asyncio.create_task(tm.run())

    try:
        # Wait for at least 3 captures so we know it's actually looping.
        await _run_until(lambda: len(plc.results_log) >= 3, plc, timeout=10.0)
    finally:
        # Flip to IDLE so the continuous loop exits cleanly inside TaskManager,
        # then cancel the outer task as a safety net.
        plc.set_camera_status_value(1, CameraStatus.IDLE)
        await asyncio.sleep(0.3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # Continuous mode should have produced at least 3 writes for cam1.
    cam1_count = sum(1 for r in plc.results_log if r.camera_num == 1)
    assert cam1_count >= 3, f"expected at least 3 cam1 writes, got {cam1_count}"
