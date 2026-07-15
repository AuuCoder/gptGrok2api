"""SQL config backend (MySQL / PostgreSQL)."""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from .base import ConfigBackend
from ._serde import flatten, unflatten

_TABLE       = "config_store"
_VERSION_KEY = "__version__"

_metadata = sa.MetaData()

config_store_table = sa.Table(
    _TABLE,
    _metadata,
    sa.Column("key",   sa.String(255), primary_key=True),
    sa.Column("value", sa.Text, nullable=False, default=""),
)


class SqlConfigBackend(ConfigBackend):
    """Flat key-value storage in a ``config_store`` table.

    Each dotted config key is one row. ``apply_patch`` only UPSERTs the
    changed rows — the rest of the table is untouched.
    Version token is stored as the integer value of the ``__version__`` row.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        dialect: str = "postgresql",
        dispose_engine: bool = True,
    ) -> None:
        self._engine  = engine
        self._dialect = dialect  # "mysql" | "postgresql"
        self._ready   = False
        self._dispose_engine = dispose_engine

    async def _ensure_table(self) -> None:
        if self._ready:
            return
        async with self._engine.begin() as conn:
            await conn.run_sync(_metadata.create_all)
        self._ready = True

    async def load(self) -> dict[str, Any]:
        await self._ensure_table()
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                sa.select(config_store_table)
                .where(config_store_table.c.key != _VERSION_KEY)
            )
        flat = {row.key: row.value for row in rows}
        return unflatten(flat)

    async def apply_patch(self, patch: dict[str, Any]) -> None:
        await self._ensure_table()
        flat = flatten(patch)
        if not flat:
            return
        async with self._engine.begin() as conn:
            for k, v in flat.items():
                await conn.execute(self._upsert(k, v))
            # Atomically increment version counter (no read-modify-write race).
            await conn.execute(self._upsert_incr_version())

    async def version(self) -> object:
        await self._ensure_table()
        async with self._engine.connect() as conn:
            row = await conn.execute(
                sa.select(config_store_table.c.value)
                .where(config_store_table.c.key == _VERSION_KEY)
            )
            val = row.scalar()
        return int(val) if val else 0

    def _upsert(self, key: str, value: str) -> sa.Insert:
        if self._dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
            return (
                insert(config_store_table)
                .values(key=key, value=value)
                .on_conflict_do_update(index_elements=["key"], set_={"value": value})
            )
        else:  # mysql
            from sqlalchemy.dialects.mysql import insert
            return (
                insert(config_store_table)
                .values(key=key, value=value)
                .on_duplicate_key_update(value=value)
            )

    def _upsert_incr_version(self) -> sa.Insert:
        """Atomically insert or increment the version counter row."""
        if self._dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
            return (
                insert(config_store_table)
                .values(key=_VERSION_KEY, value="1")
                .on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": sa.func.cast(
                        sa.func.cast(config_store_table.c.value, sa.Integer) + 1,
                        sa.Text,
                    )},
                )
            )
        else:  # mysql
            from sqlalchemy.dialects.mysql import insert
            return (
                insert(config_store_table)
                .values(key=_VERSION_KEY, value="1")
                .on_duplicate_key_update(
                    value=sa.func.cast(
                        sa.func.cast(config_store_table.c.value, sa.Integer) + 1,
                        sa.Text,
                    )
                )
            )

    async def close(self) -> None:
        if self._dispose_engine:
            from app.control.account.backends.sql import _evict_cached_engine
            _evict_cached_engine(self._engine)
            await self._engine.dispose()
