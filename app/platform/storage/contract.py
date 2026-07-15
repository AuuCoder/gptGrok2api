"""Abstract storage contract used by all persistence backends."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LockHandle(Protocol):
    """Async context manager representing a distributed lock."""

    async def __aenter__(self) -> "LockHandle": ...
    async def __aexit__(self, *_: Any) -> None: ...


class StorageError(Exception):
    """Base class for all storage-layer errors."""


class LockAcquisitionError(StorageError):
    """Raised when a lock cannot be acquired within the timeout."""


__all__ = ["LockHandle", "StorageError", "LockAcquisitionError"]
