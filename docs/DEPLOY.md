# Deployment (aarch64)

The full step-by-step deployment runbook lives at [`deploy/README.md`](../deploy/README.md). This page is the architectural overview.

## Target platform

- Hardware: NanoPi-R5S-LTS / RK3568 / aarch64
- OS: Debian 11
- Display path: `/dev/fb0` framebuffer + companion C `image_updater` binary watching `/dev/shm/output_image.rgb565` via inotify
- Runtime deps shipped in `deploy/`: two systemd unit files + three shell scripts

## Build (CI)

`.github/workflows/build-aarch64.yml` runs on `ubuntu-24.04-arm` (free GitHub-hosted ARM runner for public repos), produces `dist/main` via PyInstaller, and uploads it as an artifact.

`.github/workflows/release.yml` is triggered by tag pushes (`v*`); it bundles `dist/main` + `deploy/` + `config.example.json` + `company_name.png` into a GitHub Release.

## First-time field deployment

1. SCP `dist/main` and the `deploy/` directory to the target.
2. SCP your hand-built `image_updater` C binary (out of scope for this repo).
3. Run `deploy/install.sh` — it stops old services, sets up `/dev/shm` tmpfs symlink, installs systemd units, and starts everything.

## Updates

`deploy/update-main.sh /tmp/main.new` swaps in a new binary with automatic rollback if the new version's log shows a Traceback / FATAL within the first 5 seconds.

## Why `/dev/shm`?

Original implementation wrote `output_image.rgb565` to eMMC at ~20 GB/h, accelerating storage wear. Moving the file to tmpfs reduces eMMC writes to zero with no code change (a symlink `/home/pi/output_image.rgb565 → /dev/shm/output_image.rgb565` is transparent to both Python and C). The systemd `ExecStartPre` re-creates the file after every boot since `/dev/shm` is reset.
