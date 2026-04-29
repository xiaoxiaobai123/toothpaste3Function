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

for arg in "$@"; do
    case "$arg" in
        --help|-h)
            sed -n '2,30p' "$0"
            exit 0
            ;;
    esac
done

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
# Refuse to run if the release was extracted directly into $TARGET_DIR.
# In that case the new binary, image_updater, and tarball-shipped
# company_name.png have already overwritten the customer's files BEFORE
# this script saw them — so step 4's "backup current binary" would copy
# the new binary as if it were a rollback target, and the operator's
# logo is gone with no way to recover from this script.
#
# v0.3.1+ tarballs wrap their contents in a versioned directory so this
# can't happen via `tar -xzf` from any cwd. The check is here for the
# legacy v0.3.0 tarball and any future regression that flattens it.
# ---------------------------------------------------------------------------
if [[ "$RELEASE_DIR" == "$TARGET_DIR" ]]; then
    err "Release directory equals install directory: $RELEASE_DIR"
    err ""
    err "The tarball was extracted directly into $TARGET_DIR, overwriting"
    err "customer files (config.json is safe; license.key is safe; ROI files"
    err "are safe — but main, image_updater, and company_name.png are now"
    err "the tarball defaults, not the customer's previous versions)."
    err ""
    err "Recover by re-extracting into a fresh subdirectory:"
    err "    rm -rf ~/release && mkdir ~/release"
    err "    tar -xzf <path>/toothpaste3Function-vX.Y.Z-aarch64.tar.gz -C ~/release"
    err "    sudo ~/release/*/deploy/upgrade.sh"
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

# Detect whether image_updater was already part of this site's setup,
# BEFORE we install our new copy. Used in step 6 to decide whether to
# enable image_updater.service automatically. If the site uses feh/fbi
# instead, we don't want to start a competing display process.
HAD_IMAGE_UPDATER_BEFORE=0
if [[ -x "$TARGET_DIR/image_updater" ]] || systemctl is-enabled --quiet image_updater.service 2>/dev/null; then
    HAD_IMAGE_UPDATER_BEFORE=1
fi

# Determine the file owner. Default to "pi" because both old fronback
# and current display branch deployments use it; fall back to root if
# `pi` doesn't exist on this system. systemd runs the service as root
# regardless, so ownership of the binary itself is largely cosmetic.
if id pi >/dev/null 2>&1; then
    OWN_USER=pi
    OWN_GROUP=pi
else
    OWN_USER=root
    OWN_GROUP=root
    warn "user 'pi' does not exist — installing as root:root"
fi

install -m 0755 -o "$OWN_USER" -g "$OWN_GROUP" "$NEW_BINARY" "$TARGET_DIR/main"
ok "binary -> $TARGET_DIR/main (owner $OWN_USER:$OWN_GROUP, mode 0755)"

# image_updater: companion C binary that reads output_image.rgb565 and
# pushes pixels to /dev/fb0. Built natively on the aarch64 CI runner and
# included in the release tarball alongside `main`.
NEW_IMAGE_UPDATER=""
for candidate in "$RELEASE_DIR/image_updater" "$SCRIPT_DIR/image_updater" "$RELEASE_DIR/dist/image_updater"; do
    if [[ -f "$candidate" ]]; then
        NEW_IMAGE_UPDATER="$candidate"
        break
    fi
done
if [[ -n "$NEW_IMAGE_UPDATER" ]]; then
    if [[ -f "$TARGET_DIR/image_updater" ]]; then
        cp "$TARGET_DIR/image_updater" "$TARGET_DIR/image_updater.bak.$TIMESTAMP"
    fi
    install -m 0755 -o "$OWN_USER" -g "$OWN_GROUP" "$NEW_IMAGE_UPDATER" "$TARGET_DIR/image_updater"
    ok "image_updater -> $TARGET_DIR/image_updater"
else
    warn "no image_updater in the release directory — keeping any existing one"
fi

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
# 6. Update systemd units + tmpfs link.
#
# Service files are ALWAYS replaced. The new binary requires the env
# vars + tmpfs ExecStartPre + working directory baked into the shipped
# main.service — keeping the customer's old service file would mean
# missing LD_LIBRARY_PATH for libMvCameraControl.so, missing /dev/shm
# setup, and other gotchas that make `systemctl start` fail in ways
# that `./main` from a shell does not. We back up the old version so
# rollback is one `cp` away if something turns out wrong.
# ---------------------------------------------------------------------------
log "6/8  Update systemd + tmpfs"

