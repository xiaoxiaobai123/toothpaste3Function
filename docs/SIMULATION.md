# Simulation Mode

The project ships with a no-hardware simulation surface for algorithm
development, regression testing, and field-issue reproduction.

## Components

| Module | Purpose |
|---|---|
| `tools/simulate.py` | CLI entry point: run a Processor on saved images. |
| `camera/mock.py` (`MockCameraManager`) | Drop-in replacement for `CameraManager` that serves images from configured directories. |
| `plc/mock.py` (`MockPLCManager`, `MockCameraConfig`) | Drop-in replacement for `PLCManager` backed by an in-memory state. Records all writes in `results_log` for assertions. |

## `tools/simulate.py` — single-image and folder modes

```bash
# Single image, save the overlay PNG
python tools/simulate.py \
    --product-type BRUSH_HEAD \
    --image tests/fixtures/brush/sample.png \
    --out result.png

# Toothpaste with custom thresholds
python tools/simulate.py \
    --product-type TOOTHPASTE_FRONTBACK \
    --image sample.png \
    --param edge_intensity_threshold=40 \
    --param front_count_threshold=2000 \
    --param back_count_threshold=300

# Height check on the green channel
python tools/simulate.py \
    --product-type HEIGHT_CHECK \
    --image sample.png \
    --param channel=1 \
    --param decision_threshold=350

# Batch over a folder, emit JSON one line per frame
python tools/simulate.py \
    --product-type BRUSH_HEAD \
    --folder tests/fixtures/brush/ \
    --json-summary
```

Each `--param key=value` is matched against the field names declared in
the matching Processor module (see `processing/<algo>.py`); unknown
names abort with a usage hint listing the accepted set.

The CLI prints one line per image plus a summary tally:

```
sample_01.png  result=OK   center=(   1.00,    0.00)  took= 12.4ms
sample_02.png  result=OK   center=(   2.00,    0.00)  took= 11.2ms
sample_03.png  result=NG   center=(   0.00,    0.00)  took= 13.1ms

Summary: 3 images  OK=2  NG=1  EXC=0  avg=12.2ms
```

## Full-pipeline simulation with `MockCameraManager` + `MockPLCManager`

For tests that need to exercise the whole `TaskManager` (state machine,
asyncio loops, parallel write+combine), wire the mocks directly:

```python
import asyncio
import logging
from pathlib import Path

from camera.mock import MockCameraManager
from core.task_manager import TaskManager
from plc.enums import CameraStatus, ProductType
from plc.mock import MockCameraConfig, MockPLCManager


async def run_pipeline_for_one_capture():
    plc = MockPLCManager({
        1: MockCameraConfig(
            product_type=ProductType.BRUSH_HEAD,
            status=CameraStatus.START_TASK,
            raw_config=tuple([0, 5000, 0, 0, 3, 15, 31, 8, 20, 500,
                              0, 0, 0, 0, 15, 35, 0, 0]),
        ),
    })
    cam = MockCameraManager({1: Path("tests/fixtures/brush")})

    tm = TaskManager(plc, cam, config=None, logger=logging.getLogger())
    # Run for one cycle; cancel afterwards.
    task = asyncio.create_task(tm.run())
    await asyncio.sleep(0.5)
    task.cancel()

    assert plc.results_log, "TaskManager produced no results"
    print(f"Last result: {plc.results_log[-1]}")
```

Both mocks expose the same public API as the real managers — see
`camera/mock.py` and `plc/mock.py` for the methods covered. Anything
unimplemented is a sign that TaskManager grew a new dependency that we
haven't added a stub for; treat the resulting AttributeError as a test
gap.

## Golden tests *(future)*

`tests/golden/` is reserved for fixed input images alongside expected
outputs (JSON files: center, area, circularity, result). On CI, every
algorithm change reruns the suite — any difference fails the build,
preventing accidental regressions in unrelated algorithms. Populate as
real production images become available.

## Display-only test (legacy)

`tools/test_display.py` predates the simulation framework and tests only
the rgb565 output pipeline (no camera, no PLC, no algorithm). Keep using
it on freshly-imaged target hosts to verify the framebuffer / inotify /
C `image_updater` chain before deploying the full binary.
