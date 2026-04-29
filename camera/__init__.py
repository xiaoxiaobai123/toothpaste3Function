"""Hikvision GigE Vision camera abstraction.

Submodules are imported lazily so unit tests and tools/simulate.py can
use camera.mock.MockCameraManager without dragging in the MVS SDK
(camera.base imports MvCameraControl_class, which is only available on
hosts that have installed MVS).
"""
