"""Tests for CameraManager auto-reinit on consecutive capture failures.

Real CameraManager.__init__ talks to MVS (which isn't available in tests),
so these tests bypass it: a thin subclass skips _initialize_cameras and lets
the test inject mock CameraBase objects directly into self.cameras.
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from camera.manager import CameraManager


# --------------------------------------------------------------------------- #
# Test scaffolding
# --------------------------------------------------------------------------- #
class _ManagerNoInit(CameraManager):
    """CameraManager with _initialize_cameras suppressed — used in unit tests
    so we don't need MVS / a real camera. Tests populate self.cameras directly."""

    def __init__(self) -> None:  # noqa: D401  — explicit override
        self.cameras = {}
        self.camera_locks = {}
        self._consecutive_failures = {}
        self._last_reinit_at = {}


class _FakeCamera:
    """Capture returns None for the first `fail_n` calls, then a real ndarray.
    Captures count is exposed for assertion."""

    def __init__(self, fail_n: int = 0) -> None:
        self._fail_remaining = fail_n
        self.captures = 0

    def capture_image(self, **_: object) -> np.ndarray | None:
        self.captures += 1
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            return None
        return np.zeros((4, 4, 3), dtype=np.uint8)


@pytest.fixture
def manager() -> _ManagerNoInit:
    m = _ManagerNoInit()
    # 0-cooldown by default — individual tests can override.
    m.AUTO_REINIT_COOLDOWN_S = 0.0
    return m


def _add_camera(mgr: _ManagerNoInit, num: int, cam: _FakeCamera) -> None:
    mgr.cameras[num] = cam  # type: ignore[assignment]
    mgr.camera_locks[num] = threading.Lock()


# --------------------------------------------------------------------------- #
# Threshold behaviour
# --------------------------------------------------------------------------- #
def test_no_reinit_below_threshold(manager: _ManagerNoInit) -> None:
    cam = _FakeCamera(fail_n=2)  # only 2 failures, threshold is 3
    _add_camera(manager, 1, cam)
    reinit_calls: list[int] = []
    manager.reinitialize_camera = lambda n: reinit_calls.append(n) or True  # type: ignore[assignment]

    manager.capture_image(1)  # fail 1
    manager.capture_image(1)  # fail 2
    manager.capture_image(1)  # success — counter reset

    assert reinit_calls == []


def test_reinit_fires_at_threshold(manager: _ManagerNoInit) -> None:
    cam = _FakeCamera(fail_n=10)
    _add_camera(manager, 1, cam)
    reinit_calls: list[int] = []
    manager.reinitialize_camera = lambda n: reinit_calls.append(n) or True  # type: ignore[assignment]

    manager.capture_image(1)  # fail 1
    manager.capture_image(1)  # fail 2
    assert reinit_calls == []
    manager.capture_image(1)  # fail 3 — triggers reinit
    assert reinit_calls == [1]


def test_success_resets_counter(manager: _ManagerNoInit) -> None:
    """A successful capture clears the failure count, so a later failure
    burst starts fresh and must hit the threshold again before reinit."""
    cam = _FakeCamera(fail_n=2)
    _add_camera(manager, 1, cam)
    reinit_calls: list[int] = []
    manager.reinitialize_camera = lambda n: reinit_calls.append(n) or True  # type: ignore[assignment]

    manager.capture_image(1)  # fail 1
    manager.capture_image(1)  # fail 2
    manager.capture_image(1)  # success
    # Reset internal counter; further failures need a new burst of 3.
    cam._fail_remaining = 2
    manager.capture_image(1)  # fail 1 (fresh burst)
    manager.capture_image(1)  # fail 2
    assert reinit_calls == []


def test_reinit_success_resets_counter(manager: _ManagerNoInit) -> None:
    """When auto-reinit returns True, the failure counter resets so the next
    failure starts a fresh burst rather than triggering reinit immediately."""
    cam = _FakeCamera(fail_n=10)
    _add_camera(manager, 1, cam)
    reinit_calls: list[int] = []

    def fake_reinit(num: int) -> bool:
        reinit_calls.append(num)
        return True  # claim success even though the cam keeps failing

    manager.reinitialize_camera = fake_reinit  # type: ignore[assignment]

    for _ in range(3):
        manager.capture_image(1)
    assert reinit_calls == [1]  # one reinit at the 3rd failure
    # After "successful" reinit the counter is cleared. Next 2 failures
    # should NOT trigger another reinit (we're at count=2, below threshold).
    manager.capture_image(1)
    manager.capture_image(1)
    assert reinit_calls == [1]
    manager.capture_image(1)  # 3rd post-reinit failure → reinit again
    assert reinit_calls == [1, 1]


# --------------------------------------------------------------------------- #
# Cooldown behaviour
# --------------------------------------------------------------------------- #
def test_cooldown_blocks_back_to_back_reinits(manager: _ManagerNoInit) -> None:
    """When reinit fails (camera genuinely offline), cooldown stops us from
    hammering reinit on every subsequent failed capture."""
    manager.AUTO_REINIT_COOLDOWN_S = 60.0  # large cooldown
    cam = _FakeCamera(fail_n=100)
    _add_camera(manager, 1, cam)
    reinit_calls: list[int] = []
    manager.reinitialize_camera = lambda n: reinit_calls.append(n) or False  # type: ignore[assignment]

    for _ in range(50):
        manager.capture_image(1)

    # Threshold hit at iteration 3 → 1 reinit attempt. Then cooldown blocks
    # all further attempts within the 60s window.
    assert len(reinit_calls) == 1


# --------------------------------------------------------------------------- #
# Multi-camera independence
# --------------------------------------------------------------------------- #
def test_per_camera_failure_counts_independent(manager: _ManagerNoInit) -> None:
    cam1 = _FakeCamera(fail_n=10)
    cam2 = _FakeCamera(fail_n=0)  # cam2 always succeeds
    _add_camera(manager, 1, cam1)
    _add_camera(manager, 2, cam2)
    reinit_calls: list[int] = []
    manager.reinitialize_camera = lambda n: reinit_calls.append(n) or True  # type: ignore[assignment]

    manager.capture_image(1)  # cam1 fail 1
    manager.capture_image(2)  # cam2 success — does not affect cam1's count
    manager.capture_image(1)  # cam1 fail 2
    manager.capture_image(2)  # cam2 success
    manager.capture_image(1)  # cam1 fail 3 → reinit only cam1

    assert reinit_calls == [1]
    assert 2 not in reinit_calls
