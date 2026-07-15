from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from services.config import DATA_DIR
from utils.log import logger


_LEVEL_RE = re.compile(r"\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]\s*(.*)", re.IGNORECASE)
_MAX_FILE_BYTES = 2_000_000


def _clean(value: object) -> str:
    return str(value or "").strip()


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    env_path = _clean(os.environ.get("CHATGPT2API_RUNTIME_LOG_FILE"))
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        [
            DATA_DIR / "runtime.log",
            DATA_DIR / "app.log",
            DATA_DIR / "uvicorn.log",
            Path("runtime.log"),
            Path("app.log"),
            Path("chatgpt2api.log"),
            Path("logs") / "runtime.log",
            Path("logs") / "app.log",
        ]
    )

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve() if path.exists() else path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _parse_line(path: Path, line: str, index: int) -> dict[str, Any]:
    level = "info"
    message = line.strip()
    match = _LEVEL_RE.match(message)
    if match:
        level = match.group(1).lower()
        message = match.group(2).strip()
    digest = hashlib.sha1(f"{path}:{index}:{line}".encode("utf-8", errors="ignore")).hexdigest()[:16]
    return {
        "id": f"file-{digest}",
        "time": "",
        "level": level,
        "message": message,
        "source": "file",
        "path": str(path),
    }


def _tail_file(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    size = path.stat().st_size
    with path.open("rb") as file:
        if size > _MAX_FILE_BYTES:
            file.seek(max(size - _MAX_FILE_BYTES, 0))
        raw = file.read()
    text = raw.decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if size > _MAX_FILE_BYTES and lines:
        lines = lines[1:]
    selected = lines[-limit:]
    return [_parse_line(path, line, index) for index, line in enumerate(reversed(selected), start=1)]


def _matches(item: dict[str, Any], *, level: str, search: str, source: str) -> bool:
    if level and _clean(item.get("level")).lower() != level:
        return False
    if source and _clean(item.get("source")).lower() != source:
        return False
    if search:
        haystack = " ".join(
            [
                _clean(item.get("time")),
                _clean(item.get("level")),
                _clean(item.get("message")),
                _clean(item.get("source")),
                _clean(item.get("path")),
            ]
        ).lower()
        if search not in haystack:
            return False
    return True


def list_runtime_logs(
    *,
    limit: int = 300,
    level: str = "",
    search: str = "",
    source: str = "",
) -> dict[str, Any]:
    safe_limit = min(max(int(limit or 300), 1), 2000)
    normalized_level = _clean(level).lower()
    normalized_search = _clean(search).lower()
    normalized_source = _clean(source).lower()

    items: list[dict[str, Any]] = []
    if normalized_source in {"", "memory"}:
        items.extend(logger.get_records(limit=1000))
    if normalized_source in {"", "file"}:
        for path in _candidate_paths():
            items.extend(_tail_file(path, safe_limit))

    filtered = [
        item
        for item in items
        if _matches(item, level=normalized_level, search=normalized_search, source=normalized_source)
    ]
    return {
        "items": filtered[:safe_limit],
        "total": len(filtered),
        "limit": safe_limit,
        "sources": {
            "memory": True,
            "files": [str(path) for path in _candidate_paths()],
        },
    }
