"""Tests for the simulation mocks (camera/mock.py + plc/mock.py)."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from camera.mock import MockCameraManager
from plc.enums import CameraStatus, ProductType
from plc.mock import MockCameraConfig, MockPLCManager


@pytest.fixture
def image_dir(tmp_path: Path) -> Path:
    """Create a folder with three small PNGs of distinguishable colors."""
    for i, color in enumerate([(0, 0, 200), (0, 200, 0), (200, 0, 0)]):
        img = np.full((100, 100, 3), color, dtype=np.uint8)
        cv2.imwrite(str(tmp_path / f"frame_{i:02d}.png"), img)
    return tmp_path


def test_mock_camera_lists_active_cameras(image_dir: Path) -> None:
    mgr = MockCameraManager({1: image_dir, 2: image_dir})
    assert mgr.active_camera_nums() == [1, 2]


def test_mock_camera_skips_empty_dirs(tmp_path: Path, image_dir: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    mgr = MockCameraManager({1: image_dir, 2: empty})
    assert mgr.active_camera_nums() == [1]


def test_mock_camera_cycles_images(image_dir: Path) -> None:
    mgr = MockCameraManager({1: image_dir})
    # 3 frames in folder; capturing 5 times must cycle.
    seen = [mgr.capture_image(1) for _ in range(5)]
    assert all(img is not None for img in seen)
    # Frames should cycle: image #4 == image #1 (both are frame_00.png).
    assert np.array_equal(seen[0], seen[3])
    assert np.array_equal(seen[1], seen[4])


def test_mock_camera_no_op_controls_succeed(image_dir: Path) -> None:
    mgr = MockCameraManager({1: image_dir})
    assert mgr.set_exposure(1, 7000) is True
    assert mgr.get_exposure_time(1) == 7000.0
    assert mgr.flush_one_frame(1) is True
    assert mgr.update_trigger_mode(1, is_hardware_trigger=True) is True
    assert mgr.get_trigger_source(1) == 0
    assert mgr.update_trigger_mode(1, is_hardware_trigger=False) is True
    assert mgr.get_trigger_source(1) == MockCameraManager.SOFTWARE_TRIGGER_SOURCE


def test_mock_plc_returns_configured_settings() -> None:
    cfg = MockCameraConfig(
        product_type=ProductType.BRUSH_HEAD,
        raw_config=tuple([0, 0, 0, 0, 3, 15, 31, 8, 20, 500, 0, 0, 0, 0, 15, 35, 0, 0]),
    )
    plc = MockPLCManager({1: cfg})

    settings = plc.read_camera_settings(1)
    assert settings["product_type"] == ProductType.BRUSH_HEAD
    assert settings["raw_config"][5] == 15

    # Unconfigured camera returns empty dict (mirrors real PLC failure path).
    assert plc.read_camera_settings(2) == {}


def test_mock_plc_records_writes() -> None:
    from plc.enums import CameraResult

    plc = MockPLCManager()
    plc.write_camera_result(1, CameraResult(x=1.0, y=0.0, angle=0.0, result=True, area=0, circularity=0.0))
    plc.write_camera_result(2, CameraResult(x=2.0, y=0.0, angle=0.0, result=False, area=0, circularity=0.0))

    assert len(plc.results_log) == 2
    assert plc.results_log[0].camera_num == 1
    assert plc.results_log[0].result.x == 1.0
    assert plc.results_log[1].result.result is False


def test_mock_plc_status_transitions() -> None:
    plc = MockPLCManager({1: MockCameraConfig()})

    plc.set_camera_status_value(1, CameraStatus.START_TASK)
    assert plc.read_camera_settings(1)["status"] == CameraStatus.START_TASK

    plc.write_camera_status(1, CameraStatus.TASK_COMPLETED)
    assert plc.read_camera_settings(1)["status"] == CameraStatus.TASK_COMPLETED


def test_mock_plc_heartbeat_toggles() -> None:
    plc = MockPLCManager()
    assert plc.system_heartbeat == 0
    plc.toggle_system_heartbeat()
    assert plc.system_heartbeat == 1
    plc.toggle_system_heartbeat()
    assert plc.system_heartbeat == 0
