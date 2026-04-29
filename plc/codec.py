"""Modbus word ↔ typed-value codec helpers.

PLC values are 16-bit holding registers (uint16). Logical types larger
than 16 bits are stored as consecutive register pairs/quads with a
device-specific byte order. Mitsubishi FX5U / Q-series default to
little-endian word order, which is what we use unless overridden.

These helpers are pure functions — Processors decode their algorithm
parameters from raw_config without needing a PLCManager reference.
"""

from __future__ import annotations

import struct

from plc.enums import Endian


def word_to_int16(word: int) -> int:
    """Reinterpret a uint16 as a signed int16."""
    return word - 65536 if word >= 32768 else word


def words_to_uint32(low: int, high: int, endian: Endian = Endian.LITTLE) -> int:
    """Combine two uint16 words into a uint32."""
    if endian == Endian.LITTLE:
        return (high << 16) | low
    return (low << 16) | high


def words_to_int32(low: int, high: int, endian: Endian = Endian.LITTLE) -> int:
    """Combine two uint16 words into a signed int32."""
    value = words_to_uint32(low, high, endian)
    return value - (1 << 32) if value >= (1 << 31) else value


def words_to_float32(low: int, high: int, endian: Endian = Endian.LITTLE) -> float:
    """Combine two uint16 words into an IEEE-754 float32."""
    value = words_to_uint32(low, high, endian)
    return struct.unpack("!f", struct.pack("!I", value))[0]


def uint32_to_words(value: int, endian: Endian = Endian.LITTLE) -> list[int]:
    """Split a uint32 into two uint16 words for write_multiple_registers."""
    low, high = value & 0xFFFF, (value >> 16) & 0xFFFF
    return [low, high] if endian == Endian.LITTLE else [high, low]


def float32_to_words(value: float) -> list[int]:
    """Split a float32 into two uint16 words (PLC-compatible byte order)."""
    packed = struct.pack("<f", value)
    return [
        (packed[1] << 8) | packed[0],
        (packed[3] << 8) | packed[2],
    ]


def double_to_words(value: float) -> list[int]:
    """Split a float64 into four uint16 words."""
    packed = struct.pack("<d", value)
    return [
        (packed[1] << 8) | packed[0],
        (packed[3] << 8) | packed[2],
        (packed[5] << 8) | packed[4],
        (packed[7] << 8) | packed[6],
    ]
