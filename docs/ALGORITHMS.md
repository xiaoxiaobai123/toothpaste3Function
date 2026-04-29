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

## `TOOTHPASTE_FRONTBACK` *(P3)*

Algorithm imported from `toothpastefronback/image_processing.py`:

```
BGR image
  └─► load ROI from roi_coordinates_<camera_ip>.json or PLC params
      └─► crop ROI from grayscale image
          └─► mean blur 3x3
              └─► Sobel X (CV_64F, ksize=3)
                  └─► absolute value, convertScaleAbs
                      └─► count pixels above edge_threshold
                          └─► compare count to edge_count_threshold
                              └─► above → Front (OK)
                              └─► below → Back (NG)
```

Output `Outcome.center.x` carries the front/back code, `output_angle` reflects
edge density (informational).

---

## `HEIGHT_CHECK` *(P3)*

Algorithm imported from `toothpastefronback/HeightBasedImageProcessor.py`:

```
BGR image
  └─► extract single channel (R/G/B as configured)
      └─► threshold > threshold_value → 255 else 0
          └─► for each column, find largest Y where mask is 255
              └─► sort, take top 10
                  └─► average → max_y_avg
                      └─► max_y_avg < height_decision : 1 (OK, full)
                      └─► max_y_avg ≥ height_decision : 2 (NG, low)
                      └─► no row > min_height          : 3 (empty)
```

Output `Outcome.center.x` carries the 1/2/3 code, `output_y` carries
`max_y_avg` for HMI display.

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
