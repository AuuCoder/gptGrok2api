"""SQLite account repository (WAL mode, single-process default backend)."""

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from app.platform.runtime.clock import now_ms
from ..commands import AccountPatch, AccountUpsert, BulkReplacePoolCommand, ListAccountsQuery
from ..enums import AccountStatus
from ..models import (
    AccountChangeSet,
    AccountMutationResult,
    AccountPage,
    AccountRecord,
    RuntimeSnapshot,
)
from ..quota_defaults import default_quota_set, BASIC_CONSOLE_LIMIT, BASIC_CONSOLE_WINDOW_SECONDS

_TBL = "accounts"
_META = "account_meta"
_TOKEN_PAYLOAD_QUOTAS = (
    ("auto",      "quota_auto",      True),
    ("fast",      "quota_fast",      True),
    ("expert",    "quota_expert",    True),
    ("heavy",     "quota_heavy",     False),
    ("grok_4_3",  "quota_grok_4_3",  False),  # 补上，避免 super/heavy 账号余额显示为空
    ("console",   "quota_console",   True),
)


class LocalAccountRepository:
    """SQLite-backed account repository."""

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            # WAL 模式在 NFS / 某些 Docker bind mount 文件系统上不支持，
            # 静默 fallback 到默认 DELETE journal mode，功能不受影响。
            pass
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS {_META} (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO {_META} VALUES ('revision', '0');

                CREATE TABLE IF NOT EXISTS {_TBL} (
                    token              TEXT    NOT NULL PRIMARY KEY,
                    pool               TEXT    NOT NULL DEFAULT 'basic',
                    status             TEXT    NOT NULL DEFAULT 'active',
                    created_at         INTEGER NOT NULL,
                    updated_at         INTEGER NOT NULL,
                    tags               TEXT    NOT NULL DEFAULT '[]',
                    quota_auto         TEXT    NOT NULL DEFAULT '{{}}',
                    quota_fast         TEXT    NOT NULL DEFAULT '{{}}',
                    quota_expert       TEXT    NOT NULL DEFAULT '{{}}',
                    quota_heavy        TEXT    NOT NULL DEFAULT '{{}}',
                    quota_grok_4_3     TEXT    NOT NULL DEFAULT '{{}}',
                    quota_console      TEXT    NOT NULL DEFAULT '{{}}',
                    usage_use_count    INTEGER NOT NULL DEFAULT 0,
                    usage_fail_count   INTEGER NOT NULL DEFAULT 0,
                    usage_sync_count   INTEGER NOT NULL DEFAULT 0,
                    last_use_at        INTEGER,
                    last_fail_at       INTEGER,
                    last_fail_reason   TEXT,
                    last_sync_at       INTEGER,
                    last_clear_at      INTEGER,
                    state_reason       TEXT,
                    deleted_at         INTEGER,
                    ext                TEXT    NOT NULL DEFAULT '{{}}',
                    revision           INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_acc_revision
                    ON {_TBL} (revision);
                CREATE INDEX IF NOT EXISTS idx_acc_pool_status
                    ON {_TBL} (pool, status);
                CREATE INDEX IF NOT EXISTS idx_acc_deleted
                    ON {_TBL} (deleted_at) WHERE deleted_at IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_acc_live_updated
                    ON {_TBL} (updated_at DESC) WHERE deleted_at IS NULL;
            """)
            self._ensure_column_sync(conn, "quota_grok_4_3", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column_sync(conn, "quota_console", "TEXT NOT NULL DEFAULT '{}'")
            conn.commit()

    @staticmethod
    def _ensure_column_sync(conn: sqlite3.Connection, name: str, ddl: str) -> None:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({_TBL})").fetchall()}
        if name not in cols:
            conn.execute(f"ALTER TABLE {_TBL} ADD COLUMN {name} {ddl}")

    def _bump_revision(self, conn: sqlite3.Connection) -> int:
        conn.execute(
            f"UPDATE {_META} SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision'"
        )
        row = conn.execute(
            f"SELECT CAST(value AS INTEGER) FROM {_META} WHERE key = 'revision'"
        ).fetchone()
        return int(row[0])

    def _get_revision_sync(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            f"SELECT CAST(value AS INTEGER) FROM {_META} WHERE key = 'revision'"
        ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AccountRecord:
        d = dict(row)
        d["tags"]  = json.loads(d.get("tags")  or "[]")
        heavy_raw     = d.pop("quota_heavy",     "{}") or "{}"
        grok_4_3_raw  = d.pop("quota_grok_4_3",  "{}") or "{}"
        console_raw   = d.pop("quota_console",   "{}") or "{}"
        heavy_dict    = json.loads(heavy_raw)
        grok_4_3_dict = json.loads(grok_4_3_raw)
        console_dict  = json.loads(console_raw)
        d["quota"] = {
            "auto":   json.loads(d.pop("quota_auto",   "{}") or "{}"),
            "fast":   json.loads(d.pop("quota_fast",   "{}") or "{}"),
            "expert": json.loads(d.pop("quota_expert", "{}") or "{}"),
            **({"heavy":    heavy_dict}    if heavy_dict    else {}),
            **({"grok_4_3": grok_4_3_dict} if grok_4_3_dict else {}),
            **({"console":  console_dict}  if console_dict  else {}),
        }
        d["ext"] = json.loads(d.get("ext") or "{}")
        return AccountRecord.model_validate(d)

    @staticmethod
    def _record_to_row(record: AccountRecord, revision: int) -> dict[str, Any]:
        qs = record.quota_set()
        return {
            "token":            record.token,
            "pool":             record.pool,
            "status":           record.status.value,
            "created_at":       record.created_at,
            "updated_at":       record.updated_at,
            "tags":             json.dumps(record.tags),
            "quota_auto":       json.dumps(qs.auto.to_dict()),
            "quota_fast":       json.dumps(qs.fast.to_dict()),
            "quota_expert":     json.dumps(qs.expert.to_dict()),
            "quota_heavy":      json.dumps(qs.heavy.to_dict())    if qs.heavy    else "{}",
            "quota_grok_4_3":   json.dumps(qs.grok_4_3.to_dict()) if qs.grok_4_3 else "{}",
            "quota_console":    json.dumps(qs.console.to_dict())   if qs.console  else "{}",
            "usage_use_count":  record.usage_use_count,
            "usage_fail_count": record.usage_fail_count,
            "usage_sync_count": record.usage_sync_count,
            "last_use_at":      record.last_use_at,
            "last_fail_at":     record.last_fail_at,
            "last_fail_reason": record.last_fail_reason,
            "last_sync_at":     record.last_sync_at,
            "last_clear_at":    record.last_clear_at,
            "state_reason":     record.state_reason,
            "deleted_at":       record.deleted_at,
            "ext":              json.dumps(record.ext),
            "revision":         revision,
        }

    @staticmethod
    def _parse_tags(raw: Any) -> list[str]:
        try:
            tags = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
        return tags if isinstance(tags, list) else []

    @staticmethod
    def _payload_int(value: Any) -> int:
        return int(value or 0)

    @classmethod
    def _row_to_token_payload(cls, row: sqlite3.Row) -> dict[str, Any]:
        quota: dict[str, dict[str, int]] = {}
        for mode, _column, always_include in _TOKEN_PAYLOAD_QUOTAS:
            remaining = row[f"{mode}_remaining"]
            total = row[f"{mode}_total"]
            if always_include or remaining is not None or total is not None:
                quota[mode] = {
                    "remaining": cls._payload_int(remaining),
                    "total": cls._payload_int(total),
                }

        return {
            "token": row["token"],
            "pool": row["pool"] or "basic",
            "status": row["status"],
            "quota": quota,
            "use_count": cls._payload_int(row["usage_use_count"]),
            "fail_count": cls._payload_int(row["usage_fail_count"]),
            "last_used_at": row["last_use_at"],
            "tags": cls._parse_tags(row["tags"]),
        }

    def _upsert_sync(
        self,
        conn: sqlite3.Connection,
        items: list[AccountUpsert],
        revision: int,
    ) -> int:
        ts = now_ms()

        # 按 pool 缓存配额 JSON，避免重复计算（同一 pool 的配额完全相同）
        _quota_json_cache: dict[str, dict[str, str]] = {}

        def _get_quota_json(pool: str) -> dict[str, str]:
            cached = _quota_json_cache.get(pool)
            if cached is not None:
                return cached
            qs = default_quota_set(pool)
            result = {
                "qa": json.dumps(qs.auto.to_dict()),
                "qf": json.dumps(qs.fast.to_dict()),
                "qe": json.dumps(qs.expert.to_dict()),
                "qh": json.dumps(qs.heavy.to_dict())    if qs.heavy    else "{}",
                "qg": json.dumps(qs.grok_4_3.to_dict()) if qs.grok_4_3 else "{}",
                "qc": json.dumps(qs.console.to_dict())  if qs.console  else "{}",
            }
            _quota_json_cache[pool] = result
            return result

        # 批量准备参数
        rows: list[tuple] = []
        for item in items:
            # 轻量 token 清洗（API 层 _sanitize 已做过完整清洗，此处仅做安全兜底）
            token = str(item.token or "").strip()
            if token.startswith("sso="):
                token = token[4:]
            token = token.encode("ascii", errors="ignore").decode("ascii").strip()
            if not token:
                continue
            pool = item.pool if item.pool in ("basic", "super", "heavy") else "basic"
            q = _get_quota_json(pool)
            rows.append((
                token, pool, ts, ts,
                json.dumps(item.tags),
                q["qa"], q["qf"], q["qe"], q["qh"], q["qg"], q["qc"],
                json.dumps(item.ext),
                revision,
            ))

        if not rows:
            return 0

        # 批量插入（1 次 executemany 代替 N 次 execute）
        conn.executemany(
            f"""
            INSERT INTO {_TBL} (
                token, pool, status, created_at, updated_at,
                tags, quota_auto, quota_fast, quota_expert, quota_heavy, quota_grok_4_3, quota_console,
                usage_use_count, usage_fail_count, usage_sync_count,
                ext, revision
            ) VALUES (
                ?, ?, 'active', ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                0, 0, 0, ?, ?
            )
            ON CONFLICT(token) DO UPDATE SET
                pool           = excluded.pool,
                status         = 'active',
                deleted_at     = NULL,
                updated_at     = excluded.updated_at,
                tags           = excluded.tags,
                quota_console  = excluded.quota_console,
                ext            = excluded.ext,
                revision       = excluded.revision
            """,
            rows,
        )
        return len(rows)

    def _patch_sync(
        self,
        conn: sqlite3.Connection,
        patches: list[AccountPatch],
        revision: int,
    ) -> int:
        ts = now_ms()
        count = 0
        for patch in patches:
            # Fetch current record.
            row = conn.execute(
                f"SELECT * FROM {_TBL} WHERE token = ?", (patch.token,)
            ).fetchone()
            if row is None:
                continue
            record = self._row_to_record(row)
            qs = record.quota_set()

            sets: dict[str, Any] = {"updated_at": ts, "revision": revision}

            if patch.pool is not None:
                sets["pool"] = patch.pool
            if patch.status is not None:
                sets["status"] = patch.status.value
            if patch.state_reason is not None:
                sets["state_reason"] = patch.state_reason
            if patch.last_use_at is not None:
                sets["last_use_at"] = patch.last_use_at
            if patch.last_fail_at is not None:
                sets["last_fail_at"] = patch.last_fail_at
            if patch.last_fail_reason is not None:
                sets["last_fail_reason"] = patch.last_fail_reason
            if patch.last_sync_at is not None:
                sets["last_sync_at"] = patch.last_sync_at
            if patch.last_clear_at is not None:
                sets["last_clear_at"] = patch.last_clear_at

            # Usage counters (delta).
            if patch.usage_use_delta is not None:
                sets["usage_use_count"] = max(0, record.usage_use_count + patch.usage_use_delta)
            if patch.usage_fail_delta is not None:
                sets["usage_fail_count"] = max(0, record.usage_fail_count + patch.usage_fail_delta)
            if patch.usage_sync_delta is not None:
                sets["usage_sync_count"] = max(0, record.usage_sync_count + patch.usage_sync_delta)

            # Quota windows.
            if patch.quota_auto is not None:
                sets["quota_auto"] = json.dumps(patch.quota_auto)
            if patch.quota_fast is not None:
                sets["quota_fast"] = json.dumps(patch.quota_fast)
            if patch.quota_expert is not None:
                sets["quota_expert"] = json.dumps(patch.quota_expert)
            if patch.quota_heavy is not None:
                sets["quota_heavy"] = json.dumps(patch.quota_heavy)
            if patch.quota_grok_4_3 is not None:
                sets["quota_grok_4_3"] = json.dumps(patch.quota_grok_4_3)
            if patch.quota_console is not None:
                sets["quota_console"] = json.dumps(patch.quota_console)

            # Tags — use set arithmetic to avoid O(n×m) membership tests.
            tag_set: set[str] = set(record.tags)
            if patch.tags is not None:
                tag_set = set(patch.tags)
            if patch.add_tags:
                tag_set.update(patch.add_tags)
            if patch.remove_tags:
                tag_set.difference_update(patch.remove_tags)
            sets["tags"] = json.dumps(sorted(tag_set))

            # ext merge.
            ext = dict(record.ext)
            if patch.ext_merge:
                ext.update(patch.ext_merge)
            if patch.clear_failures:
                for k in ("cooldown_until", "cooldown_reason", "disabled_at",
                          "disabled_reason", "expired_at", "expired_reason",
                          "forbidden_strikes", "console_429_count", "console_429_last_at"):
                    ext.pop(k, None)
                sets["status"]           = AccountStatus.ACTIVE.value
                sets["usage_fail_count"] = 0
                sets["last_fail_at"]     = None
                sets["last_fail_reason"] = None
                sets["state_reason"]     = None
            sets["ext"] = json.dumps(ext)

            col_sql = ", ".join(f"{k} = :{k}" for k in sets)
            conn.execute(
                f"UPDATE {_TBL} SET {col_sql} WHERE token = :_token",
                {**sets, "_token": patch.token},
            )
            count += conn.execute("SELECT changes()").fetchone()[0]
        return count

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._init_sync)

    async def get_revision(self) -> int:
        def _sync() -> int:
            with closing(self._connect()) as conn:
                return self._get_revision_sync(conn)
        return await asyncio.to_thread(_sync)

    async def runtime_snapshot(self) -> RuntimeSnapshot:
        def _sync() -> RuntimeSnapshot:
            with closing(self._connect()) as conn:
                rev = self._get_revision_sync(conn)
                rows = conn.execute(
                    f"SELECT * FROM {_TBL} WHERE deleted_at IS NULL"
                ).fetchall()
                return RuntimeSnapshot(
                    revision=rev,
                    items=[self._row_to_record(r) for r in rows],
                )
        return await asyncio.to_thread(_sync)

    async def scan_changes(
        self,
        since_revision: int,
        *,
        limit: int = 5000,
    ) -> AccountChangeSet:
        def _sync() -> AccountChangeSet:
            with closing(self._connect()) as conn:
                rev = self._get_revision_sync(conn)
                rows = conn.execute(
                    f"SELECT * FROM {_TBL} WHERE revision > ? ORDER BY revision LIMIT ?",
                    (since_revision, limit),
                ).fetchall()
                items: list[AccountRecord] = []
                deleted: list[str] = []
                batch_max_rev = 0
                for row in rows:
                    r = self._row_to_record(row)
                    if r.revision > batch_max_rev:
                        batch_max_rev = r.revision
                    if r.is_deleted():
                        deleted.append(r.token)
                    else:
                        items.append(r)
                has_more = len(rows) == limit
                return AccountChangeSet(
                    revision=rev,
                    batch_max_revision=batch_max_rev,
                    items=items,
                    deleted_tokens=deleted,
                    has_more=has_more,
                )
        return await asyncio.to_thread(_sync)

    async def upsert_accounts(
        self,
        items: list[AccountUpsert],
    ) -> AccountMutationResult:
        if not items:
            return AccountMutationResult()

        def _sync() -> AccountMutationResult:
            with closing(self._connect()) as conn:
                rev   = self._bump_revision(conn)
                count = self._upsert_sync(conn, items, rev)
                conn.commit()
                return AccountMutationResult(upserted=count, revision=rev)

        async with self._lock:
            return await asyncio.to_thread(_sync)

    async def patch_accounts(
        self,
        patches: list[AccountPatch],
    ) -> AccountMutationResult:
        if not patches:
            return AccountMutationResult()

        def _sync() -> AccountMutationResult:
            with closing(self._connect()) as conn:
                rev   = self._bump_revision(conn)
                count = self._patch_sync(conn, patches, rev)
                conn.commit()
                return AccountMutationResult(patched=count, revision=rev)

        async with self._lock:
            return await asyncio.to_thread(_sync)

    async def delete_accounts(
        self,
        tokens: list[str],
    ) -> AccountMutationResult:
        if not tokens:
            return AccountMutationResult()

        def _sync() -> AccountMutationResult:
            ts = now_ms()
            with closing(self._connect()) as conn:
                rev = self._bump_revision(conn)
                conn.executemany(
                    f"UPDATE {_TBL} SET deleted_at = ?, updated_at = ?, revision = ? "
                    f"WHERE token = ? AND deleted_at IS NULL",
                    [(ts, ts, rev, t) for t in tokens],
                )
                count = conn.execute("SELECT changes()").fetchone()[0]
                conn.commit()
                return AccountMutationResult(deleted=count, revision=rev)

        async with self._lock:
            return await asyncio.to_thread(_sync)

    async def get_accounts(
        self,
        tokens: list[str],
    ) -> list[AccountRecord]:
        if not tokens:
            return []

        def _sync() -> list[AccountRecord]:
            with closing(self._connect()) as conn:
                placeholders = ",".join("?" * len(tokens))
                rows = conn.execute(
                    f"SELECT * FROM {_TBL} WHERE token IN ({placeholders})",
                    tokens,
                ).fetchall()
                return [self._row_to_record(r) for r in rows]

        return await asyncio.to_thread(_sync)

    async def list_accounts(
        self,
        query: ListAccountsQuery,
    ) -> AccountPage:
        def _sync() -> AccountPage:
            with closing(self._connect()) as conn:
                where_parts: list[str] = []
                params: list[Any] = []

                if not query.include_deleted:
                    where_parts.append("deleted_at IS NULL")
                if query.pool:
                    where_parts.append("pool = ?")
                    params.append(query.pool)
                if query.status:
                    where_parts.append("status = ?")
                    params.append(query.status.value)

                where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
                order_dir = "DESC" if query.sort_desc else "ASC"
                # Allow only known column names to prevent injection.
                safe_sort = query.sort_by if query.sort_by in {
                    "updated_at", "created_at", "last_use_at", "token",
                    "usage_use_count", "usage_fail_count",
                } else "updated_at"
                order_sql = f"ORDER BY {safe_sort} {order_dir}"

                total = conn.execute(
                    f"SELECT COUNT(*) FROM {_TBL} {where_sql}", params
                ).fetchone()[0]

                offset = (query.page - 1) * query.page_size
                rows = conn.execute(
                    f"SELECT * FROM {_TBL} {where_sql} {order_sql} "
                    f"LIMIT ? OFFSET ?",
                    params + [query.page_size, offset],
                ).fetchall()
                items = [self._row_to_record(r) for r in rows]
                total_pages = max(1, (total + query.page_size - 1) // query.page_size)
                rev = self._get_revision_sync(conn)
                return AccountPage(
                    items=items,
                    total=total,
                    page=query.page,
                    page_size=query.page_size,
                    total_pages=total_pages,
                    revision=rev,
                )

        return await asyncio.to_thread(_sync)

    @classmethod
    def _token_payload_select_sql(cls) -> str:
        quota_select = ", ".join(
            f"CASE WHEN json_valid({column}) "
            f"THEN json_extract({column}, '$.remaining') END AS {mode}_remaining, "
            f"CASE WHEN json_valid({column}) "
            f"THEN json_extract({column}, '$.total') END AS {mode}_total"
            for mode, column, _always_include in _TOKEN_PAYLOAD_QUOTAS
        )
        return f"""
            SELECT
                token,
                pool,
                status,
                tags,
                usage_use_count,
                usage_fail_count,
                last_use_at,
                {quota_select}
            FROM {_TBL}
            WHERE deleted_at IS NULL
            ORDER BY updated_at DESC
            """

    async def list_token_payloads(self) -> list[dict[str, Any]]:
        """Return the compact payload needed by the admin account list."""

        def _sync() -> list[dict[str, Any]]:
            with closing(self._connect()) as conn:
                cursor = conn.execute(self._token_payload_select_sql())
                return [self._row_to_token_payload(r) for r in cursor]

        return await asyncio.to_thread(_sync)

    async def list_invalid_tokens(self) -> list[str]:
        def _sync() -> list[str]:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    f"""
                    SELECT token
                    FROM {_TBL}
                    WHERE deleted_at IS NULL
                      AND status NOT IN (?, ?, ?, ?)
                    ORDER BY updated_at DESC
                    """,
                    (
                        AccountStatus.ACTIVE.value,
                        AccountStatus.COOLING.value,
                        AccountStatus.DISABLED.value,
                        AccountStatus.EXPIRED.value,  # EXPIRED 是配额误判，不当作垃圾删除
                    ),
                )
                return [row["token"] for row in rows]

        return await asyncio.to_thread(_sync)

    async def purge_deleted_accounts(
        self,
        *,
        deleted_before_ms: int,
        batch_size: int = 5000,
        vacuum: bool = True,
    ) -> int:
        def _sync() -> int:
            total = 0
            limit = max(1, int(batch_size))
            with closing(self._connect()) as conn:
                while True:
                    rows = conn.execute(
                        f"""
                        SELECT token
                        FROM {_TBL}
                        WHERE deleted_at IS NOT NULL
                          AND deleted_at < ?
                        ORDER BY deleted_at
                        LIMIT ?
                        """,
                        (deleted_before_ms, limit),
                    ).fetchall()
                    if not rows:
                        break
                    cursor = conn.executemany(
                        f"DELETE FROM {_TBL} WHERE token = ?",
                        [(row["token"],) for row in rows],
                    )
                    affected = cursor.rowcount if cursor.rowcount >= 0 else len(rows)
                    total += int(affected)
                    conn.commit()
                    if affected <= 0:
                        break
                if total > 0 and vacuum:
                    conn.execute("VACUUM")
            return total

        async with self._lock:
            return await asyncio.to_thread(_sync)

    async def replace_pool(
        self,
        command: BulkReplacePoolCommand,
    ) -> AccountMutationResult:
        def _sync() -> AccountMutationResult:
            ts = now_ms()
            with closing(self._connect()) as conn:
                rev = self._bump_revision(conn)
                # Soft-delete all existing accounts in the pool.
                conn.execute(
                    f"UPDATE {_TBL} SET deleted_at = ?, updated_at = ?, revision = ? "
                    f"WHERE pool = ? AND deleted_at IS NULL",
                    (ts, ts, rev, command.pool),
                )
                deleted = conn.execute("SELECT changes()").fetchone()[0]
                # Bump revision again for upserts.
                rev = self._bump_revision(conn)
                upserted = self._upsert_sync(conn, command.upserts, rev)
                conn.commit()
                return AccountMutationResult(
                    upserted=upserted, deleted=deleted, revision=rev
                )

        async with self._lock:
            return await asyncio.to_thread(_sync)

    async def reset_expired_console_windows(self) -> int:
        """Batch-reset exhausted + expired console quotas via direct SQL.

        处理两种异常情况：
        1. 老条件：remaining<=0 且 (reset_at IS NULL 或已过期) → 正常配额耗尽恢复
        2. 新条件 (M6)：reset_at 已过期（即使 remaining>0）→ 异常数据归位
           （来源：人工 patch、迁移数据、M1 历史副作用等）
        """
        def _sync() -> int:
            with closing(self._connect()) as conn:
                now = now_ms()
                # 先查有多少需要重置的
                count = conn.execute(
                    f"""
                    SELECT COUNT(*) FROM {_TBL}
                    WHERE status = 'active'
                      AND deleted_at IS NULL
                      AND (
                        (
                          CAST(json_extract(quota_console, '$.remaining') AS INTEGER) <= 0
                          AND (
                            json_extract(quota_console, '$.reset_at') IS NULL
                            OR CAST(json_extract(quota_console, '$.reset_at') AS INTEGER) < ?
                          )
                        )
                        OR
                        (
                          json_extract(quota_console, '$.reset_at') IS NOT NULL
                          AND CAST(json_extract(quota_console, '$.reset_at') AS INTEGER) < ?
                        )
                      )
                    """,
                    (now, now),
                ).fetchone()[0]
                if count == 0:
                    return 0

                # 从 quota_defaults.py 动态读取，不硬编码
                reset_json = json.dumps({
                    "remaining": BASIC_CONSOLE_LIMIT,
                    "total": BASIC_CONSOLE_LIMIT,
                    "window_seconds": BASIC_CONSOLE_WINDOW_SECONDS,
                    "reset_at": None,
                    "synced_at": now,
                    "source": 0,
                })
                rev = self._bump_revision(conn)
                conn.execute(
                    f"""
                    UPDATE {_TBL}
                    SET quota_console = ?, revision = ?, updated_at = ?
                    WHERE status = 'active'
                      AND deleted_at IS NULL
                      AND (
                        (
                          CAST(json_extract(quota_console, '$.remaining') AS INTEGER) <= 0
                          AND (
                            json_extract(quota_console, '$.reset_at') IS NULL
                            OR CAST(json_extract(quota_console, '$.reset_at') AS INTEGER) < ?
                          )
                        )
                        OR
                        (
                          json_extract(quota_console, '$.reset_at') IS NOT NULL
                          AND CAST(json_extract(quota_console, '$.reset_at') AS INTEGER) < ?
                        )
                      )
                    """,
                    (reset_json, rev, now, now, now),
                )
                affected = conn.execute("SELECT changes()").fetchone()[0]
                conn.commit()
                return affected

        async with self._lock:
            return await asyncio.to_thread(_sync)

    async def recover_console_expired_accounts(self) -> int:
        """Auto-recover console 429 EXPIRED accounts with successful history.

        Conditions:
        - status = 'expired'
        - state_reason = 'console_429_threshold_exceeded'
        - usage_use_count > 5
        - ext.expired_at <= now - 1 hour
        """
        def _sync() -> int:
            with closing(self._connect()) as conn:
                now = now_ms()
                recovery_threshold = now - 3600 * 1000

                # 查询符合条件的账号 token 和 ext
                rows = conn.execute(
                    f"""
                    SELECT token, ext FROM {_TBL}
                    WHERE status = 'expired'
                      AND deleted_at IS NULL
                      AND state_reason = 'console_429_threshold_exceeded'
                      AND usage_use_count > 5
                      AND CAST(json_extract(ext, '$.expired_at') AS INTEGER) <= ?
                    """,
                    (recovery_threshold,),
                ).fetchall()

                if not rows:
                    return 0

                rev = self._bump_revision(conn)
                for row in rows:
                    token, ext_raw = row
                    try:
                        ext = json.loads(ext_raw) if ext_raw else {}
                    except (ValueError, TypeError):
                        ext = {}
                    # 清理 EXPIRED 相关字段
                    for k in ("expired_at", "expired_reason",
                              "console_429_count", "console_429_last_at"):
                        ext.pop(k, None)
                    conn.execute(
                        f"""
                        UPDATE {_TBL}
                        SET status = 'active',
                            state_reason = NULL,
                            ext = ?,
                            revision = ?,
                            updated_at = ?
                        WHERE token = ?
                        """,
                        (json.dumps(ext), rev, now, token),
                    )
                conn.commit()
                return len(rows)

        async with self._lock:
            return await asyncio.to_thread(_sync)

    async def close(self) -> None:
        """No-op for SQLite — connections are opened and closed per operation."""


__all__ = ["LocalAccountRepository"]
