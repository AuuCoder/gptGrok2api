from __future__ import annotations

import hashlib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as curl_requests

from services.config import BASE_DIR, DATA_DIR
from services.json_file import read_json_file, write_json_file
from services.prompt_source_adapters import (
    adapter_label,
    normalize_adapter_name,
    parse_prompt_source_payload,
)


PROMPT_SOURCE_PATH = DATA_DIR / "prompt_sources.json"
PROMPT_LIBRARY_CACHE_PATH = DATA_DIR / "prompt_library_cache.json"
DEFAULT_PROMPT_LIBRARY_PATH = BASE_DIR / "services" / "default_prompt_library.json"
DEFAULT_PROMPT_SOURCE_ID = "banana-prompt-quicker"
DEFAULT_PROMPT_SOURCE_URL = (
    os.getenv("PROMPT_LIBRARY_DEFAULT_URL")
    or os.getenv("PROMPT_LIBRARY_REMOTE_URL")
    or "local://default_prompt_library.json"
).strip()
PROMPT_SOURCE_TIMEOUT = 8
PROMPT_SOURCE_MAX_BYTES = 4 * 1024 * 1024
PROMPT_SOURCE_FETCH_WORKERS = 4
PROMPT_SOURCE_FETCH_ATTEMPTS = 2
PROMPT_COMPOSE_MODES = {"image", "chat", "search"}
PROMPT_SOURCE_FALLBACK_URLS: dict[str, list[str]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object) -> str:
    return str(value or "").strip()


def _clean_compact(value: object) -> str:
    return re.sub(r"\s+", " ", _clean(value)).strip()


