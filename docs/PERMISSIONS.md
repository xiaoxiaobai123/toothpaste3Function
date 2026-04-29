# Permissions cheat-sheet

Single page covering every file/path the binary touches and what
permissions it needs. Use this when a deploy fails or `tail my_app.log`
shows a `Permission denied`.

## TL;DR matrix

| Path | Owner | Mode | Set by | Reason |
|---|---|---|---|---|
| `/home/pi/main` | `pi:pi` (or `root:root` if no `pi` user) | `0755` | `upgrade.sh` step 5 | systemd runs as root, ownership cosmetic |
| `/home/pi/config.json` | preserved (customer-set) | preserved | customer | systemd as root reads anything |
| `/home/pi/license.key` | preserved (customer-set) | preserved | `tools/generate_license.py` | must live in `/home/pi/` (binary's cwd) |
| `/home/pi/roi_coordinates_*.json` | preserved | preserved | customer | legacy mode reads from `/home/pi/` |
| `/home/pi/company_name.png` | installed if missing | `0644` | `upgrade.sh` | display pipeline uses |
| `/home/pi/my_app.log` | `root:root` | `0644` | created at runtime | `pi` group/other can `tail`; only root can `truncate` |
| `/dev/shm/output_image.rgb565` | `root:root` | `0666` | systemd `ExecStartPre` + `upgrade.sh` | C `image_updater` reads, Python writes — both as root, but mode 0666 lets non-root readers in too |
| `/home/pi/output_image.rgb565` | `pi:pi` (symlink) | `0777` | `upgrade.sh` | symlink to `/dev/shm/...`, no real perms |
| `/tmp/processed_image.png` | `root:root` | `0644` | binary at runtime | feh/fbi as `pi` reads; world-readable mode 0644 covers it |
| `/etc/systemd/system/main.service` | `root:root` | `0644` | `upgrade.sh` step 6 | systemd reads; only root can write |
| `/etc/systemd/system/image_updater.service` | `root:root` | `0644` | `upgrade.sh` step 6 (only if `/home/pi/image_updater` exists) | skipped for legacy customers |
| `/opt/MVS/lib/aarch64/libMvCameraControl.so` | from `MVS-*.deb` | `0755` | `dpkg -i` | loaded via ctypes at runtime |
| `/opt/MVS/Samples/aarch64/Python/MvImport/*.py` | from `MVS-*.deb` | `0644` | `dpkg -i` | imported by `camera/base.py` |

## Service runtime details

`main.service` runs as **root** (no `User=` directive). This is
deliberate:

- The original toothpastefronback program ran as root.
- Hikvision MVS sometimes wants privileged ports for GigE Vision
  control packets.
- All file writes go to world-readable paths, so non-root operators
  can still inspect logs and the display image.

If you really must run as `pi`:
1. Edit `/etc/systemd/system/main.service` and add `User=pi` under `[Service]`.
2. Make sure `/home/pi/main` is owned by `pi:pi` and executable.
3. Make sure `pi` can read `/opt/MVS/lib/aarch64/libMvCameraControl.so`.
4. Add `pi` to whatever group owns `/dev/fb0` (typically `video`).
5. `systemctl daemon-reload && systemctl restart main.service`

We do not test this configuration, so be prepared to debug.

## Common problems and how to fix them

### "Permission denied: 'output_image.rgb565'"
The Python binary tried to open `/home/pi/output_image.rgb565` (which is
a symlink to `/dev/shm/...`) but the target file in `/dev/shm` did not
exist or had wrong perms. Linux's `fs.protected_regular` blocks creation
through dangling symlinks.

**Fix manually:**
```bash
sudo touch /dev/shm/output_image.rgb565
sudo chmod 666 /dev/shm/output_image.rgb565
sudo systemctl restart main.service
```

`upgrade.sh` does this for you, and `main.service`'s `ExecStartPre` does
it on every service start, so this should not happen unless something
disabled the ExecStartPre line.

### Binary exits on startup with "Invalid license"
`license.key` is read from the **current working directory** which for
the systemd service is `/home/pi/`. If you generated the license while
in another directory, the file is in the wrong place.

**Fix:**
```bash
sudo systemctl stop main.service
cd /home/pi
sudo python3 /path/to/tools/generate_license.py
ls -la /home/pi/license.key   # must exist
sudo systemctl start main.service
```

### Operator can `cat my_app.log` but not `truncate` it
`my_app.log` is owned by `root:root` because the service runs as root.
Mode is `0644` (world-readable, owner-writable).

**Fix:** use `sudo` for write operations, or rotate via the
RotatingFileHandler that the binary already uses (5 × 5 MB = 25 MB max).

### `feh`/`fbi` doesn't update the screen after upgrade
The display program is started **outside** of our binary (probably from
`/etc/rc.local`, a desktop autostart, or a separate systemd service).
When the old fronback program exited, it might have killed feh/fbi as
well. Check:
```bash
ps aux | grep -E 'feh|fbi' | grep -v grep
```
If empty, restart whatever script started feh/fbi originally. The
binary itself does not manage feh/fbi.

### `journalctl -u image_updater` floods with "No such file or directory"
You're a legacy fronback customer and don't have the `image_updater` C
binary installed. After v0.2.3, `upgrade.sh` detects this and skips the
service. If you upgraded before v0.2.3:
```bash
sudo systemctl stop image_updater.service
sudo systemctl disable image_updater.service
```

### "Operation not permitted" creating `/etc/systemd/system/main.service`
You forgot to run `upgrade.sh` with `sudo`. The script checks `EUID` at
the top and exits with a clear message; if you're seeing this elsewhere
in the script, run with sudo.

## Verifying permissions on a deployed machine

Quick diagnostic command:
```bash
ls -la /home/pi/main /home/pi/my_app.log /home/pi/license.key \
       /home/pi/config.json /home/pi/output_image.rgb565 \
       /tmp/processed_image.png /dev/shm/output_image.rgb565 \
       /etc/systemd/system/main.service
```

What you want to see (mode column):
```
-rwxr-xr-x  main                       # 0755, executable
-rw-r--r--  my_app.log                 # 0644, world-readable
-rw-------  license.key                # 0600 ideal but 0644 OK
-rw-r--r--  config.json                # 0644
lrwxrwxrwx  output_image.rgb565 -> ... # symlink, perms irrelevant
-rw-rw-rw-  /dev/shm/output_image.rgb565  # 0666 critical
-rw-r--r--  /tmp/processed_image.png   # 0644
-rw-r--r--  /etc/systemd/system/main.service  # 0644
```

If `/dev/shm/output_image.rgb565` is **not** mode 0666, the C
`image_updater` won't be able to read it and the screen will go blank.

If `license.key` is mode 0600 but owned by a user other than `root`,
the systemd service (running as root) can still read it because root
ignores DAC.
