"""Tests for tools/switch_protocol.py — the camera-selection logic.

We don't exercise the systemctl/argparse paths (those need root + a real
service); instead we unit-test apply_camera_selection on dict inputs to
verify config.json mutations are correct, idempotent, and tolerant of
single-camera sites.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# tools/ is not a Python package, so load the script as an ad-hoc module.
_PATH = Path(__file__).resolve().parents[2] / "tools" / "switch_protocol.py"
_spec = importlib.util.spec_from_file_location("switch_protocol", _PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
apply_camera_selection = _mod.apply_camera_selection
CAMERA_PRESETS = _mod.CAMERA_PRESETS


# --------------------------------------------------------------------------- #
# Helpers for exercising _do_protocol without touching systemctl / disk.
# We monkeypatch _backup_config + write_config_atomic + restart_service +
# tail_protocol_line so the function only mutates `cfg`.
# --------------------------------------------------------------------------- #
def _stub_protocol_io(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Return a captured-print list; mock out all side-effects.

    _do_protocol normally writes to /home/pi/config.json, restarts a
    service, and tails a log file. None of that is appropriate in tests,
    so replace each with a no-op (or a recorder).
    """
    monkeypatch.setattr(_mod, "_backup_config", lambda: None)
    monkeypatch.setattr(_mod, "write_config_atomic", lambda _cfg: None)
    monkeypatch.setattr(_mod, "restart_service", lambda: 0)
    monkeypatch.setattr(_mod, "tail_protocol_line", lambda timeout_s=5.0: None)
    return []


def _two_cam_cfg(cam1: bool = True, cam2: bool = True) -> dict:
    return {
        "plc_protocol": "v2_unified",
        "cameras": {
            "camera1": {"ip": "192.168.2.10", "enabled": cam1},
            "camera2": {"ip": "192.168.3.10", "enabled": cam2},
        },
    }


# --------------------------------------------------------------------------- #
# Happy-path: each preset flips the right camera(s).
# --------------------------------------------------------------------------- #
def test_select_cam1_disables_cam2() -> None:
    cfg = _two_cam_cfg(True, True)
    changes = apply_camera_selection(cfg, "cam1")
    assert cfg["cameras"]["camera1"]["enabled"] is True
    assert cfg["cameras"]["camera2"]["enabled"] is False
    assert any("camera2.enabled: True -> False" in c for c in changes)


def test_select_cam2_disables_cam1() -> None:
    cfg = _two_cam_cfg(True, True)
    changes = apply_camera_selection(cfg, "cam2")
    assert cfg["cameras"]["camera1"]["enabled"] is False
    assert cfg["cameras"]["camera2"]["enabled"] is True
    assert any("camera1.enabled: True -> False" in c for c in changes)


def test_select_both_re_enables_disabled_cam() -> None:
    cfg = _two_cam_cfg(True, False)
    changes = apply_camera_selection(cfg, "both")
    assert cfg["cameras"]["camera1"]["enabled"] is True
    assert cfg["cameras"]["camera2"]["enabled"] is True
    assert any("camera2.enabled: False -> True" in c for c in changes)


# --------------------------------------------------------------------------- #
# Idempotency: nothing recorded if state already matches the preset.
# --------------------------------------------------------------------------- #
def test_no_op_when_already_in_target_state() -> None:
    cfg = _two_cam_cfg(True, False)
    changes = apply_camera_selection(cfg, "cam1")
    assert changes == []
    assert cfg["cameras"]["camera1"]["enabled"] is True
    assert cfg["cameras"]["camera2"]["enabled"] is False


def test_no_op_when_already_both() -> None:
    cfg = _two_cam_cfg(True, True)
    changes = apply_camera_selection(cfg, "both")
    assert changes == []


# --------------------------------------------------------------------------- #
# Default-true: a missing 'enabled' field counts as enabled and isn't
# spuriously rewritten when the preset agrees.
# --------------------------------------------------------------------------- #
def test_missing_enabled_field_counts_as_true() -> None:
    cfg = {
        "cameras": {
            "camera1": {"ip": "1.2.3.4"},                       # no enabled field
            "camera2": {"ip": "5.6.7.8", "enabled": True},
        }
    }
    changes = apply_camera_selection(cfg, "cam1")
    # camera1 was already (default-)True, so no diff line and no field added.
    assert "enabled" not in cfg["cameras"]["camera1"]
    assert all("camera1" not in c for c in changes)
    # camera2 flipped True -> False.
    assert cfg["cameras"]["camera2"]["enabled"] is False
    assert any("camera2.enabled: True -> False" in c for c in changes)


