"""Application logger — loguru with structured-field support."""

import os
import sys
from pathlib import Path

from loguru import logger as _loguru_logger

from app.platform.paths import log_dir as get_log_dir

# Re-export as the canonical logger so imports stay uniform.
logger = _loguru_logger

_configured = False
_console_sink_id: int | None = None
_file_sink_id: int | None = None
_console_level = "INFO"
_json_console = False
_file_logging = True
_log_dir_override: Path | None = None

_FMT_TEXT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def _get_env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def setup_logging(
    *,
    level: str = "INFO",
    file_level: str | None = None,
    json_console: bool = False,
    file_logging: bool = True,
    log_dir: Path | None = None,
    max_files: int = 7,
) -> None:
    """Configure loguru sinks.  Safe to call multiple times (idempotent)."""
    global _configured, _console_sink_id, _file_sink_id
    global _console_level, _json_console, _file_logging, _log_dir_override

    logger.remove()
    _console_sink_id = None
    _file_sink_id = None

    fmt_json = "{time} | {level} | {name}:{function}:{line} | {message}"

    resolved_level = level.upper()
    _console_sink_id = logger.add(
        sys.stdout,
        level=resolved_level,
        format=fmt_json if json_console else _FMT_TEXT,
        colorize=not json_console,
        enqueue=False,
        backtrace=False,
        diagnose=False,
    )

    if file_logging:
        _add_file_sink(
            file_level=(file_level or resolved_level).upper(),
            max_files=max_files,
            log_dir=log_dir,
        )

    _configured = True
    _console_level = resolved_level
    _json_console = json_console
    _file_logging = file_logging
    _log_dir_override = log_dir


def reload_logging(
    *,
    level: str | None = None,
    default_level: str = "INFO",
    json_console: bool = False,
    max_files: int = 7,
    file_level: str | None = None,
) -> None:
    """Re-configure logging from runtime values (called after config loads)."""
    resolved_level = (level or "").strip() or os.getenv("LOG_LEVEL", default_level)
    file_logging = _get_env_bool("LOG_FILE_ENABLED", True)
    setup_logging(
        level=resolved_level,
        file_level=file_level or resolved_level,
        json_console=json_console,
        file_logging=file_logging,
        max_files=max_files,
    )


def reload_file_logging(
    *,
    file_level: str | None = None,
    max_files: int = 7,
) -> None:
    """Re-configure only the file sink, preserving the current console output."""
    global _file_sink_id, _file_logging

    if not _configured:
        reload_logging(file_level=file_level, max_files=max_files)
        return

    if _file_sink_id is not None:
        logger.remove(_file_sink_id)
        _file_sink_id = None

    _file_logging = _get_env_bool("LOG_FILE_ENABLED", True)
    if not _file_logging:
        return

    _add_file_sink(
        file_level=(file_level or _console_level).upper(),
        max_files=max_files,
        log_dir=_log_dir_override,
    )


def _add_file_sink(
    *,
    file_level: str,
    max_files: int,
    log_dir: Path | None,
) -> None:
    global _file_sink_id

    _dir = log_dir or get_log_dir()
    _dir.mkdir(parents=True, exist_ok=True)
    _file_sink_id = logger.add(
        str(_dir / "app_{time:YYYY-MM-DD}.log"),
        level=file_level,
        format=_FMT_TEXT,
        rotation="00:00",  # new file every day at midnight
        retention=max_files,  # keep the last N daily files
        enqueue=True,
        encoding="utf-8",
        backtrace=False,
        diagnose=False,
    )


__all__ = ["logger", "setup_logging", "reload_logging", "reload_file_logging"]
