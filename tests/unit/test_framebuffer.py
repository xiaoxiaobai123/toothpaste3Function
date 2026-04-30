"""Tests for core/framebuffer.get_framebuffer_resolution.

The function ioctl's /dev/fb0 — a real device that doesn't exist on
Windows / macOS / headless CI runners. These tests verify the graceful
fallback path (returns None) and the cache behaviour, then mock the open
+ ioctl pair to exercise the success path.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core import framebuffer


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Each test starts with a fresh module-level cache."""
    framebuffer.reset_cache_for_tests()


# ----------------------------------------------------------------------
# Fallback path — no /dev/fb0
# ----------------------------------------------------------------------
def test_returns_none_when_fb0_missing() -> None:
    """Windows / macOS / headless CI: /dev/fb0 doesn't exist. Function
    must not raise — pre-scaling falls back to no-op, image_updater on
    the NanoPi handles whatever-size source via its slow path."""
    # On Windows the open() will raise FileNotFoundError; on Linux without
    # fb hardware it'll raise the same. Either way, get_* returns None.
    result = framebuffer.get_framebuffer_resolution()
    assert result is None


# ----------------------------------------------------------------------
# Cache behaviour
# ----------------------------------------------------------------------
def test_result_is_cached_after_first_call() -> None:
    """Second call should NOT touch open() / ioctl() — the cached value
    (None or a tuple) is returned directly."""
    framebuffer.get_framebuffer_resolution()  # primes cache
    with patch("builtins.open", side_effect=AssertionError("should not be called")):
        framebuffer.get_framebuffer_resolution()  # must not invoke open()


def test_reset_cache_forces_re_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """First call gets the natural cache value (None on Windows / no-fb).
    After reset, a fake-success patch should be observed — proving the
    function actually re-tried instead of returning the previously
    cached None."""
    first = framebuffer.get_framebuffer_resolution()
    # On Windows fcntl is None and first is None; on Linux without fb0
    # it's also None. Either way:
    assert first is None

    framebuffer.reset_cache_for_tests()
    _install_fake_fcntl(monkeypatch, 1280, 720)
    second = framebuffer.get_framebuffer_resolution()
    assert second == (1280, 720), "reset_cache_for_tests should force a re-read"


# ----------------------------------------------------------------------
# Sanity-clamp on bogus values
# ----------------------------------------------------------------------
def _install_fake_fcntl(monkeypatch: pytest.MonkeyPatch, xres: int, yres: int) -> None:
    """Install a fake fcntl module + open() that simulate a kernel ioctl
    response. Used for the success / sanity-check tests so they pass
    on Windows (no real fcntl) too."""
    import sys
    import types

    def fake_ioctl(_fd: int, _req: int, info: object) -> int:
        info.xres = xres  # type: ignore[attr-defined]
        info.yres = yres  # type: ignore[attr-defined]
        return 0

    fake_mod = types.SimpleNamespace(ioctl=fake_ioctl)
    monkeypatch.setattr("core.framebuffer.fcntl", fake_mod)

    class _FakeFile:
        def __enter__(self) -> _FakeFile: return self
        def __exit__(self, *args: object) -> None: return None
        def fileno(self) -> int: return 99

    monkeypatch.setattr("builtins.open", lambda *a, **kw: _FakeFile())
    sys.modules.pop("_dummy_fcntl_helper", None)  # silence unused import


def test_zero_dimensions_treated_as_undetectable(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ioctl somehow returns 0×0 (e.g. driver in an unexpected state),
    we don't want to pre-scale to a degenerate frame — return None and
    let image_updater's fallback path handle it."""
    _install_fake_fcntl(monkeypatch, 0, 0)
    assert framebuffer.get_framebuffer_resolution() is None


def test_huge_dimensions_treated_as_undetectable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bogus 100k×100k report would create a multi-GB pre-scaled frame.
    Reject anything > 8K to keep memory bounded."""
    _install_fake_fcntl(monkeypatch, 100_000, 100_000)
    assert framebuffer.get_framebuffer_resolution() is None


# ----------------------------------------------------------------------
# Success path — mocked ioctl returns plausible 1920×1080
# ----------------------------------------------------------------------
def test_returns_xres_yres_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_fcntl(monkeypatch, 1920, 1080)
    assert framebuffer.get_framebuffer_resolution() == (1920, 1080)
