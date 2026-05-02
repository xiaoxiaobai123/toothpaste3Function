"""Brush-head front/back detection (ProductType.BRUSH_HEAD).

Pipeline (mirrors toothpasthead/image_processing.py):
    1. Adaptive threshold + small-component filter to find bristle dots.
    2. Convex hull of the dots, then min-area rect to define the head ROI.
    3. Validate ROI by area + aspect ratio (rejects out-of-frame parts).
    4. Rotate the image so the long edge of the head is horizontal.
    5. Shrink the ROI on all sides to remove rim noise.
    6. Adaptive-threshold the upper half and the lower half independently
       and compare their dark-pixel densities.
    7. Side classification:
            upper > lower  → Front (1)
            upper < lower  → Back  (2)
            equal          → NG    (0)

PLC parameter layout (raw_config[5..15]):
    +5  shrink_pct          uint16   default 15  (5..30)
    +6  adapt_block         uint16   default 31  (forced odd, ≥3)
    +7  adapt_C             int16    default 8   (-128..127)
    +8  dot_area_min        uint16   default 20
    +9  dot_area_max        uint16   default 500
    +10-11 roi_area_min     uint32   default 50000
    +12-13 roi_area_max     uint32   default 500000
    +14 roi_ratio_min × 10  uint16   default 15  (= 1.5)
    +15 roi_ratio_max × 10  uint16   default 35  (= 3.5)

Optional manual pre-crop ROI (settings["manual_roi"], from PLCManager's
extension block at D110-D113 cam1 / D114-D117 cam2):
    (x1, y1, x2, y2) — when all four are non-zero AND form a valid
    rectangle, the image is pre-cropped to this region BEFORE the dot
    detection runs. This is the equivalent of the original head program's
    "manual ROI" feature (toothpasthead/image_processing.py:79-100) and
    is the recommended fix when the auto-detected ROI keeps tripping
    roi_area_max because background reflections are being picked up as
    bristle dots. (0,0,0,0) = auto-detect on the full frame, identical
    to v0.3.9 behaviour.

The Outcome's `center` field carries the side code (1=Front, 2=Back) in x,
mirroring the head repo's convention. `angle` stays at 0.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from core import log_config
from plc.codec import word_to_int16, words_to_uint32
from plc.enums import Endian
from processing.algorithms import validate_and_adjust_param
from processing.base import Processor
from processing.result import Outcome, ProcessResult

logger = log_config.setup_logging()


class BrushHeadProcessor(Processor):
    name = "BrushHead"

    DEFAULTS: dict[str, float] = {
        "shrink_pct": 15,
        "adapt_block": 31,
        "adapt_C": 8,
        "dot_area_min": 20,
        "dot_area_max": 500,
        "roi_area_min": 50000,
        "roi_area_max": 500000,
        "roi_ratio_min": 1.5,
        "roi_ratio_max": 3.5,
    }

    # Manual pre-crop ROI default — all-zero means "auto-detect on full
    # frame" (= v0.3.9 behaviour). Stored as a separate constant rather
    # than in DEFAULTS because it's a 4-tuple, not a scalar.
    MANUAL_ROI_DEFAULT: tuple[int, int, int, int] = (0, 0, 0, 0)

    MIN_DOTS_FOR_HULL = 10
    MIN_CROP_DIM = 20

    def process(self, image: np.ndarray, settings: dict[str, Any]) -> Outcome:
        try:
            bp = self._parse_params(settings)
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            h_img, w_img = gray.shape

            # 1. Optional manual pre-crop. When PLC sets D110-D113 (cam1) /
            # D114-D117 (cam2) — or D60-D63 in the legacy brush_head path —
            # to a valid rect, narrow the search area before dot detection.
            #
            # "Set" = any of the four words is non-zero AND the rectangle
            # has positive area after clamping to image bounds. The earlier
            # `[0] > 0 and [1] > 0` check rejected legitimate ROIs starting
            # at x=0 or y=0 (e.g. PLC writing (0, 100, 800, 600) — meant
            # to crop the top portion of the frame from the left edge —
            # was silently treated as auto-detect, and the operator's
            # purple Manual ROI overlay never appeared).
            mx1, my1, mx2, my2 = bp["manual_roi"]
            mx2 = min(int(mx2), w_img)
            my2 = min(int(my2), h_img)
            mx1 = max(0, int(mx1))
            my1 = max(0, int(my1))
            use_manual_roi = any(bp["manual_roi"]) and mx2 > mx1 and my2 > my1
            if use_manual_roi:
                search_gray = gray[my1:my2, mx1:mx2]
                manual_roi_offset = (mx1, my1)
            else:
                search_gray = gray
                manual_roi_offset = (0, 0)

            # 2. Find ROI by dot convex hull (within manual pre-crop, if set).
            roi_result = self._find_roi_by_dots(search_gray, bp)
            if roi_result is None:
                # Pull the partial diagnostics _find_roi_by_dots stashed —
                # tells the operator WHICH check failed (too few dots,
                # bad area, bad ratio) and the actual numbers.
                diag = self._last_fail_diag
                fail_reason = diag.get("fail_reason", "no valid ROI")
                logger.error(
                    f"[BrushHead] {fail_reason}"
                    + (f" (within manual ROI ({mx1},{my1})-({mx2},{my2}))" if use_manual_roi else "")
                )
                # First line (big red) shows the headline: which check failed.
                # The bottom param panel (already drawn by _fail_image) shows
                # the live numbers and configured thresholds — operator can
                # pick which D5x register to nudge in the GUI.
                return Outcome(
                    ProcessResult.NG,
                    self._fail_image(
                        image,
                        f"NG: {fail_reason}",
                        bp,
                        h_img,
                        manual_roi=(mx1, my1, mx2, my2) if use_manual_roi else None,
                        dot_count=int(diag.get("dot_count", 0)),
                        roi_area=float(diag.get("roi_area", 0.0)),
                        roi_ratio=float(diag.get("roi_ratio", 0.0)),
                    ),
                    (0.0, 0.0),
                    0.0,
                )

            rect, box, _, roi_area, roi_ratio, dot_count = roi_result
            # When pre-cropped, _find_roi_by_dots returns coordinates in the
            # SUB-image frame. Translate everything back to the full-image
            # frame so the rotation matrix and downstream draws operate on
            # the original-image coordinate system. (No-op when no manual ROI.)
            if use_manual_roi:
                offset = np.array(manual_roi_offset, dtype=np.float32)
                box = box + offset
                rect = (
                    (rect[0][0] + manual_roi_offset[0], rect[0][1] + manual_roi_offset[1]),
                    rect[1],
                    rect[2],
                )
            center = rect[0]
            long_len, short_len, long_angle = self._edge_info(box)
            rot_angle = -long_angle if long_angle <= 90 else 180 - long_angle

            # 3. Rotate image so the head's long axis is horizontal.
            M, nw, nh = self._rotation_matrix(center, rot_angle, w_img, h_img)
            rot_gray = cv2.warpAffine(gray, M, (nw, nh), borderMode=cv2.BORDER_CONSTANT, borderValue=0)

            rc = M @ np.array([center[0], center[1], 1.0])
            rcx, rcy = int(rc[0]), int(rc[1])
            hl, hs = int(long_len / 2), int(short_len / 2)

            roi_x1_rot = max(0, rcx - hl)
            roi_x2_rot = min(nw, rcx + hl)
            roi_y1_rot = max(0, rcy - hs)
            roi_y2_rot = min(nh, rcy + hs)

            # 3. Shrink ROI to drop edge noise.
            shrink_pct = bp["shrink_pct"]
            sl = int(long_len * shrink_pct / 100)
            ss = int(short_len * shrink_pct / 100)
            crop_x1 = max(0, rcx - hl + sl)
            crop_x2 = min(nw, rcx + hl - sl)
            crop_y1 = max(0, rcy - hs + ss)
            crop_y2 = min(nh, rcy + hs - ss)

            crop_gray = rot_gray[crop_y1:crop_y2, crop_x1:crop_x2]
            crop_h, crop_w = crop_gray.shape
            if crop_h < self.MIN_CROP_DIM or crop_w < self.MIN_CROP_DIM:
                logger.error(f"[BrushHead] crop too small: {crop_w}x{crop_h}")
                return Outcome(
                    ProcessResult.NG,
                    self._fail_image(
                        image,
                        f"NG: crop {crop_w}x{crop_h} too small",
                        bp,
                        h_img,
                        manual_roi=(mx1, my1, mx2, my2) if use_manual_roi else None,
                        dot_count=dot_count,
                        roi_area=roi_area,
                        roi_ratio=roi_ratio,
                    ),
                    (0.0, 0.0),
                    0.0,
                )

            # 4. Compare upper-half vs lower-half dark-pixel density.
            upper_density, lower_density = self._compute_densities(crop_gray, bp)
            diff_pct = abs(upper_density - lower_density) / max(upper_density, lower_density, 1e-9) * 100

            if upper_density > lower_density:
                side_code, result = 1, ProcessResult.OK
            elif upper_density < lower_density:
                side_code, result = 2, ProcessResult.OK
            else:
                side_code, result = 0, ProcessResult.NG

            logger.info(
                f"[BrushHead] side={side_code} dots={dot_count} "
                f"roi_area={roi_area:.0f} ratio={roi_ratio:.2f} "
                f"upper={upper_density * 100:.1f}% lower={lower_density * 100:.1f}% "
                f"diff={diff_pct:.1f}%"
            )

            # 5. Visualization on the original (unrotated) image.
            M_inv = cv2.invertAffineTransform(M)
            vis = self._draw_results(
                image,
                M_inv,
                box,
                long_angle,
                rot_angle,
                (roi_x1_rot, roi_y1_rot, roi_x2_rot, roi_y2_rot),
                (crop_x1, crop_y1, crop_x2, crop_y2),
                upper_density,
                lower_density,
                diff_pct,
                side_code,
                bp,
                h_img,
                dot_count,
                roi_area,
                roi_ratio,
                manual_roi=(mx1, my1, mx2, my2) if use_manual_roi else None,
            )

            # The Outcome.center carries the side code (head repo convention):
            # x = side_code (1=Front, 2=Back, 0=NG), y = 0.
            return Outcome(result, vis, (float(side_code), 0.0), 0.0)

        except Exception as e:
            logger.exception(f"[BrushHead] exception: {e}")
            fail_vis = image.copy() if image is not None else np.zeros((100, 100, 3), dtype=np.uint8)
            self._put_text(
                fail_vis,
                f"Exception: {str(e)[:60]}",
                (30, 50),
                color=(0, 0, 255),
                scale=0.8,
                thickness=2,
            )
            # Operator-visible manual ROI even when the algorithm crashes
            # before it could parse params normally — read from the raw
            # settings so a malformed bp dict can't hide the configured
            # rectangle. Wrapped so a second exception inside the
            # exception handler can't take down the whole call.
            try:
                raw_manual = settings.get("manual_roi") or (0, 0, 0, 0)
                if any(raw_manual):
                    h_img, w_img = fail_vis.shape[:2]
                    mx1, my1, mx2, my2 = (int(v) for v in raw_manual)
                    mx1, my1 = max(0, mx1), max(0, my1)
                    mx2, my2 = min(w_img, mx2), min(h_img, my2)
                    if mx2 > mx1 and my2 > my1:
                        cv2.rectangle(fail_vis, (mx1, my1), (mx2, my2), (255, 0, 255), 2)
                        self._put_text(
                            fail_vis,
                            "Manual ROI",
                            (mx1, max(0, my1 - 8)),
                            color=(255, 0, 255),
                            scale=0.5,
                            thickness=1,
                        )
            except Exception:
                pass
            return Outcome(ProcessResult.EXCEPTION, fail_vis, (0.0, 0.0), 0.0)

    # ------------------------------------------------------------------
    # PLC parameter parsing
    # ------------------------------------------------------------------
    def _parse_params(self, settings: dict[str, Any]) -> dict[str, Any]:
        raw = settings.get("raw_config")
        if not raw or len(raw) < 16:
            logger.warning("[BrushHead] raw_config missing or short, using defaults")
            bp = self.DEFAULTS.copy()
            bp["manual_roi"] = self.MANUAL_ROI_DEFAULT
            return bp

        endian = settings.get("endian", Endian.LITTLE)

        bp = {
            "shrink_pct": validate_and_adjust_param(raw[5], self.DEFAULTS["shrink_pct"], 5, 30),
            "adapt_block": self._sanitize_block_size(
                int(raw[6]) if raw[6] != 0 else int(self.DEFAULTS["adapt_block"])
            ),
            "adapt_C": (word_to_int16(raw[7]) if raw[7] != 0 else self.DEFAULTS["adapt_C"]),
            "dot_area_min": validate_and_adjust_param(raw[8], self.DEFAULTS["dot_area_min"], 1, 65535),
            "dot_area_max": validate_and_adjust_param(raw[9], self.DEFAULTS["dot_area_max"], 1, 65535),
            "roi_area_min": validate_and_adjust_param(
                words_to_uint32(raw[10], raw[11], endian),
                self.DEFAULTS["roi_area_min"],
                1,
                2**31,
            ),
            "roi_area_max": validate_and_adjust_param(
                words_to_uint32(raw[12], raw[13], endian),
                self.DEFAULTS["roi_area_max"],
                1,
                2**31,
            ),
            "roi_ratio_min": (raw[14] / 10.0 if raw[14] != 0 else self.DEFAULTS["roi_ratio_min"]),
            "roi_ratio_max": (raw[15] / 10.0 if raw[15] != 0 else self.DEFAULTS["roi_ratio_max"]),
        }

        if bp["dot_area_min"] >= bp["dot_area_max"]:
            logger.warning(
                f"[BrushHead] dot_area inverted ({bp['dot_area_min']} >= {bp['dot_area_max']}), using defaults"
            )
            bp["dot_area_min"] = self.DEFAULTS["dot_area_min"]
            bp["dot_area_max"] = self.DEFAULTS["dot_area_max"]

        # Manual pre-crop ROI from PLCManager's extension block (settings["manual_roi"],
        # populated by PLCManager.read_camera_settings from D110-D113 cam1 / D114-D117
        # cam2). Default = (0,0,0,0) when the extension wasn't readable or PLC didn't
        # set it — process() interprets that as "auto-detect on full frame".
        manual = settings.get("manual_roi", self.MANUAL_ROI_DEFAULT)
        if not (isinstance(manual, tuple | list) and len(manual) == 4):
            manual = self.MANUAL_ROI_DEFAULT
        bp["manual_roi"] = tuple(int(v) for v in manual)

        return bp

    @staticmethod
    def _sanitize_block_size(value: int) -> int:
        if value < 3:
            value = 3
        if value % 2 == 0:
            value += 1
        return value

    # ------------------------------------------------------------------
    # ROI detection
    # ------------------------------------------------------------------
    def _find_roi_by_dots(
        self, gray: np.ndarray, bp: dict[str, float]
    ) -> tuple[Any, np.ndarray, np.ndarray, float, float, int] | None:
        """Locate the brush head ROI from the convex hull of bristle dots.

        On failure (any of the three rejection paths below), stashes
        partial diagnostics in `self._last_fail_diag` so the caller can
        forward dot count / hull area / ratio to `_fail_image` for
        operator-visible feedback. The diagnostics are scoped to one
        process() call: success paths don't read them, and the next
        failure overwrites the previous one. Single-threaded LOOP /
        FIRE dispatch (per `legacy/fronback_orchestrator._do_brush_head`
        and v2 TaskManager) keeps this safe — never two concurrent
        callers on the same processor instance.
        """
        # Default to "no diagnostics" so a successful call doesn't leave
        # stale data behind for a future failure. Cheap dict re-assignment.
        self._last_fail_diag: dict[str, Any] = {
            "dot_count": 0,
            "roi_area": 0.0,
            "roi_ratio": 0.0,
            "fail_reason": "",
        }

        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        adapt = cv2.adaptiveThreshold(
            blur,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            int(bp["adapt_block"]),
            int(bp["adapt_C"]),
        )
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        adapt = cv2.morphologyEx(adapt, cv2.MORPH_OPEN, kernel, iterations=1)

        cnts, _ = cv2.findContours(adapt, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        dots: list[tuple[float, float]] = []
        for c in cnts:
            a = cv2.contourArea(c)
            if bp["dot_area_min"] < a < bp["dot_area_max"]:
                m = cv2.moments(c)
                if m["m00"] > 0:
                    dots.append((m["m10"] / m["m00"], m["m01"] / m["m00"]))

        dot_count = len(dots)
        self._last_fail_diag["dot_count"] = dot_count

        if dot_count < self.MIN_DOTS_FOR_HULL:
            reason = f"too few dots: {dot_count} < {self.MIN_DOTS_FOR_HULL}"
            self._last_fail_diag["fail_reason"] = reason
            logger.warning(f"[BrushHead] {reason}")
            return None

        pts = np.array(dots, dtype=np.float32)
        hull = cv2.convexHull(pts)
        rect = cv2.minAreaRect(hull)
        box = cv2.boxPoints(rect)

        long_len, short_len, _ = self._edge_info(box)

        roi_area = long_len * short_len
        self._last_fail_diag["roi_area"] = roi_area

        if not (bp["roi_area_min"] <= roi_area <= bp["roi_area_max"]):
            reason = f"area {roi_area:.0f} not in [{bp['roi_area_min']:.0f}, {bp['roi_area_max']:.0f}]"
            self._last_fail_diag["fail_reason"] = reason
            logger.warning(f"[BrushHead] ROI {reason}")
            return None

        roi_ratio = long_len / max(short_len, 1e-9)
        self._last_fail_diag["roi_ratio"] = roi_ratio

        if not (bp["roi_ratio_min"] <= roi_ratio <= bp["roi_ratio_max"]):
            reason = f"ratio {roi_ratio:.2f} not in [{bp['roi_ratio_min']:.2f}, {bp['roi_ratio_max']:.2f}]"
            self._last_fail_diag["fail_reason"] = reason
            logger.warning(f"[BrushHead] ROI {reason}")
            return None

        return rect, box, hull, roi_area, roi_ratio, dot_count

    @staticmethod
    def _edge_info(box: np.ndarray) -> tuple[float, float, float]:
        """Return (long_len, short_len, long_edge_angle_deg) of a 4-point box."""
        edges = []
        for j in range(4):
            p1, p2 = box[j], box[(j + 1) % 4]
            length = float(np.linalg.norm(p2 - p1))
            angle = float(np.degrees(np.arctan2(-(p2[1] - p1[1]), p2[0] - p1[0])) % 180)
            edges.append((length, angle))
        edges.sort(key=lambda e: e[0], reverse=True)
        return edges[0][0], edges[2][0], edges[0][1]

    @staticmethod
    def _rotation_matrix(
        center: tuple[float, float], rot_angle_deg: float, w: int, h: int
    ) -> tuple[np.ndarray, int, int]:
        """Build a rotation matrix and return the new canvas size."""
        M = cv2.getRotationMatrix2D(center, rot_angle_deg, 1.0)
        ca, sa = abs(M[0, 0]), abs(M[0, 1])
        nw = int(h * sa + w * ca)
        nh = int(h * ca + w * sa)
        M[0, 2] += (nw - w) / 2
        M[1, 2] += (nh - h) / 2
        return M, nw, nh

    # ------------------------------------------------------------------
    # Density comparison
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_densities(crop_gray: np.ndarray, bp: dict[str, float]) -> tuple[float, float]:
        mid = crop_gray.shape[0] // 2
        block = int(bp["adapt_block"])
        c = int(bp["adapt_C"])
        small_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

        upper = cv2.adaptiveThreshold(
            cv2.GaussianBlur(crop_gray[:mid], (5, 5), 0),
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            block,
            c,
        )
        upper = cv2.morphologyEx(upper, cv2.MORPH_OPEN, small_kernel, iterations=1)

        lower = cv2.adaptiveThreshold(
            cv2.GaussianBlur(crop_gray[mid:], (5, 5), 0),
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            block,
            c,
        )
        lower = cv2.morphologyEx(lower, cv2.MORPH_OPEN, small_kernel, iterations=1)

        upper_density = float(np.count_nonzero(upper)) / upper.size
        lower_density = float(np.count_nonzero(lower)) / lower.size
        return upper_density, lower_density

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
    @staticmethod
    def _put_text(
        vis: np.ndarray,
        text: str,
        pos: tuple[int, int],
        *,
        color: tuple[int, int, int],
        scale: float = 0.6,
        thickness: int = 1,
    ) -> None:
        """Draw `text` with a 2-px black outline so it stays legible
        against bright bristle highlights, dark backgrounds, or any
        gradient in between. The pre-v0.3.17 grey labels (180,180,180)
        vanished on light backgrounds; outlining + brighter colours
        fixes that without picking a single "high-contrast" colour
        that itself disappears on red/green ROI overlays.
        """
        cv2.putText(
            vis,
            text,
            pos,
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            (0, 0, 0),
            thickness + 2,
            cv2.LINE_AA,
        )
        cv2.putText(
            vis,
            text,
            pos,
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            color,
            thickness,
            cv2.LINE_AA,
        )

    def _fail_image(
        self,
        image: np.ndarray,
        msg: str,
        bp: dict[str, float],
        img_h: int,
        manual_roi: tuple[int, int, int, int] | None = None,
        *,
        dot_count: int = 0,
        roi_area: float = 0.0,
        roi_ratio: float = 0.0,
    ) -> np.ndarray:
        """Diagnostic image returned when the algorithm rejects a frame.

        Draws the failure message + (optionally) the manual pre-crop ROI
        rectangle so the operator can see WHERE the algorithm was looking.
        Matches the original toothpasthead/_fail_image behaviour — earlier
        v0.3.10 forgot to draw manual_roi here, leaving the operator
        confused about whether manual ROI was even active.

        v0.3.17+: when the caller knows partial diagnostics from
        `_find_roi_by_dots` (e.g. it found N dots but the convex hull
        area was out of range), they're forwarded to `_draw_param_info`
        so the bottom of the screen shows actual numbers rather than
        always-zero defaults — operator can see immediately whether the
        problem is dot detection, ROI area, or ratio.
        """
        vis = image.copy()
        self._put_text(vis, msg, (30, 50), color=(0, 0, 255), scale=1.2, thickness=3)
        if manual_roi is not None:
            mx1, my1, mx2, my2 = manual_roi
            cv2.rectangle(vis, (mx1, my1), (mx2, my2), (255, 0, 255), 2)
            self._put_text(
                vis,
                "Manual ROI",
                (mx1, max(0, my1 - 8)),
                color=(255, 0, 255),
                scale=0.5,
                thickness=1,
            )
        self._draw_param_info(vis, bp, img_h, dot_count, roi_area, roi_ratio)
        return vis

    @classmethod
    def _draw_param_info(
        cls,
        vis: np.ndarray,
        bp: dict[str, float],
        img_h: int,
        dot_count: int = 0,
        roi_area: float = 0,
        roi_ratio: float = 0,
    ) -> None:
        y = img_h - 160
        lines = [
            f"shrink={bp['shrink_pct']:.0f}% block={int(bp['adapt_block'])} C={int(bp['adapt_C'])}",
            f"dot_area={bp['dot_area_min']:.0f}-{bp['dot_area_max']:.0f}  dots={dot_count}",
            f"roi_area={roi_area:.0f} ({bp['roi_area_min']:.0f}-{bp['roi_area_max']:.0f})",
            f"roi_ratio={roi_ratio:.2f} ({bp['roi_ratio_min']:.1f}-{bp['roi_ratio_max']:.1f})",
        ]
        for line in lines:
            cls._put_text(vis, line, (30, y), color=(0, 255, 0), scale=0.55, thickness=1)
            y += 22

    def _draw_results(
        self,
        original: np.ndarray,
        M_inv: np.ndarray,
        _box: np.ndarray,
        long_angle: float,
        rot_angle: float,
        roi_rect_rot: tuple[int, int, int, int],
        crop_rect_rot: tuple[int, int, int, int],
        upper_density: float,
        lower_density: float,
        diff_pct: float,
        side_code: int,
        bp: dict[str, float],
        img_h: int,
        dot_count: int,
        roi_area: float,
        roi_ratio: float,
        manual_roi: tuple[int, int, int, int] | None = None,
    ) -> np.ndarray:
        vis = original.copy()

        # Manual pre-crop ROI overlay (purple, axis-aligned). Drawn first so
        # the auto-detected red ROI lands on top of it visually.
        if manual_roi is not None:
            mx1, my1, mx2, my2 = manual_roi
            cv2.rectangle(vis, (mx1, my1), (mx2, my2), (255, 0, 255), 2)
            self._put_text(
                vis,
                "Manual ROI",
                (mx1, max(0, my1 - 8)),
                color=(255, 0, 255),
                scale=0.5,
                thickness=1,
            )

        roi_corners = self._transform_rect_to_original(M_inv, *roi_rect_rot)
        cv2.drawContours(vis, [roi_corners], -1, (0, 0, 255), 2)

        shrink_corners = self._transform_rect_to_original(M_inv, *crop_rect_rot)
        cv2.drawContours(vis, [shrink_corners], -1, (0, 255, 0), 2)

        crop_x1, crop_y1, crop_x2, crop_y2 = crop_rect_rot
        mid_y_rot = (crop_y1 + crop_y2) // 2
        mid_pt1, mid_pt2 = self._transform_line_to_original(M_inv, crop_x1, mid_y_rot, crop_x2)
        cv2.line(vis, mid_pt1, mid_pt2, (0, 255, 255), 2)

        if side_code == 1:
            side_text, side_color = "FRONT (1)", (0, 255, 0)
        elif side_code == 2:
            side_text, side_color = "BACK (2)", (0, 100, 255)
        else:
            side_text, side_color = "UNKNOWN (0)", (0, 0, 255)
        self._put_text(vis, side_text, (30, 50), color=side_color, scale=1.3, thickness=3)

        self._put_text(
            vis,
            f"Upper: {upper_density * 100:.1f}%",
            (30, 90),
            color=(255, 150, 0),
            scale=0.8,
            thickness=2,
        )
        self._put_text(
            vis,
            f"Lower: {lower_density * 100:.1f}%",
            (30, 120),
            color=(0, 150, 255),
            scale=0.8,
            thickness=2,
        )
        self._put_text(
            vis,
            f"Diff: {diff_pct:.1f}%",
            (30, 150),
            color=(0, 255, 255),
            scale=0.8,
            thickness=2,
        )

        self._put_text(
            vis,
            f"Dots: {dot_count}  Area: {roi_area:.0f}  Ratio: {roi_ratio:.2f}",
            (30, 180),
            color=(0, 255, 0),
            scale=0.6,
            thickness=1,
        )

        angle_text = f"Angle: {long_angle:.1f} deg" if abs(rot_angle) > 0.5 else "Angle: 0 deg"
        self._put_text(vis, angle_text, (30, 205), color=(0, 255, 0), scale=0.6, thickness=1)

        self._put_text(
            vis,
            "Red=ROI  Green=Analysis  Yellow=Split",
            (30, img_h - 15),
            color=(0, 255, 0),
            scale=0.5,
            thickness=1,
        )

        self._draw_param_info(vis, bp, img_h, dot_count, roi_area, roi_ratio)
        return vis

    @staticmethod
    def _transform_rect_to_original(M_inv: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> np.ndarray:
        corners_rot = np.array([[x1, y1, 1], [x2, y1, 1], [x2, y2, 1], [x1, y2, 1]], dtype=np.float64)
        corners_orig = (M_inv @ corners_rot.T).T
        return corners_orig.astype(np.intp)

    @staticmethod
    def _transform_line_to_original(
        M_inv: np.ndarray, x1: int, y: int, x2: int
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        pts_rot = np.array([[x1, y, 1], [x2, y, 1]], dtype=np.float64)
        pts_orig = (M_inv @ pts_rot.T).T
        p1 = (int(pts_orig[0, 0]), int(pts_orig[0, 1]))
        p2 = (int(pts_orig[1, 0]), int(pts_orig[1, 1]))
        return p1, p2
