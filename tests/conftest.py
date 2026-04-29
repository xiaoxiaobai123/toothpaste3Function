"""Shared pytest fixtures.

In-progress: full mock-camera/PLC fixtures arrive in P4 along with
tools/simulate.py and tests/scenarios/*.yml. This file exists today
only to give pytest a place to live and to verify importability.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is importable when pytest is invoked from any cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
