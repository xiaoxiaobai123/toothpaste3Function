"""Shared pytest fixtures.

In-progress: full mock-camera/PLC fixtures arrive in P4 along with
tools/simulate.py and tests/scenarios/*.yml. This file exists today
only to give pytest a place to live and to verify importability.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the project root is importable when pytest is invoked from any cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _reset_framebuffer_cache() -> None:
    """Force core.framebuffer to re-detect on every test.

    The framebuffer-resolution cache is module-scoped and would otherwise
    leak across tests — e.g. test_framebuffer.py installs a fake ioctl
    returning (1280, 720), which would then be observed by the legacy
    display tests as a "real" framebuffer and cause them to pre-scale
    their composed output to that size, breaking dimension assertions.
    """
    from core import framebuffer
    framebuffer.reset_cache_for_tests()
