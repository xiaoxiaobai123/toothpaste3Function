"""Low-level Modbus TCP client used by the PLC manager.

Wraps pyModbusTCP with:
    - Block reads larger than 125 registers split into chunks
    - Block writes larger than 123 registers split into chunks
    - Signed-register handling for D124/125/126 (system command words)
    - Connection state logging
"""

from __future__ import annotations

from pyModbusTCP.client import ModbusClient

from core import log_config

logger = log_config.setup_logging()


class PLCBase:
    """Modbus TCP client wrapper.

    Exposes simple read_status / write_status / write_multiple_registers
    methods. Higher-level type marshalling (uint32 / float / double splits)
    lives in plc/manager.py.
    """

    SIGNED_REGISTERS = {124, 125, 126}
    MAX_READ_PER_CALL = 125
    MAX_WRITE_PER_CALL = 123

    def __init__(self, plc_ip: str, port: int = 502) -> None:
        self.client = ModbusClient(host=plc_ip, port=port, auto_open=True, timeout=1)
        self.connected = self.client.open()
        if self.connected:
            logger.info(f"[PLC] connected {plc_ip}")
        else:
            logger.error(f"[PLC] connect failed {plc_ip}")

    def read_status(self, address: int, count: int = 1) -> int | list[int] | None:
        """Read consecutive holding registers.

        Returns a single int if count == 1, otherwise a list of ints.
        Returns None on any read failure (caller decides retry policy).
        """
        try:
            if count <= self.MAX_READ_PER_CALL:
                registers = self.client.read_holding_registers(address, count)
                if not registers:
                    logger.error(f"[PLC] read failed addr={address} count={count}")
                    return None
                return registers[0] if count == 1 else registers

            # Multi-chunk read
            collected: list[int] = []
            cur = address
            remaining = count
            while remaining > 0:
                chunk = min(self.MAX_READ_PER_CALL, remaining)
                registers = self.client.read_holding_registers(cur, chunk)
                if registers is None:
                    logger.error(f"[PLC] read failed addr={cur} count={chunk}")
                    return None
                collected.extend(registers)
                cur += chunk
                remaining -= chunk
            return collected[0] if count == 1 else collected
        except Exception as e:
            logger.exception(f"[PLC] read exception addr={address} count={count}: {e}")
            return None

    def write_status(self, address: int, value: int) -> bool:
        """Write a single holding register, handling signed-register conversion."""
        try:
            if address in self.SIGNED_REGISTERS:
                if not (-32768 <= value <= 32767):
                    logger.error(f"[PLC] signed value out of range at D{address}: {value}")
                    return False
                if value < 0:
                    value = 65536 + value
            else:
                if not (0 <= value <= 65535):
                    logger.error(f"[PLC] unsigned value out of range at D{address}: {value}")
                    return False
            return bool(self.client.write_single_register(address, value))
        except Exception as e:
            logger.exception(f"[PLC] write exception addr={address}: {e}")
            return False

    def write_multiple_registers(self, address: int, values: list[int]) -> bool:
        """Write a block of values, splitting into <=123-word chunks."""
        try:
            cur = address
            remaining = list(values)
            while remaining:
                chunk = remaining[: self.MAX_WRITE_PER_CALL]
                processed = [v + 65536 if v < 0 else v for v in chunk]
                if not self.client.write_multiple_registers(cur, processed):
                    logger.error(f"[PLC] block write failed addr={cur} values={processed}")
                    return False
                cur += len(chunk)
                remaining = remaining[self.MAX_WRITE_PER_CALL :]
            return True
        except Exception as e:
            logger.exception(f"[PLC] block write exception addr={address}: {e}")
            return False

    def close(self) -> None:
        if self.client.is_open:
            self.client.close()
            logger.info("[PLC] connection closed")
        else:
            logger.warning("[PLC] connection already closed")
