#!/usr/bin/env bash
# Smart, offline upgrade for fronback / head deployments.
#
# Workflow (no internet on the NanoPi required):
#   1. Operator downloads the release tarball on a PC with internet.
#      gh release download vX.Y.Z --repo xiaoxiaobai123/toothpaste3Function
#      …or just grab toothpaste3Function-vX.Y.Z-aarch64.tar.gz from the
#      Releases page.
#   2. Copy the tarball onto a USB stick.
#   3. On the NanoPi:  tar -xzf … && cd <extracted dir> && sudo ./deploy/upgrade.sh
#
# Behaviour:
#   * Preserves /home/pi/config.json            (customer's per-site config)
#   * Preserves /home/pi/roi_coordinates_*.json (per-camera ROI from old fronback)
#   * Preserves /home/pi/license.key            (per-machine fingerprint)
#   * Preserves /home/pi/company_name.png       (customer-customised logo)
#   * Backs up old /home/pi/main as /home/pi/main.bak.<timestamp>
#   * On startup failure within 5 seconds, automatically rolls back to the backup.
#
# Exits non-zero on any unrecoverable error so a deploy script can fail loudly.

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate paths relative to this script.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_DIR="/home/pi"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

# ---------------------------------------------------------------------------
# Pretty output.
# ---------------------------------------------------------------------------
log() { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()  { printf '\033[1;32m  OK\033[0m  %s\n' "$*"; }
warn(){ printf '\033[1;33m  WRN\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m  ERR\033[0m %s\n' "$*" >&2; }

if [[ "${EUID:-1000}" -ne 0 ]]; then
    err "Run with sudo:  sudo ./deploy/upgrade.sh"
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Locate the new binary.
# ---------------------------------------------------------------------------
log "1/8  Locate new binary"
NEW_BINARY=""
for candidate in "$RELEASE_DIR/main" "$SCRIPT_DIR/main" "$RELEASE_DIR/dist/main"; do
    if [[ -f "$candidate" ]]; then
        NEW_BINARY="$candidate"
        break
    fi
done
if [[ -z "$NEW_BINARY" ]]; then
    err "Cannot find a 'main' binary under $RELEASE_DIR or $SCRIPT_DIR."
    err "Make sure you're running this from inside an extracted release tarball."
    exit 1
fi
ok "new binary: $NEW_BINARY"

# ---------------------------------------------------------------------------
# 2. Snapshot customer files we must preserve.
# ---------------------------------------------------------------------------
log "2/8  Inventory customer files (will preserve)"

mkdir -p "$TARGET_DIR"
PRESERVED=()
preserve_if_exists() {
    local f="$1"
    if [[ -e "$TARGET_DIR/$f" ]]; then
        PRESERVED+=("$f")
        ok "preserve $TARGET_DIR/$f"
    fi
}

preserve_if_exists "config.json"
preserve_if_exists "license.key"
preserve_if_exists "company_name.png"
shopt -s nullglob
for roi_file in "$TARGET_DIR"/roi_coordinates_*.json; do
    PRESERVED+=("$(basename "$roi_file")")
    ok "preserve $roi_file"
done
shopt -u nullglob

if [[ ${#PRESERVED[@]} -eq 0 ]]; then
    warn "No existing customer files found in $TARGET_DIR."
    warn "First-time install? You'll need to provide config.json + license.key + ROI"
    warn "files before the binary will work. Continuing anyway."
fi

# ---------------------------------------------------------------------------
# 3. Stop services so we can swap files atomically.
# ---------------------------------------------------------------------------
log "3/8  Stop services"
systemctl stop main.service 2>/dev/null || true
systemctl stop image_updater.service 2>/dev/null || true
# Belt and braces — kill any orphan processes.
pkill -f "$TARGET_DIR/main" 2>/dev/null || true
ok "services stopped"

# ---------------------------------------------------------------------------
# 4. Back up current binary so we can roll back on failure.
# ---------------------------------------------------------------------------
log "4/8  Backup current binary"
BACKUP=""
if [[ -f "$TARGET_DIR/main" ]]; then
    BACKUP="$TARGET_DIR/main.bak.$TIMESTAMP"
    cp "$TARGET_DIR/main" "$BACKUP"
    ok "backed up old binary to $BACKUP"
else
    warn "no existing $TARGET_DIR/main to back up"
fi

# ---------------------------------------------------------------------------
# 5. Install new binary + non-customer assets.
# ---------------------------------------------------------------------------
log "5/8  Install new files"
install -m 0755 -o pi -g pi "$NEW_BINARY" "$TARGET_DIR/main"
ok "binary -> $TARGET_DIR/main"

# config.example.json: always update so the operator can see the new schema.
if [[ -f "$RELEASE_DIR/config.example.json" ]]; then
    cp "$RELEASE_DIR/config.example.json" "$TARGET_DIR/config.example.json"
    ok "config.example.json refreshed (compare with your config.json if PLC schema changed)"
fi

# company_name.png: only install if the operator hasn't customised it already.
if [[ ! -f "$TARGET_DIR/company_name.png" ]] && [[ -f "$RELEASE_DIR/company_name.png" ]]; then
    cp "$RELEASE_DIR/company_name.png" "$TARGET_DIR/company_name.png"
    ok "company_name.png installed (none was present)"
fi

# ---------------------------------------------------------------------------
# 6. Update systemd units + tmpfs link (idempotent).
# ---------------------------------------------------------------------------
log "6/8  Update systemd + tmpfs"
cp "$SCRIPT_DIR/main.service" /etc/systemd/system/main.service
[[ -f "$SCRIPT_DIR/image_updater.service" ]] && cp "$SCRIPT_DIR/image_updater.service" /etc/systemd/system/image_updater.service
systemctl daemon-reload
ok "systemd units installed"

touch /dev/shm/output_image.rgb565
chmod 666 /dev/shm/output_image.rgb565
if [[ ! -L "$TARGET_DIR/output_image.rgb565" ]]; then
    # If a real file is already there, back it up first.
    if [[ -f "$TARGET_DIR/output_image.rgb565" ]]; then
        mv "$TARGET_DIR/output_image.rgb565" "$TARGET_DIR/output_image.rgb565.bak.$TIMESTAMP"
    fi
    ln -sfn /dev/shm/output_image.rgb565 "$TARGET_DIR/output_image.rgb565"
    chown -h pi:pi "$TARGET_DIR/output_image.rgb565"
fi
ok "tmpfs link ready"

# ---------------------------------------------------------------------------
# 7. Start with rollback safety net.
# ---------------------------------------------------------------------------
log "7/8  Start services (with auto-rollback on failure)"
systemctl enable main.service >/dev/null 2>&1 || true
systemctl enable image_updater.service >/dev/null 2>&1 || true
systemctl start main.service
sleep 4
systemctl start image_updater.service 2>/dev/null || true
sleep 4

ROLLED_BACK=0
if ! systemctl is-active --quiet main.service; then
    err "main.service is not active 8 seconds after start"
    ROLLED_BACK=1
elif tail -n 80 "$TARGET_DIR/my_app.log" 2>/dev/null | grep -qE "Traceback|FATAL|ModuleNotFoundError|ImportError"; then
    err "main.service log shows a fatal error within 8 seconds:"
    tail -n 20 "$TARGET_DIR/my_app.log" >&2 || true
    ROLLED_BACK=1
fi

if [[ $ROLLED_BACK -eq 1 ]]; then
    if [[ -n "$BACKUP" && -f "$BACKUP" ]]; then
        warn "rolling back to $BACKUP"
        systemctl stop main.service 2>/dev/null || true
        cp "$BACKUP" "$TARGET_DIR/main"
        systemctl start main.service
        sleep 3
        if systemctl is-active --quiet main.service; then
            err "rollback successful — old version is running again"
        else
            err "rollback ALSO failed — manual intervention needed"
            err "old binary is at: $BACKUP"
        fi
    else
        err "no backup to roll back to (first-time install). New binary is broken."
    fi
    exit 2
fi

# ---------------------------------------------------------------------------
# 8. Summary.
# ---------------------------------------------------------------------------
log "8/8  Verify"
ok "main.service active"
if systemctl is-active --quiet image_updater.service; then
    ok "image_updater.service active"
else
    warn "image_updater.service not active (might still be retrying — check journalctl -u image_updater)"
fi

echo
echo "==========================================================="
log "Upgrade complete"
echo
echo "Binary version (from log):"
sed -n 's/.*\[SYS\] version: //p' "$TARGET_DIR/my_app.log" 2>/dev/null | tail -1 | sed 's/^/  /'
echo
echo "Preserved files (untouched):"
for f in "${PRESERVED[@]}"; do
    echo "  - $TARGET_DIR/$f"
done
[[ -n "$BACKUP" ]] && echo "Old binary backed up at: $BACKUP"
echo
echo "Live log:        tail -f $TARGET_DIR/my_app.log"
echo "Service status:  systemctl status main.service"
echo "Roll back later: cp $BACKUP $TARGET_DIR/main && systemctl restart main.service"
echo "==========================================================="
