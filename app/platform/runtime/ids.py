"""Compact, monotonic lease-id generation without UUID overhead."""

import threading

_lock = threading.Lock()
_counter: int = 0


def next_id() -> int:
    """Return a process-local monotonically increasing integer id."""
    global _counter
    with _lock:
        _counter += 1
        return _counter


def next_hex(length: int = 12) -> str:
    """Return a zero-padded hex string derived from the monotonic counter."""
    return format(next_id(), f"0{length}x")
