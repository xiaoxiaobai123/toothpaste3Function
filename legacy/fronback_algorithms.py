"""Faithful copies of the original toothpastefronback detection algorithms.

These functions reproduce the behaviour of:
    toothpastefronback/image_processing.py::detect_edges
    toothpastefronback/image_processing.py::process_and_display_with_scale
    toothpastefronback/HeightBasedImageProcessor.py::process_and_analyze_image

The algorithms are kept here (and not in `processing/`) because:
    * The new `processing/toothpaste_frontback.py` and `processing/height_check.py`
      use **single-camera, absolute-threshold** logic that diverges from
      what's deployed in the field.
    * Field customers expect identical results for identical inputs. Any
      refinement to thresholds or filtering would change the boundary
      between OK and NG cases for products already running on these
      machines.
    * The improvements proposed elsewhere (HSV+Otsu, time smoothing,
      gradient magnitude, etc.) are deliberately *not* applied here —
      they live in the v2 path for new customers.

If you need to refactor the algorithms, do it in `processing/` and let
new customers opt in via config.json. Do not change the functions in
this module without explicit confirmation from a deployment owner.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# Constants taken from the original program.
EDGE_INTENSITY_THRESHOLD = 30  # hard-coded in detect_edges (image_processing.py:81)
TOP_N_HEIGHT_COLUMNS = 10  # hard-coded in compare_max_y_coordinates
HEIGHT_CHANNEL_INDEX = 2  # 'red' channel in BGR storage (image[:, :, 2])


@dataclass(frozen=True)
class FrontbackResult:
    """Output of the dual-camera frontback comparison.

    is_front: True ⇔ cam1 has more edges than cam2 (per original semantics).
    edge1_count / edge2_count: per-camera Sobel-X edge counts, written to
    D20-D23 by the legacy protocol layer.
    """

    is_front: bool
    edge1_count: int
    edge2_count: int


@dataclass(frozen=True)
class HeightResult:
    """Output of the single-camera height check.

    state: 1=OK (filled below threshold), 2=NG (overfill), 3=EMPTY (no
    column above min_height). Mirrors the original 1/2/3 codes that
    customers' PLCs already interpret.
    max_y_avg: top-N column max-Y average, written to D40.
    """

    state: int
    max_y_avg: int


# --------------------------------------------------------------------------- #
# Frontback detection
# --------------------------------------------------------------------------- #
def compute_frontback(
    image1: np.ndarray,
    image2: np.ndarray,
    roi1: dict,
    roi2: dict,
    edge_threshold: int = EDGE_INTENSITY_THRESHOLD,
) -> FrontbackResult:
    """Compute frontback decision by comparing edge counts of two cameras.

    The original program's logic is exactly:
        result = (edge1_count > edge2_count)
    No tie-break, no absolute threshold — purely relative. This is the
    rationale behind the dual-camera setup: shop-floor lighting changes
    affect both feeds equally, so the relative comparison is robust.
    """
    e1 = _count_sobel_edges(image1, roi1, edge_threshold)
    e2 = _count_sobel_edges(image2, roi2, edge_threshold)
    return FrontbackResult(is_front=e1 > e2, edge1_count=e1, edge2_count=e2)


def _count_sobel_edges(image: np.ndarray, roi: dict, threshold: int) -> int:
    """Crop ROI -> grayscale -> 3x3 mean blur -> Sobel-X -> count > threshold."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    y1, y2 = int(roi["y1"]), int(roi["y2"])
    x1, x2 = int(roi["x1"]), int(roi["x2"])
    cropped = gray[y1:y2, x1:x2]
    if cropped.size == 0:
        return 0

    # `cv2.blur` with 3x3 box kernel — same as the original.
    blurred = cv2.blur(cropped, (3, 3))
    # Sobel X gradient, ksize=3, CV_64F to keep negative values.
    sobel = cv2.Sobel(blurred, cv2.CV_64F, 1, 0, ksize=3)
    # Absolute value -> uint8.
    abs_sobel = cv2.convertScaleAbs(sobel)
    return int(np.sum(abs_sobel > threshold))


# --------------------------------------------------------------------------- #
# Height detection
# --------------------------------------------------------------------------- #
def compute_height(
    image: np.ndarray,
    brightness_threshold: int,
    min_height: int,
    height_comparison: int,
) -> HeightResult:
    """Single-channel column max-Y averaging, matching the original.

    Pipeline (from HeightBasedImageProcessor.process_and_analyze_image):
        1. Take the **red** channel (`image[:, :, 2]` for BGR storage).
        2. mask = channel > brightness_threshold (255 / 0).
        3. For each column, find the largest Y where mask == 255.
        4. If no column reaches min_height: state = 3 (EMPTY), avg = 0.
        5. Otherwise: average of the top-10 largest max-Y values.
        6. avg >= height_comparison → state = 2 (NG/overfill);
           else                     → state = 1 (OK).
    """
    if image.ndim != 3 or image.shape[2] < 3:
        return HeightResult(state=3, max_y_avg=0)

    channel = image[:, :, HEIGHT_CHANNEL_INDEX]
    mask = (channel > brightness_threshold).astype(np.uint8) * 255

    # Per-column maximum Y where mask is set. Vectorised replacement for
    # the original's per-column np.where(...) loop.
    cols_with_signal = np.any(mask > 0, axis=0)
    if not np.any(cols_with_signal):
        return HeightResult(state=3, max_y_avg=0)

    # Reverse rows so np.argmax finds the LAST 255 (= largest Y) per column.
    last_y_from_bottom = np.argmax(mask[::-1] > 0, axis=0)
    max_y_per_col = mask.shape[0] - 1 - last_y_from_bottom
    # argmax returns 0 for all-zero columns; mask those out.
    max_y_per_col = np.where(cols_with_signal, max_y_per_col, 0)

    if not np.any(max_y_per_col > min_height):
        return HeightResult(state=3, max_y_avg=0)

    # Top-N largest max-Y values, averaged. np.partition is O(N) instead
    # of O(N log N) for sorting all columns just to take the top 10.
    n = min(TOP_N_HEIGHT_COLUMNS, max_y_per_col.size)
    top_n = np.partition(max_y_per_col, -n)[-n:]
    avg = int(round(float(np.mean(top_n))))

    state = 2 if avg >= height_comparison else 1
    return HeightResult(state=state, max_y_avg=avg)
