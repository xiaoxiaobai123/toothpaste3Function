"""Tests for CameraBase._apply_roi — ROI lifecycle including the reset path.

Real CameraBase.init_camera talks to MVS (which isn't available in tests),
so these tests construct CameraBase directly (its __init__ only stores
fields, no SDK calls) and inject a mock self.cam that records SetIntValue /
GetIntValue calls.

Critical regression coverage: when self.roi is None we must STILL emit
SetIntValue Width/Height = sensor max + offset 0. Hikvision GigE persists
ROI state in firmware across MV_CC_OpenDevice cycles, so a no-op reset
leaves a previously-configured small ROI in effect (v0.3.12 bug — the
operator screen and algorithm both went out of sync after `roi --reset`).
"""

from __future__ import annotations

from camera.base import CameraBase


# --------------------------------------------------------------------------- #
# Mock SDK handle
# --------------------------------------------------------------------------- #
class _MockMvCamera:
    """Records every SetIntValue / GetIntValue. GetIntValue("Width" / "Height")
    populates the passed-in struct with `nMax` = SENSOR_MAX_W / H."""

    SENSOR_MAX_W = 1280   # MV-CA013-A0GC = 1280x1024 (130-万)
    SENSOR_MAX_H = 1024

    def __init__(self) -> None:
        self.set_calls: list[tuple[str, int]] = []
        self.get_calls: list[str] = []
        self.fail_get: bool = False

    def MV_CC_SetIntValue(self, name: str, value: int) -> int:
        self.set_calls.append((name, value))
        return 0

    def MV_CC_GetIntValue(self, name: str, struct_obj) -> int:
        self.get_calls.append(name)
        if self.fail_get:
            return 0x80000007  # MV_E_PARAMETER
        if name == "Width":
            struct_obj.nCurValue = 800   # echoes residual ROI; not what we care about
            struct_obj.nMax = self.SENSOR_MAX_W
        elif name == "Height":
            struct_obj.nCurValue = 600
            struct_obj.nMax = self.SENSOR_MAX_H
        return 0


def _camera_with_mock(roi: dict | None) -> tuple[CameraBase, _MockMvCamera]:
    cam = CameraBase("192.168.2.10", "192.168.2.123", camera_num=1, roi=roi)
    mock = _MockMvCamera()
    cam.cam = mock  # type: ignore[assignment]
    return cam, mock


# --------------------------------------------------------------------------- #
# Configured-ROI path: SetIntValue with the explicit width/height/offsets.
# --------------------------------------------------------------------------- #
def test_apply_roi_configured_writes_width_height_offsets() -> None:
    cam, mock = _camera_with_mock(
        {"width": 800, "height": 600, "offset_x": 240, "offset_y": 100}
    )
    assert cam._apply_roi() is True
    assert ("OffsetX", 0) in mock.set_calls   # zeroed FIRST
    assert ("OffsetY", 0) in mock.set_calls
    assert ("Width", 800) in mock.set_calls
    assert ("Height", 600) in mock.set_calls
    assert ("OffsetX", 240) in mock.set_calls
    assert ("OffsetY", 100) in mock.set_calls
    # No GetIntValue needed when ROI is explicit.
    assert mock.get_calls == []


def test_apply_roi_configured_zeros_offsets_before_setting_size() -> None:
    """OffsetX=0 must come BEFORE SetIntValue Width — Width.nMax is dynamic."""
    cam, mock = _camera_with_mock(
        {"width": 800, "height": 600, "offset_x": 240, "offset_y": 100}
    )
    cam._apply_roi()

    set_names = [n for n, _ in mock.set_calls]
    # First two writes must be the offset zeroing.
    assert set_names.index("OffsetX") < set_names.index("Width")
    assert set_names.index("OffsetY") < set_names.index("Height")
    # Final-offset writes come after Width/Height.
    final_ox = max(i for i, (n, v) in enumerate(mock.set_calls) if n == "OffsetX")
    final_oy = max(i for i, (n, v) in enumerate(mock.set_calls) if n == "OffsetY")
    assert final_ox > set_names.index("Width")
    assert final_oy > set_names.index("Height")


def test_apply_roi_configured_zero_offsets_writes_only_zero() -> None:
    """If config ROI has no offset, we should only write OffsetX/Y=0 once
    (the zero-first call), not redundantly write OffsetX=0 again at the end."""
    cam, mock = _camera_with_mock({"width": 800, "height": 600})
    cam._apply_roi()
    offsetx_writes = [v for n, v in mock.set_calls if n == "OffsetX"]
    offsety_writes = [v for n, v in mock.set_calls if n == "OffsetY"]
    assert offsetx_writes == [0]
    assert offsety_writes == [0]


# --------------------------------------------------------------------------- #
# Reset path (self.roi is None): the regression-fix critical path.
# --------------------------------------------------------------------------- #
def test_apply_roi_reset_queries_sensor_max_and_writes_it() -> None:
    """When self.roi is None we MUST explicitly set Width=sensor_max,
    Height=sensor_max so a previously-configured small ROI is cleared."""
    cam, mock = _camera_with_mock(roi=None)
    assert cam._apply_roi() is True

    # Must zero offsets first so Width.nMax / Height.nMax = sensor max.
    set_names = [n for n, _ in mock.set_calls]
    assert set_names.index("OffsetX") < set_names.index("Width")
    assert set_names.index("OffsetY") < set_names.index("Height")

    # GetIntValue must have been called for Width and Height to find
    # the sensor max (offset zeroed first).
    assert "Width" in mock.get_calls
    assert "Height" in mock.get_calls

    # Width/Height set to the sensor max returned by the mock.
    assert ("Width", _MockMvCamera.SENSOR_MAX_W) in mock.set_calls
    assert ("Height", _MockMvCamera.SENSOR_MAX_H) in mock.set_calls

    # No re-application of non-zero offsets in the reset path.
    offsetx_writes = [v for n, v in mock.set_calls if n == "OffsetX"]
    offsety_writes = [v for n, v in mock.set_calls if n == "OffsetY"]
    assert offsetx_writes == [0]
    assert offsety_writes == [0]


def test_apply_roi_reset_returns_false_when_get_max_fails() -> None:
    """If the camera rejects GetIntValue (driver/firmware bug), don't try
    SetIntValue with garbage — return False so init_camera bails."""
    cam, mock = _camera_with_mock(roi=None)
    mock.fail_get = True
    assert cam._apply_roi() is False
    # Should not have written Width/Height (we didn't know sensor max).
    set_names = [n for n, _ in mock.set_calls]
    assert "Width" not in set_names
    assert "Height" not in set_names


# --------------------------------------------------------------------------- #
# Idempotency / order edge cases (the same ROI is applied each init).
# --------------------------------------------------------------------------- #
def test_apply_roi_called_twice_is_idempotent() -> None:
    """Calling twice (e.g. across a re-init) emits the same writes both times,
    so a second pass corrects any drift accumulated outside our control."""
    cam, mock = _camera_with_mock(
        {"width": 800, "height": 600, "offset_x": 240, "offset_y": 100}
    )
    cam._apply_roi()
    first_round = list(mock.set_calls)
    mock.set_calls.clear()
    cam._apply_roi()
    second_round = mock.set_calls
    assert first_round == second_round
