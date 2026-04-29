"""Runtime version banner.

Prefers the build-time-injected commit string written by the CI workflow
into VERSION_INFO; falls back to a live `git` invocation in dev; finally
returns "unknown" when running from a PyInstaller bundle.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Filled in by the CI workflow before PyInstaller build (see release.yml).
# Format example: "branch=main commit=abc1234"
VERSION_INFO: str | None = None


def get_version_info() -> str:
    if VERSION_INFO:
        return VERSION_INFO

    here = Path(__file__).resolve().parent.parent
    if not (here / ".git").exists():
        return "unknown (not a git repo or bundled binary)"

    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=here,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=here,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
        dirty = (
            subprocess.run(
                ["git", "diff-index", "--quiet", "HEAD"],
                cwd=here,
                stderr=subprocess.DEVNULL,
            ).returncode
            != 0
        )
        suffix = "+dirty" if dirty else ""
        return f"branch={branch} commit={commit}{suffix}"
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "unknown"


def workdir() -> str:
    return os.getcwd()
