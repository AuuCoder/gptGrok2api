"""Local media cache helpers with optional capacity enforcement."""

import asyncio
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Literal

from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger

from .media_paths import (
    image_files_dir,
    local_media_cache_db_path,
    local_media_lock_path,
    video_files_dir,
)

MediaType = Literal["image", "video"]

_LOW_WATERMARK_RATIO = 0.60
_TABLE = "local_media_files"
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"})


class LocalMediaCacheStore:
    """Manage local media files and enforce per-type cache limits."""

    def __init__(self, *, config_provider: Callable[[], Any] = get_config) -> None:
        self._config_provider = config_provider
        self._init_lock = threading.Lock()
        self._thread_locks = {
            "image": threading.Lock(),
            "video": threading.Lock(),
        }
        self._initialized_dbs: set[Path] = set()

    def save_image(self, raw: bytes, mime: str, file_id: str) -> str:
        """Persist an image and return the stable local file ID."""
        ext = ".png" if "png" in mime.lower() else ".jpg"
        self._save("image", file_id=file_id, raw=raw, suffix=ext)
        return file_id

    def save_video(self, raw: bytes, file_id: str) -> Path:
        """Persist a video and return the final file path."""
        return self._save("video", file_id=file_id, raw=raw, suffix=".mp4")

    def reconcile(self, media_type: MediaType) -> None:
        """Rebuild the on-disk index for one media type and enforce limits."""
        max_bytes = self._limit_bytes(media_type)
        if max_bytes <= 0:
            return
        with self._guard(media_type), closing(self._connect()) as conn:
            rows = []
            for path in self._iter_files(media_type):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                rows.append(
                    (
                        media_type,
                        path.name,
                        int(stat.st_size),
                        int(stat.st_mtime_ns),
                        int(stat.st_mtime_ns),
                    )
                )
            conn.execute(f"DELETE FROM {_TABLE} WHERE media_type = ?", (media_type,))
            if rows:
                conn.executemany(
                    f"""
                    INSERT INTO {_TABLE} (
                        media_type,
                        name,
                        size_bytes,
                        created_at_ns,
                        updated_at_ns
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            self._enforce_limit_locked(conn, media_type)
            conn.commit()

    def delete(self, media_type: MediaType, name: str) -> bool:
        """Delete a single local media file and keep the index consistent."""
        safe_name = self._validate_name(media_type, name)
        path = self._path_for_name(media_type, safe_name)
        with self._guard(media_type):
            existed = path.is_file()
            if existed:
                path.unlink(missing_ok=True)
            self._delete_index_row_if_present(media_type, safe_name)
        return existed

    def clear(self, media_type: MediaType) -> int:
        """Delete all tracked local media files for one media type."""
        removed = 0
        with self._guard(media_type):
            for path in self._iter_files(media_type):
                if path.is_file():
                    path.unlink(missing_ok=True)
                    removed += 1
            self._delete_index_rows_if_present(media_type)
        return removed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _save(
        self,
        media_type: MediaType,
        *,
        file_id: str,
        raw: bytes,
        suffix: str,
    ) -> Path:
        path = self._media_dir(media_type) / f"{file_id}{suffix}"
        if self._limit_bytes(media_type) <= 0:
            self._write_if_missing(path, raw)
            return path

        with self._guard(media_type), closing(self._connect()) as conn:
            if path.exists():
                self._upsert_existing_row(conn, media_type, path)
            else:
                self._atomic_write(path, raw)
                self._upsert_new_row(conn, media_type, path)
            self._enforce_limit_locked(conn, media_type, protected_names={path.name})
            conn.commit()
        return path

    def _limit_bytes(self, media_type: MediaType) -> int:
        cfg = self._config_provider()
        limit_mb = max(0, int(cfg.get_int(f"cache.local.{media_type}_max_mb", 0)))
        return limit_mb * 1024 * 1024

    def _target_bytes(self, max_bytes: int) -> int:
        return max(0, int(max_bytes * _LOW_WATERMARK_RATIO))

    def _media_dir(self, media_type: MediaType) -> Path:
        return image_files_dir() if media_type == "image" else video_files_dir()

    def _allowed_exts(self, media_type: MediaType) -> frozenset[str]:
        return _IMAGE_EXTS if media_type == "image" else _VIDEO_EXTS

    def _iter_files(self, media_type: MediaType):
        allowed = self._allowed_exts(media_type)
        for path in self._media_dir(media_type).glob("*"):
            if path.is_file() and path.suffix.lower() in allowed:
                yield path

    def _path_for_name(self, media_type: MediaType, name: str) -> Path:
        return self._media_dir(media_type) / name

    def _validate_name(self, media_type: MediaType, name: str) -> str:
        value = (name or "").strip()
        if not value:
            raise ValueError("missing file name")
        if Path(value).name != value:
            raise ValueError("invalid file name")
        if Path(value).suffix.lower() not in self._allowed_exts(media_type):
            raise ValueError("unsupported file type")
        return value

    def _write_if_missing(self, path: Path, raw: bytes) -> None:
        if path.exists():
            return
        self._atomic_write(path, raw)

    def _atomic_write(self, path: Path, raw: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return
        tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.part"
        try:
            with tmp.open("wb") as handle:
                handle.write(raw)
            if path.exists():
                return
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    @contextmanager
    def _guard(self, media_type: MediaType):
        thread_lock = self._thread_locks[media_type]
        thread_lock.acquire()
        fd: int | None = None
        try:
            lock_path = local_media_lock_path(media_type)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                import fcntl

                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX)
            except (ImportError, OSError, AttributeError):
                fd = None
            yield
        finally:
            if fd is not None:
                try:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                except (ImportError, OSError):
                    pass
            thread_lock.release()

    def _connect(self) -> sqlite3.Connection:
        db_path = local_media_cache_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema(conn, db_path)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection, db_path: Path) -> None:
        if db_path in self._initialized_dbs:
            return
        with self._init_lock:
            if db_path in self._initialized_dbs:
                return
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    media_type    TEXT    NOT NULL,
                    name          TEXT    NOT NULL,
                    size_bytes    INTEGER NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    PRIMARY KEY (media_type, name)
                );
                CREATE INDEX IF NOT EXISTS idx_local_media_order
                    ON {_TABLE} (media_type, created_at_ns, name);
                """
            )
            conn.commit()
            self._initialized_dbs.add(db_path)

    def _upsert_existing_row(
        self,
        conn: sqlite3.Connection,
        media_type: MediaType,
        path: Path,
    ) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        created_at_ns = self._lookup_created_at_ns(
            conn,
            media_type,
            path.name,
            fallback=int(stat.st_mtime_ns),
        )
        conn.execute(
            f"""
            INSERT INTO {_TABLE} (
                media_type,
                name,
                size_bytes,
                created_at_ns,
                updated_at_ns
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(media_type, name) DO UPDATE SET
                size_bytes = excluded.size_bytes,
                updated_at_ns = excluded.updated_at_ns
            """,
            (
                media_type,
                path.name,
                int(stat.st_size),
                created_at_ns,
                int(stat.st_mtime_ns),
            ),
        )

    def _upsert_new_row(
        self,
        conn: sqlite3.Connection,
        media_type: MediaType,
        path: Path,
    ) -> None:
        stat = path.stat()
        now_ns = time.time_ns()
        conn.execute(
            f"""
            INSERT INTO {_TABLE} (
                media_type,
                name,
                size_bytes,
                created_at_ns,
                updated_at_ns
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(media_type, name) DO UPDATE SET
                size_bytes = excluded.size_bytes,
                created_at_ns = excluded.created_at_ns,
                updated_at_ns = excluded.updated_at_ns
            """,
            (
                media_type,
                path.name,
                int(stat.st_size),
                now_ns,
                now_ns,
            ),
        )

    def _lookup_created_at_ns(
        self,
        conn: sqlite3.Connection,
        media_type: MediaType,
        name: str,
        *,
        fallback: int,
    ) -> int:
        row = conn.execute(
            f"""
            SELECT created_at_ns
            FROM {_TABLE}
            WHERE media_type = ? AND name = ?
            """,
            (media_type, name),
        ).fetchone()
        return int(row["created_at_ns"]) if row else int(fallback)

    def _usage_bytes(self, conn: sqlite3.Connection, media_type: MediaType) -> int:
        row = conn.execute(
            f"SELECT COALESCE(SUM(size_bytes), 0) AS total FROM {_TABLE} WHERE media_type = ?",
            (media_type,),
        ).fetchone()
        return int(row["total"]) if row else 0

    def _newest_name(self, conn: sqlite3.Connection, media_type: MediaType) -> str:
        row = conn.execute(
            f"""
            SELECT name
            FROM {_TABLE}
            WHERE media_type = ?
            ORDER BY created_at_ns DESC, name DESC
            LIMIT 1
            """,
            (media_type,),
        ).fetchone()
        return str(row["name"]) if row else ""

    def _enforce_limit_locked(
        self,
        conn: sqlite3.Connection,
        media_type: MediaType,
        *,
        protected_names: set[str] | None = None,
    ) -> None:
        max_bytes = self._limit_bytes(media_type)
        if max_bytes <= 0:
            return

        usage_bytes = self._usage_bytes(conn, media_type)
        if usage_bytes <= max_bytes:
            return

        protected = set(protected_names or ())
        newest_name = self._newest_name(conn, media_type)
        if newest_name:
            protected.add(newest_name)

        target_bytes = self._target_bytes(max_bytes)
        rows = conn.execute(
            f"""
            SELECT name, size_bytes
            FROM {_TABLE}
            WHERE media_type = ?
            ORDER BY created_at_ns ASC, name ASC
            """,
            (media_type,),
        ).fetchall()

        removed = 0
        for row in rows:
            if usage_bytes <= target_bytes:
                break

            name = str(row["name"])
            if name in protected:
                continue

            path = self._path_for_name(media_type, name)
            size_bytes = int(row["size_bytes"])

            if path.exists():
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(
                        "local media cache delete failed: media_type={} name={} error={}",
                        media_type,
                        name,
                        exc,
                    )
                    continue

            conn.execute(
                f"DELETE FROM {_TABLE} WHERE media_type = ? AND name = ?",
                (media_type, name),
            )
            usage_bytes = max(0, usage_bytes - size_bytes)
            removed += 1

        if removed:
            logger.info(
                "local media cache trimmed: media_type={} removed={} usage_bytes={} limit_bytes={} target_bytes={}",
                media_type,
                removed,
                usage_bytes,
                max_bytes,
                target_bytes,
            )
        elif usage_bytes > max_bytes:
            logger.info(
                "local media cache kept newest oversized file: media_type={} usage_bytes={} limit_bytes={}",
                media_type,
                usage_bytes,
                max_bytes,
            )

    def _delete_index_row_if_present(self, media_type: MediaType, name: str) -> None:
        db_path = local_media_cache_db_path()
        if not db_path.exists():
            return
        with closing(self._connect()) as conn:
            conn.execute(
                f"DELETE FROM {_TABLE} WHERE media_type = ? AND name = ?",
                (media_type, name),
            )
            conn.commit()

    def _delete_index_rows_if_present(self, media_type: MediaType) -> None:
        db_path = local_media_cache_db_path()
        if not db_path.exists():
            return
        with closing(self._connect()) as conn:
            conn.execute(f"DELETE FROM {_TABLE} WHERE media_type = ?", (media_type,))
            conn.commit()


