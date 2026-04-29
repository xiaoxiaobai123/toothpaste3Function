# Simulation Mode *(P4)*

Simulation lets the project run without cameras or a PLC, useful for
algorithm tuning, regression testing, and CI golden tests on x86 hosts.

## Components

- `camera/mock.py` — mock CameraBase that returns images from a folder
  in response to capture/trigger calls.
- `plc/mock.py` — mock PLCBase backed by a state-machine YAML file
  describing the timing of register writes.
- `tools/simulate.py` *(planned)* — CLI entry point that wires the mocks
  to the real `TaskManager` and runs scenarios.

## Planned CLI

```bash
# Single algorithm against a single image
python tools/simulate.py \
  --product-type LARGE_CIRCLE \
  --image tests/fixtures/large_circle/sample_01.png \
  --plc-params 'gray_upper=120,area_lower=50000,roi_x=512,roi_y=640'

# Full scenario with mock cameras and a scripted PLC state machine
python tools/simulate.py --scenario tests/scenarios/dual_camera_brush.yml

# Display pipeline only (no camera, no PLC) — works today via tools/test_display.py
python tools/test_display.py --interval 0 --count 30 --profile
```

## Scenario YAML schema *(P4 draft)*

```yaml
cameras:
  1: { product_type: LARGE_CIRCLE, image_dir: tests/fixtures/large_circle/ }
  2: { product_type: BRUSH_HEAD,   image_dir: tests/fixtures/brush/ }

plc_state_machine:
  - at: 0s
    set: { cam1.status: IDLE, cam2.status: IDLE }
  - at: 1s
    set: { cam1.status: START_TASK }
  - at: 2s
    expect: { cam1.result: OK }
  - at: 3s
    set: { cam2.status: START_LOOP }
  - at: 8s
    set: { cam2.status: IDLE }
```

## Golden tests

`tests/golden/` will hold fixed input images alongside expected outputs
(JSON files with center, area, circularity, result). On CI, every
algorithm change reruns the suite — any difference fails the build,
preventing accidental regressions in unrelated algorithms.
