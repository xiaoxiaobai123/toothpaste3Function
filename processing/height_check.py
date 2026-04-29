"""Toothpaste height detection (ProductType.HEIGHT_CHECK).

Algorithm imported from toothpastefronback/HeightBasedImageProcessor.py.

Pipeline:
    1. Crop ROI from the captured image.
    2. Extract a single colour channel (PLC-selectable: 0=R, 1=G, 2=B).
    3. Threshold the channel: > pixel_threshold → 255 else 0.
    4. For every column, find the largest Y where the mask is 255.
    5. If no column reaches min_height, classify as EMPTY (3).
    6. Otherwise average the top-10 max-Y values:
         max_y_avg <  decision_threshold → OK fill level (1)
         max_y_avg >= decision_threshold → high fill / overflow (2)

Note: in this image-coordinate system Y grows downward — so a *lower*
max_y_avg means the toothpaste reached *higher* into the frame. The
comparison direction here matches the original fronback implementation;
adjust on-site if your camera mounting reverses the sense.

PLC parameter layout (raw_config[5..15]):
    +5   channel              uint16  default 2  (0=R, 1=G, 2=B)
    +6   pixel_threshold      uint16  default 100  (0-255)
    +7   min_height           uint16  default 100
    +8   decision_threshold   uint16  default 300
    +9   roi_x1               uint16  (0 = full frame)
    +10  roi_y1               uint16
    +11  roi_x2               uint16
    +12  roi_y2               uint16

Result encoding in Outcome.center:
    x = state code (1=OK, 2=high, 3=empty, 0=exception)
    y = max_y_avg (informational, for HMI display)
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from core import log_config
from processing.algorithms import validate_and_adjust_param
from processing.base import Processor
from processing.result import Outcome, ProcessResult

logger = log_config.setup_logging()

# Map PLC channel index → BGR storage index (OpenCV uses BGR by default).
_CHANNEL_TO_BGR = {0: 2, 1: 1, 2: 0}  # 0=R→idx 2, 1=G→idx 1, 2=B→idx 0
TOP_N_COLUMNS = 10


class HeightCheckProcessor(Processor):
    name = "HeightCheck"

    DEFAULTS: dict[str, float] = {
        "channel": 2,
        "pixel_threshold": 100,
        "min_height": 100,
        "decision_threshold": 300,
    }

    def process(self, image: np.ndarray, settings: dict[str, Any]) -> Outcome:
        try:
            params = self._parse_params(settings)
            h, w = image.shape[:2]

            rx1 = int(params["roi_x1"]) if params["roi_x1"] > 0 else 0
            ry1 = int(params["roi_y1"]) if params["roi_y1"] > 0 else 0
            rx2 = int(params["roi_x2"]) if params["roi_x2"] > 0 else w
            ry2 = int(params["roi_y2"]) if params["roi_y2"] > 0 else h
            rx1, rx2 = max(0, min(rx1, w)), max(0, min(rx2, w))
            ry1, ry2 = max(0, min(ry1, h)), max(0, min(ry2, h))
            if rx2 <= rx1 or ry2 <= ry1:
                logger.warning(f"[Height] invalid ROI ({rx1},{ry1})-({rx2},{ry2}); using full frame")
                rx1, ry1, rx2, ry2 = 0, 0, w, h

            roi = image[ry1:ry2, rx1:rx2]

            channel_idx = _CHANNEL_TO_BGR.get(int(params["channel"]), 0)
            channel = roi[:, :, channel_idx]
            mask = (channel > params["pixel_threshold"]).astype(np.uint8) * 255

            # Per-column max-Y in the ROI's local coordinates.
            cols_with_signal = np.any(mask > 0, axis=0)
            max_y_per_col = np.zeros(mask.shape[1], dtype=np.int32)
            if np.any(cols_with_signal):
                # Reverse along axis=0 so np.argmax finds the LAST 255 (largest Y).
                last_y_from_bottom = np.argmax(mask[::-1] > 0, axis=0)
                max_y_per_col = mask.shape[0] - 1 - last_y_from_bottom
                # Columns with no signal at all: argmax returns 0 spuriously,
                # so mask them out.
                max_y_per_col = np.where(cols_with_signal, max_y_per_col, 0)

            if not np.any(max_y_per_col > params["min_height"]):
                state_code = 3
                result = ProcessResult.NG
                max_y_avg = 0
                label = "EMPTY (3)"
                logger.info(f"[Height] {label}: no column above min_height={params['min_height']:.0f}")
            else:
                top_n = np.partition(max_y_per_col, -TOP_N_COLUMNS)[-TOP_N_COLUMNS:]
                max_y_avg = int(round(float(np.mean(top_n))))

                if max_y_avg >= params["decision_threshold"]:
                    state_code = 2
                    result = ProcessResult.OK
                    label = "HIGH (2)"
                else:
                    state_code = 1
                    result = ProcessResult.OK
                    label = "OK (1)"

                logger.info(
                    f"[Height] {label} max_y_avg={max_y_avg} "
                    f"(thresh={params['decision_threshold']:.0f}) "
                    f"channel={int(params['channel'])} "
                    f"pixel_thresh={params['pixel_threshold']:.0f} "
                    f"ROI=({rx1},{ry1})-({rx2},{ry2})"
                )

            vis = self._draw_results(
                image, (rx1, ry1, rx2, ry2), max_y_per_col, max_y_avg, state_code, label, params
            )
            return Outcome(result, vis, (float(state_code), float(max_y_avg)), 0.0)

        except Exception as e:
            logger.exception(f"[Height] exception: {e}")
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
        if not raw or len(raw) < 13:
            logger.warning("[Height] raw_config missing or short, using defaults")
            return {**self.DEFAULTS, "roi_x1": 0, "roi_y1": 0, "roi_x2": 0, "roi_y2": 0}

        return {
            "channel": validate_and_adjust_param(raw[5], self.DEFAULTS["channel"], 0, 2)
            if raw[5] != 0 or self.DEFAULTS["channel"] == 0
            else self.DEFAULTS["channel"],
            "pixel_threshold": validate_and_adjust_param(raw[6], self.DEFAULTS["pixel_threshold"], 1, 255),
            "min_height": validate_and_adjust_param(raw[7], self.DEFAULTS["min_height"], 1, 65535),
            "decision_threshold": validate_and_adjust_param(
                raw[8], self.DEFAULTS["decision_threshold"], 1, 65535
            ),
            "roi_x1": raw[9],
            "roi_y1": raw[10],
            "roi_x2": raw[11],
            "roi_y2": raw[12],
        }

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
    @staticmethod
    def _draw_results(
        image: np.ndarray,
        roi: tuple[int, int, int, int],
        max_y_per_col: np.ndarray,
        max_y_avg: int,
        state_code: int,
        label: str,
        params: dict[str, float],
    ) -> np.ndarray:
        vis = image.copy()
        rx1, ry1, rx2, ry2 = roi

        cv2.rectangle(vis, (rx1, ry1), (rx2, ry2), (0, 255, 0), 2)

        # Decision-threshold line (in image coords, i.e. ROI-local + ry1).
        thresh_y = ry1 + int(params["decision_threshold"])
        if ry1 <= thresh_y < ry2:
            cv2.line(vis, (rx1, thresh_y), (rx2, thresh_y), (0, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(
                vis,
                "decision",
                (rx1 + 4, thresh_y - 4),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 255, 255),
                1,
                cv2.LINE_AA,
            )

        # Plot per-column max-Y as a thin contour over the ROI.
        if max_y_per_col is not None and max_y_per_col.size > 0:
            for x_local, y_local in enumerate(max_y_per_col):
                if y_local > 0:
                    cv2.circle(vis, (rx1 + x_local, ry1 + int(y_local)), 1, (255, 100, 100), -1)

        if state_code == 1:
            color = (0, 255, 0)
        elif state_code == 2:
            color = (0, 100, 255)
        elif state_code == 3:
            color = (200, 200, 200)
        else:
            color = (0, 0, 255)

        cv2.putText(vis, label, (30, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.3, color, 3, cv2.LINE_AA)
        cv2.putText(
            vis,
            f"Max-Y avg: {max_y_avg}",
            (30, 90),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            vis,
            f"channel={int(params['channel'])} pixel_thresh={params['pixel_threshold']:.0f} "
            f"min_h={params['min_height']:.0f} decision={params['decision_threshold']:.0f}",
            (30, 120),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (200, 200, 200),
            1,
            cv2.LINE_AA,
        )
        return vis
