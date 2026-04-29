# toothpaste3Function

Industrial machine-vision system that combines **three product lines** into a single binary, with the optimized display pipeline from the production big/small-circle line. Built for aarch64 (NanoPi-R5S / RK3568) running Debian 11.

## What's in this project

Three PLC-selectable detection modes, each independently selectable per camera via PLC register D14/D34:

| ProductType (PLC value) | Algorithm | Origin |
|---|---|---|
| `TOOTHPASTE_FRONTBACK` (1) | Sobel edge counting in PLC-defined ROI | toothpastefronback ✅ |
| `HEIGHT_CHECK` (2) | Per-column max-Y of color-channel threshold | toothpastefronback ✅ |
| `BRUSH_HEAD` (3) | Adaptive threshold + dot convex hull + upper/lower density compare | toothpasthead ✅ |

The **display pipeline** (tmpfs output, RGB565 conversion, cached overlays, parallel asyncio writes) is ported from the `tianchangbigsmallcircle` display branch — that branch's circle-detection algorithms are *not* part of this project.

> **Status:** P0–P4 complete + legacy fronback compatibility shim (`legacy/`).
> All three algorithms implemented; `tools/simulate.py` runs any algorithm on saved images without hardware; existing toothpastefronback customers can drop in this binary with **zero PLC changes** by setting `"plc_protocol": "legacy_fronback"` in `config.json`.

## Two PLC protocols supported

| `plc_protocol` (in `config.json`) | Use when | Algorithms used | PLC layout |
|---|---|---|---|
| `v2_unified` *(default)* | New deployments (head, future products) | Selectable per-camera via D14/D34 (`ProductType`) | 18-word config blocks at D10..D27 / D30..D47, 17-word result blocks at D70.. / D90.. |
| `legacy_fronback` | Existing toothpastefronback customers — drop-in replacement | TOOTHPASTE_FRONTBACK + HEIGHT_CHECK, mode-switched by D2 | Original protocol: D0/D1/D2/D3/D4 + D10-D11 + D20-D23 + D30-D36 + D40 |

See [`docs/PLC寄存器手册.md`](docs/PLC寄存器手册.md) (中文,工程师手册;包含 legacy 协议附录).

## Performance baseline (inherited from display branch)

- **3 FPS → 11 FPS** through PLC block reads, tmpfs output, OpenCV RGB565 conversion, class-level caches, parallel asyncio writes, and hardware ROI.
- **0 GB/h eMMC writes** — display image lives in `/dev/shm`.
- **~80 ms / frame budget** for cap+algo+write.

## Quick start

```bash
# 1. Install dependencies (dev / x86 host)
python3.11 -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e .[dev,build]

# 2. Configure
cp config.example.json config.json
# edit camera IPs / PLC IP / hardware ROI to match your setup

# 3. Run on hardware
python main.py

# 4. Run display-only test (no camera / no PLC required)
python tools/test_display.py --interval 0 --count 30 --profile

# 5. Run unit tests (no hardware needed)
pytest tests -v
```

## Project layout

```
toothpaste3Function/
├── main.py              entry point
├── main.spec            PyInstaller spec
├── config.example.json  template config
├── company_name.png     logo bar (bundled into binary)
├── core/                cross-cutting infra (log, config, task_manager, license, version)
├── camera/              Hikvision GigE wrapper + manager + (mock — P4)
├── plc/                 Modbus TCP wrapper + register layout + codec helpers + (mock — P4)
├── processing/          detection algorithms + display pipeline
│   ├── algorithms.py    cross-algorithm helpers (parameter clamping, coord conversion)
│   ├── base.py          Processor abstract base class
│   ├── display_utils.py rgb565 + combine + cached bars
│   ├── brush_head.py    ProductType.BRUSH_HEAD
│   └── registry.py      ProductType → Processor lookup
├── tools/               test_display, simulate (P4), benchmark, license-gen
├── deploy/              install / update / uninstall scripts + systemd units
├── docs/                architecture, PLC registers, algorithms, deploy, sim
├── tests/               unit / integration / golden / fixtures
└── .github/workflows/   lint-test (x86) + build-aarch64 (ARM runner) + release
```

## Adding a new detection algorithm

1. New file `processing/<name>.py` with a class inheriting `Processor` and implementing `process(image, settings) -> Outcome`.
2. Add the `ProductType` enum member in `plc/enums.py`.
3. Register the class in `processing/registry.py`.
4. Document the +5..+17 register layout in `docs/PLC_REGISTERS.md`.

The orchestration layer (TaskManager) never changes when you add an algorithm — `BRUSH_HEAD` proves the pattern by reusing only public Processor / Outcome / dispatch APIs.

## Build / deploy

aarch64 builds run on GitHub-hosted ARM runners (`ubuntu-24.04-arm`) — see `.github/workflows/build-aarch64.yml`. Tag a release with `vX.Y.Z` to upload binaries to GitHub Releases via `release.yml`.

For first-time field deployment see [`deploy/README.md`](deploy/README.md).

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module diagram and data flow
- [`docs/PLC_REGISTERS.md`](docs/PLC_REGISTERS.md) — per-ProductType register table
- [`docs/ALGORITHMS.md`](docs/ALGORITHMS.md) — algorithm flowcharts and tuning guide
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — aarch64 deployment
- [`docs/SIMULATION.md`](docs/SIMULATION.md) — running without hardware

## License

Proprietary. License key generated per-device — see `core/license_utils.py`.