# --------------------------------------------------------------------------- #
# Single-camera site: missing camera2 entry is silently skipped.
# --------------------------------------------------------------------------- #
def test_single_camera_site_skips_missing_entry() -> None:
    cfg = {
        "cameras": {
            "camera1": {"ip": "1.2.3.4", "enabled": True},
        }
    }
    changes = apply_camera_selection(cfg, "cam2")
    # camera1 turned off, camera2 stays absent (not magically created).
    assert cfg["cameras"]["camera1"]["enabled"] is False
    assert "camera2" not in cfg["cameras"]
    assert any("camera1.enabled: True -> False" in c for c in changes)


def test_empty_cameras_dict() -> None:
    cfg = {"cameras": {}}
    changes = apply_camera_selection(cfg, "both")
    assert changes == []
    assert cfg["cameras"] == {}


def test_missing_cameras_key_creates_empty_dict() -> None:
    cfg: dict = {"plc_protocol": "v2_unified"}
    changes = apply_camera_selection(cfg, "both")
    assert changes == []
    assert cfg["cameras"] == {}


# --------------------------------------------------------------------------- #
# Input validation.
# --------------------------------------------------------------------------- #
def test_unknown_selection_raises() -> None:
    cfg = _two_cam_cfg()
    with pytest.raises(ValueError, match="unknown camera selection"):
        apply_camera_selection(cfg, "cam99")


def test_presets_cover_expected_targets() -> None:
    assert set(CAMERA_PRESETS.keys()) == {"cam1", "cam2", "both"}
    # Each preset references both camera1 and camera2 (so 'both' enables both,
    # 'cam1' explicitly disables camera2, etc.).
    for preset in CAMERA_PRESETS.values():
        assert set(preset.keys()) == {"camera1", "camera2"}


# --------------------------------------------------------------------------- #
# Non-mutation: cfg should not gain or lose unrelated keys.
# --------------------------------------------------------------------------- #
def test_unrelated_cfg_keys_untouched() -> None:
    cfg = {
        "plc_protocol": "legacy_fronback",
        "plc": {"ip": "192.168.1.10"},
        "cameras": {
            "camera1": {"ip": "192.168.2.10", "enabled": True, "host_lan": "192.168.2.123"},
            "camera2": {"ip": "192.168.3.10", "enabled": True, "host_lan": "192.168.3.123"},
        },
    }
    apply_camera_selection(cfg, "cam1")
    assert cfg["plc_protocol"] == "legacy_fronback"
    assert cfg["plc"] == {"ip": "192.168.1.10"}
    assert cfg["cameras"]["camera1"]["host_lan"] == "192.168.2.123"
    assert cfg["cameras"]["camera2"]["host_lan"] == "192.168.3.123"
    assert cfg["cameras"]["camera2"]["ip"] == "192.168.3.10"


# --------------------------------------------------------------------------- #
# _do_protocol: switching INTO legacy auto-enables both cameras (legacy
# fronback can't run with only one camera). Switching to v2 leaves them.
# --------------------------------------------------------------------------- #
def test_legacy_switch_auto_enables_both_cameras(monkeypatch: pytest.MonkeyPatch) -> None:
    """Common scenario: tester left v2 + cam1 only, switches back to legacy."""
    _stub_protocol_io(monkeypatch)
    cfg = _two_cam_cfg(cam1=True, cam2=False)
    cfg["plc_protocol"] = "v2_unified"

    rc = _mod._do_protocol(cfg, "legacy", no_restart=True)

    assert rc == 0
    assert cfg["plc_protocol"] == "legacy_fronback"
    assert cfg["cameras"]["camera1"]["enabled"] is True
    assert cfg["cameras"]["camera2"]["enabled"] is True


