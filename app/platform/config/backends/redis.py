"""Redis config backend (multi-replica / K8s)."""

from typing import Any

from .base import ConfigBackend
from ._serde import flatten, unflatten

_DEFAULT_HASH_KEY    = "config:user"
_DEFAULT_VERSION_KEY = "config:version"


class RedisConfigBackend(ConfigBackend):
    """Flat key-value storage in a Redis Hash.

    Each dotted config key is one Hash field (JSON-encoded value).
    ``apply_patch`` only writes the changed fields — all other fields
    in the hash are untouched.
    """

    def __init__(
        self,
        redis,
        hash_key: str = _DEFAULT_HASH_KEY,
        version_key: str = _DEFAULT_VERSION_KEY,
    ) -> None:
        self._r           = redis
        self._hash_key    = hash_key
        self._version_key = version_key

    async def load(self) -> dict[str, Any]:
        raw: dict[bytes, bytes] = await self._r.hgetall(self._hash_key)
        if not raw:
            return {}
        flat = {k.decode(): v.decode() for k, v in raw.items()}
        return unflatten(flat)

    async def apply_patch(self, patch: dict[str, Any]) -> None:
        flat = flatten(patch)
        if not flat:
            return
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hset(self._hash_key, mapping=flat)
            pipe.incr(self._version_key)
            await pipe.execute()

    async def version(self) -> object:
        v = await self._r.get(self._version_key)
        return int(v) if v else 0

    async def close(self) -> None:
        await self._r.aclose()
