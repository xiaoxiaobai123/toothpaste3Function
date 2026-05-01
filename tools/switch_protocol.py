#!/usr/bin/env python3
"""Flip /home/pi/config.json plc_protocol, per-camera enabled flag, or
hardware ROI; then restart main.service.

Designed to run on the NanoPi itself via SSH; writes config changes
atomically and (where applicable) tails fresh log lines so you can
confirm the change took effect.

Usage (on the NanoPi, requires root for systemctl):
    sudo python3 tools/switch_protocol.py legacy
    sudo python3 tools/switch_protocol.py v2
    sudo python3 tools/switch_protocol.py status         # just show current

    # Toggle which cameras are active without changing protocol:
    sudo python3 tools/switch_protocol.py cameras cam1   # only camera1 enabled
    sudo python3 tools/switch_protocol.py cameras cam2   # only camera2 enabled
    sudo python3 tools/switch_protocol.py cameras both   # both enabled

    # Set hardware ROI on the camera (smaller frame -> faster capture).
    # Width/height/offsets must be multiples of 4 (MVS requirement).
    # Algorithm ROI files are auto-translated to the new coord space.
    sudo python3 tools/switch_protocol.py roi cam1 \\
        --width 800 --height 600 --offset-x 240 --offset-y 100
    sudo python3 tools/switch_protocol.py roi both \\
        --width 800 --height 600 --offset-x 240 --offset-y 100
    sudo python3 tools/switch_protocol.py roi cam1 --reset    # back to full frame

    sudo python3 tools/switch_protocol.py legacy --no-restart        # rewrite, don't reboot
    sudo python3 tools/switch_protocol.py cameras cam1 --no-restart
    sudo python3 tools/switch_protocol.py roi cam1 --width 800 ... --no-restart

Aliases:
    legacy            = legacy_fronback
    v2                = v2_unified

Notes:
- Switching INTO legacy auto-enables both cameras (legacy fronback's
  side-by-side composite needs both). Switching to v2 leaves the camera
  enabled flags alone — use `cameras cam1` etc. to toggle them explicitly.
- Hardware ROI is applied at camera init by main.service via the MVS SDK
  (MV_CC_SetIntValue on Width / Height / OffsetX / OffsetY). The camera
  itself is `MV_ACCESS_Exclusive` once main holds it, so this tool does
  NOT talk to the camera directly — it edits config.json and restarts
  main.service, which on next start applies the new ROI. `--reset`
  removes the roi field so the camera starts in full-frame mode again.
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
# main.service runs with WorkingDirectory=/home/pi/ so this is also where
# legacy/fronback_orchestrator.make_file_roi_provider looks up algorithm
# ROI files at runtime.
ALGO_ROI_DIR = Path("/home/pi")

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
    "cam1": {"camera1": True, "camera2": False},
    "cam2": {"camera1": False, "camera2": True},
    "both": {"camera1": True, "camera2": True},
}

# Camera-target presets reused for `cameras` and `roi` actions.
ROI_TARGETS: dict[str, list[int]] = {
    "cam1": [1],
    "cam2": [2],
    "both": [1, 2],
}

# MVS SDK requires width/height/offsets to be multiples of this on most
# Hikvision GigE sensors. Stricter than necessary on a few models (some
# allow step=2) but safe everywhere — silently snapping to a different
# value would be confusing, so we reject misaligned input.
ROI_ALIGNMENT = 4


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
    for cam_key in sorted(cams.keys()):
        roi = cams[cam_key].get("roi")
        if roi is None:
            print(f"{cam_key}.roi         : (none — full frame)")
        else:
            print(
                f"{cam_key}.roi         : "
                f"{{width: {roi.get('width')}, height: {roi.get('height')}, "
                f"offset_x: {roi.get('offset_x', 0)}, offset_y: {roi.get('offset_y', 0)}}}"
            )
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


# --------------------------------------------------------------------------- #
# Hardware ROI: pure mutation helpers.
# --------------------------------------------------------------------------- #
def apply_hardware_roi(cfg: dict, cam_num: int, hw_roi: dict[str, int]) -> str:
    """Set cfg["cameras"]["cameraN"]["roi"] = hw_roi. Returns one change line.

    Raises KeyError if cameraN is not configured. hw_roi must contain
    width/height (positive ints) and may contain offset_x/offset_y
    (default 0). All four values must be multiples of ROI_ALIGNMENT.
    """
    cam_key = f"camera{cam_num}"
    if cam_key not in cfg.get("cameras", {}):
        raise KeyError(f"{cam_key} not configured in {CONFIG}")

    width = int(hw_roi["width"])
    height = int(hw_roi["height"])
    offset_x = int(hw_roi.get("offset_x", 0))
    offset_y = int(hw_roi.get("offset_y", 0))

    if width <= 0 or height <= 0:
        raise ValueError(f"width/height must be positive, got {width}x{height}")
    for name, value in (("width", width), ("height", height), ("offset_x", offset_x), ("offset_y", offset_y)):
        if value % ROI_ALIGNMENT != 0:
            raise ValueError(
                f"{name}={value} is not a multiple of {ROI_ALIGNMENT} "
                f"(MVS hardware ROI alignment requirement)"
            )

    new_roi = {"width": width, "height": height, "offset_x": offset_x, "offset_y": offset_y}
    old = cfg["cameras"][cam_key].get("roi")
    cfg["cameras"][cam_key]["roi"] = new_roi
    if old is None:
        return f"{cam_key}.roi: (none) -> {new_roi}"
    return f"{cam_key}.roi: {old} -> {new_roi}"


def reset_hardware_roi(cfg: dict, cam_num: int) -> str | None:
    """Delete cfg["cameras"]["cameraN"]["roi"]. Returns the change line, or
    None if there was nothing to remove.
    """
    cam_key = f"camera{cam_num}"
    cam = cfg.get("cameras", {}).get(cam_key)
    if cam is None or "roi" not in cam:
        return None
    old = cam.pop("roi")
    return f"{cam_key}.roi: {old} -> (none)"


def translate_algo_roi(
    algo_roi: dict[str, int],
    offset_x: int,
    offset_y: int,
    width: int,
    height: int,
) -> dict[str, int]:
    """Translate a full-frame algorithm ROI into hardware-cropped frame coords.

    new_x = old_x - hw_offset_x, clamped to [0, width].
    Same for y. After hardware ROI is applied, the camera only ships the
    cropped region to host memory, so algorithm ROI coordinates that were
    previously relative to the full sensor frame must be re-expressed
    relative to the cropped frame.
    """
    return {
        "x1": max(0, min(width, int(algo_roi["x1"]) - offset_x)),
        "y1": max(0, min(height, int(algo_roi["y1"]) - offset_y)),
        "x2": max(0, min(width, int(algo_roi["x2"]) - offset_x)),
        "y2": max(0, min(height, int(algo_roi["y2"]) - offset_y)),
    }


# --------------------------------------------------------------------------- #
# Algorithm ROI file IO.
#
# The runtime path is `<base>/roi_coordinates_<ip-with-underscores>.json`,
# matching legacy.fronback_orchestrator.make_file_roi_provider.
#
# When this tool first applies a hardware ROI, it snapshots the existing
# (full-frame) algorithm ROI to `<file>.full_frame.json` so that:
#   * subsequent `roi` invocations can re-translate from the original,
#     not the already-translated value (otherwise the offset stacks up);
#   * `--reset` can restore the full-frame coordinates exactly.
# --------------------------------------------------------------------------- #
def algo_roi_path(base_dir: Path, camera_ip: str) -> Path:
    return base_dir / f"roi_coordinates_{camera_ip.replace('.', '_')}.json"


def algo_roi_snapshot_path(base_dir: Path, camera_ip: str) -> Path:
    return base_dir / f"roi_coordinates_{camera_ip.replace('.', '_')}.full_frame.json"


def apply_algo_roi_translation(base_dir: Path, camera_ip: str, hw_roi: dict[str, int]) -> list[str]:
    """Translate the algorithm ROI file in-place to the cropped frame's coords.

    Snapshots the full-frame ROI to `<file>.full_frame.json` on first call
    so re-applies always start from the original, and `--reset` can
    restore it bit-for-bit.

    Returns informational lines for the caller to print. Missing /
    malformed ROI files are reported but do NOT raise — single-camera
    deployments and fresh installs commonly lack one of the files.
    """
    path = algo_roi_path(base_dir, camera_ip)
    snap_path = algo_roi_snapshot_path(base_dir, camera_ip)

    if not path.is_file() and not snap_path.is_file():
        return [f"  (no algorithm ROI file at {path}, nothing to translate)"]

    # First ever apply: snapshot the on-disk full-frame ROI.
    if not snap_path.is_file():
        snap_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    raw = json.loads(snap_path.read_text(encoding="utf-8"))
    try:
        full = {k: int(float(raw[k])) for k in ("x1", "y1", "x2", "y2")}
    except (KeyError, ValueError, TypeError) as e:
        return [f"  (algorithm ROI snapshot {snap_path} unreadable: {e}; skipped)"]

    new = translate_algo_roi(
        full,
        offset_x=int(hw_roi.get("offset_x", 0)),
        offset_y=int(hw_roi.get("offset_y", 0)),
        width=int(hw_roi["width"]),
        height=int(hw_roi["height"]),
    )
    path.write_text(json.dumps(new, indent=4) + "\n", encoding="utf-8")
    return [
        f"  algorithm ROI: {path}",
        f"    full-frame: {full}",
        f"    translated: {new}",
    ]


def reset_algo_roi_translation(base_dir: Path, camera_ip: str) -> list[str]:
    """Restore algorithm ROI from the .full_frame.json snapshot, then delete it."""
    path = algo_roi_path(base_dir, camera_ip)
    snap_path = algo_roi_snapshot_path(base_dir, camera_ip)
    if not snap_path.is_file():
        return [f"  (no .full_frame snapshot at {snap_path}; algorithm ROI left as-is)"]
    path.write_text(snap_path.read_text(encoding="utf-8"), encoding="utf-8")
    snap_path.unlink()
    return [f"  algorithm ROI restored from {snap_path.name} (snapshot deleted)"]


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


def _do_roi(
    cfg: dict,
    selection: str,
    hw_roi: dict[str, int] | None,
    reset: bool,
    translate_algo: bool,
    no_restart: bool,
) -> int:
    cam_nums = ROI_TARGETS[selection]

    backed_up = False
    output_lines: list[str] = []
    any_change = False

    for cam_num in cam_nums:
        cam_key = f"camera{cam_num}"
        if cam_key not in cfg.get("cameras", {}):
            output_lines.append(f"{cam_key} not configured, skipped")
            continue

        camera_ip = cfg["cameras"][cam_key].get("ip")

        if reset:
            change = reset_hardware_roi(cfg, cam_num)
            if change is None:
                output_lines.append(f"{cam_key}.roi already (none), nothing to do")
                continue
            if not backed_up:
                _backup_config()
                backed_up = True
            output_lines.append(change)
            any_change = True
            if translate_algo and camera_ip:
                output_lines.extend(reset_algo_roi_translation(ALGO_ROI_DIR, camera_ip))
            continue

        # Apply path.
        assert hw_roi is not None  # main() guards this
        try:
            change = apply_hardware_roi(cfg, cam_num, hw_roi)
        except (KeyError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if not backed_up:
            _backup_config()
            backed_up = True
        output_lines.append(change)
        any_change = True
        if translate_algo and camera_ip:
            output_lines.extend(apply_algo_roi_translation(ALGO_ROI_DIR, camera_ip, hw_roi))

    for line in output_lines:
        print(line)

    if not any_change:
        return 0

    write_config_atomic(cfg)

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
        choices=[*ALIASES.keys(), "status", "cameras", "roi"],
        help="legacy / v2 / status / cameras / roi",
    )
    p.add_argument(
        "selection",
        nargs="?",
        choices=list(CAMERA_PRESETS.keys()),
        default=None,
        help="for cameras / roi actions: cam1 | cam2 | both",
    )
    p.add_argument(
        "--no-restart",
        action="store_true",
        help="update config.json but don't restart main.service",
    )
    # roi-specific flags.
    p.add_argument(
        "--width", type=int, default=None, help="(roi) hardware ROI width in pixels (multiple of 4)"
    )
    p.add_argument(
        "--height", type=int, default=None, help="(roi) hardware ROI height in pixels (multiple of 4)"
    )
    p.add_argument(
        "--offset-x", type=int, default=0, help="(roi) hardware ROI offset X (multiple of 4, default 0)"
    )
    p.add_argument(
        "--offset-y", type=int, default=0, help="(roi) hardware ROI offset Y (multiple of 4, default 0)"
    )
    p.add_argument(
        "--reset", action="store_true", help="(roi) remove the hardware ROI for the target camera(s)"
    )
    p.add_argument(
        "--no-translate-algo-roi",
        action="store_true",
        help="(roi) skip translating roi_coordinates_<ip>.json (advanced)",
    )
    args = p.parse_args()

    # Cross-action validation.
    if args.action in ("cameras", "roi") and args.selection is None:
        p.error(f"'{args.action}' action requires a target: cam1, cam2, or both")
    if args.action not in ("cameras", "roi") and args.selection is not None:
        p.error(f"'{args.selection}' is only valid with the 'cameras' or 'roi' action")

    # roi-specific flag combinations.
    roi_dim_flags_set = args.width is not None or args.height is not None
    if args.action == "roi":
        if args.reset and roi_dim_flags_set:
            p.error("--reset cannot be combined with --width / --height")
        if not args.reset and (args.width is None or args.height is None):
            p.error("'roi' action requires --width and --height (or --reset)")
    elif roi_dim_flags_set or args.reset or args.offset_x or args.offset_y or args.no_translate_algo_roi:
        p.error(
            "--width / --height / --offset-* / --reset / --no-translate-algo-roi "
            "are only valid with the 'roi' action"
        )

    cfg = read_config()

    if args.action == "status":
        show_status(cfg)
        return 0
    if args.action == "cameras":
        return _do_cameras(cfg, args.selection, args.no_restart)
    if args.action == "roi":
        hw_roi = None
        if not args.reset:
            hw_roi = {
                "width": args.width,
                "height": args.height,
                "offset_x": args.offset_x,
                "offset_y": args.offset_y,
            }
        return _do_roi(
            cfg,
            args.selection,
            hw_roi,
            reset=args.reset,
            translate_algo=not args.no_translate_algo_roi,
            no_restart=args.no_restart,
        )
    return _do_protocol(cfg, args.action, args.no_restart)


if __name__ == "__main__":
    sys.exit(main())
