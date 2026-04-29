"""End-to-end tests for tools/simulate.py — the no-hardware CLI.

We invoke simulate.py as a subprocess to mirror real CLI usage (and catch
argparse / sys.path issues) on synthetic images that exercise each
ProductType.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SIMULATE = REPO_ROOT / "tools" / "simulate.py"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SIMULATE), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
    )


@pytest.fixture
def brush_image(tmp_path: Path) -> Path:
    """Synthetic brush head — upper half denser → Front."""
    img = np.full((600, 800, 3), 230, dtype=np.uint8)
    rng = np.random.default_rng(seed=42)
    for _ in range(80):
        x = int(rng.integers(110, 690))
        y = int(rng.integers(205, 295))
        cv2.circle(img, (x, y), 4, (40, 40, 40), -1)
    for _ in range(20):
        x = int(rng.integers(110, 690))
        y = int(rng.integers(305, 395))
        cv2.circle(img, (x, y), 4, (40, 40, 40), -1)
    path = tmp_path / "brush.png"
    cv2.imwrite(str(path), img)
    return path


@pytest.fixture
def toothpaste_image(tmp_path: Path) -> Path:
    """Synthetic toothpaste — many vertical stripes → Front."""
    img = np.full((400, 600, 3), 220, dtype=np.uint8)
    for x in range(20, 580, 12):
        cv2.line(img, (x, 50), (x, 350), (40, 40, 40), 2)
    path = tmp_path / "toothpaste.png"
    cv2.imwrite(str(path), img)
    return path


def test_simulate_brush_head_single_image(brush_image: Path, tmp_path: Path) -> None:
    out = tmp_path / "result.png"
    proc = _run(
        [
            "--product-type",
            "BRUSH_HEAD",
            "--image",
            str(brush_image),
            "--out",
            str(out),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    assert "result=OK" in proc.stdout
    assert out.is_file()


def test_simulate_toothpaste_single_image(toothpaste_image: Path) -> None:
    proc = _run(
        [
            "--product-type",
            "TOOTHPASTE_FRONTBACK",
            "--image",
            str(toothpaste_image),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    assert "result=OK" in proc.stdout


def test_simulate_height_check_single_image(tmp_path: Path) -> None:
    img = np.zeros((500, 600, 3), dtype=np.uint8)
    img[100:250, :, 0] = 200  # blue channel, max-Y ≈ 249 → state 1 (OK)
    image_path = tmp_path / "height.png"
    cv2.imwrite(str(image_path), img)

    proc = _run(
        [
            "--product-type",
            "HEIGHT_CHECK",
            "--image",
            str(image_path),
            "--param",
            "channel=2",
            "--param",
            "pixel_threshold=100",
            "--param",
            "decision_threshold=300",
        ]
    )
    assert proc.returncode == 0, proc.stderr
    assert "result=OK" in proc.stdout


def test_simulate_rejects_unknown_param(brush_image: Path) -> None:
    proc = _run(
        [
            "--product-type",
            "BRUSH_HEAD",
            "--image",
            str(brush_image),
            "--param",
            "no_such_param=42",
        ]
    )
    assert proc.returncode != 0
    assert "Unknown parameter" in proc.stderr or "Unknown parameter" in proc.stdout


def test_simulate_folder_mode_summarizes(brush_image: Path, tmp_path: Path) -> None:
    # Drop a couple of frames into a folder.
    folder = tmp_path / "frames"
    folder.mkdir()
    for i in range(3):
        (folder / f"copy_{i}.png").write_bytes(brush_image.read_bytes())

    proc = _run(
        [
            "--product-type",
            "BRUSH_HEAD",
            "--folder",
            str(folder),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    assert "Summary: 3 images" in proc.stdout
    assert "OK=3" in proc.stdout


def test_simulate_folder_mode_json_summary(brush_image: Path, tmp_path: Path) -> None:
    folder = tmp_path / "frames"
    folder.mkdir()
    for i in range(2):
        (folder / f"f_{i}.png").write_bytes(brush_image.read_bytes())

    proc = _run(
        [
            "--product-type",
            "BRUSH_HEAD",
            "--folder",
            str(folder),
            "--json-summary",
        ]
    )
    assert proc.returncode == 0, proc.stderr

    json_lines = [line for line in proc.stdout.splitlines() if line.startswith("{") and "result" in line]
    assert len(json_lines) == 2
    parsed = [json.loads(line) for line in json_lines]
    for entry in parsed:
        assert entry["result"] == "OK"
        assert int(entry["center_x"]) == 1  # Front
