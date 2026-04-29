"""Display rendering for the legacy fronback protocol.

Existing customers' machines have an external image viewer (typically
`feh` or `fbi`) watching `/tmp/processed_image.png` and rendering it on
the operator screen. The original toothpastefronback program wrote that
file at the end of every detection cycle.

The new binary's v2 path uses a different display chain (rgb565 in
/dev/shm + a separate C image_updater process). For legacy customers,
we keep writing `/tmp/processed_image.png` so their existing display
mechanism continues to work — drop-in compatibility.

What we render mirrors the original:
    Frontback mode:
        [ cam1@0.4x + crosshair + color bar ]
        [   white  ][ separator ][   white  ]
        [ cam2@0.4x + crosshair + color bar ]
        Color bar = blue on the "non-winning" side, grey on the "winning"
        side. Matches process_and_display_with_scale.

    Height mode:
        Raw cam2 frame, written unchanged.
        Matches HeightBasedImageProcessor.process_and_analyze_image,
        which only does `cv2.imwrite(img_file_path, self.original_image)`.

Output path is configurable so tests can write into a tmp_path; default
is the original `/tmp/processed_image.png`.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

DEFAULT_DISPLAY_PATH = "/tmp/processed_image.png"

# Colours from the original program (BGR storage).
_COLOR_LOSER = (255, 0, 0)  # blue bar — what original wrote, unchanged
_COLOR_WINNER = (128, 128, 128)  # grey bar
_COLOR_CROSSHAIR = (0, 255, 0)  # green crosshair lines
_COLOR_BORDER = (255, 255, 255)  # white border + separator

_RESIZE_FACTOR = 0.4
_BAR_HEIGHT = 25
_BORDER_WIDTH = 2
_SEPARATOR_WIDTH = 2


def render_frontback(
    image1: np.ndarray,
    image2: np.ndarray,
    is_front: bool,
    output_path: str | Path = DEFAULT_DISPLAY_PATH,
) -> Path:
    """Build and write the dual-camera operator-screen image.

    `is_front` mirrors the orchestrator's decision: True when cam1 has
    more edges (D0 = 1). The colour-bar mapping below intentionally
    matches the original program (loser-blue, winner-grey).
    """
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
    composed = cv2.hconcat([panel1, separator, panel2])

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), composed)
    return out


def render_height(
    image: np.ndarray,
    output_path: str | Path = DEFAULT_DISPLAY_PATH,
) -> Path:
    """Write the cam2 raw frame to disk — matches the original height path."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), image)
    return out


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
