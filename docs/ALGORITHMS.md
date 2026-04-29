# Algorithms

This document describes each detection algorithm's pipeline, default
parameters, and tuning notes.

## `BRUSH_HEAD` (`BrushHeadProcessor`) ✅

Brush-head front/back classification by comparing dark-pixel densities of
the upper and lower halves of the head ROI.

### Pipeline

```
BGR image
  └─► grayscale + Gaussian blur 5x5
      └─► adaptiveThreshold (GAUSSIAN_C, INV, block, C)
          └─► morphology open (3x3 ellipse)
              └─► find contours, filter by [dot_area_min, dot_area_max]
                  └─► extract centroids
                      └─► need ≥ 10 dots → convex hull
                          └─► minAreaRect(hull) → (rect, box)
                              └─► validate area + aspect ratio
                                  └─► rotate image so long edge is horizontal
                                      └─► shrink ROI by shrink_pct%
                                          └─► split crop into upper / lower halves
                                              └─► adaptiveThreshold each half
                                                  └─► count dark pixels (density)
                                                      └─► upper > lower : Front (1)
                                                      └─► lower > upper : Back  (2)
                                                      └─► tied         : NG    (0)
```

### Result

`Outcome.center.x` carries the side code (1 / 2 / 0); `y` and `angle` are 0.
The TaskManager writes side code into PLC `output_x` so the PLC ladder can
gate downstream actuators.

### Tuning

- **False NG (no ROI found)**: dots are too few — widen `dot_area_min..max`,
  or check exposure (over/underexposed bristles disappear at `adaptiveThreshold`).
- **Wrong rotation**: head shape too symmetric, `aspect_ratio` rejects it
  — relax `roi_ratio_min` toward 1.0.
- **Side flips between consecutive captures**: density difference is too
  small (`diff_pct < ~5%`). Check lighting uniformity; bristles must cast
  visibly different shadows for one side.

### PLC parameter mapping

See [`PLC_REGISTERS.md`](PLC_REGISTERS.md#brush_head-3--brushheadprocessor-).

---

## `TOOTHPASTE_FRONTBACK` (`ToothpasteFrontBackProcessor`) ✅

Algorithm imported from `toothpastefronback/image_processing.py`.

```
BGR image
  └─► crop PLC-supplied ROI (full frame if all corners are 0)
      └─► grayscale
          └─► mean blur 3x3
              └─► Sobel X (CV_64F, ksize=3)
                  └─► absolute value, convertScaleAbs (uint8)
                      └─► count pixels above edge_intensity_threshold
                          └─► compare to front / back thresholds
                              └─► count <  back  : EXCEPTION (no product)
                              └─► count >= front : Front (1, OK)
                              └─► else           : Back  (2, OK)
```

Outcome encoding: `center.x = side code (1/2/0)`, `center.y = edge count`.

**Tuning**:
- **All frames classified as EXCEPTION**: `back_count_threshold` is too
  high — capture a "no product" frame and check the logged edge count.
- **Front and back never separate**: increase `edge_intensity_threshold`
  (e.g. 30 → 50) to reject low-contrast noise; this widens the gap
  between front (lots of relief / text) and back (mostly smooth tube).

---

## `HEIGHT_CHECK` (`HeightCheckProcessor`) ✅

Algorithm imported from `toothpastefronback/HeightBasedImageProcessor.py`.

```
BGR image
  └─► crop PLC-supplied ROI (full frame if all corners are 0)
      └─► extract single channel (R/G/B per channel parameter)
          └─► threshold > pixel_threshold → 255 else 0
              └─► for each column, find largest Y where mask is 255
                  └─► no column reaches min_height : EMPTY (3)
                  └─► top-10 column average:
                          max_y_avg <  decision : OK   (1)
                          max_y_avg >= decision : HIGH (2)
```

Outcome encoding: `center.x = state code (1/2/3/0)`, `center.y = max_y_avg`.

**Tuning**:
- **Always reads EMPTY (3)**: `pixel_threshold` is too high — most frames'
  pixels can't reach it. Drop it (e.g. 100 → 60) until the algorithm
  starts seeing fill.
- **Always reads HIGH (2)**: `decision_threshold` is too small for the
  ROI height. Raise it.
- **Image Y grows downward**: a lower `max_y_avg` means the toothpaste
  reached higher in the frame. If your camera mount inverts the scene,
  remap meaning OK ↔ HIGH on-site rather than negating values.

---

## Architecture: how processors plug in

Every algorithm is a `Processor` subclass with one method:

```python
class Processor(ABC):
    name: str

    @abstractmethod
    def process(self, image: np.ndarray, settings: dict[str, Any]) -> Outcome:
        ...
```

`settings` is the dict from `PLCManager.read_camera_settings()`:

```python
{
    "status": CameraStatus,
    "trigger_mode": CameraTriggerStatus,
    "exposure_time": int,
    "pixel_distance": float,
    "product_type": ProductType,
    "raw_config": tuple[int, ...],   # 18 raw words; processor decodes [5..17]
    "endian": Endian,                # optional, default LITTLE
}
```

The Processor decodes its parameters from `raw_config[5..]` using the
shared codec helpers in `plc/codec.py` (`words_to_uint32`, `word_to_int16`,
`words_to_float32`, …) — keeping decoding logic alongside each algorithm
instead of in PLCManager.

`Outcome` is a fixed-shape NamedTuple all processors return:

```python
NamedTuple(
    result: ProcessResult,             # OK | NG | EXCEPTION
    image:  np.ndarray,                # BGR with overlays
    center: tuple[float, float],       # algorithm-specific encoding
    angle:  float,                     # degrees, 0 if N/A
)
```
