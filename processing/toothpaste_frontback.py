"""Toothpaste front/back detection (ProductType.TOOTHPASTE_FRONTBACK).

Algorithm imported from toothpastefronback/image_processing.py:detect_edges.

Pipeline:
    1. Crop ROI from the captured image (PLC-supplied corners; full frame
       if all corners are 0).
    2. Convert to grayscale, mean-blur 3x3.
    3. Sobel X edge detection (CV_64F, ksize=3) → absolute → uint8.
    4. Count pixels above edge_intensity_threshold.
    5. Compare count to thresholds:
         count <  back_count_threshold   → EXCEPTION (no product detected)
         count >= front_count_threshold  → Front (1, OK)
         else                            → Back  (2, OK)

PLC parameter layout (raw_config[5..15]):
    +5     edge_intensity_threshold  uint16  default 30   (0-255)
    +6-7   front_count_threshold     uint32  default 1000 (LE word order)
    +8-9   back_count_threshold      uint32  default 100
    +10    roi_x1                    uint16  (0 = full frame)
    +11    roi_y1                    uint16
    +12    roi_x2                    uint16
    +13    roi_y2                    uint16

Result encoding in Outcome.center:
    x = side code (1=Front, 2=Back, 0=EXCEPTION)
    y = edge count (informational, for HMI tuning)
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from core import log_config
from plc.codec import words_to_uint32
from plc.enums import Endian
from processing.algorithms import validate_and_adjust_param
from processing.base import Processor
from processing.result import Outcome, ProcessResult

logger = log_config.setup_logging()


class ToothpasteFrontBackProcessor(Processor):
    name = "ToothpasteFrontBack"

    DEFAULTS: dict[str, float] = {
        "edge_intensity_threshold": 30,
        "front_count_threshold": 1000,
        "back_count_threshold": 100,
    }

    def process(self, image: np.ndarray, settings: dict[str, Any]) -> Outcome:
        try:
            params = self._parse_params(settings)
            h, w = image.shape[:2]

            # Resolve ROI: 0 means "use full frame" (PLC-friendly default).
            rx1 = int(params["roi_x1"]) if params["roi_x1"] > 0 else 0
            ry1 = int(params["roi_y1"]) if params["roi_y1"] > 0 else 0
            rx2 = int(params["roi_x2"]) if params["roi_x2"] > 0 else w
            ry2 = int(params["roi_y2"]) if params["roi_y2"] > 0 else h

            rx1, rx2 = max(0, min(rx1, w)), max(0, min(rx2, w))
            ry1, ry2 = max(0, min(ry1, h)), max(0, min(ry2, h))

            if rx2 <= rx1 or ry2 <= ry1:
                logger.warning(f"[Toothpaste] invalid ROI ({rx1},{ry1})-({rx2},{ry2}); using full frame")
                rx1, ry1, rx2, ry2 = 0, 0, w, h

            roi = image[ry1:ry2, rx1:rx2]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if roi.ndim == 3 else roi

            blurred = cv2.blur(gray, (3, 3))
            sobel = cv2.Sobel(blurred, cv2.CV_64F, 1, 0, ksize=3)
            sobel_abs = cv2.convertScaleAbs(sobel)
            edge_count = int(np.sum(sobel_abs > params["edge_intensity_threshold"]))

            if edge_count < params["back_count_threshold"]:
                side_code = 0
                result = ProcessResult.EXCEPTION
                label = "NO PRODUCT"
            elif edge_count >= params["front_count_threshold"]:
                side_code = 1
                result = ProcessResult.OK
                label = "FRONT (1)"
            else:
                side_code = 2
                result = ProcessResult.OK
                label = "BACK (2)"

            logger.info(
                f"[Toothpaste] {label} edges={edge_count} "
                f"(thresh: back<{params['back_count_threshold']:.0f} "
                f"front>={params['front_count_threshold']:.0f}) "
                f"ROI=({rx1},{ry1})-({rx2},{ry2})"
            )

            vis = self._draw_results(image, (rx1, ry1, rx2, ry2), edge_count, side_code, label, params)
            return Outcome(result, vis, (float(side_code), float(edge_count)), 0.0)

        except Exception as e:
            logger.exception(f"[Toothpaste] exception: {e}")
            fail_vis = image.copy() if image is not None else np.zeros((100, 100, 3), dtype=np.uint8)
            cv2.putText(
                fail_vis,
                f"Exception: {str(e)[:60]}",
                (30, 50),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 0, 255),
                2,
            )
            return Outcome(ProcessResult.EXCEPTION, fail_vis, (0.0, 0.0), 0.0)

    # ------------------------------------------------------------------
    # PLC parameter parsing
    # ------------------------------------------------------------------
    def _parse_params(self, settings: dict[str, Any]) -> dict[str, float]:
        raw = settings.get("raw_config")
        if not raw or len(raw) < 14:
            logger.warning("[Toothpaste] raw_config missing or short, using defaults")
            return {**self.DEFAULTS, "roi_x1": 0, "roi_y1": 0, "roi_x2": 0, "roi_y2": 0}

        endian = settings.get("endian", Endian.LITTLE)

        params: dict[str, float] = {
            "edge_intensity_threshold": validate_and_adjust_param(
                raw[5], self.DEFAULTS["edge_intensity_threshold"], 1, 255
            ),
            "front_count_threshold": validate_and_adjust_param(
                words_to_uint32(raw[6], raw[7], endian),
                self.DEFAULTS["front_count_threshold"],
                1,
                2**31,
            ),
            "back_count_threshold": validate_and_adjust_param(
                words_to_uint32(raw[8], raw[9], endian),
                self.DEFAULTS["back_count_threshold"],
                1,
                2**31,
            ),
            "roi_x1": raw[10],
            "roi_y1": raw[11],
            "roi_x2": raw[12],
            "roi_y2": raw[13],
        }

        # Sanity: front >= back, otherwise classification is meaningless.
        if params["front_count_threshold"] <= params["back_count_threshold"]:
            logger.warning(
                f"[Toothpaste] front_count {params['front_count_threshold']:.0f} "
                f"<= back_count {params['back_count_threshold']:.0f}, using defaults"
            )
            params["front_count_threshold"] = self.DEFAULTS["front_count_threshold"]
            params["back_count_threshold"] = self.DEFAULTS["back_count_threshold"]

        return params

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
    @staticmethod
    def _draw_results(
        image: np.ndarray,
        roi: tuple[int, int, int, int],
        edge_count: int,
        side_code: int,
        label: str,
        params: dict[str, float],
    ) -> np.ndarray:
        vis = image.copy()
        rx1, ry1, rx2, ry2 = roi

        cv2.rectangle(vis, (rx1, ry1), (rx2, ry2), (0, 255, 0), 2)

        if side_code == 1:
            color = (0, 255, 0)
        elif side_code == 2:
            color = (0, 100, 255)
        else:
            color = (0, 0, 255)
        cv2.putText(vis, label, (30, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.3, color, 3, cv2.LINE_AA)

        cv2.putText(
            vis,
            f"Edges: {edge_count}",
            (30, 90),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            vis,
            f"Front>={params['front_count_threshold']:.0f}  Back>={params['back_count_threshold']:.0f}  "
            f"Edge thresh={params['edge_intensity_threshold']:.0f}",
            (30, 120),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (200, 200, 200),
            1,
            cv2.LINE_AA,
        )
        cv2.putText(
            vis,
            f"ROI=({rx1},{ry1})-({rx2},{ry2})",
            (30, 145),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (200, 200, 200),
            1,
            cv2.LINE_AA,
        )
        return vis
