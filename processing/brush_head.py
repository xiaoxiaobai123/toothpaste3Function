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

            # 1. Resolve manual pre-crop ROI from PLC. The helper always
            # returns a visual rectangle + label so the operator screen
            # gets the purple overlay + "Manual ROI" label every cycle —
            # even when PLC hasn't set D60-D63 (default = full-frame
            # auto) or set them to garbage (default + "invalid" label).
            # `use_manual_roi` only affects the algorithm's pre-crop
            # decision, not whether the rectangle is drawn.
            visual_manual_roi, manual_roi_label, use_manual_roi = self._resolve_manual_roi(
                bp["manual_roi"], w_img, h_img
            )
            if use_manual_roi:
                mx1, my1, mx2, my2 = visual_manual_roi
                search_gray = gray[my1:my2, mx1:mx2]
                manual_roi_offset = (mx1, my1)
            else:
                # Sentinel values for the (ignored) manual_roi log line.
                mx1, my1, mx2, my2 = visual_manual_roi
                search_gray = gray
                manual_roi_offset = (0, 0)

            # 2. Find ROI by dot convex hull (within manual pre-crop, if set).
            roi_result = self._find_roi_by_dots(search_gray, bp)
            if roi_result is None:
                # Pull the partial diagnostics _find_roi_by_dots stashed —
                # tells the operator WHICH check failed (too few dots,
                # bad area, bad ratio), the actual numbers, the rejected
                # ROI box (if any), and all detected dot centroids.
                diag = self._last_fail_diag
                fail_reason = diag.get("fail_reason", "no valid ROI")
                logger.error(
                    f"[BrushHead] {fail_reason}"
                    + (f" (within manual ROI ({mx1},{my1})-({mx2},{my2}))" if use_manual_roi else "")
                )
                # _find_roi_by_dots ran on `search_gray` which may have
                # been pre-cropped to manual_roi — translate the box
                # and hull back to full-image coords so the overlays
                # land on the right pixels. dot centroids are translated
                # inside _fail_image via `dots_offset` instead.
                failed_box = diag.get("failed_box")
                hull = diag.get("hull")
                if use_manual_roi:
                    offset_arr = np.array(manual_roi_offset, dtype=np.float32)
                    if failed_box is not None:
                        failed_box = failed_box + offset_arr
                    if hull is not None:
                        hull = hull + offset_arr.reshape(1, 1, 2)

                return Outcome(
                    ProcessResult.NG,
                    self._fail_image(
                        image,
                        f"NG: {fail_reason}",
                        bp,
                        h_img,
                        manual_roi=visual_manual_roi,
                        manual_roi_label=manual_roi_label,
                        dot_count=int(diag.get("dot_count", 0)),
                        roi_area=float(diag.get("roi_area", 0.0)),
                        roi_ratio=float(diag.get("roi_ratio", 0.0)),
                        failed_box=failed_box,
                        hull=hull,
                        dots=diag.get("dots"),
                        dots_offset=manual_roi_offset if use_manual_roi else (0, 0),
                    ),
                    (0.0, 0.0),
                    0.0,
                )

            rect, box, hull, roi_area, roi_ratio, dot_count = roi_result
            # When pre-cropped, _find_roi_by_dots returns coordinates in the
            # SUB-image frame. Translate everything (box, hull, rect.center)
            # back to the full-image frame so the rotation matrix and
            # downstream draws operate on the original-image coordinate
            # system. (No-op when no manual ROI.)
            if use_manual_roi:
                offset = np.array(manual_roi_offset, dtype=np.float32)
                box = box + offset
                hull = hull + offset.reshape(1, 1, 2)
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
                        manual_roi=visual_manual_roi,
                        manual_roi_label=manual_roi_label,
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
                manual_roi=visual_manual_roi,
                manual_roi_label=manual_roi_label,
                hull=hull,
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
        forward dot count / hull area / ratio + the actual rejected box
        + the detected dot centroids to `_fail_image` for operator-visible
        feedback. The diagnostics are scoped to one process() call:
        success paths don't read them, and the next failure overwrites
        the previous one. Single-threaded LOOP / FIRE dispatch (per
        `legacy/fronback_orchestrator._do_brush_head` and v2 TaskManager)
        keeps this safe — never two concurrent callers on the same
        processor instance.
        """
        # Default to "no diagnostics" so a successful call doesn't leave
        # stale data behind for a future failure. Cheap dict re-assignment.
        self._last_fail_diag: dict[str, Any] = {
            "dot_count": 0,
            "roi_area": 0.0,
            "roi_ratio": 0.0,
            "fail_reason": "",
            # When the ROI is rejected for area/ratio, this is the actual
            # 4-point box the algorithm computed — caller draws it in
            # orange so the operator sees "the algorithm thought the head
            # was here" and can immediately tell whether the dot detector
            # picked up the wrong region.
            "failed_box": None,
            # Convex hull of the detected dots — drawn cyan so the
            # operator sees the algorithm's actual wrap shape, not just
            # the rotated minAreaRect (which can have corners outside
            # the hull and look disconnected from the bristle dots).
            "hull": None,
            # All detected dot centroids (in sub-image coords). When too
            # few dots, drawing them as small circles tells the operator
            # whether the adaptive threshold even detected anything.
            "dots": [],
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
        self._last_fail_diag["dots"] = dots

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
        # Stash the actual computed box so the fail screen can draw it
        # in orange — operator immediately sees "the algorithm thought
        # the head was *here*" and can tell whether dot detection picked
        # up the wrong region (e.g. background reflections were counted
        # as bristle dots, ballooning the convex hull).
        self._last_fail_diag["failed_box"] = box
        # Also stash the convex hull itself — drawn in cyan on both
        # success and fail screens so the operator sees the actual
        # geometry the algorithm wrapped around the dots, not just the
        # rotated minAreaRect (which can have corners outside the hull
        # and looks visually disconnected from the bristle pattern).
        self._last_fail_diag["hull"] = hull

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
    def _resolve_manual_roi(
        plc_roi: tuple[int, int, int, int],
        w_img: int,
        h_img: int,
    ) -> tuple[tuple[int, int, int, int], str, bool]:
        """Resolve PLC-supplied manual ROI into (visual_rect, label, use_for_crop).

        Three outcomes:
          * Valid PLC value          → (clamped_rect, "Manual ROI", True)
                                       — algorithm crops to this rect
          * PLC set (0,0,0,0)        → (full_frame_inset, "Manual ROI: auto (full frame)", False)
                                       — operator still sees a purple
                                       rectangle, but the algorithm runs
                                       on the full frame
          * PLC value invalid        → (full_frame_inset, "Manual ROI: invalid {raw} (using full frame)", False)
                                       — covers reversed (x2<x1), zero-area,
                                       and out-of-frame cases. The label
                                       tells the operator the PLC value
                                       was rejected (vs silently ignored)
                                       so they know to fix D60-D63.

        The "default visual rectangle" insets 30 px from each edge so
        the "Manual ROI: ..." label drawn just above the rectangle is
        actually on screen (5 px inset put the label at y = -3, off the
        top edge — operator never saw it).
        """
        full_visual = (30, 30, max(31, w_img - 30), max(31, h_img - 30))

        if not any(plc_roi):
            return full_visual, "Manual ROI: auto (full frame)", False

        px1, py1, px2, py2 = plc_roi
        cx1 = max(0, min(w_img, int(px1)))
        cy1 = max(0, min(h_img, int(py1)))
        cx2 = max(0, min(w_img, int(px2)))
        cy2 = max(0, min(h_img, int(py2)))

        if cx2 <= cx1 or cy2 <= cy1:
            return (
                full_visual,
                f"Manual ROI: invalid {tuple(int(v) for v in plc_roi)} (using full frame)",
                False,
            )

        return (cx1, cy1, cx2, cy2), "Manual ROI", True

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
        """Thin delegate to the shared `processing.display_utils.put_text_outlined`.

        Kept on the class so existing call sites (`self._put_text` and
        `cls._put_text` in subclasses / static contexts) keep working
        without churn. The shared module-level function is the canonical
        implementation; legacy fronback / height paths use it directly.
        """
        from processing.display_utils import put_text_outlined

        put_text_outlined(vis, text, pos, color=color, scale=scale, thickness=thickness)

    def _fail_image(
        self,
        image: np.ndarray,
        msg: str,
        bp: dict[str, float],
        img_h: int,
        manual_roi: tuple[int, int, int, int] | None = None,
        *,
        manual_roi_label: str = "Manual ROI",
        dot_count: int = 0,
        roi_area: float = 0.0,
        roi_ratio: float = 0.0,
        failed_box: np.ndarray | None = None,
        hull: np.ndarray | None = None,
        dots: list[tuple[float, float]] | None = None,
        dots_offset: tuple[int, int] = (0, 0),
    ) -> np.ndarray:
        """Diagnostic image returned when the algorithm rejects a frame.

        Draws (in priority order):
          1. Big red headline `msg` at top-left
          2. Purple PLC-configured manual ROI rectangle (if set)
          3. Orange "Fail ROI" outline of the rejected box (if any) —
             this is the convex-hull minAreaRect the algorithm computed
             before tripping the area / ratio check. Lets the operator
             see *where* the algorithm thought the head was, so they can
             tell at a glance whether dot detection picked up background
             reflections vs the actual bristles.
          4. Blue dot markers for every detected bristle centroid (the
             raw dot detector output) — surfaces "the adapter found 5
             dots scattered randomly" vs "found 50 dots clustered in
             the wrong spot".
          5. Bottom param panel with live + configured thresholds.

        v0.3.10+ added the manual_roi overlay; v0.3.17 added live diag
        numbers; v0.3.19 added the orange Fail ROI + dot markers.

        `dots_offset` is added to every dot centroid before drawing —
        when manual_roi pre-crop is active, _find_roi_by_dots stashes
        dot coords in the sub-image frame, so the caller passes the
        crop's (x, y) origin to put markers back in full-image coords.
        Same applies to `failed_box` when the caller pre-translates it.
        """
        vis = image.copy()
        self._put_text(vis, msg, (30, 50), color=(0, 0, 255), scale=1.2, thickness=3)

        if manual_roi is not None:
            mx1, my1, mx2, my2 = manual_roi
            cv2.rectangle(vis, (mx1, my1), (mx2, my2), (255, 0, 255), 2)
            # Label position: prefer just above the rect; if the rect
            # starts within ~20 px of the top edge, the label would be
            # clipped — drop it inside the rect's top-left instead.
            label_y = my1 - 8 if my1 >= 25 else my1 + 18
            self._put_text(
                vis,
                manual_roi_label,
                (mx1 + 4, label_y),
                color=(255, 0, 255),
                scale=0.5,
                thickness=1,
            )

        if hull is not None:
            # Cyan polygon (BGR (255, 255, 0)) — the convex hull of the
            # detected dots. Drawn before the orange minAreaRect so the
            # rectangle lays on top; together they tell the operator
            # "the algorithm wrapped *this* shape (cyan) around the dots
            # (blue), then computed *that* rectangle (orange) as the
            # rotated bounding box".
            cv2.drawContours(vis, [hull.astype(np.intp)], -1, (255, 255, 0), 2)

        if failed_box is not None:
            # Orange outline (BGR (0, 165, 255)) — distinct from the
            # success red (0,0,255) so an operator can tell at a glance
            # the screen is in fail mode.
            cv2.drawContours(vis, [failed_box.astype(np.intp)], -1, (0, 165, 255), 2)
            top_left = tuple(failed_box.astype(int).min(axis=0))
            self._put_text(
                vis,
                "Fail ROI",
                (int(top_left[0]), max(0, int(top_left[1]) - 8)),
                color=(0, 165, 255),
                scale=0.5,
                thickness=1,
            )

        if dots:
            ox, oy = dots_offset
            for x, y in dots:
                cv2.circle(vis, (int(x) + ox, int(y) + oy), 3, (255, 200, 0), -1)

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
        manual_roi_label: str = "Manual ROI",
        hull: np.ndarray | None = None,
    ) -> np.ndarray:
        vis = original.copy()

        # Manual pre-crop ROI overlay (purple, axis-aligned). Drawn first so
        # the auto-detected red ROI lands on top of it visually.
        if manual_roi is not None:
            mx1, my1, mx2, my2 = manual_roi
            cv2.rectangle(vis, (mx1, my1), (mx2, my2), (255, 0, 255), 2)
            label_y = my1 - 8 if my1 >= 25 else my1 + 18
            self._put_text(
                vis,
                manual_roi_label,
                (mx1 + 4, label_y),
                color=(255, 0, 255),
                scale=0.5,
                thickness=1,
            )

        # Convex hull (cyan) — drawn before the red ROI so the rotated
        # rectangle lays on top. Without this overlay the operator sees
        # the rotated red rect "floating" with no obvious connection to
        # the bristle pattern; the hull shows the actual shape the
        # algorithm wrapped around the dot cloud.
        if hull is not None:
            cv2.drawContours(vis, [hull.astype(np.intp)], -1, (255, 255, 0), 2)

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
            "Cyan=Hull  Red=ROI  Green=Analysis  Yellow=Split",
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