def _clean_prompt_display_text(value: object) -> str:
    text = _clean(value)
    if not text:
        return ""
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
    )
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"</(?:p|div|li|h[1-6])>", " ", text, flags=re.I)
    text = re.sub(r"<img\b[^>\n]*(?:>|$)", " ", text, flags=re.I)
    text = re.sub(r"!\[[^\]]*]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = _clean(value).lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "none", "null", ""}:
        return False
    return default


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _float_or_zero(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _string_list(value: object, *, max_items: int = 24) -> list[str]:
    if isinstance(value, list):
        candidates = value
    elif isinstance(value, str):
        candidates = value.replace(",", "\n").splitlines()
    else:
        candidates = []
    seen: set[str] = set()
    items: list[str] = []
    for candidate in candidates:
        item = _clean(candidate)
        if not item or item in seen:
            continue
        seen.add(item)
        items.append(item)
        if len(items) >= max_items:
            break
    return items


def _first_text(*values: object) -> str:
    for value in values:
        if isinstance(value, list):
            nested = _first_text(*value)
            if nested:
                return nested
            continue
        text = _clean(value)
        if text:
            return text
    return ""


def _stable_id(*parts: object, length: int = 16) -> str:
    raw = "\n".join(_clean(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:length]


def _canonical_source_url(url: str) -> str:
    normalized = _clean(url)
    parsed = urlparse(normalized)
    if parsed.netloc.lower() != "github.com":
        return normalized
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 5 and parts[2] == "blob":
        owner, repo, _, branch = parts[:4]
        file_path = "/".join(parts[4:])
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
    return normalized


def _validate_source_url(url: str) -> str:
    normalized = _canonical_source_url(url)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid prompt source url")
    return normalized


def _validate_homepage(value: object) -> str:
    homepage = _clean(value)
    if not homepage:
        return ""
    parsed = urlparse(homepage)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return homepage[:700]
    return ""


def _resolve_source_url(source_url: str, value: object) -> str:
    raw = _first_text(value)
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https", "data", "blob"}:
        return raw
    return urljoin(source_url, raw)


def _normalize_mode(value: object) -> str:
    raw = _clean(value).lower()
    if raw in PROMPT_COMPOSE_MODES:
        return raw
    return "image"


def _normalize_image_mode(value: object) -> str:
    raw = _clean(value).lower()
    if raw in {"edit", "image-to-image", "i2i"}:
        return "edit"
    if raw in {"generate", "image", "text-to-image", "t2i"}:
        return "generate"
    return ""


def _source_sort_key(source: dict[str, Any]) -> tuple[int, str, str]:
    order = source.get("sort_order")
    return (
        order if isinstance(order, int) else 9999,
        _clean(source.get("name")).lower(),
        _clean(source.get("id")),
    )


def _sort_key(item: dict[str, Any]) -> tuple[int, int, str, str, str, str]:
    order = item.get("sort_order")
    has_order = 0 if isinstance(order, int) else 1
    order_value = order if isinstance(order, int) else 9999
    return (
        0 if bool(item.get("enabled", True)) else 1,
        has_order,
        f"{order_value:08d}",
        _clean(item.get("source_name")).lower(),
        _clean(item.get("category")),
        _clean(item.get("title")),
    )


def _prompt_fingerprint(item: dict[str, Any]) -> str:
    prompt = _clean_compact(item.get("prompt")).lower()
    title = _clean_compact(item.get("title")).lower()
    if len(prompt) > 32:
        return hashlib.sha1(prompt.encode("utf-8")).hexdigest()
    return hashlib.sha1(f"{title}\n{prompt}".encode("utf-8")).hexdigest()


def _source_fetch_urls(source: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for value in [_clean(source.get("url")), *PROMPT_SOURCE_FALLBACK_URLS.get(_clean(source.get("id")), [])]:
        try:
            url = _validate_source_url(value)
        except ValueError:
            continue
        candidates = [url]
        jsdelivr_main = re.match(r"^(https://cdn\.jsdelivr\.net/gh/[^@]+)@main/(.+)$", url)
        if jsdelivr_main:
            candidates.append(f"{jsdelivr_main.group(1)}/{jsdelivr_main.group(2)}")
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            urls.append(candidate)
    return urls


def _default_sources() -> list[dict[str, Any]]:
    now = _now_iso()
    return [
        {
            "id": DEFAULT_PROMPT_SOURCE_ID,
            "name": "Banana Prompt Quicker",
            "url": DEFAULT_PROMPT_SOURCE_URL,
            "adapter": "json",
            "homepage": "",
            "enabled": True,
            "built_in": True,
            "sort_order": 0,
            "created_at": now,
            "updated_at": now,
        },
    ]


def _normalize_source(raw: object, *, built_in: bool = False) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        raw_url = _clean(raw.get("url") or "")
        if raw_url.startswith("local://") and (built_in or bool(raw.get("built_in"))):
            url = raw_url
        else:
            url = _validate_source_url(raw_url)
    except ValueError:
        return None
    now = _now_iso()
    source_id = _clean(raw.get("id")) or _stable_id(url)
    is_built_in = bool(raw.get("built_in")) or built_in
    adapter = normalize_adapter_name(raw.get("adapter"), url) if is_built_in else "json"
    return {
        "id": source_id[:80],
        "name": (_clean(raw.get("name")) or urlparse(url).netloc or "Prompt Source")[:120],
        "url": url,
        "adapter": adapter,
        "adapter_label": adapter_label(adapter),
        "homepage": _validate_homepage(raw.get("homepage") or ""),
        "enabled": _bool(raw.get("enabled"), True),
        "built_in": is_built_in,
        "sort_order": _int_or_none(raw.get("sort_order")),
        "created_at": _clean(raw.get("created_at")) or now,
        "updated_at": _clean(raw.get("updated_at")) or now,
    }


def _merge_builtin_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Keep sources already stored by an operator; only the built-in defaults are local.
    merged = list(sources)
    for builtin in _default_sources():
        normalized_builtin = _normalize_source(builtin, built_in=True)
        if normalized_builtin is None:
            continue
        match_index = next(
            (
                index
                for index, source in enumerate(merged)
                if source.get("id") == normalized_builtin["id"] or source.get("url") == normalized_builtin["url"]
            ),
            None,
        )
        if match_index is None:
            merged.append(normalized_builtin)
            continue
        existing = merged[match_index]
        merged[match_index] = {
            **normalized_builtin,
            "name": _clean(existing.get("name")) or normalized_builtin["name"],
            "enabled": bool(existing.get("enabled", True)),
            "sort_order": existing.get("sort_order") if existing.get("sort_order") is not None else normalized_builtin["sort_order"],
            "created_at": _clean(existing.get("created_at")) or normalized_builtin["created_at"],
            "updated_at": _clean(existing.get("updated_at")) or normalized_builtin["updated_at"],
        }
    return merged


def _normalize_prompt(raw: object, source: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = _clean_compact(_first_text(raw.get("title"), raw.get("title_cn"), raw.get("title_zh"), raw.get("title_en"), raw.get("name")))
    prompt = _clean(raw.get("prompt") or raw.get("content") or raw.get("text") or raw.get("positive_prompt"))
    if not title or not prompt:
        return None

    source_id = _clean(source.get("id"))
    source_name = _clean(source.get("name"))
    source_url = _clean(source.get("url"))
    created_at = _clean(raw.get("created_at") or raw.get("created")) or _now_iso()
    preview = _resolve_source_url(
        source_url,
        _first_text(raw.get("preview"), raw.get("preview_url"), raw.get("image"), raw.get("image_url")),
    )
    reference_candidates = _string_list(raw.get("reference_image_urls") or raw.get("reference_images") or raw.get("images"), max_items=12)
    if preview:
        reference_candidates.insert(0, preview)
    reference_image_urls = [_resolve_source_url(source_url, value) for value in reference_candidates]
    tags = _string_list(raw.get("tags"), max_items=20)
    category = _clean_compact(_first_text(raw.get("category"), raw.get("category_cn"), raw.get("category_zh")))[:80]
    if category and category not in tags:
        tags.append(category)
    item_id = _clean(raw.get("id")) or _stable_id(source_id, title, prompt)
    description = _clean_prompt_display_text(
        _first_text(raw.get("description"), raw.get("description_cn"), raw.get("note"), raw.get("summary"))
    )[:700]
    return {
        "id": f"{source_id}:{item_id}"[:140],
        "source_id": source_id,
        "source_name": source_name,
        "source_url": source_url,
        "title": title[:140],
        "description": description,
        "preview": preview[:700],
        "link": _resolve_source_url(source_url, raw.get("link") or raw.get("source_url"))[:700],
        "prompt": prompt,
        "mode": _normalize_mode(raw.get("compose_mode") or raw.get("mode")),
        "image_mode": _normalize_image_mode(raw.get("image_mode") or raw.get("mode")),
        "category": category,
        "sub_category": _clean_compact(_first_text(raw.get("sub_category"), raw.get("subcategory")))[:80],
        "tags": tags,
        "reference_image_urls": list(dict.fromkeys([url for url in reference_image_urls if url]))[:12],
        "image_model": _clean_compact(raw.get("image_model") or raw.get("model"))[:80],
        "image_size": _clean_compact(raw.get("image_size") or raw.get("size"))[:40],
        "image_count": _int_or_none(raw.get("image_count") or raw.get("n")),
        "enabled": _bool(raw.get("enabled"), True),
        "sort_order": _int_or_none(raw.get("sort_order")),
        "created_at": created_at,
        "updated_at": _clean(raw.get("updated_at")) or created_at,
    }


class PromptLibraryService:
    def __init__(self, path: Path = PROMPT_SOURCE_PATH):
        self.path = path
        self._lock = RLock()
        self._cache_lock = RLock()
        self._source_cache: dict[str, dict[str, Any]] = {}
        self._cache_loaded = False

    def _ensure_cache_loaded_locked(self) -> None:
        with self._cache_lock:
            if self._cache_loaded:
                return
            data = read_json_file(
                PROMPT_LIBRARY_CACHE_PATH,
                name="prompt_library_cache.json",
                default_factory=dict,
                expected_types=(dict,),
            )
            raw_sources = data.get("sources", {}) if isinstance(data, dict) else {}
            if isinstance(raw_sources, dict):
                for source_id, cache in raw_sources.items():
                    if not isinstance(cache, dict):
                        continue
                    items = cache.get("items")
                    self._source_cache[_clean(source_id)] = {
                        "items": items if isinstance(items, list) else [],
                        "adapter": _clean(cache.get("adapter")),
                        "fetched_at": _float_or_zero(cache.get("fetched_at")),
                        "last_sync_at": _clean(cache.get("last_sync_at")),
                        "last_error": _clean(cache.get("last_error")),
                        "last_fetch_ms": _int_or_none(cache.get("last_fetch_ms")),
                    }
            self._cache_loaded = True

    def _save_cache_snapshot_locked(self) -> None:
        with self._cache_lock:
            write_json_file(
                PROMPT_LIBRARY_CACHE_PATH,
                {
                    "updated_at": _now_iso(),
                    "sources": self._source_cache,
                },
            )

    def _load_sources_locked(self) -> list[dict[str, Any]]:
        data = read_json_file(
            self.path,
            name="prompt_sources.json",
            default_factory=dict,
            expected_types=(dict, list),
        )
        raw_sources = data if isinstance(data, list) else data.get("sources", [])
        sources = [source for raw in raw_sources if (source := _normalize_source(raw)) is not None]
        return sorted(_merge_builtin_sources(sources), key=_source_sort_key)

    def _save_sources_locked(self, sources: list[dict[str, Any]]) -> None:
        write_json_file(self.path, {"sources": sorted(sources, key=_source_sort_key)})

    def _seed_default_cache_locked(self, sources: list[dict[str, Any]]) -> None:
        self._ensure_cache_loaded_locked()
        default_source = next((source for source in sources if source.get("id") == DEFAULT_PROMPT_SOURCE_ID), None)
        if default_source is None:
            return
        with self._cache_lock:
            current = dict(self._source_cache.get(DEFAULT_PROMPT_SOURCE_ID) or {})
            if current.get("items"):
                return
        if not DEFAULT_PROMPT_LIBRARY_PATH.exists():
            return
        try:
            payload = DEFAULT_PROMPT_LIBRARY_PATH.read_bytes()
            adapter_name, raw_items = parse_prompt_source_payload(payload, default_source, content_type="application/json")
            items = sorted(
                [item for raw in raw_items if (item := _normalize_prompt(raw, default_source)) is not None],
                key=_sort_key,
            )
        except Exception as exc:
            with self._cache_lock:
                current = dict(self._source_cache.get(DEFAULT_PROMPT_SOURCE_ID) or {})
                if current.get("items"):
                    return
                self._source_cache[DEFAULT_PROMPT_SOURCE_ID] = {
                    "items": [],
                    "adapter": "json",
                    "fetched_at": 0,
                    "last_sync_at": "",
                    "last_error": f"default snapshot failed: {exc}"[:500],
                    "last_fetch_ms": 0,
                }
            self._save_cache_snapshot_locked()
            return
        with self._cache_lock:
            current = dict(self._source_cache.get(DEFAULT_PROMPT_SOURCE_ID) or {})
            if current.get("items"):
                return
            self._source_cache[DEFAULT_PROMPT_SOURCE_ID] = {
                "items": items,
                "adapter": adapter_name,
                "fetched_at": 0,
                "last_sync_at": _now_iso(),
                "last_error": "",
                "last_fetch_ms": 0,
            }
        self._save_cache_snapshot_locked()

    def _source_status_locked(self, source: dict[str, Any]) -> dict[str, Any]:
        self._ensure_cache_loaded_locked()
        with self._cache_lock:
            cache = dict(self._source_cache.get(_clean(source.get("id"))) or {})
        adapter = _clean(cache.get("adapter") or source.get("adapter"))
        return {
            **source,
            "adapter": adapter,
            "adapter_label": adapter_label(adapter),
            "prompt_count": len(cache.get("items") or []),
            "last_sync_at": _clean(cache.get("last_sync_at")),
            "last_error": _clean(cache.get("last_error")),
            "last_fetch_ms": _int_or_none(cache.get("last_fetch_ms")),
        }

    def _fetch_source_locked(self, source: dict[str, Any], *, force: bool = False) -> list[dict[str, Any]]:
        self._ensure_cache_loaded_locked()
        source_id = _clean(source.get("id"))
        with self._cache_lock:
            cache = dict(self._source_cache.get(source_id) or {})
        if source_id == DEFAULT_PROMPT_SOURCE_ID and _clean(source.get("url")).startswith("local://"):
            self._seed_default_cache_locked([source])
            with self._cache_lock:
                return list((self._source_cache.get(source_id) or {}).get("items") or [])
        if not force and cache:
            return list(cache.get("items") or [])

        started = time.monotonic()
        last_error: Exception | None = None
        try:
            items: list[dict[str, Any]] = []
            adapter_name = _clean(source.get("adapter"))
            for fetch_url in _source_fetch_urls(source):
                for _attempt in range(PROMPT_SOURCE_FETCH_ATTEMPTS):
                    try:
                        response = curl_requests.get(
                            fetch_url,
                            headers={
                                "Accept": "application/json,text/markdown,text/html,text/plain,*/*",
                                "User-Agent": "chatgpt2api-prompt-library/3.0",
                            },
                            timeout=PROMPT_SOURCE_TIMEOUT,
                        )
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "")
                        payload = response.content[: PROMPT_SOURCE_MAX_BYTES + 1]
                        if len(payload) > PROMPT_SOURCE_MAX_BYTES:
                            raise ValueError("prompt source too large")
                        source_for_items = {**source, "url": fetch_url}
                        adapter_name, raw_items = parse_prompt_source_payload(payload, source_for_items, content_type=content_type)
                        items = sorted(
                            [item for raw in raw_items if (item := _normalize_prompt(raw, source_for_items)) is not None],
                            key=_sort_key,
                        )
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                if last_error is None:
                    break
            else:
                raise last_error or ValueError("prompt source unavailable")

            with self._cache_lock:
                self._source_cache[source_id] = {
                    "items": items,
                    "adapter": adapter_name,
                    "fetched_at": time.time(),
                    "last_sync_at": _now_iso(),
                    "last_error": "",
                    "last_fetch_ms": int((time.monotonic() - started) * 1000),
                }
            self._save_cache_snapshot_locked()
            return list(items)
        except Exception as exc:
            fallback_items = list((cache or {}).get("items") or [])
            with self._cache_lock:
                self._source_cache[source_id] = {
                    "items": fallback_items,
                    "adapter": _clean((cache or {}).get("adapter") or source.get("adapter")),
                    "fetched_at": _float_or_zero((cache or {}).get("fetched_at")),
                    "last_sync_at": _clean((cache or {}).get("last_sync_at")),
                    "last_error": str(exc)[:500],
                    "last_fetch_ms": int((time.monotonic() - started) * 1000),
                }
            self._save_cache_snapshot_locked()
            return fallback_items

    def _list_cached_items_locked(self, *, include_disabled_items: bool = False) -> dict[str, Any]:
        sources = self._load_sources_locked()
        self._seed_default_cache_locked(sources)
        enabled_sources = [source for source in sources if include_disabled_items or bool(source.get("enabled", True))]
        with self._cache_lock:
            source_cache = {
                source_id: dict(cache)
                for source_id, cache in self._source_cache.items()
                if isinstance(cache, dict)
            }

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        cached_source_count = 0
        for source in enabled_sources:
            cache = source_cache.get(_clean(source.get("id"))) or {}
            if cache.get("items"):
                cached_source_count += 1
            source_items = list(cache.get("items") or [])
            if not include_disabled_items:
                source_items = [item for item in source_items if bool(item.get("enabled", True))]
            for item in source_items:
                if not isinstance(item, dict):
                    continue
                fingerprint = _prompt_fingerprint(item)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                normalized_item = dict(item)
                normalized_item["description"] = _clean_prompt_display_text(normalized_item.get("description"))[:700]
                items.append(normalized_item)

        return {
            "items": sorted(items, key=_sort_key),
            "synced": cached_source_count > 0,
            "cached_source_count": cached_source_count,
            "enabled_source_count": len([source for source in sources if bool(source.get("enabled", True))]),
        }

    def _list_items_locked(self, *, include_disabled_items: bool = False, force: bool = False, source_id: str = "") -> list[dict[str, Any]]:
        sources = self._load_sources_locked()
        if source_id:
            sources = [source for source in sources if source.get("id") == source_id]
        else:
            sources = [source for source in sources if bool(source.get("enabled", True))]
        if not sources:
            return []

        fetched: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        if len(sources) == 1:
            fetched = [(sources[0], self._fetch_source_locked(sources[0], force=force))]
        else:
            max_workers = min(PROMPT_SOURCE_FETCH_WORKERS, len(sources))
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="prompt-source") as executor:
                futures = {
                    executor.submit(self._fetch_source_locked, source, force=force): source
                    for source in sources
                }
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        fetched.append((source, future.result()))
                    except Exception:
                        fetched.append((source, []))

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source, source_items in sorted(fetched, key=lambda pair: _source_sort_key(pair[0])):
            if not include_disabled_items:
                source_items = [item for item in source_items if bool(item.get("enabled", True))]
            for item in source_items:
                fingerprint = _prompt_fingerprint(item)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                normalized_item = dict(item)
                normalized_item["description"] = _clean_prompt_display_text(normalized_item.get("description"))[:700]
                items.append(normalized_item)
        return sorted(items, key=_sort_key)

    def list_cached(self) -> dict[str, Any]:
        return self._list_cached_items_locked(include_disabled_items=False)

    def list_sources(self) -> list[dict[str, Any]]:
        sources = self._load_sources_locked()
        self._seed_default_cache_locked(sources)
        return [self._source_status_locked(source) for source in sources]

    def update_source(self, source_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        normalized_id = _clean(source_id)
        if not normalized_id:
            return None
        with self._lock:
            sources = self._load_sources_locked()
            for index, current in enumerate(sources):
                if current.get("id") != normalized_id:
                    continue
                candidate = dict(current)
                if "enabled" in payload:
                    candidate["enabled"] = payload["enabled"]
                candidate["id"] = current["id"]
                candidate["built_in"] = bool(current.get("built_in"))
                candidate["created_at"] = current.get("created_at") or _now_iso()
                candidate["updated_at"] = _now_iso()
                source = _normalize_source(candidate)
                if source is None:
                    raise ValueError("invalid prompt source url")
                sources[index] = source
                self._save_sources_locked(sources)
                return self._source_status_locked(source)
        return None

    def refresh(self, source_id: str = "") -> dict[str, Any]:
        with self._lock:
            items = [
                dict(item)
                for item in self._list_items_locked(
                    include_disabled_items=True,
                    force=True,
                    source_id=_clean(source_id),
                )
            ]
            sources = [self._source_status_locked(source) for source in self._load_sources_locked()]
            source_errors = [
                {
                    "id": _clean(source.get("id")),
                    "name": _clean(source.get("name")) or _clean(source.get("id")),
                    "error": _clean(source.get("last_error")),
                }
                for source in sources
                if bool(source.get("enabled", True)) and _clean(source.get("last_error"))
            ]
            if not source_id:
                items = self._list_cached_items_locked(include_disabled_items=True)["items"]
            return {
                "items": items,
                "sources": sources,
                "source_error_count": len(source_errors),
                "source_errors": source_errors,
            }


prompt_library_service = PromptLibraryService()
