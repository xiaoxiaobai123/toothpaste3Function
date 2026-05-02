"""Display-pipeline helpers: result bar, image combination, RGB565 conversion.

Class-level caches eliminate repeated allocation per frame:
    company bar          one image per output width (logo strip)
    result bar           one image per (width, dtype, ProcessResult)
    combine canvas       one image per (height, total_width)

Combined with the OpenCV-based BGR→RGB565 conversion (3-5x faster than
numpy bit-shifting) and tmpfs output (`/dev/shm`), this is the display
pipeline that took the original implementation from 3 FPS to ~11 FPS.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from core import log_config
from processing.result import ProcessResult

logger = log_config.setup_logging()

_BAR_COLORS: dict[ProcessResult, tuple[int, int, int]] = {
    ProcessResult.OK: (0, 255, 0),
    ProcessResult.NG: (0, 0, 255),
    ProcessResult.EXCEPTION: (128, 128, 128),
}
_BAR_HEIGHT = 90

# Caches keyed by image properties to avoid per-frame allocation.
_company_bar_cache: dict[int, np.ndarray] = {}
_result_bar_cache: dict[tuple[int, str, ProcessResult], np.ndarray] = {}
_combine_canvas_cache: dict[tuple[int, int], np.ndarray] = {}


def put_text_outlined(
    vis: np.ndarray,
    text: str,
    pos: tuple[int, int],
    *,
    color: tuple[int, int, int],
    scale: float = 0.6,
    thickness: int = 1,
    font: int = cv2.FONT_HERSHEY_SIMPLEX,
) -> None:
    """Draw `text` at `pos` with a black outline so it stays legible
    against bright highlights, dark backgrounds, or any gradient in
    between. Outline is `thickness + 2` px black underneath, then the
    requested colour on top.

    Originally lived inside BrushHeadProcessor as `_put_text`; promoted
    to module level in v0.3.23 so all three legacy display paths
    (frontback / height / brush_head) and the offline-placeholder share
    one styling rule.
    """
    cv2.putText(vis, text, pos, font, scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(vis, text, pos, font, scale, color, thickness, cv2.LINE_AA)


def _get_company_bar_path() -> Path:
    """Look up company_name.png next to the binary or in assets/."""
    here = Path(__file__).resolve().parent.parent
    candidates = [here / "company_name.png", here / "assets" / "company_name.png"]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(f"company_name.png not found, searched: {[str(c) for c in candidates]}")


def _get_company_bar(width: int) -> np.ndarray:
    """Return a width-matched company-name bar, cached per width."""
    bar = _company_bar_cache.get(width)
    if bar is not None:
        return bar
    raw = cv2.imread(str(_get_company_bar_path()))
    if raw is None:
        raise ValueError("Failed to read company_name.png")
    if raw.shape[1] != width:
        scale = width / raw.shape[1]
        new_height = int(raw.shape[0] * scale)
        raw = cv2.resize(raw, (width, new_height), interpolation=cv2.INTER_AREA)
    _company_bar_cache[width] = raw
    return raw


def _get_result_bar(width: int, dtype: np.dtype, result: ProcessResult) -> np.ndarray:
    color = _BAR_COLORS.get(result, (128, 128, 128))
    key = (width, dtype.str, result)
    bar = _result_bar_cache.get(key)
    if bar is None:
        bar = np.full((_BAR_HEIGHT, width, 3), color, dtype=dtype)
        _result_bar_cache[key] = bar
    return bar


def add_result_bar(image: np.ndarray, result: ProcessResult) -> np.ndarray:
    height, width = image.shape[:2]
    if not isinstance(result, ProcessResult):
        logger.warning(f"Unexpected result type {type(result)}, using EXCEPTION color")
        result = ProcessResult.EXCEPTION
    bar = _get_result_bar(width, image.dtype, result)
    try:
        return cv2.vconcat([image, bar])
    except cv2.error as e:
        logger.error(f"add_result_bar error: {e} (image {image.shape} bar {bar.shape})")
        return image


def add_company_name(image: np.ndarray) -> np.ndarray:
    bar = _get_company_bar(image.shape[1])
    return cv2.vconcat([bar, image])


def fit_to_framebuffer(image: np.ndarray, fb_size: tuple[int, int]) -> np.ndarray:
    """Scale `image` to fit `fb_size = (width, height)` exactly, preserving
    aspect ratio with black letterbox bands on the under-filled axis.

    The result is byte-identical-shape to the framebuffer, so v0.3.8+
    image_updater takes its NEON fast path: just convert RGB565→ARGB
    row-by-row and memcpy to /dev/fb0 — no scaling code touched.

    For dev environments without a fb (get_framebuffer_resolution() returns
    None), call sites should skip this entirely; image_updater on the
    NanoPi falls back to its scaling path same as pre-v0.3.8.
    """
    fb_w, fb_h = fb_size
    src_h, src_w = image.shape[:2]
    if src_w == fb_w and src_h == fb_h:
        return image  # already-sized, no resampling cost
    scale = min(fb_w / src_w, fb_h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    # INTER_AREA is OpenCV's recommended down-sampler; it also handles
    # up-sampling reasonably for the small ratios we hit in practice
    # (composed cam panels are typically 0.4–0.6× of the fb dimensions).
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)

    canvas = np.zeros((fb_h, fb_w, 3), dtype=np.uint8)  # black letterbox
    x_off = (fb_w - new_w) // 2
    y_off = (fb_h - new_h) // 2
    canvas[y_off : y_off + new_h, x_off : x_off + new_w] = resized
    return canvas


def combine_images(images: list[np.ndarray]) -> np.ndarray:
    """Side-by-side concat of two camera images with a 10px white divider.

    Uses a cached canvas so we skip np.zeros allocation on every frame.
    """
    assert len(images) == 2, "combine_images expects exactly two images"
    height, width = images[0].shape[:2]
    out_w = width * 2 + 10

    canvas = _combine_canvas_cache.get((height, out_w))
    if canvas is None:
        canvas = np.empty((height, out_w, 3), dtype=np.uint8)
        canvas[:, width : width + 10] = (255, 255, 255)
        _combine_canvas_cache[(height, out_w)] = canvas

    canvas[:, :width] = images[0]
    canvas[:, width + 10 :] = images[1]
    return canvas


def process_and_combine_images(results: dict[int, object]) -> np.ndarray | None:
    """Build the operator-screen image from per-camera Outcome tuples.

    Single camera: skip combine (~40 ms saved), use that image directly.
    Two cameras:   side-by-side concat with divider.
    """
    target_size = (1024, 1280)
    images: list[np.ndarray] = []

    for camera_num, result in results.items():
        if result is None:
            img = np.full((*target_size, 3), [0, 255, 0], dtype=np.uint8)
            process_result: ProcessResult = ProcessResult.EXCEPTION
        else:
            # `result` is a tuple/Outcome: (process_result, image, center, angle)
            process_result, result_image = result[0], result[1]
            img = result_image

        try:
            img = add_result_bar(img, process_result)
        except Exception as e:
            logger.error(f"add_result_bar failed for cam{camera_num}: {e}")
        images.append(img)

    try:
        if len(images) == 1:
            combined = images[0]
        elif len(images) == 2:
            combined = combine_images(images)
        else:
            combined = images[0] if images else None
        if combined is None:
            return None
        composed = add_company_name(combined)
        # Optional pre-scale to framebuffer dims so image_updater (v0.3.8+)
        # takes its NEON fast path. No-op when no /dev/fb0 (dev env, CI).
        from core.framebuffer import get_framebuffer_resolution

        fb_size = get_framebuffer_resolution()
        if fb_size is not None:
            composed = fit_to_framebuffer(composed, fb_size)
        return composed
    except Exception as e:
        logger.error(f"combine/add_company_name failed: {e}")
        return images[0] if images else None


def convert_to_rgb565(image: np.ndarray) -> np.ndarray | None:
    """BGR → RGB565 using OpenCV's native C/SIMD path.

    Output shape (H, W, 2) uint8 — 3-5x faster than numpy bit-shifting,
    written byte-for-byte to disk by save_rgb565_with_header.
    """
    if image is None:
        return None
    return cv2.cvtColor(image, cv2.COLOR_BGR2BGR565)


def save_rgb565_with_header(image: np.ndarray, filename: str) -> None:
    """Write a 2-int32 (width, height) header + raw pixels — direct write.

    NOT an atomic rename. The C ``image_updater`` watches ``filename`` with
    inotify ``IN_CLOSE_WRITE`` only, and ``rename(2)`` on the destination
    fires ``IN_MOVED_TO`` on the parent directory + ``IN_MOVE_SELF`` on the
    old inode — neither is ``IN_CLOSE_WRITE``. Atomic-rename here would
    silently break the display refresh chain on every customer that uses
    the image_updater rgb565 pipeline (this is the v0.3.0–v0.3.2 bug fixed
    in v0.3.3).

    Tearing isn't a real concern with ``IN_CLOSE_WRITE``: it fires only
    when the file descriptor closes, after the full 2.5 MB payload has
    been written. The original tearing concern was about ``IN_MODIFY``,
    which fires mid-write — but that's not what image_updater listens to.

    Accepts (H, W) uint16 (numpy fallback) and (H, W, 2) uint8 (OpenCV).
    """
    if image.ndim == 3:
        height, width, _ = image.shape
    else:
        height, width = image.shape
    header = np.array([width, height], dtype=np.int32)

    with open(filename, "wb") as f:
        f.write(header.tobytes())
        f.write(image.tobytes())


def clear_caches() -> None:
    """Drop all caches; useful for tests verifying allocation counts."""
    _company_bar_cache.clear()
    _result_bar_cache.clear()
    _combine_canvas_cache.clear()
