"""Display rendering for the legacy fronback protocol.

Two output sinks every cycle, written in parallel:

1. ``/home/pi/output_image.rgb565`` — the file that the C ``image_updater``
   process watches via inotify and renders on /dev/fb0. This is what
   actually shows up on the operator screen for sites running the
   image_updater chain.

2. ``/tmp/processed_image.png`` — the file that the original
   toothpastefronback program wrote, used by older sites running
   ``feh`` / ``fbi`` instead of image_updater. Cheap to keep writing
   even on machines that no longer use it (PNG encode + write of a
   small composed image is sub-millisecond on tmpfs).

Composition mirrors the original program:
    Frontback mode:
        [ cam1@0.4x + crosshair + colour bar ]
        [   white  ][ separator ][   white   ]
        [ cam2@0.4x + crosshair + colour bar ]
        Colour bar = blue on the loser, grey on the winner — matches
        process_and_display_with_scale's mapping.

    Height mode:
        Raw cam2 frame, written unchanged. Matches
        HeightBasedImageProcessor.process_and_analyze_image, which only
        called ``cv2.imwrite(self.img_file_path, self.original_image)``.

Both paths are configurable so tests can write into a tmp_path.
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

from core.framebuffer import get_framebuffer_resolution
from processing.display_utils import (
    add_company_name,
    convert_to_rgb565,
    fit_to_framebuffer,
    save_rgb565_with_header,
)

logger = logging.getLogger(__name__)


def _maybe_fit_to_fb(image: np.ndarray) -> np.ndarray:
    """Pre-scale to framebuffer dims when /dev/fb0 is detectable; otherwise
    return the image unchanged. v0.3.8+ image_updater fast-paths the
    pre-scaled case (NEON RGB565→ARGB + memcpy)."""
    fb_size = get_framebuffer_resolution()
    if fb_size is None:
        return image
    return fit_to_framebuffer(image, fb_size)

DEFAULT_PNG_PATH = "/tmp/processed_image.png"
DEFAULT_RGB565_PATH = "/home/pi/output_image.rgb565"

# Colours from the original program (BGR storage).
_COLOR_LOSER = (255, 0, 0)  # blue bar — what original wrote, unchanged
_COLOR_WINNER = (128, 128, 128)  # grey bar
_COLOR_CROSSHAIR = (0, 255, 0)  # green crosshair lines
_COLOR_BORDER = (255, 255, 255)  # white border + separator

_RESIZE_FACTOR = 0.4
_BAR_HEIGHT = 25
_BORDER_WIDTH = 2
_SEPARATOR_WIDTH = 2

# ROI rectangle — drawn on the original full-res image before _build_panel
# resizes it down by 0.4x, so the line ends up ~1.6 px on the panel: clearly
# visible without overpowering the actual image.
_ROI_COLOR = (0, 255, 255)  # yellow (BGR)
_ROI_THICKNESS = 4

# Default placeholder size when both cameras are missing — typical Hikvision
# GigE frame is 1280x800 (the resize step crunches it down to 0.4x anyway,
# so the exact value mostly affects text legibility).
_PLACEHOLDER_DEFAULT_SIZE = (800, 1280)  # (height, width)


def _draw_roi(image: np.ndarray, roi: dict[str, int]) -> np.ndarray:
    """Stamp a yellow rectangle showing the algorithm ROI on a copy of `image`.

    ROI keys: x1/y1/x2/y2 in original-image pixel coordinates (matches
    `_count_sobel_edges` slicing). Coordinates are clamped to image bounds
    so a malformed ROI file (e.g. coordinates beyond frame size) doesn't
    crash render.
    """
    out = image.copy()
    h, w = out.shape[:2]
    x1 = max(0, min(w - 1, int(roi.get("x1", 0))))
    y1 = max(0, min(h - 1, int(roi.get("y1", 0))))
    x2 = max(0, min(w - 1, int(roi.get("x2", w - 1))))
    y2 = max(0, min(h - 1, int(roi.get("y2", h - 1))))
    if x2 > x1 and y2 > y1:
        cv2.rectangle(out, (x1, y1), (x2, y2), _ROI_COLOR, _ROI_THICKNESS)
    return out


def _prep_panel_image(
    image: np.ndarray | None,
    roi: dict[str, int] | None,
    cam_num: int,
    ref_shape: tuple[int, int] | None,
) -> np.ndarray:
    """Resolve image vs OFFLINE placeholder, and stamp the ROI rectangle
    on the camera image when one is provided. The placeholder branch
    intentionally skips ROI — there's no algorithm running, so no ROI
    is meaningful."""
    if image is None:
        return _offline_placeholder(cam_num, ref_shape)
    if roi is None:
        return image
    return _draw_roi(image, roi)


def _offline_placeholder(camera_num: int, ref_shape: tuple[int, int] | None = None) -> np.ndarray:
    """Black panel showing 'CAM N OFFLINE' + a hint to check cable/power/IP.

    Used by `compose_frontback` when one camera failed to capture but the
    other succeeded — substituting this for the missing image lets the
    operator screen still update each cycle, so a dropped camera shows up
    immediately instead of a frozen old frame.

    `ref_shape` is `(height, width)` of the working camera's frame; matching
    those dimensions keeps the dual-panel layout balanced visually.
    """
    h, w = ref_shape if ref_shape is not None else _PLACEHOLDER_DEFAULT_SIZE
    panel = np.zeros((h, w, 3), dtype=np.uint8)

    title = f"CAM {camera_num} OFFLINE"
    title_scale = max(1.5, w / 600.0)
    title_thickness = max(2, int(title_scale * 2))
    (tw, th), _ = cv2.getTextSize(title, cv2.FONT_HERSHEY_SIMPLEX, title_scale, title_thickness)
    cv2.putText(
        panel, title,
        ((w - tw) // 2, h // 2 - 20),
        cv2.FONT_HERSHEY_SIMPLEX, title_scale, (0, 0, 255), title_thickness, cv2.LINE_AA,
    )

    hint = "CHECK CABLE / POWER / IP"
    hint_scale = max(0.7, w / 1200.0)
    hint_thickness = max(1, int(hint_scale * 1.5))
    (hw_, _), _ = cv2.getTextSize(hint, cv2.FONT_HERSHEY_SIMPLEX, hint_scale, hint_thickness)
    cv2.putText(
        panel, hint,
        ((w - hw_) // 2, h // 2 + th + 30),
        cv2.FONT_HERSHEY_SIMPLEX, hint_scale, (255, 255, 255), hint_thickness, cv2.LINE_AA,
    )

    return panel


def compose_frontback(
    image1: np.ndarray | None,
    image2: np.ndarray | None,
    is_front: bool,
    roi1: dict[str, int] | None = None,
    roi2: dict[str, int] | None = None,
) -> np.ndarray:
    """Build the dual-camera composed BGR image. Pure CPU, no I/O.

    Either image may be None — a 'CAM N OFFLINE' placeholder is substituted
    so the operator screen stays live and shows which camera dropped. When
    a camera is offline `is_front` is meaningless (the algorithm wasn't
    run), so both panels get the loser-colour bar to avoid implying a
    spurious pass/fail result.

    Optional `roi1` / `roi2`: when supplied, a yellow rectangle is drawn
    on the corresponding camera image showing the algorithm's region of
    interest. Lets the operator visually confirm the ROI is positioned
    over the inspected product. ROI is skipped when its camera is offline
    (no algorithm running, no ROI to show).
    """
    # Match placeholder dimensions to whichever camera DID capture, so the
    # two panels stay the same size after the 0.4x downscale.
    ref_shape: tuple[int, int] | None = None
    if image1 is not None:
        ref_shape = image1.shape[:2]
    elif image2 is not None:
        ref_shape = image2.shape[:2]

    img1 = _prep_panel_image(image1, roi1, 1, ref_shape)
    img2 = _prep_panel_image(image2, roi2, 2, ref_shape)

    panel1 = _build_panel(img1)
    panel2 = _build_panel(img2)

    one_offline = image1 is None or image2 is None
    if one_offline:
        # Algorithm didn't run; show neutral loser-colour on both to avoid
        # signalling a winner.
        panel1 = _add_color_bar(panel1, _COLOR_LOSER)
        panel2 = _add_color_bar(panel2, _COLOR_LOSER)
    elif is_front:
        # Original logic:
        #   if not result:                     # cam2 won (= "Back")
        #       img1 -> blue bar, img2 -> grey
        #   else:                              # cam1 won (= "Front")
        #       img2 -> blue bar, img1 -> grey
        panel1 = _add_color_bar(panel1, _COLOR_WINNER)
        panel2 = _add_color_bar(panel2, _COLOR_LOSER)
    else:
        panel1 = _add_color_bar(panel1, _COLOR_LOSER)
        panel2 = _add_color_bar(panel2, _COLOR_WINNER)

    panel1 = cv2.copyMakeBorder(
        panel1,
        _BORDER_WIDTH,
        _BORDER_WIDTH,
        _BORDER_WIDTH,
        _BORDER_WIDTH,
        cv2.BORDER_CONSTANT,
        value=list(_COLOR_BORDER),
    )
    panel2 = cv2.copyMakeBorder(
        panel2,
        _BORDER_WIDTH,
        _BORDER_WIDTH,
        _BORDER_WIDTH,
        _BORDER_WIDTH,
        cv2.BORDER_CONSTANT,
        value=list(_COLOR_BORDER),
    )

    separator = np.full((panel1.shape[0], _SEPARATOR_WIDTH, 3), _COLOR_BORDER, dtype=np.uint8)
    composed = cv2.hconcat([panel1, separator, panel2])

    # Stamp the company-name banner on top — same width-keyed cache as the
    # v2 path uses (processing/display_utils._company_bar_cache), so the
    # PNG is read from disk only on the first frame of each output width
    # and reused thereafter (~0 ms overhead per subsequent frame).
    #
    # Tolerant: if company_name.png is missing or unreadable on a fresh dev
    # machine, log a warning and ship the cam-only composition. Display
    # never blocks on branding artefacts.
    try:
        return add_company_name(composed)
    except Exception as e:
        logger.warning(f"[Legacy] company_name overlay skipped: {e}")
        return composed


def render_frontback(
    image1: np.ndarray | None,
    image2: np.ndarray | None,
    is_front: bool,
    png_path: str | Path | None = DEFAULT_PNG_PATH,
    rgb565_path: str | Path | None = DEFAULT_RGB565_PATH,
    roi1: dict[str, int] | None = None,
    roi2: dict[str, int] | None = None,
) -> np.ndarray:
    """Compose + write to all configured display sinks.

    Either path can be set to None to skip that sink. Default writes both;
    most legacy customers' image_updater watches the rgb565 sink, while
    older sites still using feh/fbi watch the PNG one.

    Either image may be None — `compose_frontback` substitutes an OFFLINE
    placeholder so the operator screen still updates with which camera
    dropped. Caller is responsible for skipping algorithm/PLC writes in
    that case (see `LegacyFronbackOrchestrator._do_frontback`).

    v0.3.8+: when /dev/fb0 is detectable, the composition is pre-scaled to
    match its dimensions exactly. This lets image_updater's NEON fast path
    fire (RGB565→ARGB + direct memcpy, no scaling code touched), cutting
    render time roughly 5× at 1080p. On dev machines without a fb the
    composition ships unchanged and image_updater's slow path scales it.

    v0.3.9+: optional `roi1` / `roi2` overlay yellow rectangles on the
    corresponding camera images, showing where the algorithm's ROI is
    actually being computed.
    """
    composed = compose_frontback(image1, image2, is_front, roi1=roi1, roi2=roi2)
    composed = _maybe_fit_to_fb(composed)
    _write_sinks(composed, png_path, rgb565_path)
    return composed


def render_height(
    image: np.ndarray,
    png_path: str | Path | None = DEFAULT_PNG_PATH,
    rgb565_path: str | Path | None = DEFAULT_RGB565_PATH,
) -> np.ndarray:
    """Write the raw cam2 frame to display sinks — matches the original height path.

    v0.3.8+: pre-scales to /dev/fb0 dims when available (same fast-path
    optimisation as render_frontback). Pure no-op for tests / dev env.
    """
    image = _maybe_fit_to_fb(image)
    _write_sinks(image, png_path, rgb565_path)
    return image


def _write_sinks(
    image: np.ndarray,
    png_path: str | Path | None,
    rgb565_path: str | Path | None,
) -> None:
    """Common writer for both rendering modes.

    Writes are independent — a failure on one path doesn't skip the other.
    The orchestrator wraps the whole render call in try/except so display
    failures never block PLC writes.
    """
    if png_path is not None:
        out = Path(png_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), image)

    if rgb565_path is not None:
        rgb565 = convert_to_rgb565(image)
        if rgb565 is not None:
            out = Path(rgb565_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            save_rgb565_with_header(rgb565, str(out))


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _build_panel(image: np.ndarray) -> np.ndarray:
    """Resize to 40 % and stamp a green crosshair through the centre."""
    width = max(1, int(image.shape[1] * _RESIZE_FACTOR))
    height = max(1, int(image.shape[0] * _RESIZE_FACTOR))
    resized = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA).copy()
    cx, cy = width // 2, height // 2
    cv2.line(resized, (cx, 0), (cx, height), _COLOR_CROSSHAIR, 1)
    cv2.line(resized, (0, cy), (width, cy), _COLOR_CROSSHAIR, 1)
    return resized


def _add_color_bar(panel: np.ndarray, color: tuple[int, int, int]) -> np.ndarray:
    bar = np.full((_BAR_HEIGHT, panel.shape[1], 3), color, dtype=np.uint8)
    return cv2.vconcat([panel, bar])
