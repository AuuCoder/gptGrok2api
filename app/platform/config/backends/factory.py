"""Config backend factory — follows ACCOUNT_STORAGE automatically."""

import os
from pathlib import Path

from app.platform.paths import data_path
from .base import ConfigBackend


def get_config_backend_name() -> str:
    """Return the active config backend name (mirrors ACCOUNT_STORAGE)."""
    return os.getenv("ACCOUNT_STORAGE", "local").strip().lower()


def create_config_backend() -> ConfigBackend:
    """Instantiate the config backend that matches the account storage backend.

    ``ACCOUNT_STORAGE=local``       → TOML file (``${DATA_DIR}/config.toml``)
    ``ACCOUNT_STORAGE=redis``       → Redis  (ACCOUNT_REDIS_URL)
    ``ACCOUNT_STORAGE=mysql``       → MySQL  (ACCOUNT_MYSQL_URL)
    ``ACCOUNT_STORAGE=postgresql``  → PostgreSQL (ACCOUNT_POSTGRESQL_URL)

    No extra env vars needed — reuses the same connection settings as accounts.
    """
    backend = get_config_backend_name()

    if backend == "local":
        return _make_toml()
    if backend == "redis":
        return _make_redis()
    if backend in ("mysql", "postgresql"):
        return _make_sql(backend)

    raise ValueError(f"Unknown account storage backend: {backend!r}")


def _make_toml() -> ConfigBackend:
    from .toml import TomlConfigBackend

    path_str = os.getenv("CONFIG_LOCAL_PATH", str(data_path("config.toml"))).strip()
    path = Path(path_str)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[5] / path
    return TomlConfigBackend(path)


def _make_redis() -> ConfigBackend:
    from redis.asyncio import Redis
    from .redis import RedisConfigBackend

    url = os.getenv("ACCOUNT_REDIS_URL", "").strip()
    if not url:
        raise ValueError("Redis config backend requires ACCOUNT_REDIS_URL")
    r = Redis.from_url(url, decode_responses=False)
    return RedisConfigBackend(r)


def _make_sql(dialect: str) -> ConfigBackend:
    from .sql import SqlConfigBackend
    from app.control.account.backends.sql import (
        create_mysql_engine,
        create_pgsql_engine,
    )

    if dialect == "mysql":
        url = os.getenv("ACCOUNT_MYSQL_URL", "").strip()
        if not url:
            raise ValueError("MySQL config backend requires ACCOUNT_MYSQL_URL")
        engine = create_mysql_engine(url)
    else:
        url = os.getenv("ACCOUNT_POSTGRESQL_URL", "").strip()
        if not url:
            raise ValueError("PostgreSQL config backend requires ACCOUNT_POSTGRESQL_URL")
        engine = create_pgsql_engine(url)

    return SqlConfigBackend(engine, dialect=dialect, dispose_engine=False)


__all__ = ["create_config_backend", "get_config_backend_name"]
