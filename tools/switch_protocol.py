#!/usr/bin/env python3
"""Flip /home/pi/config.json plc_protocol or per-camera enabled flag, then
restart main.service.

Designed to run on the NanoPi itself via SSH; writes the config change
atomically and tails the next "plc_protocol:" log line so you can confirm
the new protocol actually took effect.

Usage (on the NanoPi, requires root for systemctl):
    sudo python3 tools/switch_protocol.py legacy
    sudo python3 tools/switch_protocol.py v2
    sudo python3 tools/switch_protocol.py status         # just show current

    # Toggle which cameras are active without changing protocol:
    sudo python3 tools/switch_protocol.py cameras cam1   # only camera1 enabled
    sudo python3 tools/switch_protocol.py cameras cam2   # only camera2 enabled
    sudo python3 tools/switch_protocol.py cameras both   # both enabled

    sudo python3 tools/switch_protocol.py legacy --no-restart        # rewrite, don't reboot
    sudo python3 tools/switch_protocol.py cameras cam1 --no-restart

Aliases:
    legacy            = legacy_fronback
    v2                = v2_unified

Note: switching INTO legacy auto-enables both cameras (legacy fronback's
side-by-side composite needs both). Switching to v2 leaves the camera
enabled flags alone — use `cameras cam1` etc. to toggle them explicitly.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path("/home/pi/config.json")
LOG = Path("/home/pi/my_app.log")

ALIASES = {
    "legacy": "legacy_fronback",
    "v2": "v2_unified",
    "legacy_fronback": "legacy_fronback",
    "v2_unified": "v2_unified",
}

# Each preset names the desired enabled state for camera1/camera2. Cameras
# absent from the config (e.g. single-camera sites) are silently skipped so
# this works on any deployment.
CAMERA_PRESETS: dict[str, dict[str, bool]] = {
    "cam1": {"camera1": True,  "camera2": False},
    "cam2": {"camera1": False, "camera2": True},
    "both": {"camera1": True,  "camera2": True},
}


def read_config() -> dict:
    if not CONFIG.is_file():
        sys.exit(f"error: {CONFIG} does not exist")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def show_status(cfg: dict) -> None:
    proto = cfg.get("plc_protocol", "(missing — defaults to v2_unified)")
    print(f"current plc_protocol: {proto}")
    cams = cfg.get("cameras", {})
    print(f"cameras configured  : {sorted(cams.keys())}")
    enabled = [k for k, v in cams.items() if v.get("enabled", True)]
    print(f"cameras enabled     : {sorted(enabled)}")
    plc_ip = cfg.get("plc", {}).get("ip", "(missing)")
    print(f"plc.ip              : {plc_ip}")


def write_config_atomic(cfg: dict) -> None:
    """Write via .tmp + os.replace so a crash mid-write can't corrupt the file."""
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(CONFIG)


def apply_camera_selection(cfg: dict, selection: str) -> list[str]:
    """Mutate cfg["cameras"][cameraN]["enabled"] in-place to match `selection`.

    Returns a list of human-readable change lines (empty if cfg already
    matches). Cameras absent from cfg are silently skipped — single-camera
    sites should still work.
    """
    if selection not in CAMERA_PRESETS:
        raise ValueError(f"unknown camera selection: {selection!r}")
    desired = CAMERA_PRESETS[selection]
    cameras = cfg.setdefault("cameras", {})
    changes: list[str] = []
    for cam_key, want in desired.items():
        if cam_key not in cameras:
            continue
        cur = bool(cameras[cam_key].get("enabled", True))
        if cur != want:
            cameras[cam_key]["enabled"] = want
            changes.append(f"{cam_key}.enabled: {cur} -> {want}")
    return changes


def restart_service() -> int:
    if os.geteuid() != 0:
        print("error: systemctl restart needs root. Re-run with sudo.")
        return 1
    print("restarting main.service ...")
    rc = subprocess.run(["systemctl", "restart", "main.service"]).returncode
    if rc != 0:
        print(f"error: systemctl restart returned {rc}")
        return rc
    return 0


