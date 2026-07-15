"""Abstract config backend interface."""

from abc import ABC, abstractmethod
from typing import Any


class ConfigBackend(ABC):
    """Persist and reload user config overrides.

    Storage model: flat key-value pairs (dotted keys → JSON-serialised values).
    ``load()``        rebuilds the full nested dict from all stored pairs.
    ``apply_patch()`` writes *only* the changed keys, leaving the rest untouched.
    """

    @abstractmethod
    async def load(self) -> dict[str, Any]:
        """Return the full stored user-overrides as a nested dict."""

    @abstractmethod
    async def apply_patch(self, patch: dict[str, Any]) -> None:
        """Persist only the keys present in *patch* (nested dict)."""

    @abstractmethod
    async def version(self) -> object:
        """Return an opaque version token (cheap — called on every request)."""

    async def close(self) -> None:
        """Release any resources held by the backend (optional)."""
