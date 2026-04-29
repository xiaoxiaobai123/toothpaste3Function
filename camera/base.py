"""Hikvision GigE Vision camera wrapper.

Wraps MvCameraControl_class with:
    - Hardware ROI support (Width/Height/OffsetX/OffsetY) — significantly
      reduces frame size and capture time when only a small region is
      needed by the algorithm.
    - Software/hardware trigger switching.
    - Exposure read/write with read-back verification.
    - flush_one_frame(): discards a stale frame after exposure changes,
      preventing the next algorithm pass from seeing a transitional frame.
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from camera.environment import setup_camera_environment
from core import log_config

logger = log_config.setup_logging()

setup_camera_environment()  # must run before MvCameraControl_class import

from MvCameraControl_class import (  # noqa: E402
    MV_CC_DEVICE_INFO,
    MV_CC_PIXEL_CONVERT_PARAM,
    MV_FRAME_OUT_INFO_EX,
    MV_GIGE_DEVICE,
    MV_GIGE_DEVICE_INFO,
    MV_TRIGGER_MODE_ON,
    MV_TRIGGER_SOURCE_SOFTWARE,
    MVCC_ENUMVALUE,
    MVCC_FLOATVALUE,
    MVCC_INTVALUE,
    MV_ACCESS_Exclusive,
    MvCamera,
    PixelType_Gvsp_RGB8_Packed,
    byref,
    c_ubyte,
    memmove,
    memset,
    sizeof,
)


class CameraBase:
    """Single-camera lifecycle and frame-capture helper."""

    def __init__(
        self,
        device_ip: str,
        net_ip: str,
        camera_num: int | None = None,
        roi: dict[str, int] | None = None,
    ) -> None:
        """ROI is {'width', 'height', 'offset_x', 'offset_y'} or None.

        Setting ROI tells the camera to transmit only that region; payload
        size drops, capture+algo speed up. Note: algorithm-side ROI in the
        PLC config (D25-27 / D45-47) is then in coordinates relative to
        this smaller image.
        """
        self.device_ip = device_ip
        self.net_ip = net_ip
        self.camera_num = camera_num
        self.roi = roi
        self.cam: MvCamera | None = None
        self.nPayloadSize: int = 0

    @property
    def _tag(self) -> str:
        return f"[Cam{self.camera_num}]" if self.camera_num else f"[{self.device_ip}]"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def init_camera(self) -> bool:
        try:
            self.cam = MvCamera()
            if not self._create_and_open_device():
                logger.error(f"{self._tag} failed to create/open device ({self.device_ip})")
                return False
            self._set_common_parameters()
            logger.info(f"{self._tag} initialized ({self.device_ip})")
            return True
        except Exception as e:
            logger.error(f"{self._tag} init error: {e}")
            self.close_camera()
            return False

    def _create_and_open_device(self) -> bool:
        stDevInfo = self._create_device_info()
        if not self._create_handle(stDevInfo):
            return False
        return self._open_device()

    def _create_device_info(self) -> MV_CC_DEVICE_INFO:
        stDevInfo = MV_CC_DEVICE_INFO()
        stGigEDev = MV_GIGE_DEVICE_INFO()
        device_ip_parts = [int(x) for x in self.device_ip.split(".")]
        stGigEDev.nCurrentIp = (
            (device_ip_parts[0] << 24)
            | (device_ip_parts[1] << 16)
            | (device_ip_parts[2] << 8)
            | device_ip_parts[3]
        )
        net_ip_parts = [int(x) for x in self.net_ip.split(".")]
        stGigEDev.nNetExport = (
            (net_ip_parts[0] << 24) | (net_ip_parts[1] << 16) | (net_ip_parts[2] << 8) | net_ip_parts[3]
        )
        stDevInfo.nTLayerType = MV_GIGE_DEVICE
        stDevInfo.SpecialInfo.stGigEInfo = stGigEDev
        return stDevInfo

    def _create_handle(self, stDevInfo: MV_CC_DEVICE_INFO) -> bool:
        ret = self.cam.MV_CC_CreateHandle(stDevInfo)
        if ret != 0:
            logger.error(f"Create handle fail! ret[0x{ret:x}]")
            return False
        return True

    def _open_device(self) -> bool:
        ret = self.cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
        if ret != 0:
            logger.error(f"{self._tag} open device failed, ret=0x{ret:x}")
            return False
        logger.info(f"{self._tag} opened ({self.device_ip})")
        return True

    def _set_common_parameters(self) -> None:
        self.cam.MV_CC_SetEnumValue("AcquisitionMode", 2)
        self._set_packet_size()
        # ROI must be applied before GetPayloadSize, otherwise the buffer
        # is sized for the full frame and we waste memory + bandwidth.
        self._apply_roi()
        self._get_payload_size()

    def _apply_roi(self) -> bool:
        """Apply hardware ROI from self.roi. Skip when not configured.

        Order matters: zero offsets first, then set width/height, then
        re-apply offsets. Otherwise a new size + old offset combo can
        exceed the sensor's max range and the camera rejects it.
        """
        if not self.roi:
            return True

        w, h = self.roi["width"], self.roi["height"]
        ox, oy = self.roi.get("offset_x", 0), self.roi.get("offset_y", 0)

        self.cam.MV_CC_SetIntValue("OffsetX", 0)
        self.cam.MV_CC_SetIntValue("OffsetY", 0)

        ret = self.cam.MV_CC_SetIntValue("Width", w)
        if ret != 0:
            logger.error(f"{self._tag} set Width={w} failed, ret=0x{ret:x} (must be a multiple of 4 or 8?)")
            return False

        ret = self.cam.MV_CC_SetIntValue("Height", h)
        if ret != 0:
            logger.error(f"{self._tag} set Height={h} failed, ret=0x{ret:x}")
            return False

        if ox:
            ret = self.cam.MV_CC_SetIntValue("OffsetX", ox)
            if ret != 0:
                logger.warning(f"{self._tag} set OffsetX={ox} failed, ret=0x{ret:x}")
        if oy:
            ret = self.cam.MV_CC_SetIntValue("OffsetY", oy)
            if ret != 0:
                logger.warning(f"{self._tag} set OffsetY={oy} failed, ret=0x{ret:x}")

        logger.info(f"{self._tag} ROI applied: {w}x{h} @ ({ox},{oy})")
        return True

    def _set_packet_size(self) -> None:
        nPacketSize = self.cam.MV_CC_GetOptimalPacketSize()
        if int(nPacketSize) > 0:
            ret = self.cam.MV_CC_SetIntValue("GevSCPSPacketSize", nPacketSize)
            if ret != 0:
                logger.warning(f"Set packet size fail! ret[0x{ret:x}]")
        else:
            logger.warning(f"Get packet size fail! ret[0x{nPacketSize:x}]")

    def _get_payload_size(self) -> bool:
        stParam = MVCC_INTVALUE()
        memset(byref(stParam), 0, sizeof(MVCC_INTVALUE))
        ret = self.cam.MV_CC_GetIntValue("PayloadSize", stParam)
        if ret != 0:
            logger.error(f"Get payload size fail! ret[0x{ret:x}]")
            return False
        self.nPayloadSize = stParam.nCurValue
        return True

    # ------------------------------------------------------------------
    # Trigger / acquisition
    # ------------------------------------------------------------------
    def update_trigger_mode(self, is_hardware_trigger: bool) -> bool:
        ret = self.cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_ON)
        if ret != 0:
            logger.error(f"Set trigger mode fail! ret[0x{ret:x}]")
            return False

        if is_hardware_trigger:
            ret = self.cam.MV_CC_SetEnumValue("TriggerSource", 0)
            if ret != 0:
                logger.error(f"Set trigger source fail! ret[0x{ret:x}]")
                return False
            ret = self.cam.MV_CC_SetEnumValue("LineSelector", 0)
            if ret != 0:
                logger.error(f"Set line selector fail! ret[0x{ret:x}]")
                return False
            ret = self.cam.MV_CC_SetEnumValue("TriggerActivation", 1)
            if ret != 0:
                logger.error(f"Set trigger activation fail! ret[0x{ret:x}]")
                return False
            ret = self.cam.MV_CC_SetIntValue("LineDebouncerTime", 20)
            if ret != 0:
                logger.error(f"Set line debouncer time fail! ret[0x{ret:x}]")
                return False
        else:
            ret = self.cam.MV_CC_SetEnumValue("TriggerSource", MV_TRIGGER_SOURCE_SOFTWARE)
            if ret != 0:
                logger.error(f"Set trigger source fail! ret[0x{ret:x}]")
                return False

        mode = "hardware" if is_hardware_trigger else "software"
        logger.info(f"{self._tag} trigger mode → {mode}")
        return True

    def start_grabbing(self) -> bool:
        ret = self.cam.MV_CC_StartGrabbing()
        if ret != 0:
            logger.error(f"{self._tag} start grabbing failed, ret=0x{ret:x}")
            return False
        logger.info(f"{self._tag} grabbing started")
        return True

    def stop_grabbing(self) -> bool:
        ret = self.cam.MV_CC_StopGrabbing()
        if ret != 0:
            logger.error(f"{self._tag} stop grabbing failed, ret=0x{ret:x}")
            return False
        logger.info(f"{self._tag} grabbing stopped")
        return True

    def capture_image(
        self,
        is_hardware_trigger: bool,
        max_retries: int = 3,
    ) -> np.ndarray | None:
        attempts = 1 if is_hardware_trigger else max_retries

        for attempt in range(attempts):
            if not is_hardware_trigger:
                ret = self.cam.MV_CC_SetCommandValue("TriggerSoftware")
                if ret != 0:
                    logger.error(f"{self._tag} software trigger failed, ret=0x{ret:x}")
                    if attempts > 1:
                        time.sleep(1)
                    continue

            stOutFrame = MV_FRAME_OUT_INFO_EX()
            memset(byref(stOutFrame), 0, sizeof(stOutFrame))
            data_buf = (c_ubyte * self.nPayloadSize)()
            ret = self.cam.MV_CC_GetOneFrameTimeout(byref(data_buf), self.nPayloadSize, stOutFrame, 1000)
            if ret == 0:
                return self.convert_and_save_image(stOutFrame, data_buf)
            logger.error(f"{self._tag} get frame failed, ret=0x{ret:x} (attempt {attempt + 1}/{attempts})")
            if attempt < attempts - 1:
                time.sleep(1)

        logger.error(f"{self._tag} capture failed after {attempts} attempt(s)")
        return None

    def convert_and_save_image(self, stOutFrame: MV_FRAME_OUT_INFO_EX, data_buf: object) -> np.ndarray | None:
        nRGBSize = stOutFrame.nWidth * stOutFrame.nHeight * 3
        stConvertParam = MV_CC_PIXEL_CONVERT_PARAM()
        memset(byref(stConvertParam), 0, sizeof(stConvertParam))
        stConvertParam.nWidth = stOutFrame.nWidth
        stConvertParam.nHeight = stOutFrame.nHeight
        stConvertParam.pSrcData = data_buf
        stConvertParam.nSrcDataLen = stOutFrame.nFrameLen
        stConvertParam.enSrcPixelType = stOutFrame.enPixelType
        stConvertParam.enDstPixelType = PixelType_Gvsp_RGB8_Packed
        stConvertParam.pDstBuffer = (c_ubyte * nRGBSize)()
        stConvertParam.nDstBufferSize = nRGBSize
        ret = self.cam.MV_CC_ConvertPixelType(stConvertParam)
        if ret != 0:
            logger.error(f"Convert pixel type fail! ret[0x{ret:x}]")
            return None

        img_buff = (c_ubyte * nRGBSize)()
        memmove(byref(img_buff), stConvertParam.pDstBuffer, nRGBSize)
        img = np.frombuffer(img_buff, dtype=np.uint8).reshape(
            stConvertParam.nHeight, stConvertParam.nWidth, 3
        )
        return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

    # ------------------------------------------------------------------
    # Exposure
    # ------------------------------------------------------------------
    def write_exposure_time(self, exposure_value: float) -> bool:
        ret = self.cam.MV_CC_SetFloatValue("ExposureTime", float(exposure_value))
        if ret != 0:
            logger.error(f"{self._tag} set exposure failed, ret=0x{ret:x}")
            return False

        time.sleep(0.1)

        target = float(exposure_value)
        actual = None
        for _ in range(5):
            actual = self.get_exposure_time()
            if actual is not None and abs(actual - target) <= 1.0:
                break
            time.sleep(0.05)
        else:
            logger.warning(f"{self._tag} exposure readback mismatch: expected {target}, got {actual}")

        # Drain any pre-exposure frames lingering in the camera buffer.
        try:
            clear_ret = self.cam.MV_CC_ClearImageBuffer()
            if clear_ret != 0:
                logger.warning(f"{self._tag} clear buffer ret=0x{clear_ret:x}")
        except Exception as e:
            logger.warning(f"{self._tag} clear buffer unsupported: {e}")

        logger.info(f"{self._tag} exposure → {int(exposure_value)}us")
        return True

    def flush_one_frame(self, timeout_ms: int = 1000) -> bool:
        """Software-trigger one frame and discard it.

        Used after exposure changes in software-trigger mode to ensure the
        next algorithm pass does not see a transitional frame.
        """
        ret = self.cam.MV_CC_SetCommandValue("TriggerSoftware")
        if ret != 0:
            logger.warning(f"{self._tag} flush trigger failed, ret=0x{ret:x}")
            return False

        stOutFrame = MV_FRAME_OUT_INFO_EX()
        memset(byref(stOutFrame), 0, sizeof(stOutFrame))
        data_buf = (c_ubyte * self.nPayloadSize)()
        ret = self.cam.MV_CC_GetOneFrameTimeout(byref(data_buf), self.nPayloadSize, stOutFrame, timeout_ms)
        if ret != 0:
            logger.warning(f"{self._tag} flush frame timeout, ret=0x{ret:x}")
            return False

        logger.debug(f"{self._tag} discarded 1 stale frame")
        return True

    # ------------------------------------------------------------------
    # Cleanup + queries
    # ------------------------------------------------------------------
    def close_camera(self) -> None:
        if self.cam is None:
            return
        try:
            self.stop_grabbing()
        except Exception as e:
            logger.warning(f"{self._tag} stop_grabbing in close: {e}")
        self.cam.MV_CC_CloseDevice()
        self.cam.MV_CC_DestroyHandle()
        logger.info(f"{self._tag} closed")

    def reinitialize_camera(self) -> bool:
        logger.info(f"{self._tag} reinitializing")
        self.close_camera()
        if self.init_camera():
            logger.info(f"{self._tag} reinitialized")
            return True
        logger.error(f"{self._tag} reinitialize failed")
        return False

    def read_enum_value(self, key: str) -> int | None:
        enum_value = MVCC_ENUMVALUE()
        try:
            ret = self.cam.MV_CC_GetEnumValue(key, enum_value)
            if ret == 0:
                return enum_value.nCurValue
            logger.error(f"Failed to read enum {key} from {self.device_ip}: 0x{ret:x}")
            return None
        except Exception as e:
            logger.error(f"Exception reading enum {key} from {self.device_ip}: {e}")
            return None

    def get_float_value(self, key: str) -> float | None:
        float_value = MVCC_FLOATVALUE()
        try:
            ret = self.cam.MV_CC_GetFloatValue(key, float_value)
            if ret == 0:
                return float_value.fCurValue
            logger.error(f"Failed to read float {key} from {self.device_ip}: 0x{ret:x}")
            return None
        except Exception as e:
            logger.error(f"Exception reading float {key} from {self.device_ip}: {e}")
            return None

    def get_trigger_source(self) -> int | None:
        return self.read_enum_value("TriggerSource")

    def get_exposure_time(self) -> float | None:
        return self.get_float_value("ExposureTime")