install_service_file() {
    local src="$1"
    local dst="$2"
    local name
    name="$(basename "$dst")"

    if [[ ! -f "$src" ]]; then
        return
    fi

    if [[ -f "$dst" ]]; then
        if cmp -s "$src" "$dst"; then
            ok "$name already up to date"
            return
        fi
        local backup="${dst}.bak.${TIMESTAMP}"
        cp "$dst" "$backup"
        ok "$name updated (old saved to $backup)"
    else
        ok "$name installed for the first time"
    fi

    cp "$src" "$dst"
}

install_service_file "$SCRIPT_DIR/main.service" /etc/systemd/system/main.service

# image_updater.service: install only when this site was already using
# the rgb565 + image_updater display chain. Sites using feh/fbi to view
# /tmp/processed_image.png don't want a competing /dev/fb0 writer.
# Detection happened in step 5 BEFORE we copied our new binary, so
# HAD_IMAGE_UPDATER_BEFORE reflects the *prior* state of this machine.
HAS_IMAGE_UPDATER=0
if [[ $HAD_IMAGE_UPDATER_BEFORE -eq 1 ]]; then
    install_service_file "$SCRIPT_DIR/image_updater.service" /etc/systemd/system/image_updater.service
    HAS_IMAGE_UPDATER=1
    ok "image_updater.service installed (rgb565 display chain detected)"
else
    warn "image_updater chain not detected on this machine before upgrade"
    warn "  → installed binary at $TARGET_DIR/image_updater for later opt-in"
    warn "  → service NOT enabled — would compete with feh/fbi if those are running"
    warn "  → to enable later: sudo systemctl enable --now image_updater.service"
    systemctl disable image_updater.service >/dev/null 2>&1 || true
fi

systemctl daemon-reload
ok "systemd reloaded"

# /home/pi/output_image.rgb565 must be a regular file, not a symlink to
# /dev/shm. Older upgrade.sh created the symlink for tmpfs perf, but the
# combination of "symlink + Python's atomic rename" silently broke the
# inotify chain that image_updater depends on (rename(2) doesn't fire
# IN_CLOSE_WRITE on the destination — see save_rgb565_with_header in
# processing/display_utils.py for the full reasoning, fixed in v0.3.3).
#
# 1Hz writes to a regular eMMC file are well within wear-leveling tolerance
# for years of operation, so dropping the tmpfs trick is the simpler path.
if [[ -L "$TARGET_DIR/output_image.rgb565" ]]; then
    rm -f "$TARGET_DIR/output_image.rgb565"
fi
if [[ ! -f "$TARGET_DIR/output_image.rgb565" ]]; then
    touch "$TARGET_DIR/output_image.rgb565"
fi
chmod 666 "$TARGET_DIR/output_image.rgb565"
chown "$OWN_USER:$OWN_GROUP" "$TARGET_DIR/output_image.rgb565" 2>/dev/null || true
ok "rgb565 sink ready (regular file at $TARGET_DIR/output_image.rgb565)"

# ---------------------------------------------------------------------------
# 7. Start with rollback safety net.
# ---------------------------------------------------------------------------
log "7/8  Start services (with auto-rollback on failure)"
systemctl enable main.service >/dev/null 2>&1 || true
if [[ $HAS_IMAGE_UPDATER -eq 1 ]]; then
    systemctl enable image_updater.service >/dev/null 2>&1 || true
fi
systemctl start main.service
sleep 4
if [[ $HAS_IMAGE_UPDATER -eq 1 ]]; then
    systemctl start image_updater.service 2>/dev/null || true
fi
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
if [[ $HAS_IMAGE_UPDATER -eq 1 ]]; then
    if systemctl is-active --quiet image_updater.service; then
        ok "image_updater.service active"
    else
        warn "image_updater.service not active (might still be retrying — check journalctl -u image_updater)"
    fi
else
    ok "image_updater.service skipped (display via /tmp/processed_image.png)"
fi

# Sanity check: license.key must be in $TARGET_DIR for the binary to find it.
if [[ -f "$TARGET_DIR/license.key" ]]; then
    ok "license.key present at $TARGET_DIR/license.key"
else
    warn "no license.key in $TARGET_DIR — the binary will exit on startup"
    warn "  generate one in this directory:"
    warn "    cd $TARGET_DIR && python3 /path/to/tools/generate_license.py"
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
