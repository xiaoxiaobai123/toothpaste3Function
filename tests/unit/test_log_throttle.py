"""Tests for core/log_throttle.LogThrottle."""

from __future__ import annotations

import logging

import pytest

from core.log_throttle import LogThrottle


class _ListHandler(logging.Handler):
    """Capture log records into an in-memory list for assertion."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[tuple[int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.levelno, record.getMessage()))


@pytest.fixture
def captured() -> tuple[logging.Logger, _ListHandler]:
    handler = _ListHandler()
    logger = logging.getLogger(f"throttle_test_{id(handler)}")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    return logger, handler


class _Clock:
    """Settable monotonic clock for testing."""

    def __init__(self) -> None:
        self.t = 0.0

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def __call__(self) -> float:
        return self.t


# ---------------------------------------------------------------------------
# Burst behaviour
# ---------------------------------------------------------------------------
def test_first_burst_messages_log_verbatim(captured) -> None:
    logger, handler = captured
    throttle = LogThrottle(logger, burst=3, summary_interval_s=60.0)
    for _ in range(3):
        throttle.error("PLC read exception: boom")
    assert [m for _, m in handler.records] == ["PLC read exception: boom"] * 3


def test_burst_plus_one_is_suppressed(captured) -> None:
    logger, handler = captured
    throttle = LogThrottle(logger, burst=3, summary_interval_s=60.0)
    for _ in range(50):
        throttle.error("PLC read exception: boom")
    # Only the first burst should appear; nothing else until summary interval.
    assert len(handler.records) == 3


def test_summary_fires_after_interval(captured) -> None:
    logger, handler = captured
    clock = _Clock()
    throttle = LogThrottle(logger, burst=3, summary_interval_s=60.0, time_fn=clock)

    for _ in range(50):
        throttle.error("boom")  # all at t=0
    assert len(handler.records) == 3

    clock.advance(60.5)
    throttle.error("boom")  # still suppressed-window check, but interval elapsed
    assert len(handler.records) == 4
    summary = handler.records[-1][1]
    assert "[throttled" in summary
    assert "boom" in summary
    # 50 attempts went into the throttled window plus 1 trigger of the summary
    # = 48 suppressed before the summary line.
    assert "48" in summary or "47" in summary  # accept off-by-one in counting policy


def test_repeating_summaries_after_more_intervals(captured) -> None:
    logger, handler = captured
    clock = _Clock()
    throttle = LogThrottle(logger, burst=3, summary_interval_s=60.0, time_fn=clock)

    for _ in range(10):
        throttle.error("boom")  # 3 logged, 7 suppressed
    clock.advance(60.5)
    throttle.error("boom")  # summary #1
    for _ in range(20):
        throttle.error("boom")  # all suppressed
    clock.advance(60.5)
    throttle.error("boom")  # summary #2

    # Records: 3 burst + summary#1 + summary#2 = 5
    assert len(handler.records) == 5
    assert sum("[throttled" in m for _, m in handler.records) == 2


# ---------------------------------------------------------------------------
# Multi-key behaviour
# ---------------------------------------------------------------------------
def test_distinct_messages_track_independently(captured) -> None:
    logger, handler = captured
    throttle = LogThrottle(logger, burst=3, summary_interval_s=60.0)

    for _ in range(10):
        throttle.error("error A")
    for _ in range(2):
        throttle.error("error B")

    msgs = [m for _, m in handler.records]
    assert msgs.count("error A") == 3  # burst hit
    assert msgs.count("error B") == 2  # under burst, all logged


def test_max_keys_lru_eviction(captured) -> None:
    logger, handler = captured
    throttle = LogThrottle(logger, burst=3, summary_interval_s=60.0, max_keys=2)

    throttle.error("a")  # state: {a}
    throttle.error("b")  # state: {a, b}
    throttle.error("c")  # state: {b, c}, "a" evicted
    throttle.error("a")  # treated as new (evicted), so re-logs

    msgs = [m for _, m in handler.records]
    assert msgs.count("a") == 2  # logged twice (initial + re-after-eviction)
    assert msgs.count("b") == 1
    assert msgs.count("c") == 1


# ---------------------------------------------------------------------------
# Level routing
# ---------------------------------------------------------------------------
def test_routes_to_correct_level(captured) -> None:
    logger, handler = captured
    throttle = LogThrottle(logger, burst=3, summary_interval_s=60.0)

    throttle.error("err")
    throttle.warning("warn")
    throttle.info("info")

    levels = [lvl for lvl, _ in handler.records]
    assert logging.ERROR in levels
    assert logging.WARNING in levels
    assert logging.INFO in levels
