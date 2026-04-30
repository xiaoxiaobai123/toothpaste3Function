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