def tail_protocol_line(timeout_s: float = 5.0) -> str | None:
    """Wait up to timeout_s for a fresh '[SYS] plc_protocol:' line in the log."""
    if not LOG.is_file():
        return None
    deadline = time.monotonic() + timeout_s
    last_seen = ""
    while time.monotonic() < deadline:
        # The log is loguru-formatted with embedded escape codes ("binary file"
        # to grep), so use python read.
        try:
            text = LOG.read_text(encoding="utf-8", errors="replace")
        except OSError:
            time.sleep(0.2)
            continue
        for line in text.splitlines()[::-1]:
            if "plc_protocol:" in line:
                last_seen = line.strip()
                break
        if last_seen:
            return last_seen
        time.sleep(0.2)
    return last_seen or None


def _backup_config() -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = CONFIG.with_suffix(f".bak.{ts}")
    backup.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"backed up old config to {backup}")
    return backup


def _do_protocol(cfg: dict, action: str, no_restart: bool) -> int:
    new_proto = ALIASES[action]
    old_proto = cfg.get("plc_protocol", "(missing)")
    proto_changed = old_proto != new_proto

    # Legacy fronback composes a side-by-side image from cam1 + cam2 and
    # requires both cameras enabled. Switching INTO legacy auto-fixes any
    # single-camera state left over from v2 testing.
    cam_changes: list[str] = []
    if action in ("legacy", "legacy_fronback"):
        cam_changes = apply_camera_selection(cfg, "both")

    if not proto_changed and not cam_changes:
        print(f"already {new_proto}, nothing to do")
        return 0

    _backup_config()
    if proto_changed:
        cfg["plc_protocol"] = new_proto
        print(f"plc_protocol: {old_proto}  ->  {new_proto}")
    for line in cam_changes:
        print(line)
    write_config_atomic(cfg)

    if no_restart:
        print("(skipping service restart, you'll need to: sudo systemctl restart main.service)")
        return 0

    rc = restart_service()
    if rc != 0:
        return rc

    line = tail_protocol_line()
    if line:
        print(f"\nlatest log:\n  {line}")
    else:
        print("\n(no plc_protocol log line found yet — service may still be starting)")
        print("check manually: tail -n 50 /home/pi/my_app.log")
    return 0


def _do_cameras(cfg: dict, selection: str, no_restart: bool) -> int:
    changes = apply_camera_selection(cfg, selection)
    if not changes:
        print(f"already in '{selection}' state, nothing to do")
        return 0

    _backup_config()
    for line in changes:
        print(line)
    write_config_atomic(cfg)

    enabled_after = [k for k, v in cfg.get("cameras", {}).items() if v.get("enabled", True)]
    if not enabled_after:
        print("WARNING: 0 cameras enabled after this change — service will start with no cameras.")
    else:
        print(f"cameras enabled now : {sorted(enabled_after)}")

    if no_restart:
        print("(skipping service restart, you'll need to: sudo systemctl restart main.service)")
        return 0

    return restart_service()


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "action",
        choices=[*ALIASES.keys(), "status", "cameras"],
        help="legacy / v2 / status / cameras",
    )
    p.add_argument(
        "selection",
        nargs="?",
        choices=list(CAMERA_PRESETS.keys()),
        default=None,
        help="for 'cameras' action: cam1 | cam2 | both",
    )
    p.add_argument(
        "--no-restart", action="store_true",
        help="update config.json but don't restart main.service",
    )
    args = p.parse_args()

    if args.action == "cameras" and args.selection is None:
        p.error("'cameras' action requires a target: cam1, cam2, or both")
    if args.action != "cameras" and args.selection is not None:
        p.error(f"'{args.selection}' is only valid with the 'cameras' action")

    cfg = read_config()

    if args.action == "status":
        show_status(cfg)
        return 0
    if args.action == "cameras":
        return _do_cameras(cfg, args.selection, args.no_restart)
    return _do_protocol(cfg, args.action, args.no_restart)


if __name__ == "__main__":
    sys.exit(main())
