# image_updater

Tiny C process that watches an RGB565 image file with inotify and renders
it on `/dev/fb0` (Linux framebuffer). Companion to the Python vision
binary, which produces the file.

## Why a separate process

The Python side writes a fresh `output_image.rgb565` after every
detection cycle. inotify wakes this watcher; we mmap the framebuffer,
load the file, scale-to-fit with letterboxed margins, convert each
RGB565 pixel to ARGB8888, and copy to the framebuffer. Keeping this
out of Python lets the algorithm thread keep working without blocking
on framebuffer I/O.

## File format

Same shape Python writes via `processing/display_utils.py`:

```
[ 4 bytes ] width  (int32, little-endian)
[ 4 bytes ] height (int32, little-endian)
[ width * height * 2 bytes ] RGB565 pixels (row-major)
```

## Build

```bash
cd image_updater
make                     # produces ./image_updater
```

CI builds it on `ubuntu-24.04-arm` and bundles the binary into the
release tarball alongside `main`.

## Run

```bash
./image_updater                                    # default path /home/pi/output_image.rgb565
./image_updater /path/to/output_image.rgb565       # override path
```

systemd unit at `deploy/image_updater.service` wraps it with a tight
restart loop so it self-recovers after `inotify_add_watch` failures
on a freshly cleared `/dev/shm`.

## Known limitations (improvements deferred to "stage 2")

- Allocates a fresh buffer per frame (`malloc` then `free`).
- Spawns a detached pthread per inotify event.
- Floods stdout with timing prints — fine for debugging, costs CPU and
  fills `journalctl` in production.
- Per-pixel scalar conversion to ARGB; NEON SIMD would be ~5× faster.
- Writes directly to the visible framebuffer; on a slow scan-out the
  user can see screen tearing during the conversion sweep.

None of these block correct operation at ~11 FPS. Address them when the
target FPS climbs or operators report visible jitter.
