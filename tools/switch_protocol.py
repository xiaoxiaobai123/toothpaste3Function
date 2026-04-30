#!/usr/bin/env python3
"""Flip /home/pi/config.json plc_protocol + restart main.service.

Designed to run on the NanoPi itself via SSH; writes the config change
atomically and tails the next "plc_protocol:" log line so you can confirm
the new protocol actually took effect.

Usage (on the NanoPi, requires root for systemctl):
    sudo python3 tools/switch_protocol.py legacy
    sudo python3 tools/switch_protocol.py v2
    sudo python3 tools/switch_protocol.py status         # just show current
    sudo python3 tools/switch_protocol.py legacy --no-restart  # rewrite, don't reboot

Aliases:
    legacy            = legacy_fronback
    v2                = v2_unified
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


def read_config() -> dict:
    if not CONFIG.is_file():
        sys.exit(f"error: {CONFIG} does not exist")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def show_status(cfg: dict) -> None:
    proto = cfg.get("plc_protocol", "(missing — defaults to v2_unified)")
    print(f"current plc_protocol: {proto}")
    cams = cfg.get("cameras", {})
    print(f"cameras configured  : {sorted(cams.keys())}")
    plc_ip = cfg.get("plc", {}).get("ip", "(missing)")
    print(f"plc.ip              : {plc_ip}")


def write_config_atomic(cfg: dict) -> None:
    """Write via .tmp + os.replace so a crash mid-write can't corrupt the file."""
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(CONFIG)


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


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "action",
        choices=[*ALIASES.keys(), "status"],
        help="legacy / v2 / status",
    )
    p.add_argument(
        "--no-restart", action="store_true",
        help="update config.json but don't restart main.service",
    )
    args = p.parse_args()

    cfg = read_config()

    if args.action == "status":
        show_status(cfg)
        return 0

    new_proto = ALIASES[args.action]
    old_proto = cfg.get("plc_protocol", "(missing)")

    if old_proto == new_proto:
        print(f"already {new_proto}, nothing to do")
        return 0

    # Backup current config alongside (timestamped) so rollback is `cp` away.
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = CONFIG.with_suffix(f".bak.{ts}")
    backup.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"backed up old config to {backup}")

    cfg["plc_protocol"] = new_proto
    write_config_atomic(cfg)
    print(f"plc_protocol: {old_proto}  ->  {new_proto}")

    if args.no_restart:
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


if __name__ == "__main__":
    sys.exit(main())
