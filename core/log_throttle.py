"""Throttled logger wrapper — dedupes repeated identical errors.

Hot async loops in this codebase poll the PLC every 50–100 ms. When a
persistent fault (PLC unreachable, protocol mismatch, camera offline)
triggers the same exception each iteration, the unthrottled logger
floods my_app.log with thousands of identical lines per minute and the
RotatingFileHandler quickly rotates real signal out of the 25 MB window.

This wrapper:
    * Logs the first ``burst`` occurrences of an exact message verbatim.
    * Suppresses subsequent identical messages.
    * Every ``summary_interval_s`` of continued repetition, emits one
      summary line: "<msg>  [throttled — N more in last 60s]", and
      arms the next burst window.

Different message strings track independently. An LRU cap (``max_keys``)
keeps the bookkeeping bounded if the caller logs unbounded distinct
strings (e.g. messages with timestamps).

Thread-safe — TaskManager runs camera loops in asyncio.to_thread workers,
so multiple threads will hit the throttle concurrently.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock

DEFAULT_BURST = 3
DEFAULT_SUMMARY_INTERVAL_S = 60.0
DEFAULT_MAX_KEYS = 256


class LogThrottle:
    """Wrap a stdlib logger; expose error/warning/info that dedupe by exact message."""

    def __init__(
        self,
        logger: logging.Logger,
        burst: int = DEFAULT_BURST,
        summary_interval_s: float = DEFAULT_SUMMARY_INTERVAL_S,
        max_keys: int = DEFAULT_MAX_KEYS,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._logger = logger
        self._burst = burst
        self._summary_interval = summary_interval_s
        self._max_keys = max_keys
        self._time_fn = time_fn or time.monotonic
        # key → {count, first_at, last_logged_at, level}
        # OrderedDict so we can LRU-evict on overflow.
        self._state: OrderedDict[str, dict[str, float | int]] = OrderedDict()
        self._lock = Lock()

    def error(self, msg: str) -> None:
        self._submit(logging.ERROR, msg)

    def warning(self, msg: str) -> None:
        self._submit(logging.WARNING, msg)

    def info(self, msg: str) -> None:
        self._submit(logging.INFO, msg)

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------
    def _submit(self, level: int, msg: str) -> None:
        now = self._time_fn()
        with self._lock:
            entry = self._state.get(msg)
            if entry is None:
                # First sighting of this message — log normally + start counting.
                self._state[msg] = {
                    "count": 1,
                    "first_at": now,
                    "last_logged_at": now,
                    "level": level,
                }
                self._evict_overflow()
                self._logger.log(level, msg)
                return

            # Move to MRU end of OrderedDict for LRU policy.
            self._state.move_to_end(msg)
            entry["count"] = int(entry["count"]) + 1

            if entry["count"] <= self._burst:
                # Still within the burst window — log normally.
                entry["last_logged_at"] = now
                self._logger.log(level, msg)
                return

            # Burst exhausted — suppress unless summary interval has elapsed.
            elapsed = now - float(entry["last_logged_at"])
            if elapsed >= self._summary_interval:
                suppressed = int(entry["count"]) - self._burst
                summary = (
                    f"{msg}  [throttled — same message repeated {suppressed} "
                    f"more times in last {elapsed:.0f}s]"
                )
                self._logger.log(level, summary)
                entry["last_logged_at"] = now
                # Reset the count so we re-arm a "burst" window after the summary,
                # but cap it at burst so we don't immediately log the next 3 again
                # — only the very next summary, summary_interval later.
                entry["count"] = self._burst

    def _evict_overflow(self) -> None:
        while len(self._state) > self._max_keys:
            self._state.popitem(last=False)
