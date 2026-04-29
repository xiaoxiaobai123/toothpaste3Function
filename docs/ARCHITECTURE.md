# Architecture

## Layers

```
┌──────────────────────────────────────────────────────────────────────┐
│  main.py                                                             │
│      asyncio.run( TaskManager.run() )                                │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
       ┌───────────────────────┼────────────────────────┐
       │                       │                        │
       ▼                       ▼                        ▼
┌────────────┐          ┌────────────┐           ┌──────────────┐
│ camera/    │          │ plc/       │           │ processing/  │
│ Hikvision  │          │ Modbus TCP │           │ Detection    │
│ GigE       │          │ block r/w  │           │ algorithms   │
└─────┬──────┘          └────┬───────┘           └──────┬───────┘
      │                      │                          │
      ▼                      ▼                          ▼
  GigE NIC              Modbus PLC              processing/registry.py
                                                       │
                                                       ▼
                                             ProductType → Processor

                  Display path (every successful capture):
                        Outcome → process_and_combine_images
                                → convert_to_rgb565
                                → save to /dev/shm/output_image.rgb565
                                → C image_updater (inotify)
                                → /dev/fb0
```

## Per-camera asyncio loop (TaskManager)

```
loop:
    settings = await read_plc_settings(camera_num)        # atomic block read
    align_trigger_mode(settings.status)
    await ensure_exposure(...)                            # diff-only, w/ flush
    if status == START_TASK:    process_single_capture()
    elif status == START_LOOP:  process_continuous_capture()
    sleep(0.1)
```

A capture ends with `asyncio.gather(write_result_to_plc, process_combined_results)` — Modbus-network write and disk-image build happen in parallel because they share no state.

## Data contracts

### `dict` returned by `PLCManager.read_camera_settings()`

```python
{
    "status": CameraStatus,
    "trigger_mode": CameraTriggerStatus,
    "exposure_time": int,                          # microseconds
    "pixel_distance": float,                       # mm/px scale
    "product_type": ProductType,
    "raw_config": tuple[int, ...],                 # 18 raw words; processor decodes [5..17]
}
```

PLCManager only decodes the **generic** fields used by every algorithm
(status / trigger / exposure / pixel_distance / product_type). Each
Processor decodes its own parameters from `raw_config[5..17]` using the
shared codec helpers in `plc/codec.py`. This keeps PLC layout knowledge
together with the algorithm that owns it — adding a new ProductType
never requires touching PLCManager.

### `Outcome` returned by every `Processor.process()`

```python
NamedTuple(
    result: ProcessResult,             # OK | NG | EXCEPTION
    image:  np.ndarray,                # BGR with overlays
    center: tuple[float, float],       # image-centered, scaled by pixel_distance
    angle:  float,                     # degrees, 0 if N/A
)
```

## Why subpackages

Each subpackage (`camera`, `plc`, `processing`) has a single external surface area exported through `__init__.py`. Internal helper modules (`base`, `manager`, `algorithms`, …) are imports of the subpackage but not part of the public API. New modules added inside a subpackage do not require touching higher layers.

## Cross-cutting concerns in `core/`

`log_config`, `config_manager`, `version`, `license_utils`, and `task_manager` are all imported by multiple subpackages and have no domain meaning by themselves — they live in `core/` to make those imports symmetric.