local_media_cache = LocalMediaCacheStore()


def save_local_image(raw: bytes, mime: str, file_id: str) -> str:
    """Persist an image to local cache and return the file ID."""
    return local_media_cache.save_image(raw, mime, file_id)


def save_local_video(raw: bytes, file_id: str) -> Path:
    """Persist a video to local cache and return the file path."""
    return local_media_cache.save_video(raw, file_id)


def clear_local_media_files(media_type: MediaType) -> int:
    """Delete all local media files for the requested type."""
    return local_media_cache.clear(media_type)


def delete_local_media_file(media_type: MediaType, name: str) -> bool:
    """Delete one local media file by file name."""
    return local_media_cache.delete(media_type, name)


async def reconcile_local_media_cache_async(
    media_type: MediaType | None = None,
) -> None:
    """Rebuild local media cache indexes and enforce configured limits."""
    if media_type is None:
        for item in ("image", "video"):
            await asyncio.to_thread(local_media_cache.reconcile, item)
        return
    await asyncio.to_thread(local_media_cache.reconcile, media_type)


__all__ = [
    "LocalMediaCacheStore",
    "clear_local_media_files",
    "delete_local_media_file",
    "local_media_cache",
    "reconcile_local_media_cache_async",
    "save_local_image",
    "save_local_video",
]
