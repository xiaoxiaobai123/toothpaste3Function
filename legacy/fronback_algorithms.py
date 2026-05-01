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

import asyncio
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
class TopColumn:
    """One column from the top-N max-Y group used in the height average.

    x is the column index in **full image coordinates** (already offset
    by left_limit when an ROI was supplied to compute_height), so the
    display layer can draw markers without re-offsetting.
    """

    x: int
    max_y: int


@dataclass(frozen=True)
class HeightResult:
    """Output of the single-camera height check.

    state: 1=OK (filled below threshold), 2=NG (overfill), 3=EMPTY (no
    column above min_height). Mirrors the original 1/2/3 codes that
    customers' PLCs already interpret.
    max_y_avg: top-N column max-Y average, written to D40.
    top_columns: per-column (x, max_y) for the N columns that contributed
        to the average — used by the display layer to highlight which
        columns the algorithm picked. Tuple (immutable) of length 0..N.
        Default empty so existing callers and tests don't have to change.
    """

    state: int
    max_y_avg: int
    top_columns: tuple[TopColumn, ...] = ()


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


async def compute_frontback_parallel(
    image1: np.ndarray,
    image2: np.ndarray,
    roi1: dict,
    roi2: dict,
    edge_threshold: int = EDGE_INTENSITY_THRESHOLD,
) -> FrontbackResult:
    """Same numerical result as `compute_frontback`, but runs the two
    per-camera Sobel computations concurrently in worker threads.

    On the NanoPi-R5S (4-core Cortex-A55) this halves the algorithm
    portion of the LOOP cycle — Sobel is the long pole and the cv2
    calls release the GIL, so the two threads actually run in parallel.

    Output is byte-identical to the sync version because:
        * `_count_sobel_edges` is pure (no side effects, no shared state).
        * The OK/NG comparison is `e1 > e2`, identical scalars in any order.

    Tests in tests/integration/test_legacy_orchestrator.py verify this
    equivalence on golden inputs.
    """
    e1, e2 = await asyncio.gather(
        asyncio.to_thread(_count_sobel_edges, image1, roi1, edge_threshold),
        asyncio.to_thread(_count_sobel_edges, image2, roi2, edge_threshold),
    )
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
    left_limit: int = 0,
    right_limit: int = 0,
) -> HeightResult:
    """Single-channel column max-Y averaging, matching the original.

    Pipeline (from HeightBasedImageProcessor.process_and_analyze_image):
        1. Take the **red** channel (`image[:, :, 2]` for BGR storage).
        2. Optional: crop columns to [left_limit, right_limit) before mask.
        3. mask = channel > brightness_threshold (255 / 0).
        4. For each column, find the largest Y where mask == 255.
        5. If no column reaches min_height: state = 3 (EMPTY), avg = 0.
        6. Otherwise: average of the top-10 largest max-Y values.
        7. avg >= height_comparison → state = 2 (NG/overfill);
           else                     → state = 1 (OK).

    `left_limit` / `right_limit` correspond to PLC D33 / D34. The
    original toothpastefronback program read those words then never used
    them (verified in `_sources/fronback/HeightBasedImageProcessor.py` —
    it accepted only threshold + threshold_value + height_value). We honor
    them here as an additive backward-compat extension: when both are 0
    (default), behaviour is byte-identical to the original; when set, the
    column scan is restricted to [left_limit, right_limit), filtering out
    edge-of-frame reflections that previously inflated max_y_avg.

    `top_columns` in the result reports the (x, max_y) pairs that fed the
    average — the display layer uses this to highlight which columns the
    algorithm picked, so the operator can verify the height judgement
    visually. Empty tuple for state==3 (no average computed).
    """
    if image.ndim != 3 or image.shape[2] < 3:
        return HeightResult(state=3, max_y_avg=0)

    channel = image[:, :, HEIGHT_CHANNEL_INDEX]
    h_full, w_full = channel.shape

    # ROI crop (D33/D34). 0 = no limit. Inverted/empty range falls back to
    # full frame so a misconfigured PLC value can't silently disable height
    # detection (preferable to reporting EMPTY on every frame).
    x_start = max(0, int(left_limit))
    x_end = min(w_full, int(right_limit)) if right_limit > 0 else w_full
    if x_end <= x_start:
        x_start, x_end = 0, w_full
    cropped = channel[:, x_start:x_end]

    mask = (cropped > brightness_threshold).astype(np.uint8) * 255

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

    # Top-N largest max-Y values, averaged. np.argpartition gives us the
    # indices of the top-N (O(N)) so we can also report which columns
    # contributed to top_columns.
    n = min(TOP_N_HEIGHT_COLUMNS, max_y_per_col.size)
    top_idx = np.argpartition(max_y_per_col, -n)[-n:]
    top_max_ys = max_y_per_col[top_idx]
    avg = int(round(float(np.mean(top_max_ys))))

    # Translate back to full-image x coordinates so the display layer can
    # draw markers on the original frame regardless of any ROI crop.
    top_columns = tuple(
        TopColumn(x=int(idx) + x_start, max_y=int(my)) for idx, my in zip(top_idx, top_max_ys, strict=True)
    )

    state = 2 if avg >= height_comparison else 1
    return HeightResult(state=state, max_y_avg=avg, top_columns=top_columns)
