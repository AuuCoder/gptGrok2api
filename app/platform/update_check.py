"""GitHub release update checks with lightweight in-process caching."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re
import time
from typing import Any

import aiohttp

from app.platform.meta import get_project_version

_GITHUB_RELEASES_API_URL = "https://api.github.com/repos/AuuCoder/gptGrok2api/releases?per_page=20"
_GITHUB_CHANGELOG_URL = "https://raw.githubusercontent.com/AuuCoder/gptGrok2api/main/CHANGELOG.md"
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
    if text.startswith("GitHub update query failed:"):
        status_match = re.search(r"GitHub update query failed:\s*(\d{3})", text)
        if status_match:
            return f"GitHub update query failed ({status_match.group(1)})."
        return "GitHub update query failed."
    if text == "GitHub Releases returned no published version":
        return text
    return text or "Update check failed."


def _success_cache_ttl(payload: dict[str, Any]) -> float:
    current_version = str(payload.get("current_version") or "")
    latest_version = str(payload.get("latest_version") or "")
    changelog = str(payload.get("changelog") or "")
    release_pending = _is_newer(current_version, latest_version)
    has_unreleased_notes = bool(re.search(r"^##\s+Unreleased\s*$", changelog, re.MULTILINE))
    if release_pending or has_unreleased_notes:
        return _ERROR_TTL_SECONDS
    return _CACHE_TTL_SECONDS


def _build_payload(release: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
    current_version = get_project_version()
    release = release or {}
    latest_version = _normalize_version(str(release.get("tag_name") or release.get("name") or ""))
    release_name = str(release.get("name") or "").strip()
    release_url = str(release.get("html_url") or _RELEASE_PAGE_URL).strip()
    published_at = str(release.get("published_at") or "").strip()
    release_notes = str(release.get("body") or "").strip()
    changelog = str(release.get("changelog") or "").strip()
    has_remote = bool(release)
    return {
        "current_version": current_version,
        "latest_version": latest_version,
        "release_name": release_name,
        "release_url": release_url,
        "published_at": published_at,
        "release_notes": release_notes,
        "changelog": changelog,
        "update_available": has_remote and bool(latest_version) and _is_newer(latest_version, current_version),
        "checked_at": _utc_now_iso(),
        "status": "error" if error else "ok",
        "error": _normalize_error_message(error),
    }


async def _fetch_github_text(session: aiohttp.ClientSession, url: str, accept: str) -> str:
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "gptgrok2api-update-check",
    }
    async with session.get(url, headers=headers) as response:
        content = (await response.text()).strip()
        if response.status != 200:
            raise RuntimeError(f"GitHub update query failed: {response.status} {content}".strip())
        return content


async def _fetch_github_releases(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    content = await _fetch_github_text(
        session,
        _GITHUB_RELEASES_API_URL,
        "application/vnd.github+json",
    )
    try:
        payload = json.loads(content)
    except ValueError as exc:
        raise RuntimeError("GitHub Releases returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("GitHub Releases returned an invalid payload")
    return [item for item in payload if isinstance(item, dict)]


async def _fetch_github_changelog(session: aiohttp.ClientSession) -> str:
    return await _fetch_github_text(session, _GITHUB_CHANGELOG_URL, "text/plain")


async def _fetch_latest_release() -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        releases, changelog = await asyncio.gather(
            _fetch_github_releases(session),
            _fetch_github_changelog(session),
        )
    latest = _select_latest_release(releases)
    if latest is None:
        raise RuntimeError("GitHub Releases returned no published version")
    return {**latest, "changelog": changelog}


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
            ttl = _success_cache_ttl(payload)
        except Exception as exc:
            payload = _build_payload(error=str(exc))
            ttl = _ERROR_TTL_SECONDS

        _CACHE["payload"] = payload
        _CACHE["expires_at"] = now + ttl
        return payload


__all__ = ["get_latest_release_info"]
