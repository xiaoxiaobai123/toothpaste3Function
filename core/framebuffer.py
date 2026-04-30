"""Read /dev/fb0's pixel dimensions at startup via ioctl(FBIOGET_VSCREENINFO).

Used by the display compose paths to optionally pre-scale the composed
image to match the framebuffer resolution exactly. When the source
image's dimensions match the framebuffer's, image_updater (v0.3.8+) takes
its NEON-accelerated fast path — no scaling, just RGB565→ARGB conversion
+ direct memcpy. That's roughly 5× faster than the scaling fallback.

If /dev/fb0 isn't accessible (Windows / macOS development machine, or no
fb hardware on the CI runner), returns None and the compose paths ship
the cam-only composition unchanged. image_updater then takes its slow
path with bicubic-ish scaling, same as v0.3.7. Either way, display works.

Why ioctl and not /sys/class/graphics/fb0/virtual_size?
    `virtual_size` reports the virtual screen size, which on multi-buffer
    setups (RK3568 NanoPi included) is 2× or 3× the visible/active height.
    Pre-scaling to the virtual size produces a too-tall payload that
    image_updater's size-mismatch check rejects.

    `FBIOGET_VSCREENINFO.{xres,yres}` reports the visible/active area —
    what's actually being shown on the panel. That's what we want.
"""

from __future__ import annotations

import ctypes
from typing import Any

# fcntl is Linux/BSD only — on Windows the module doesn't exist. We
# import it lazily so this module can still be imported on dev machines;
# get_framebuffer_resolution() returns None there, which is exactly the
# behaviour we want (no pre-scaling, image_updater handles raw composition).
fcntl: Any | None
try:
    import fcntl  # noqa: F811  -- conditional import, mypy/ruff get confused
except ImportError:
    fcntl = None

# From <linux/fb.h>:
#   #define FBIOGET_VSCREENINFO  0x4600
_FBIOGET_VSCREENINFO = 0x4600


class _FbVarScreenInfo(ctypes.Structure):
    """Subset of struct fb_var_screeninfo from <linux/fb.h>.

    We only read xres/yres; the rest is padding so the kernel can write
    its full ~160-byte struct without overflowing.
    """

    _fields_ = (
        ("xres", ctypes.c_uint32),
        ("yres", ctypes.c_uint32),
        ("xres_virtual", ctypes.c_uint32),
        ("yres_virtual", ctypes.c_uint32),
        ("_padding", ctypes.c_uint8 * 256),
    )


_SENTINEL: Any = object()  # marker: not yet attempted
_cached: tuple[int, int] | None | Any = _SENTINEL


def get_framebuffer_resolution() -> tuple[int, int] | None:
    """Return (width, height) of /dev/fb0's visible area, or None if undetectable.

    Cached after the first call — fb resolution doesn't change at runtime
    in our deployment scenarios. If a customer hot-plugs a new display,
    restart main.service to re-detect.
    """
    global _cached
    if _cached is not _SENTINEL:
        return _cached
    if fcntl is None:
        _cached = None
        return _cached
    try:
        with open("/dev/fb0", "rb") as f:
            info = _FbVarScreenInfo()
            fcntl.ioctl(f.fileno(), _FBIOGET_VSCREENINFO, info)
        width = int(info.xres)
        height = int(info.yres)
        # Sanity check: reject obviously bogus values rather than
        # producing a multi-megabyte malformed pre-scaled frame.
        bogus = width <= 0 or height <= 0 or width > 8192 or height > 8192
        _cached = None if bogus else (width, height)
    except (OSError, AttributeError):
        # No /dev/fb0 (Windows, headless CI, container without fb device)
        # or fcntl module missing. Falls through to None.
        _cached = None
    return _cached


def reset_cache_for_tests() -> None:
    """Force re-reading on the next call. Tests only — production uses
    the cached value for the entire process lifetime."""
    global _cached
    _cached = _SENTINEL
