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

from pathlib import Path

import cv2
import numpy as np

from processing.display_utils import convert_to_rgb565, save_rgb565_with_header

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


def compose_frontback(image1: np.ndarray, image2: np.ndarray, is_front: bool) -> np.ndarray:
    """Build the dual-camera composed BGR image. Pure CPU, no I/O."""
    panel1 = _build_panel(image1)
    panel2 = _build_panel(image2)

    # Original logic:
    #   if not result:                     # cam2 won (= "Back")
    #       img1 -> blue bar, img2 -> grey
    #   else:                              # cam1 won (= "Front")
    #       img2 -> blue bar, img1 -> grey
    if is_front:
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
    return cv2.hconcat([panel1, separator, panel2])


def render_frontback(
    image1: np.ndarray,
    image2: np.ndarray,
    is_front: bool,
    png_path: str | Path | None = DEFAULT_PNG_PATH,
    rgb565_path: str | Path | None = DEFAULT_RGB565_PATH,
) -> np.ndarray:
    """Compose + write to all configured display sinks.

    Either path can be set to None to skip that sink. Default writes both;
    most legacy customers' image_updater watches the rgb565 sink, while
    older sites still using feh/fbi watch the PNG one.
    """
    composed = compose_frontback(image1, image2, is_front)
    _write_sinks(composed, png_path, rgb565_path)
    return composed


def render_height(
    image: np.ndarray,
    png_path: str | Path | None = DEFAULT_PNG_PATH,
    rgb565_path: str | Path | None = DEFAULT_RGB565_PATH,
) -> np.ndarray:
    """Write the raw cam2 frame to display sinks — matches the original height path."""
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
