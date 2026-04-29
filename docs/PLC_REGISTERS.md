# PLC Register Map

Modbus TCP address space, one register = one 16-bit word. All addresses
shown as `D<n>` (e.g., `D10` = holding register 10).

## System registers

| D# | R/W | Field | Notes |
|---|---|---|---|
| 50  | RW | plc_heartbeat            | PLC writes; we only read for diagnostics |
| 120 | W  | system_status            | `SystemStatus.value` (STARTING/IDLE/PROCESSING/ERROR) |
| 121 | W  | error_code               | uint16, set when a fatal condition is detected |
| 122 | RW | system_heartbeat         | Toggled 0↔1 once per second by us |
| 123 | W  | camera1_trigger_status   | Reflects the Cam1 hardware trigger source line state |
| 124 | W  | camera2_trigger_status   | Same for Cam2 (signed register, see plc/base.py) |

## Per-camera config block (atomic block read)

Read once per loop iteration: D1+D10..D27 for cam1, D2+D30..D47 for cam2.
The Modbus server snapshots all requested registers at the same instant,
so we cannot observe an inconsistent mid-update state.

### Generic fields (all ProductTypes)

| D# (Cam1) | D# (Cam2) | Field | Type |
|---|---|---|---|
| 1  | 2  | status                  | uint16 — `CameraStatus.value` (10=task, 11=loop) |
| 10 | 30 | trigger                 | uint16 — `CameraTriggerStatus.value` (0=off, 1=hw, 2=sw) |
| 11 | 31 | exposure                | uint16 — microseconds (0 = leave alone) |
| 12-13 | 32-33 | pixel_distance     | float32 — mm per pixel (LE word order) |
| 14 | 34 | product_type            | uint16 — `ProductType.value` (1..3; see below) |

### Algorithm-specific fields

Words **D15..D27** (Cam1) / **D35..D47** (Cam2) are **interpreted by the active Processor** — semantics differ per ProductType. Each Processor reads `raw_config[5..17]` and decodes its own parameters.

## Per-camera result block (atomic block write)

Written once per capture: D70-D86 for cam1, D90-D106 for cam2.

| D# (Cam1) | D# (Cam2) | Field | Type | Notes |
|---|---|---|---|---|
| 70-73 | 90-93 | output_x        | float64 (4 words) | algorithm-specific (see below) |
| 74-77 | 94-97 | output_y        | float64 | |
| 78-81 | 98-101 | output_angle   | float64 | degrees |
| 82    | 102    | result          | uint16 | 1=OK, 2=NG/EXCEPTION |
| 83-84 | 103-104 | area          | uint32 | |
| 85-86 | 105-106 | circularity   | float32 | |

---

## ProductType-specific layouts

### `BRUSH_HEAD` (3) — `BrushHeadProcessor` ✅

Brush-head front/back detection via dot convex hull + density comparison.

**Read fields (D15..D27 for Cam1, +20 for Cam2):**

| Offset | Field | Type | Default if 0 | Range |
|---|---|---|---|---|
| +5 | shrink_pct | uint16 | 15 | 5-30 (% of long/short edge to crop) |
| +6 | adapt_block | uint16 | 31 | 3-99 (forced odd) |
| +7 | adapt_C | int16 | 8 | -128 to 127 (signed) |
| +8 | dot_area_min | uint16 | 20 | 1-65535 (pixels) |
| +9 | dot_area_max | uint16 | 500 | 1-65535 |
| +10-11 | roi_area_min | uint32 | 50000 | LE word order |
| +12-13 | roi_area_max | uint32 | 500000 | |
| +14 | roi_ratio_min × 10 | uint16 | 15 (= 1.5) | |
| +15 | roi_ratio_max × 10 | uint16 | 35 (= 3.5) | |
| +16-17 | reserved | — | — | future: manual ROI corners |

**Result encoding:**

| Field | Meaning |
|---|---|
| `output_x` | side code: 1=Front (upper denser), 2=Back (lower denser), 0=NG |
| `output_y` | always 0 |
| `output_angle` | always 0 |
| `result` | 1 if OK (decisive side found), 2 if NG/EXCEPTION |

### `TOOTHPASTE_FRONTBACK` (1) — `ToothpasteFrontBackProcessor` *(P3)*

Sobel edge counting inside an ROI loaded from `roi_coordinates_<camera_ip>.json` (or PLC-supplied corners). Layout TBD — will likely be:

| Offset | Field | Notes |
|---|---|---|
| +5 | edge_threshold | grayscale cutoff for edge density |
| +6 | edge_count_threshold | min edges → "front" |
| +7-8 | roi_x1 | manual ROI x1 (uint32) |
| +9-10 | roi_y1 | manual ROI y1 |
| +11-12 | roi_x2 | manual ROI x2 |
| +13-14 | roi_y2 | manual ROI y2 |

### `HEIGHT_CHECK` (2) — `HeightCheckProcessor` *(P3)*

Per-column max-Y of a single color-channel threshold, averaged over the top 10 columns. Layout TBD:

| Offset | Field | Notes |
|---|---|---|
| +5 | channel | 0=R, 1=G, 2=B |
| +6 | threshold | grayscale cutoff |
| +7 | min_height | min Y to count |
| +8 | height_decision | OK threshold for max-Y average |
