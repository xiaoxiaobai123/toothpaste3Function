"""License validation tied to a stable per-device fingerprint.

Identity sources (combined and SHA-256-hashed):
    /proc/cpuinfo Hardware + Revision  (ARM SoC fields)
    First non-loopback / non-wireless NIC MAC address
    /proc/device-tree/model              (board model on aarch64)

The expected license is SHA-256(fingerprint) and is stored in license.key
next to the binary. validate_license() returns False on any read or hash
mismatch — caller decides whether to abort.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

LICENSE_FILE = "license.key"


def get_cpu_id() -> str | None:
    """Return a stable hardware fingerprint, or None if it cannot be read."""
    parts: list[str] = []

    # 1. SoC info from /proc/cpuinfo
    try:
        with open("/proc/cpuinfo") as f:
            cpuinfo = f.read()
        m = re.search(r"Hardware\s+:\s+(.*)", cpuinfo)
        if m:
            parts.append(m.group(1).strip())
        m = re.search(r"Revision\s+:\s+(.*)", cpuinfo)
        if m:
            parts.append(m.group(1).strip())
    except OSError as e:
        print(f"Warning: could not read /proc/cpuinfo: {e}")

    # 2. First wired NIC MAC
    try:
        with open("/proc/net/dev") as f:
            ifaces = [line.split(":")[0].strip() for line in f.readlines()[2:]]
        ifaces = [i for i in ifaces if i != "lo" and not i.startswith("wlan")]
        if ifaces:
            mac_path = Path(f"/sys/class/net/{ifaces[0]}/address")
            if mac_path.exists():
                parts.append(mac_path.read_text().strip())
    except OSError as e:
        print(f"Warning: could not read MAC address: {e}")

    # 3. Board model
    try:
        model_path = Path("/proc/device-tree/model")
        if model_path.exists():
            parts.append(model_path.read_text().strip("\x00").strip())
    except OSError as e:
        print(f"Warning: could not read device model: {e}")

    if not parts:
        return None
    return hashlib.sha256(":".join(parts).encode()).hexdigest()


def generate_license(cpu_id: str) -> bool:
    if not cpu_id:
        return False
    try:
        key = hashlib.sha256(cpu_id.encode()).hexdigest()
        with open(LICENSE_FILE, "w") as f:
            f.write(key)
        return True
    except OSError as e:
        print(f"Error generating license: {e}")
        return False


def validate_license() -> bool:
    try:
        if not os.path.isfile(LICENSE_FILE):
            return False
        with open(LICENSE_FILE) as f:
            stored = f.read().strip()
        cpu_id = get_cpu_id()
        if not cpu_id:
            return False
        expected = hashlib.sha256(cpu_id.encode()).hexdigest()
        return stored == expected
    except OSError as e:
        print(f"Error validating license: {e}")
        return False
