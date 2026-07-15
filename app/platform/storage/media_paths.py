"""Shared local media storage paths."""

from pathlib import Path

from app.platform.paths import data_path


def _files_dir() -> Path:
    return data_path("files")


def _cache_dir() -> Path:
    return data_path("cache")


def image_files_dir() -> Path:
    """Return the local image storage directory."""
    path = _files_dir() / "images"
    path.mkdir(parents=True, exist_ok=True)
    return path


def video_files_dir() -> Path:
    """Return the local video storage directory."""
    path = _files_dir() / "videos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def local_media_cache_db_path() -> Path:
    """Return the SQLite index path for local media cache bookkeeping."""
    path = _cache_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path / "local_media_cache.db"


def local_media_lock_path(media_type: str) -> Path:
    """Return the advisory lock file used by one media-type cache operation."""
    path = _cache_dir() / "locks"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"local_media_{media_type}.lock"


__all__ = [
    "image_files_dir",
    "local_media_cache_db_path",
    "local_media_lock_path",
    "video_files_dir",
]
