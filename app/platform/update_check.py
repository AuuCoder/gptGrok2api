"""GitHub release update checks with lightweight in-process caching."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
import time
from typing import Any

import aiohttp

from app.platform.meta import get_project_version

_UPDATE_BASE_URL = "https://pro.muyuai.top/updates"
_RELEASE_PAGE_URL = "https://github.com/AuuCoder/gptGrok2api/releases"
_CACHE_TTL_SECONDS = 86400.0
_ERROR_TTL_SECONDS = 300.0
_LOCK = asyncio.Lock()
_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_version(value: str) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("v"):
        text = text[1:]
    return text


def _parse_version(value: str) -> tuple[int, int, int, int, int] | None:
    normalized = _normalize_version(value)
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:(?:\.|-)?rc(\d+))?$", normalized, re.IGNORECASE)
    if not match:
        return None
    major, minor, patch, rc = match.groups()
    is_final = 1 if rc is None else 0
    rc_number = int(rc or 0)
    return int(major or 0), int(minor or 0), int(patch or 0), is_final, rc_number


def _is_newer(latest: str, current: str) -> bool:
    latest_parsed = _parse_version(latest)
    current_parsed = _parse_version(current)
    if latest_parsed and current_parsed:
        return latest_parsed > current_parsed
    return _normalize_version(latest) > _normalize_version(current)


def _release_version_key(release: dict[str, Any]) -> tuple[int, int, int, int, int] | None:
    version = str(release.get("tag_name") or release.get("name") or "").strip()
    return _parse_version(version)


def _select_latest_release(releases: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates: list[tuple[tuple[int, int, int, int, int], dict[str, Any]]] = []
    for release in releases:
        if not isinstance(release, dict) or bool(release.get("draft")):
            continue
        version_key = _release_version_key(release)
        if version_key is None:
            continue
        candidates.append((version_key, release))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _normalize_error_message(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("Update source query failed:"):
        status_match = re.search(r"Update source query failed:\s*(\d{3})", text)
        if status_match:
            return f"Update source query failed ({status_match.group(1)})."
        return "Update source query failed."
    if text == "Update source returned an empty version":
        return text
    return text or "Update check failed."


def _build_payload(release: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
    current_version = get_project_version()
    release = release or {}
    latest_version = _normalize_version(str(release.get("tag_name") or release.get("name") or ""))
    release_name = str(release.get("name") or "").strip()
    release_url = str(release.get("html_url") or "").strip()
    published_at = str(release.get("published_at") or "").strip()
    release_notes = str(release.get("body") or "").strip()
    has_remote = bool(release)
    return {
        "current_version": current_version,
        "latest_version": latest_version,
        "release_name": release_name,
        "release_url": release_url,
        "published_at": published_at,
        "release_notes": release_notes,
        "update_available": has_remote and bool(latest_version) and _is_newer(latest_version, current_version),
        "checked_at": _utc_now_iso(),
        "status": "error" if error else "ok",
        "error": _normalize_error_message(error),
    }


async def _fetch_latest_release() -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=10)
    headers = {
        "Accept": "text/plain",
        "User-Agent": "gptgrok2api-update-check",
    }
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def fetch_text(filename: str) -> str:
            url = f"{_UPDATE_BASE_URL}/{filename}"
            async with session.get(url, headers=headers) as response:
                content = (await response.text()).strip()
                if response.status != 200:
                    raise RuntimeError(f"Update source query failed: {response.status} {content}".strip())
                return content

        version, changelog = await asyncio.gather(
            fetch_text("VERSION"),
            fetch_text("CHANGELOG.md"),
        )
        if not version:
            raise RuntimeError("Update source returned an empty version")
        return {
            "tag_name": version,
            "name": f"v{_normalize_version(version)}",
            "html_url": _RELEASE_PAGE_URL,
            "published_at": "",
            "body": changelog,
        }


async def get_latest_release_info(force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached = _CACHE.get("payload")
    expires_at = float(_CACHE.get("expires_at") or 0.0)
    if not force and cached and expires_at > now:
        return cached

    async with _LOCK:
        cached = _CACHE.get("payload")
        expires_at = float(_CACHE.get("expires_at") or 0.0)
        now = time.monotonic()
        if not force and cached and expires_at > now:
            return cached

        try:
            release = await _fetch_latest_release()
            payload = _build_payload(release=release)
            ttl = _CACHE_TTL_SECONDS
        except Exception as exc:
            payload = _build_payload(error=str(exc))
            ttl = _ERROR_TTL_SECONDS

        _CACHE["payload"] = payload
        _CACHE["expires_at"] = now + ttl
        return payload


__all__ = ["get_latest_release_info"]