def test_legacy_no_op_when_already_legacy_and_both_cameras(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_protocol_io(monkeypatch)
    cfg = _two_cam_cfg(cam1=True, cam2=True)
    cfg["plc_protocol"] = "legacy_fronback"

    rc = _mod._do_protocol(cfg, "legacy", no_restart=True)

    assert rc == 0
    assert cfg["plc_protocol"] == "legacy_fronback"
    assert cfg["cameras"]["camera1"]["enabled"] is True
    assert cfg["cameras"]["camera2"]["enabled"] is True


def test_legacy_fixes_single_cam_state_even_when_protocol_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If somehow protocol is already legacy but cam2 is off, running
    `legacy` should still re-enable cam2 — both must be on for legacy."""
    _stub_protocol_io(monkeypatch)
    cfg = _two_cam_cfg(cam1=True, cam2=False)
    cfg["plc_protocol"] = "legacy_fronback"

    rc = _mod._do_protocol(cfg, "legacy", no_restart=True)

    assert rc == 0
    assert cfg["cameras"]["camera2"]["enabled"] is True


def test_v2_switch_does_not_touch_cameras(monkeypatch: pytest.MonkeyPatch) -> None:
    """Going v2 keeps whatever single-cam selection the user already has."""
    _stub_protocol_io(monkeypatch)
    cfg = _two_cam_cfg(cam1=True, cam2=False)
    cfg["plc_protocol"] = "legacy_fronback"

    rc = _mod._do_protocol(cfg, "v2", no_restart=True)

    assert rc == 0
    assert cfg["plc_protocol"] == "v2_unified"
    assert cfg["cameras"]["camera1"]["enabled"] is True
    assert cfg["cameras"]["camera2"]["enabled"] is False  # NOT auto-enabled


def test_v2_no_op_when_already_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_protocol_io(monkeypatch)
    cfg = _two_cam_cfg(cam1=True, cam2=False)
    cfg["plc_protocol"] = "v2_unified"

    rc = _mod._do_protocol(cfg, "v2", no_restart=True)

    assert rc == 0
    # Still single-cam, unchanged.
    assert cfg["cameras"]["camera2"]["enabled"] is False


# --------------------------------------------------------------------------- #
# Hardware ROI: pure coordinate math.
# --------------------------------------------------------------------------- #
def test_translate_algo_roi_basic_offset_subtraction() -> None:
    """ROI {290..990, 150..650} on a 1280x800 frame, hw ROI offset (240, 100)
    width/height (800, 600) -> {50..750, 50..550}."""
    out = _mod.translate_algo_roi(
        {"x1": 290, "y1": 150, "x2": 990, "y2": 650},
        offset_x=240, offset_y=100, width=800, height=600,
    )
    assert out == {"x1": 50, "y1": 50, "x2": 750, "y2": 550}


def test_translate_algo_roi_clamps_negative_to_zero() -> None:
    """ROI start in the cropped-out region (negative after subtract) -> 0."""
    out = _mod.translate_algo_roi(
        {"x1": 100, "y1": 100, "x2": 700, "y2": 500},
        offset_x=240, offset_y=200, width=800, height=600,
    )
    # x1: 100-240 = -140 -> clamped to 0
    # y1: 100-200 = -100 -> clamped to 0
    assert out["x1"] == 0
    assert out["y1"] == 0
    # x2/y2 valid
    assert out["x2"] == 460
    assert out["y2"] == 300


def test_translate_algo_roi_clamps_to_width_height() -> None:
    """ROI extending past the cropped region -> clamped to width/height."""
    out = _mod.translate_algo_roi(
        {"x1": 300, "y1": 200, "x2": 9999, "y2": 9999},
        offset_x=240, offset_y=100, width=800, height=600,
    )
    assert out["x2"] == 800
    assert out["y2"] == 600


def test_translate_algo_roi_zero_offset_is_identity_under_clamp() -> None:
    """offset=0 keeps coords; only clamp may apply."""
    out = _mod.translate_algo_roi(
        {"x1": 100, "y1": 50, "x2": 700, "y2": 500},
        offset_x=0, offset_y=0, width=800, height=600,
    )
    assert out == {"x1": 100, "y1": 50, "x2": 700, "y2": 500}


# --------------------------------------------------------------------------- #
# Hardware ROI: cfg dict mutation.
# --------------------------------------------------------------------------- #
def test_apply_hardware_roi_writes_full_dict() -> None:
    cfg = _two_cam_cfg()
    line = _mod.apply_hardware_roi(
        cfg, 1, {"width": 800, "height": 600, "offset_x": 240, "offset_y": 100}
    )
    assert cfg["cameras"]["camera1"]["roi"] == {
        "width": 800, "height": 600, "offset_x": 240, "offset_y": 100,
    }
    assert "(none)" in line  # was no roi before


def test_apply_hardware_roi_replaces_existing() -> None:
    cfg = _two_cam_cfg()
    cfg["cameras"]["camera1"]["roi"] = {
        "width": 1024, "height": 768, "offset_x": 0, "offset_y": 0
    }
    line = _mod.apply_hardware_roi(
        cfg, 1, {"width": 800, "height": 600, "offset_x": 240, "offset_y": 100}
    )
    assert cfg["cameras"]["camera1"]["roi"]["width"] == 800
    assert "1024" in line and "800" in line


def test_apply_hardware_roi_unconfigured_camera_raises() -> None:
    cfg = {"cameras": {"camera1": {"ip": "1.2.3.4"}}}
    with pytest.raises(KeyError, match="camera2"):
        _mod.apply_hardware_roi(cfg, 2, {"width": 800, "height": 600})


def test_apply_hardware_roi_rejects_non_4_aligned_width() -> None:
    cfg = _two_cam_cfg()
    with pytest.raises(ValueError, match="width=801"):
        _mod.apply_hardware_roi(cfg, 1, {"width": 801, "height": 600})


def test_apply_hardware_roi_rejects_non_4_aligned_offset() -> None:
    cfg = _two_cam_cfg()
    with pytest.raises(ValueError, match="offset_x=241"):
        _mod.apply_hardware_roi(
            cfg, 1, {"width": 800, "height": 600, "offset_x": 241, "offset_y": 0}
        )


def test_apply_hardware_roi_rejects_zero_dimensions() -> None:
    cfg = _two_cam_cfg()
    with pytest.raises(ValueError, match="positive"):
        _mod.apply_hardware_roi(cfg, 1, {"width": 0, "height": 600})


def test_reset_hardware_roi_removes_field() -> None:
    cfg = _two_cam_cfg()
    cfg["cameras"]["camera1"]["roi"] = {
        "width": 800, "height": 600, "offset_x": 0, "offset_y": 0
    }
    line = _mod.reset_hardware_roi(cfg, 1)
    assert line is not None
    assert "roi" not in cfg["cameras"]["camera1"]


def test_reset_hardware_roi_returns_none_when_already_absent() -> None:
    cfg = _two_cam_cfg()
    assert _mod.reset_hardware_roi(cfg, 1) is None


# --------------------------------------------------------------------------- #
# Algorithm ROI file IO + roundtrip.
# --------------------------------------------------------------------------- #
def _seed_algo_roi(base_dir: Path, ip: str, roi: dict[str, int]) -> Path:
    path = _mod.algo_roi_path(base_dir, ip)
    path.write_text(__import__("json").dumps(roi), encoding="utf-8")
    return path


def test_apply_algo_roi_translation_creates_snapshot_on_first_call(
    tmp_path: Path,
) -> None:
    ip = "192.168.2.10"
    full_frame_roi = {"x1": 290, "y1": 150, "x2": 990, "y2": 650}
    _seed_algo_roi(tmp_path, ip, full_frame_roi)

    hw_roi = {"width": 800, "height": 600, "offset_x": 240, "offset_y": 100}
    _mod.apply_algo_roi_translation(tmp_path, ip, hw_roi)

    snap_path = _mod.algo_roi_snapshot_path(tmp_path, ip)
    assert snap_path.is_file()
    snap = __import__("json").loads(snap_path.read_text(encoding="utf-8"))
    assert snap == full_frame_roi

    new = __import__("json").loads(_mod.algo_roi_path(tmp_path, ip).read_text(encoding="utf-8"))
    assert new == {"x1": 50, "y1": 50, "x2": 750, "y2": 550}


def test_apply_algo_roi_translation_reuses_snapshot_on_second_call(
    tmp_path: Path,
) -> None:
    """Second apply with different hw_roi should translate from the ORIGINAL
    full-frame coords, not from the already-translated coords (no offset
    stacking)."""
    ip = "192.168.2.10"
    _seed_algo_roi(tmp_path, ip, {"x1": 290, "y1": 150, "x2": 990, "y2": 650})

    # First apply.
    _mod.apply_algo_roi_translation(
        tmp_path, ip, {"width": 800, "height": 600, "offset_x": 240, "offset_y": 100}
    )
    # Second apply with bigger crop (different offset).
    _mod.apply_algo_roi_translation(
        tmp_path, ip, {"width": 1024, "height": 768, "offset_x": 100, "offset_y": 50}
    )

    new = __import__("json").loads(_mod.algo_roi_path(tmp_path, ip).read_text(encoding="utf-8"))
    # Computed from original (290, 150, 990, 650) - (100, 50)
    assert new == {"x1": 190, "y1": 100, "x2": 890, "y2": 600}


def test_apply_algo_roi_translation_missing_file_does_not_raise(
    tmp_path: Path,
) -> None:
    """Single-camera or fresh deployment with no ROI file -> graceful skip."""
    ip = "10.0.0.1"
    lines = _mod.apply_algo_roi_translation(
        tmp_path, ip, {"width": 800, "height": 600, "offset_x": 0, "offset_y": 0}
    )
    assert any("nothing to translate" in line for line in lines)
    # Did not create any files.
    assert not _mod.algo_roi_path(tmp_path, ip).exists()
    assert not _mod.algo_roi_snapshot_path(tmp_path, ip).exists()


def test_reset_algo_roi_translation_restores_from_snapshot(tmp_path: Path) -> None:
    ip = "192.168.2.10"
    full_frame_roi = {"x1": 290, "y1": 150, "x2": 990, "y2": 650}
    _seed_algo_roi(tmp_path, ip, full_frame_roi)

    # Apply, then reset.
    _mod.apply_algo_roi_translation(
        tmp_path, ip, {"width": 800, "height": 600, "offset_x": 240, "offset_y": 100}
    )
    _mod.reset_algo_roi_translation(tmp_path, ip)

    restored = __import__("json").loads(
        _mod.algo_roi_path(tmp_path, ip).read_text(encoding="utf-8")
    )
    assert restored == full_frame_roi
    # Snapshot deleted.
    assert not _mod.algo_roi_snapshot_path(tmp_path, ip).exists()


def test_reset_algo_roi_translation_no_snapshot_is_noop(tmp_path: Path) -> None:
    ip = "192.168.2.10"
    full_frame_roi = {"x1": 100, "y1": 100, "x2": 500, "y2": 500}
    _seed_algo_roi(tmp_path, ip, full_frame_roi)

    lines = _mod.reset_algo_roi_translation(tmp_path, ip)

    assert any("no .full_frame snapshot" in line for line in lines)
    untouched = __import__("json").loads(
        _mod.algo_roi_path(tmp_path, ip).read_text(encoding="utf-8")
    )
    assert untouched == full_frame_roi


# --------------------------------------------------------------------------- #
# _do_roi: integration of cfg mutation + filesystem.
# --------------------------------------------------------------------------- #
def _stub_roi_io(monkeypatch: pytest.MonkeyPatch, base_dir: Path) -> None:
    """Stub config / restart side-effects, redirect ROI file dir to tmp_path."""
    monkeypatch.setattr(_mod, "_backup_config", lambda: None)
    monkeypatch.setattr(_mod, "write_config_atomic", lambda _cfg: None)
    monkeypatch.setattr(_mod, "restart_service", lambda: 0)
    monkeypatch.setattr(_mod, "ALGO_ROI_DIR", base_dir)


def test_do_roi_apply_both_cameras_writes_two_roi_dicts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _stub_roi_io(monkeypatch, tmp_path)
    cfg = _two_cam_cfg()
    _seed_algo_roi(tmp_path, "192.168.2.10",
                   {"x1": 290, "y1": 150, "x2": 990, "y2": 650})
    _seed_algo_roi(tmp_path, "192.168.3.10",
                   {"x1": 290, "y1": 150, "x2": 990, "y2": 650})

    rc = _mod._do_roi(
        cfg, "both",
        {"width": 800, "height": 600, "offset_x": 240, "offset_y": 100},
        reset=False, translate_algo=True, no_restart=True,
    )

    assert rc == 0
    assert cfg["cameras"]["camera1"]["roi"]["width"] == 800
    assert cfg["cameras"]["camera2"]["roi"]["width"] == 800


def test_do_roi_reset_removes_roi_and_restores_algo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _stub_roi_io(monkeypatch, tmp_path)
    cfg = _two_cam_cfg()
    full = {"x1": 290, "y1": 150, "x2": 990, "y2": 650}
    _seed_algo_roi(tmp_path, "192.168.2.10", full)

    # First apply, then reset.
    _mod._do_roi(
        cfg, "cam1",
        {"width": 800, "height": 600, "offset_x": 240, "offset_y": 100},
        reset=False, translate_algo=True, no_restart=True,
    )
    _mod._do_roi(
        cfg, "cam1", None,
        reset=True, translate_algo=True, no_restart=True,
    )

    assert "roi" not in cfg["cameras"]["camera1"]
    restored = __import__("json").loads(
        _mod.algo_roi_path(tmp_path, "192.168.2.10").read_text(encoding="utf-8")
    )
    assert restored == full


def test_do_roi_skips_unconfigured_camera(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If config only has camera1, `roi both --width ...` should not crash —
    just apply to camera1 and skip camera2 with a note."""
    _stub_roi_io(monkeypatch, tmp_path)
    cfg = {"cameras": {"camera1": {"ip": "1.2.3.4", "enabled": True}}}

    rc = _mod._do_roi(
        cfg, "both",
        {"width": 800, "height": 600, "offset_x": 0, "offset_y": 0},
        reset=False, translate_algo=False, no_restart=True,
    )

    assert rc == 0
    assert cfg["cameras"]["camera1"]["roi"]["width"] == 800
    assert "camera2" not in cfg["cameras"]
