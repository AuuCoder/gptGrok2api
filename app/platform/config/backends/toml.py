"""TOML-file config backend (default, single-instance)."""

import asyncio
import os
from pathlib import Path
from typing import Any

import tomli_w
import tomllib

from .base import ConfigBackend
from ..loader import _deep_merge as deep_merge


class TomlConfigBackend(ConfigBackend):
    """Stores user overrides in a local TOML file.

    Flat-key concern doesn't apply here: TOML is the native format and the
    file is small enough that a full rewrite is negligible for local use.
    Version token = file mtime.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    async def load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        return await asyncio.to_thread(self._read)

    async def apply_patch(self, patch: dict[str, Any]) -> None:
        await asyncio.to_thread(self._merge_write, patch)

    async def version(self) -> object:
        return _mtime(self._path)

    def _read(self) -> dict[str, Any]:
        with open(self._path, "rb") as fh:
            return tomllib.load(fh)

    def _merge_write(self, patch: dict[str, Any]) -> None:
        existing = {}
        if self._path.exists():
            with open(self._path, "rb") as fh:
                existing = tomllib.load(fh)
        merged = deep_merge(existing, patch)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "wb") as fh:
            tomli_w.dump(merged, fh)


def _mtime(path: Path) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0
