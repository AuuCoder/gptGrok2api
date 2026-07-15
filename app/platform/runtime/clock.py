"""Monotonic-safe time utilities for hot-path and control-plane use."""

import time


def now_ms() -> int:
    """Return current wall-clock time in milliseconds."""
    return int(time.time() * 1000)


def now_s() -> int:
    """Return current wall-clock time in whole seconds."""
    return int(time.time())


def ms_to_s(ms: int) -> int:
    """Convert millisecond timestamp to second timestamp."""
    return ms // 1000


def s_to_ms(s: int) -> int:
    """Convert second timestamp to millisecond timestamp."""
    return s * 1000
