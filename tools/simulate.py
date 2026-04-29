#!/usr/bin/env python3
"""Run a single image through a Processor without hardware.

Useful for:
    - Algorithm development / parameter tuning before deploying.
    - Reproducing customer-reported issues from saved frames.
    - Smoke-testing builds on x86 dev machines.

Examples
--------

    # Brush head, default parameters, save the overlay image
    python tools/simulate.py \\
        --product-type BRUSH_HEAD \\
        --image tests/fixtures/brush/sample.png \\
        --out result.png

    # Toothpaste front/back with tighter thresholds
    python tools/simulate.py \\
        --product-type TOOTHPASTE_FRONTBACK \\
        --image sample.png \\
        --param edge_intensity_threshold=40 \\
        --param front_count_threshold=2000 \\
        --param back_count_threshold=300

    # Height check on the green channel
    python tools/simulate.py \\
        --product-type HEIGHT_CHECK \\
        --image sample.png \\
        --param channel=1 \\
        --param decision_threshold=350

    # Batch: run on every image in a folder, summarize results as JSON
    python tools/simulate.py \\
        --product-type BRUSH_HEAD \\
        --folder tests/fixtures/brush/ \\
        --json-summary

Parameters are matched to the field names declared in each Processor's
docstring (see processing/<name>.py). Unknown names abort with an error
listing the accepted names — no silent typos.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

# Make `import core ...` work whether simulate.py is launched from project
# root or any subdirectory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from plc.codec import float32_to_words, uint32_to_words  # noqa: E402
from plc.enums import Endian, ProductType  # noqa: E402
from processing import dispatch  # noqa: E402
from processing.result import Outcome, ProcessResult  # noqa: E402

# ----------------------------------------------------------------------
# Per-ProductType parameter packing into raw_config[5..17].
# Each entry returns (raw_config_tuple, accepted_param_names) given a
# user-supplied dict of overrides.
# ----------------------------------------------------------------------

# The PLC raw_config block is 18 words; words 0..4 are reserved for the
# generic header (trigger / exposure / pixel_distance / product_type),
# leaving 13 algorithm-specific slots.
RAW_CONFIG_SIZE = 18


def _pack_brush_head(overrides: dict[str, float], pixel_distance: float) -> tuple[int, ...]:
    """Layout — see processing/brush_head.py module docstring."""
    accepted = {
        "shrink_pct",
        "adapt_block",
        "adapt_C",
        "dot_area_min",
        "dot_area_max",
        "roi_area_min",
        "roi_area_max",
        "roi_ratio_min",
        "roi_ratio_max",
    }
    _validate(overrides, accepted)

    raw = [0] * RAW_CONFIG_SIZE
    pd = float32_to_words(pixel_distance)
    raw[2], raw[3] = pd[0], pd[1]
    raw[4] = ProductType.BRUSH_HEAD.value
    raw[5] = int(overrides.get("shrink_pct", 0))
    raw[6] = int(overrides.get("adapt_block", 0))
    raw[7] = _signed_int16(int(overrides.get("adapt_C", 0)))
    raw[8] = int(overrides.get("dot_area_min", 0))
    raw[9] = int(overrides.get("dot_area_max", 0))
    rmin = uint32_to_words(int(overrides.get("roi_area_min", 0)), Endian.LITTLE)
    raw[10], raw[11] = rmin[0], rmin[1]
    rmax = uint32_to_words(int(overrides.get("roi_area_max", 0)), Endian.LITTLE)
    raw[12], raw[13] = rmax[0], rmax[1]
    if "roi_ratio_min" in overrides:
        raw[14] = int(round(overrides["roi_ratio_min"] * 10))
    if "roi_ratio_max" in overrides:
        raw[15] = int(round(overrides["roi_ratio_max"] * 10))
    return tuple(raw)


def _pack_toothpaste(overrides: dict[str, float], pixel_distance: float) -> tuple[int, ...]:
    accepted = {
        "edge_intensity_threshold",
        "front_count_threshold",
        "back_count_threshold",
        "roi_x1",
        "roi_y1",
        "roi_x2",
        "roi_y2",
    }
    _validate(overrides, accepted)

    raw = [0] * RAW_CONFIG_SIZE
    pd = float32_to_words(pixel_distance)
    raw[2], raw[3] = pd[0], pd[1]
    raw[4] = ProductType.TOOTHPASTE_FRONTBACK.value
    raw[5] = int(overrides.get("edge_intensity_threshold", 0))
    front = uint32_to_words(int(overrides.get("front_count_threshold", 0)), Endian.LITTLE)
    raw[6], raw[7] = front[0], front[1]
    back = uint32_to_words(int(overrides.get("back_count_threshold", 0)), Endian.LITTLE)
    raw[8], raw[9] = back[0], back[1]
    raw[10] = int(overrides.get("roi_x1", 0))
    raw[11] = int(overrides.get("roi_y1", 0))
    raw[12] = int(overrides.get("roi_x2", 0))
    raw[13] = int(overrides.get("roi_y2", 0))
    return tuple(raw)


def _pack_height_check(overrides: dict[str, float], pixel_distance: float) -> tuple[int, ...]:
    accepted = {
        "channel",
        "pixel_threshold",
        "min_height",
        "decision_threshold",
        "roi_x1",
        "roi_y1",
        "roi_x2",
        "roi_y2",
    }
    _validate(overrides, accepted)

    raw = [0] * RAW_CONFIG_SIZE
    pd = float32_to_words(pixel_distance)
    raw[2], raw[3] = pd[0], pd[1]
    raw[4] = ProductType.HEIGHT_CHECK.value
    raw[5] = int(overrides.get("channel", 0))
    raw[6] = int(overrides.get("pixel_threshold", 0))
    raw[7] = int(overrides.get("min_height", 0))
    raw[8] = int(overrides.get("decision_threshold", 0))
    raw[9] = int(overrides.get("roi_x1", 0))
    raw[10] = int(overrides.get("roi_y1", 0))
    raw[11] = int(overrides.get("roi_x2", 0))
    raw[12] = int(overrides.get("roi_y2", 0))
    return tuple(raw)


_PACKERS = {
    ProductType.BRUSH_HEAD: _pack_brush_head,
    ProductType.TOOTHPASTE_FRONTBACK: _pack_toothpaste,
    ProductType.HEIGHT_CHECK: _pack_height_check,
}


def _validate(overrides: dict[str, float], accepted: set[str]) -> None:
    unknown = set(overrides) - accepted
    if unknown:
        raise SystemExit(
            f"Unknown parameter(s): {sorted(unknown)}.\nAccepted for this ProductType: {sorted(accepted)}"
        )


def _signed_int16(value: int) -> int:
    if value < 0:
        value += 65536
    return value & 0xFFFF


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _parse_overrides(items: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--param value must be key=value, got: {item}")
        key, _, raw = item.partition("=")
        try:
            out[key.strip()] = float(raw)
        except ValueError as e:
            raise SystemExit(f"--param {key} value is not numeric: {raw}") from e
    return out


def _build_settings(
    product_type: ProductType,
    overrides: dict[str, float],
    pixel_distance: float,
) -> dict:
    packer = _PACKERS.get(product_type)
    if packer is None:
        raise SystemExit(
            f"No packer registered for {product_type}; only {sorted(p.name for p in _PACKERS)} are supported"
        )
    raw_config = packer(overrides, pixel_distance)
    return {
        "exposure_time": 5000,
        "pixel_distance": pixel_distance,
        "product_type": product_type,
        "raw_config": raw_config,
        "endian": Endian.LITTLE,
    }


def _run_one(
    image_path: Path,
    product_type: ProductType,
    overrides: dict[str, float],
    pixel_distance: float,
    out_path: Path | None,
) -> tuple[Outcome, float]:
    image = cv2.imread(str(image_path))
    if image is None:
        raise SystemExit(f"Cannot read image: {image_path}")

    processor = dispatch(product_type)
    if processor is None:
        raise SystemExit(f"No Processor registered for {product_type}")

    settings = _build_settings(product_type, overrides, pixel_distance)

    t0 = time.perf_counter()
    outcome = processor.process(image, settings)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_path), outcome.image)

    return outcome, elapsed_ms


def main() -> None:
    p = argparse.ArgumentParser(
        description="Run a Processor on saved images without hardware.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples")[1] if "Examples" in __doc__ else "",
    )
    p.add_argument(
        "--product-type",
        required=True,
        choices=sorted(p.name for p in _PACKERS),
        help="Detection algorithm to run.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--image", type=Path, help="Single image to process.")
    src.add_argument("--folder", type=Path, help="Folder of images to batch-process.")

    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Algorithm parameter override; repeat for multiple. "
        "See processing/<algo>.py for accepted names.",
    )
    p.add_argument("--pixel-distance", type=float, default=1.0, help="mm/pixel scale (default 1.0).")
    p.add_argument("--out", type=Path, help="When --image is set, save overlay image here.")
    p.add_argument("--out-dir", type=Path, help="When --folder is set, save overlay images into this dir.")
    p.add_argument(
        "--json-summary", action="store_true", help="When --folder is set, print one JSON line per image."
    )

    args = p.parse_args()
    product_type = ProductType[args.product_type]
    overrides = _parse_overrides(args.param)

    if args.image is not None:
        outcome, elapsed_ms = _run_one(
            args.image,
            product_type,
            overrides,
            args.pixel_distance,
            args.out,
        )
        print(
            f"{args.image.name}  result={outcome.result.name}  "
            f"center=({outcome.center[0]:.2f}, {outcome.center[1]:.2f})  "
            f"angle={outcome.angle:.2f}  took={elapsed_ms:.1f}ms"
        )
        if args.out is not None:
            print(f"  overlay → {args.out}")
        if outcome.result == ProcessResult.EXCEPTION:
            sys.exit(2)
        return

    # Folder mode.
    if not args.folder.is_dir():
        raise SystemExit(f"Folder not found: {args.folder}")
    paths: list[Path] = []
    for ext in ("*.png", "*.jpg", "*.jpeg"):
        paths.extend(args.folder.glob(ext))
    paths = sorted(set(paths))
    if not paths:
        raise SystemExit(f"No images in {args.folder}")

    counts = {ProcessResult.OK: 0, ProcessResult.NG: 0, ProcessResult.EXCEPTION: 0}
    total_ms = 0.0
    for path in paths:
        out_path = args.out_dir / path.name if args.out_dir else None
        outcome, elapsed_ms = _run_one(
            path,
            product_type,
            overrides,
            args.pixel_distance,
            out_path,
        )
        counts[outcome.result] += 1
        total_ms += elapsed_ms

        if args.json_summary:
            print(
                json.dumps(
                    {
                        "file": path.name,
                        "result": outcome.result.name,
                        "center_x": outcome.center[0],
                        "center_y": outcome.center[1],
                        "angle": outcome.angle,
                        "elapsed_ms": round(elapsed_ms, 2),
                    }
                )
            )
        else:
            print(
                f"{path.name:30s}  {outcome.result.name:9s}  "
                f"center=({outcome.center[0]:7.2f}, {outcome.center[1]:7.2f})  "
                f"took={elapsed_ms:5.1f}ms"
            )

    avg_ms = total_ms / len(paths) if paths else 0.0
    print(
        f"\nSummary: {len(paths)} images  "
        f"OK={counts[ProcessResult.OK]}  "
        f"NG={counts[ProcessResult.NG]}  "
        f"EXC={counts[ProcessResult.EXCEPTION]}  "
        f"avg={avg_ms:.1f}ms"
    )


if __name__ == "__main__":
    main()
