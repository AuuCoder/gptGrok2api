"""Revision semantics for storage backends.

## Contract

- A **revision** is a monotonically increasing integer scoped to a single
  storage domain (e.g. accounts, proxies).
- Every mutation that changes persisted state **must** increment the revision
  by exactly 1.
- Readers compare their last-seen revision against the current value to detect
  whether new data is available (``scan_changes(since_revision=...)``).
- Revisions are **not** globally ordered across domains — only within one.

## RevisionTracker mixin

Backends that manage their own revision counter (e.g. SQLite, in-memory)
can inherit from ``RevisionTracker`` to get a thread-safe bump + read helper.
"""

import threading


class RevisionTracker:
    """Thread-safe monotonic revision counter.

    Subclass or mix into a storage backend to provide ``bump()`` and
    ``current`` without touching the database.
    """

    def __init__(self, initial: int = 0) -> None:
        self._revision = initial
        self._lock = threading.Lock()

    @property
    def current(self) -> int:
        with self._lock:
            return self._revision

    def bump(self) -> int:
        """Increment and return the new revision."""
        with self._lock:
            self._revision += 1
            return self._revision

    def set(self, value: int) -> None:
        """Force-set the revision (e.g. after loading from DB)."""
        with self._lock:
            self._revision = value


__all__ = ["RevisionTracker"]
