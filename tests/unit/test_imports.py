"""Smoke test: every public module imports cleanly.

Catches typos, circular imports, and missing __init__.py exports before
the more expensive integration tests run. Does NOT exercise the camera
SDK (camera/base.py needs MvCameraControl_class) or the live PLC.
"""

from __future__ import annotations


def test_core_imports() -> None:
    from core import config_manager, license_utils, log_config, version  # noqa: F401


def test_plc_imports() -> None:
    from plc import (  # noqa: F401
        CameraResult,
        CameraStatus,
        CameraTriggerStatus,
        Endian,
        ProductType,
        SystemStatus,
    )
    from plc.base import PLCBase  # noqa: F401
    from plc.codec import (  # noqa: F401
        double_to_words,
        float32_to_words,
        uint32_to_words,
        word_to_int16,
        words_to_float32,
        words_to_uint32,
    )
    from plc.manager import PLCManager  # noqa: F401


def test_processing_imports() -> None:
    from processing import PROCESSORS, Outcome, ProcessResult, dispatch  # noqa: F401
    from processing.algorithms import (  # noqa: F401
        adjust_bounds,
        convert_to_center_coordinates,
        validate_and_adjust_param,
    )
    from processing.brush_head import BrushHeadProcessor  # noqa: F401


def test_product_type_values() -> None:
    """Confirm ProductType integer values match the PLC contract."""
    from plc.enums import ProductType

    assert ProductType.NONE.value == 0
    assert ProductType.TOOTHPASTE_FRONTBACK.value == 1
    assert ProductType.HEIGHT_CHECK.value == 2
    assert ProductType.BRUSH_HEAD.value == 3


def test_registry_exposes_implemented_processors() -> None:
    """Registry must contain every implemented ProductType, and only those."""
    from plc.enums import ProductType
    from processing import dispatch
    from processing.brush_head import BrushHeadProcessor

    assert isinstance(dispatch(ProductType.BRUSH_HEAD), BrushHeadProcessor)
    # P3 not yet implemented — registry returns None.
    assert dispatch(ProductType.TOOTHPASTE_FRONTBACK) is None
    assert dispatch(ProductType.HEIGHT_CHECK) is None


def test_codec_round_trip() -> None:
    """Encoding then decoding produces the original value."""
    from plc.codec import (
        float32_to_words,
        uint32_to_words,
        word_to_int16,
        words_to_float32,
        words_to_uint32,
    )
    from plc.enums import Endian

    # uint32 round-trip
    for v in (0, 1, 50000, 65535, 2**31 - 1, 4_000_000_000):
        words = uint32_to_words(v, Endian.LITTLE)
        assert words_to_uint32(words[0], words[1], Endian.LITTLE) == v

    # float32 round-trip (within float precision)
    for v in (0.0, 1.0, -1.5, 3.14159, 1.5e-3, -1e6):
        words = float32_to_words(v)
        decoded = words_to_float32(words[0], words[1])
        assert abs(decoded - v) < 1e-3 + abs(v) * 1e-6

    # Signed int16
    assert word_to_int16(0) == 0
    assert word_to_int16(32767) == 32767
    assert word_to_int16(32768) == -32768
    assert word_to_int16(65535) == -1
