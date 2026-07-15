"""First-boot data migration.

Config migration
----------------
local   : seeds ``${DATA_DIR}/config.toml`` from ``config.defaults.toml`` if
          the file does not exist yet — gives users an editable copy on first run.
redis / sql : if the backend is empty (version == 0) AND
          ``${DATA_DIR}/config.toml`` exists, migrates the user overrides into
          the DB backend. If it does not exist either, nothing is written
          (defaults are always loaded from ``config.defaults.toml`` at runtime).

Account migration
-----------------
Runs only when ACCOUNT_STORAGE != "local".
If ``${DATA_DIR}/accounts.db`` (the previous local SQLite store) exists AND the
target backend is empty (revision == 0), all accounts are copied into the
new backend — preserving pool, status, quota, usage stats, and timestamps.
After a successful migration the SQLite file is renamed to
``${DATA_DIR}/accounts.db.migrated`` so the same migration is never re-run.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from app.platform.paths import data_path

if TYPE_CHECKING:
    from app.control.account.commands import AccountPatch
    from app.control.account.repository import AccountRepository
    from app.platform.config.backends.base import ConfigBackend

_BASE_DIR     = Path(__file__).resolve().parents[3]
_DEFAULTS_PATH = _BASE_DIR / "config.defaults.toml"
_USER_CFG_PATH = data_path("config.toml")
_LOCAL_DB_PATH = data_path("accounts.db")
_BATCH         = 500  # accounts per upsert/patch batch


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_startup_migrations(
    config_backend: "ConfigBackend",
    account_repo: "AccountRepository",
) -> None:
    """Run all first-boot migrations.  Safe to call on every startup."""
    await _migrate_config(config_backend)
    await _migrate_basic_refresh_interval(config_backend)
    await _migrate_accounts(account_repo)
    await _backfill_grok_4_3_quota(account_repo)
    await _normalize_basic_fast_only_quota(account_repo)
    await _backfill_console_quota(account_repo)


# ---------------------------------------------------------------------------
# Config migration
# ---------------------------------------------------------------------------

async def _migrate_config(backend: "ConfigBackend") -> None:
    from app.platform.config.backends.factory import get_config_backend_name
    from app.platform.config.loader import load_toml

    backend_name = get_config_backend_name()

    if backend_name == "local":
        # Seed ${DATA_DIR}/config.toml from defaults so users have an editable file.
        if not _USER_CFG_PATH.exists() and _DEFAULTS_PATH.exists():
            await asyncio.to_thread(shutil.copy2, _DEFAULTS_PATH, _USER_CFG_PATH)
            logger.info("config: seeded {} from config.defaults.toml", _USER_CFG_PATH)
        return

    # DB / Redis backends — migrate only if backend is empty.
    if await backend.version() != 0:
        return  # already has data, skip

    if _USER_CFG_PATH.exists():
        user_data = await asyncio.to_thread(load_toml, _USER_CFG_PATH)
        if user_data:
            await backend.apply_patch(user_data)
            logger.info(
                "config: migrated {} -> {} backend ({} keys)",
                _USER_CFG_PATH,
                backend_name,
                _count_keys(user_data),
            )
            return

    logger.debug("config: {} backend is empty, no local overrides to migrate", backend_name)


async def _migrate_basic_refresh_interval(backend: "ConfigBackend") -> None:
    data = await backend.load()
    account = data.get("account", {})
    refresh = account.get("refresh", {}) if isinstance(account, dict) else {}
    value = refresh.get("basic_interval_sec") if isinstance(refresh, dict) else None
    try:
        old_default = int(value)
    except (TypeError, ValueError):
        return
    if old_default != 36_000:
        return
    await backend.apply_patch({"account": {"refresh": {"basic_interval_sec": 86_400}}})
    logger.info("config: updated basic refresh interval default from 36000s to 86400s")


# ---------------------------------------------------------------------------
# Account migration
# ---------------------------------------------------------------------------

async def _migrate_accounts(target_repo: "AccountRepository") -> None:
    from app.control.account.backends.factory import get_repository_backend

    if get_repository_backend() == "local":
        return  # already on local, nothing to migrate

    sqlite_path = _LOCAL_DB_PATH
    if not sqlite_path.exists():
        return  # no previous local data

    # Check whether the target already has data.
    snapshot = await target_repo.runtime_snapshot()
    if snapshot.revision > 0 or snapshot.items:
        logger.debug("account: target backend not empty (revision={}), skipping migration", snapshot.revision)
        return

    logger.info("account: migrating accounts from {} → {} backend", sqlite_path, get_repository_backend())
    count = await _copy_accounts(sqlite_path, target_repo)

    # Rename the SQLite file so this migration is never re-run.
    done_path = sqlite_path.with_suffix(".db.migrated")
    await asyncio.to_thread(sqlite_path.rename, done_path)
    logger.info("account: migration complete ({} accounts), renamed {} → {}", count, sqlite_path.name, done_path.name)


async def _copy_accounts(sqlite_path: Path, target: "AccountRepository") -> int:
    """Read all accounts from the local SQLite file and write to *target*."""
    from app.control.account.backends.local import LocalAccountRepository
    from app.control.account.commands import AccountUpsert, ListAccountsQuery

    source = LocalAccountRepository(sqlite_path)
    await source.initialize()

    total = 0
    page = 1

    try:
        while True:
            result = await source.list_accounts(
                ListAccountsQuery(page=page, page_size=_BATCH, include_deleted=True)
            )
            records = result.items
            if not records:
                break

            # Step 1: upsert — creates records with token / pool / tags / ext.
            upserts = [
                AccountUpsert(token=r.token, pool=r.pool, tags=r.tags, ext=r.ext)
                for r in records
            ]
            await target.upsert_accounts(upserts)

            # Step 2: patch — fills status, quota, usage counters, timestamps.
            patches = [_record_to_patch(r) for r in records]
            await target.patch_accounts(patches)

            # Step 3: soft-delete records that were deleted in the source.
            deleted_tokens = [r.token for r in records if r.deleted_at is not None]
            if deleted_tokens:
                await target.delete_accounts(deleted_tokens)

            total += len(records)
            if page >= result.total_pages:
                break
            page += 1
    finally:
        await source.close()

    return total


def _record_to_patch(r) -> "AccountPatch":
    from app.control.account.commands import AccountPatch

    qs = r.quota_set()
    return AccountPatch(
        token=r.token,
        status=r.status,
        quota_auto=qs.auto.to_dict()   if qs.auto   else None,
        quota_fast=qs.fast.to_dict()   if qs.fast   else None,
        quota_expert=qs.expert.to_dict() if qs.expert else None,
        quota_heavy=qs.heavy.to_dict()    if qs.heavy    else None,
        quota_grok_4_3=qs.grok_4_3.to_dict() if qs.grok_4_3 else None,
        quota_console=qs.console.to_dict() if qs.console else None,
        # Usage counts — target starts at 0, so actual value == delta.
        usage_use_delta=r.usage_use_count   or None,
        usage_fail_delta=r.usage_fail_count or None,
        usage_sync_delta=r.usage_sync_count or None,
        last_use_at=r.last_use_at,
        last_fail_at=r.last_fail_at,
        last_fail_reason=r.last_fail_reason,
        last_sync_at=r.last_sync_at,
        last_clear_at=r.last_clear_at,
        state_reason=r.state_reason,
        ext_merge=r.ext or None,
    )


# ---------------------------------------------------------------------------
# Backfill quota_grok_4_3 for super/heavy accounts imported before the field existed.
# ---------------------------------------------------------------------------

async def _backfill_grok_4_3_quota(repo: "AccountRepository") -> None:
    from app.control.account.commands import AccountPatch, ListAccountsQuery
    from app.control.account.quota_defaults import default_quota_window

    patches: list[AccountPatch] = []
    page = 1
    while True:
        result = await repo.list_accounts(
            ListAccountsQuery(page=page, page_size=_BATCH, include_deleted=False)
        )
        for record in result.items:
            if record.pool not in ("super", "heavy"):
                continue
            if record.quota_set().grok_4_3 is not None:
                continue
            window = default_quota_window(record.pool, 4)
            if window is None:
                continue
            patches.append(AccountPatch(token=record.token, quota_grok_4_3=window.to_dict()))
        if page >= result.total_pages:
            break
        page += 1

    if not patches:
        return

    total = 0
    for i in range(0, len(patches), _BATCH):
        batch = patches[i : i + _BATCH]
        res = await repo.patch_accounts(batch)
        total += res.patched
    logger.info("account: backfilled quota_grok_4_3 for {} super/heavy accounts", total)


async def _normalize_basic_fast_only_quota(repo: "AccountRepository") -> None:
    from app.control.account.commands import AccountPatch, ListAccountsQuery
    from app.control.account.quota_defaults import normalize_quota_set

    patches: list[AccountPatch] = []
    page = 1
    while True:
        result = await repo.list_accounts(
            ListAccountsQuery(
                page=page,
                page_size=_BATCH,
                pool="basic",
                include_deleted=False,
            )
        )
        for record in result.items:
            normalized = normalize_quota_set("basic", record.quota_set())
            if normalized.to_dict() == record.quota_set().to_dict():
                continue
            patches.append(
                AccountPatch(
                    token=record.token,
                    quota_auto=normalized.auto.to_dict(),
                    quota_fast=normalized.fast.to_dict(),
                    quota_expert=normalized.expert.to_dict(),
                )
            )
        if page >= result.total_pages:
            break
        page += 1

    if not patches:
        return

    total = 0
    for i in range(0, len(patches), _BATCH):
        batch = patches[i : i + _BATCH]
        res = await repo.patch_accounts(batch)
        total += res.patched
    logger.info("account: normalized {} basic accounts to fast-only quota", total)


async def _backfill_console_quota(repo: "AccountRepository") -> None:
    """Backfill quota_console for all accounts that don't have it yet."""
    from app.control.account.commands import AccountPatch, ListAccountsQuery
    from app.control.account.quota_defaults import default_quota_window

    patches: list[AccountPatch] = []
    page = 1
    while True:
        result = await repo.list_accounts(
            ListAccountsQuery(page=page, page_size=_BATCH, include_deleted=False)
        )
        for record in result.items:
            if record.quota_set().console is not None:
                continue
            window = default_quota_window(record.pool, 5)
            if window is None:
                continue
            patches.append(AccountPatch(token=record.token, quota_console=window.to_dict()))
        if page >= result.total_pages:
            break
        page += 1

    if not patches:
        return

    total = 0
    for i in range(0, len(patches), _BATCH):
        batch = patches[i : i + _BATCH]
        res = await repo.patch_accounts(batch)
        total += res.patched
    logger.info("account: backfilled quota_console for {} accounts", total)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_keys(nested: dict, prefix: str = "") -> int:
    count = 0
    for v in nested.values():
        if isinstance(v, dict):
            count += _count_keys(v)
        else:
            count += 1
    return count
