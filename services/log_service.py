from __future__ import annotations

import hashlib
import json
import itertools
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from services.config import DATA_DIR
from services.protocol.error_response import anthropic_error_response, openai_error_response
from services.realtime_monitor_service import realtime_monitor_service
from utils.diagnostics import exception_diagnostic_fields
from utils.helper import anthropic_sse_stream, image_sse_stream, sse_json_stream
from utils.log import logger
from utils.timezone import beijing_from_timestamp, beijing_now_str

LOG_TYPE_CALL = "call"
LOG_TYPE_ACCOUNT = "account"
INTERNAL_RESPONSE_KEYS = {"_account_email", "_conversation_id", "_call_id", "_image_urls"}
LOG_IMAGE_URL_RE = re.compile(r"(?:!\[[^\]]*\]\()(?P<url>(?:https?://|/images/|/image-thumbnails/)[^\s)\"']+)\)")
PERF_WAIT_WARN_MS = 1000
REQUEST_TEXT_EXCERPT_LIMIT = 1000
REQUEST_TEXT_FULL_LIMIT = 50000


class LogService:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._line_count_cache: tuple[int, int, int] | None = None
        self._type_count_cache: dict[str, tuple[int, int, int]] = {}

    @staticmethod
    def _legacy_id(raw_line: str, line_number: int) -> str:
        payload = f"{line_number}:{raw_line}".encode("utf-8", errors="ignore")
        return hashlib.sha1(payload).hexdigest()[:24]

    def _parse_line(self, raw_line: str, line_number: int) -> dict[str, Any] | None:
        try:
            item = json.loads(raw_line)
        except Exception:
            return None
        if not isinstance(item, dict):
            return None
        parsed = dict(item)
        parsed["id"] = str(parsed.get("id") or self._legacy_id(raw_line, line_number))
        return parsed

    @staticmethod
    def _serialize_item(item: dict[str, Any]) -> str:
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _timestamp(item: dict[str, Any]) -> float | None:
        text = str(item.get("time") or "").strip()
        if not text:
            return None
        normalized = text.replace("T", " ")[:19]
        candidates = (
            (normalized[:19], "%Y-%m-%d %H:%M:%S"),
            (normalized[:10], "%Y-%m-%d"),
        )
        for value, fmt in candidates:
            try:
                return time.mktime(time.strptime(value, fmt))
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _matches_filters(item: dict[str, Any], *, type: str = "", start_date: str = "", end_date: str = "") -> bool:
        t = str(item.get("time") or "")
        day = t[:10]
        if type and item.get("type") != type:
            return False
        if start_date and day < start_date:
            return False
        if end_date and day > end_date:
            return False
        return True

    @staticmethod
    def _detail_value(item: dict[str, Any], key: str, default: object = "") -> object:
        detail = item.get("detail")
        if isinstance(detail, dict):
            value = detail.get(key)
            if value not in (None, ""):
                return value
        value = item.get(key)
        return default if value in (None, "") else value

    @staticmethod
    def _clean(value: object) -> str:
        return str(value or "").strip()

    @classmethod
    def _is_failed(cls, item: dict[str, Any]) -> bool:
        status = cls._clean(cls._detail_value(item, "status")).lower()
        return status in {"failed", "error", "fail"} or bool(
            cls._detail_value(item, "error") or cls._detail_value(item, "error_code")
        )

    @classmethod
    def _is_limited(cls, item: dict[str, Any]) -> bool:
        text = " ".join(
            cls._clean(cls._detail_value(item, key))
            for key in ("status", "error_code", "reason", "error")
        ).lower()
        return any(keyword in text for keyword in ("limit", "quota", "429", "rate_limited", "rate limit", "受限", "限流"))

    @classmethod
    def _is_image_log(cls, item: dict[str, Any]) -> bool:
        endpoint = cls._clean(cls._detail_value(item, "endpoint")).lower()
        model = cls._clean(cls._detail_value(item, "model")).lower()
        return "/images/" in endpoint or ("/v1/chat" in endpoint and "image" in model)

    @classmethod
    def _matches_extended_filters(
        cls,
        item: dict[str, Any],
        *,
        type: str = "",
        start_date: str = "",
        end_date: str = "",
        status: str = "",
        endpoint: str = "",
        model: str = "",
        account: str = "",
        conversation_id: str = "",
        search: str = "",
    ) -> bool:
        if not cls._matches_filters(item, type=type, start_date=start_date, end_date=end_date):
            return False
        normalized_status = cls._clean(status).lower()
        if normalized_status == "success" and cls._clean(cls._detail_value(item, "status")).lower() != "success":
            return False
        if normalized_status == "failed" and not cls._is_failed(item):
            return False
        if normalized_status == "limited" and not cls._is_limited(item):
            return False
        if endpoint and cls._clean(cls._detail_value(item, "endpoint")) != endpoint:
            return False
        if model and cls._clean(cls._detail_value(item, "model")) != model:
            return False
        if account and cls._clean(cls._detail_value(item, "account_email")) != account:
            return False
        if conversation_id and cls._clean(cls._detail_value(item, "conversation_id")) != conversation_id:
            return False
        query = cls._clean(search).lower()
        if query:
            haystack = " ".join(
                cls._clean(value)
                for value in (
                    item.get("id"),
                    item.get("time"),
                    item.get("type"),
                    item.get("summary"),
                    cls._detail_value(item, "endpoint"),
                    cls._detail_value(item, "model"),
                    cls._detail_value(item, "status"),
                    cls._detail_value(item, "key_id"),
                    cls._detail_value(item, "key_name"),
                    cls._detail_value(item, "account_email"),
                    cls._detail_value(item, "conversation_id"),
                    cls._detail_value(item, "request_text"),
                    cls._detail_value(item, "request_text_full"),
                    cls._detail_value(item, "error"),
                    cls._detail_value(item, "error_code"),
                    cls._detail_value(item, "reason"),
                    cls._detail_value(item, "stage"),
                )
            ).lower()
            if query not in haystack:
                return False
        return True

    def _line_count(self) -> int:
        if not self.path.exists():
            return 0
        stat = self.path.stat()
        size = stat.st_size
        if size <= 0:
            return 0
        cache = self._line_count_cache
        if cache and cache[0] == size and cache[1] == stat.st_mtime_ns:
            return cache[2]
        newline_count = 0
        with self.path.open("rb") as file:
            while True:
                chunk = file.read(1024 * 1024)
                if not chunk:
                    break
                newline_count += chunk.count(b"\n")
            file.seek(size - 1)
            tail = file.read(1)
        total = newline_count if tail == b"\n" else newline_count + 1
        self._line_count_cache = (size, stat.st_mtime_ns, total)
        return total

    def _line_count_for_type(self, type: str) -> int:
        type_filter = self._clean(type)
        if not type_filter:
            return self._line_count()
        if not self.path.exists():
            return 0
        stat = self.path.stat()
        size = stat.st_size
        if size <= 0:
            return 0
        cache = self._type_count_cache.get(type_filter)
        if cache and cache[0] == size and cache[1] == stat.st_mtime_ns:
            return cache[2]
        needles = (
            f'"type":"{type_filter}"'.encode("utf-8"),
            f'"type": "{type_filter}"'.encode("utf-8"),
        )
        total = 0
        with self.path.open("rb") as file:
            for raw_line in file:
                head = raw_line[:512]
                if any(needle in head for needle in needles):
                    total += 1
        self._type_count_cache[type_filter] = (size, stat.st_mtime_ns, total)
        return total

    def _iter_raw_lines_reverse(self):
        if not self.path.exists():
            return
        total_lines = self._line_count()
        if total_lines <= 0:
            return
        line_number = total_lines - 1
        buffer = b""
        skipped_trailing_newline = False
        with self.path.open("rb") as file:
            position = file.seek(0, 2)
            while position > 0:
                read_size = min(1024 * 1024, position)
                position -= read_size
                file.seek(position)
                buffer = file.read(read_size) + buffer
                parts = buffer.split(b"\n")
                buffer = parts[0]
                for raw_line in reversed(parts[1:]):
                    if (
                        not skipped_trailing_newline
                        and raw_line == b""
                        and line_number == total_lines - 1
                    ):
                        skipped_trailing_newline = True
                        continue
                    skipped_trailing_newline = True
                    if raw_line.endswith(b"\r"):
                        raw_line = raw_line[:-1]
                    yield raw_line.decode("utf-8", errors="ignore"), line_number
                    line_number -= 1
            if line_number >= 0:
                if buffer.endswith(b"\r"):
                    buffer = buffer[:-1]
                yield buffer.decode("utf-8", errors="ignore"), line_number

    def _iter_recent_raw_lines(self, max_items: int, total_lines: int | None = None):
        if not self.path.exists() or max_items <= 0:
            return
        line_number = (total_lines - 1) if total_lines is not None else -1
        buffer = b""
        emitted = 0
        skipped_trailing_newline = False
        with self.path.open("rb") as file:
            position = file.seek(0, 2)
            while position > 0 and emitted < max_items:
                read_size = min(1024 * 1024, position)
                position -= read_size
                file.seek(position)
                buffer = file.read(read_size) + buffer
                parts = buffer.split(b"\n")
                buffer = parts[0]
                for raw_line in reversed(parts[1:]):
                    if not skipped_trailing_newline and raw_line == b"":
                        skipped_trailing_newline = True
                        continue
                    skipped_trailing_newline = True
                    if raw_line.endswith(b"\r"):
                        raw_line = raw_line[:-1]
                    yield raw_line.decode("utf-8", errors="ignore"), line_number
                    emitted += 1
                    line_number -= 1
                    if emitted >= max_items:
                        break
            if emitted < max_items and buffer:
                if buffer.endswith(b"\r"):
                    buffer = buffer[:-1]
                yield buffer.decode("utf-8", errors="ignore"), line_number

    def _iter_parsed_reverse(self):
        for raw_line, line_number in self._iter_raw_lines_reverse() or ():
            item = self._parse_line(raw_line, line_number)
            if item is not None:
                yield item

    def _iter_recent_parsed(self, max_items: int, total_lines: int | None = None):
        for raw_line, line_number in self._iter_recent_raw_lines(max_items, total_lines=total_lines) or ():
            item = self._parse_line(raw_line, line_number)
            if item is not None:
                yield item

    @staticmethod
    def _has_precise_query(
        *,
        start_date: str = "",
        end_date: str = "",
        status: str = "",
        endpoint: str = "",
        model: str = "",
        account: str = "",
        conversation_id: str = "",
        search: str = "",
    ) -> bool:
        return any(
            str(value or "").strip()
            for value in (start_date, end_date, status, endpoint, model, account, conversation_id, search)
        )

    def _page_response(
        self,
        *,
        items: list[dict[str, Any]],
        total: int,
        limit: int,
        offset: int,
        has_more: bool | None = None,
        statuses: Counter[str],
        endpoints: Counter[str],
        models: Counter[str],
        accounts: Counter[str],
        stats: Counter,
    ) -> dict[str, Any]:
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total if has_more is None else has_more,
            "facets": {
                "statuses": dict(statuses),
                "endpoints": dict(endpoints),
                "models": dict(models),
                "accounts": dict(accounts),
            },
            "stats": {
                "total": total,
                "success": int(stats["success"]),
                "failed": int(stats["failed"]),
                "limited": int(stats["limited"]),
                "image": int(stats["image"]),
            },
        }

    def _accumulate_page_stats(
        self,
        item: dict[str, Any],
        *,
        statuses: Counter[str],
        endpoints: Counter[str],
        models: Counter[str],
        accounts: Counter[str],
        stats: Counter,
    ) -> None:
        status_label = self._clean(self._detail_value(item, "status")) or "unknown"
        endpoint_label = self._clean(self._detail_value(item, "endpoint"))
        model_label = self._clean(self._detail_value(item, "model"))
        account_label = self._clean(self._detail_value(item, "account_email"))
        statuses[status_label] += 1
        if endpoint_label:
            endpoints[endpoint_label] += 1
        if model_label:
            models[model_label] += 1
        if account_label:
            accounts[account_label] += 1

        if status_label.lower() == "success":
            stats["success"] += 1
        if self._is_failed(item):
            stats["failed"] += 1
        if self._is_limited(item):
            stats["limited"] += 1
        if self._is_image_log(item):
            stats["image"] += 1

    def _list_page_fast(self, *, type: str = "", limit: int, offset: int) -> dict[str, Any]:
        total_lines = self._line_count()
        type_filter = self._clean(type)
        total = self._line_count_for_type(type_filter)
        target_count = offset + limit + 1
        matched: list[dict[str, Any]] = []
        for item in self._iter_recent_parsed(total_lines, total_lines=total_lines) or ():
            if type_filter and item.get("type") != type_filter:
                continue
            matched.append(item)
            if len(matched) >= target_count:
                break
        has_more = len(matched) > offset + limit
        items = matched[offset:offset + limit]
        statuses: Counter[str] = Counter()
        endpoints: Counter[str] = Counter()
        models: Counter[str] = Counter()
        accounts: Counter[str] = Counter()
        stats = Counter()
        for item in items:
            self._accumulate_page_stats(
                item,
                statuses=statuses,
                endpoints=endpoints,
                models=models,
                accounts=accounts,
                stats=stats,
            )
        stats["total"] = total
        response = self._page_response(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=has_more if type_filter else None,
            statuses=statuses,
            endpoints=endpoints,
            models=models,
            accounts=accounts,
            stats=stats,
        )
        response["facets_scope"] = "page"
        response["stats_scope"] = "page"
        if type_filter:
            response["total_scope"] = "type_count"
        return response

    def add(self, type: str, summary: str = "", detail: dict[str, Any] | None = None, **data: Any) -> None:
        item = {
            "id": uuid4().hex,
            "time": beijing_now_str(),
            "type": type,
            "summary": summary,
            "detail": detail or data,
        }
        with self._lock:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(self._serialize_item(item) + "\n")
        if type == LOG_TYPE_CALL:
            from services.dashboard_metrics_service import safe_record_dashboard_call

            safe_record_dashboard_call(item)

    def list(self, type: str = "", start_date: str = "", end_date: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        items: list[dict[str, Any]] = []
        for item in self._iter_parsed_reverse():
            if not self._matches_filters(item, type=type, start_date=start_date, end_date=end_date):
                continue
            items.append(item)
            if len(items) >= limit:
                break
        return items

    def list_page(
        self,
        *,
        type: str = "",
        start_date: str = "",
        end_date: str = "",
        status: str = "",
        endpoint: str = "",
        model: str = "",
        account: str = "",
        conversation_id: str = "",
        search: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit or 200), 20000))
        safe_offset = max(0, int(offset or 0))
        if not self._has_precise_query(
            start_date=start_date,
            end_date=end_date,
            status=status,
            endpoint=endpoint,
            model=model,
            account=account,
            conversation_id=conversation_id,
            search=search,
        ):
            return self._list_page_fast(type=type, limit=safe_limit, offset=safe_offset)

        items: list[dict[str, Any]] = []
        total = 0
        statuses: Counter[str] = Counter()
        endpoints: Counter[str] = Counter()
        models: Counter[str] = Counter()
        accounts: Counter[str] = Counter()
        stats = Counter()

        for item in self._iter_parsed_reverse() or ():
            if not self._matches_extended_filters(
                item,
                type=type,
                start_date=start_date,
                end_date=end_date,
                status=status,
                endpoint=endpoint,
                model=model,
                account=account,
                conversation_id=conversation_id,
                search=search,
            ):
                continue

            total += 1
            self._accumulate_page_stats(
                item,
                statuses=statuses,
                endpoints=endpoints,
                models=models,
                accounts=accounts,
                stats=stats,
            )

            if total <= safe_offset:
                continue
            if len(items) < safe_limit:
                items.append(item)

        return self._page_response(
            items=items,
            total=total,
            limit=safe_limit,
            offset=safe_offset,
            has_more=None,
            statuses=statuses,
            endpoints=endpoints,
            models=models,
            accounts=accounts,
            stats=stats,
        )

    def delete(self, ids: list[str]) -> dict[str, int]:
        target_ids = {str(item or "").strip() for item in ids if str(item or "").strip()}
        if not self.path.exists() or not target_ids:
            return {"removed": 0}
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            kept_lines: list[str] = []
            removed = 0
            for line_number, raw_line in enumerate(lines):
                item = self._parse_line(raw_line, line_number)
                if item is None:
                    kept_lines.append(raw_line)
                    continue
                if str(item.get("id") or "") in target_ids:
                    removed += 1
                    continue
                kept_lines.append(self._serialize_item(item))
            content = "\n".join(kept_lines)
            if content:
                content += "\n"
            self.path.write_text(content, encoding="utf-8")
        return {"removed": removed}

    def _cleanup_old(self, retention_days: int, *, dry_run: bool) -> dict[str, int | bool]:
        if not self.path.exists():
            return {"removed": 0, "kept": 0, "removed_size_bytes": 0, "dry_run": dry_run}
        try:
            days = max(1, int(retention_days))
        except (TypeError, ValueError):
            days = 30
        cutoff = time.time() - days * 86400
        with self._lock:
            removed = 0
            kept = 0
            removed_size_bytes = 0
            temp_path = self.path.with_name(f"{self.path.name}.cleanup-{uuid4().hex}.tmp")
            try:
                output_file = None if dry_run else temp_path.open("w", encoding="utf-8", newline="\n")
                try:
                    with self.path.open("r", encoding="utf-8", errors="ignore") as input_file:
                        for line_number, raw_line in enumerate(input_file):
                            raw_line_clean = raw_line.rstrip("\n").rstrip("\r")
                            item = self._parse_line(raw_line_clean, line_number)
                            timestamp = self._timestamp(item) if item is not None else None
                            if timestamp is not None and timestamp < cutoff:
                                removed += 1
                                removed_size_bytes += len(raw_line.encode("utf-8", errors="ignore"))
                                continue
                            kept += 1
                            if output_file is not None:
                                output_file.write((self._serialize_item(item) if item is not None else raw_line_clean) + "\n")
                finally:
                    if output_file is not None:
                        output_file.close()

                if not dry_run:
                    if removed:
                        temp_path.replace(self.path)
                    elif temp_path.exists():
                        temp_path.unlink()
            except Exception:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
                raise
        return {"removed": removed, "kept": kept, "removed_size_bytes": removed_size_bytes, "dry_run": dry_run}

    def preview_cleanup_old(self, retention_days: int) -> dict[str, int | bool]:
        return self._cleanup_old(retention_days, dry_run=True)

    def cleanup_old(self, retention_days: int) -> dict[str, int | bool]:
        return self._cleanup_old(retention_days, dry_run=False)


log_service = LogService(DATA_DIR / "logs.jsonl")


def cleanup_old_logs() -> dict[str, int]:
    from services.config import config

    return log_service.cleanup_old(config.log_retention_days)


def _auto_cleanup_worker(stop_event: threading.Event) -> None:
    while not stop_event.wait(1800):
        try:
            result = cleanup_old_logs()
            if int(result.get("removed") or 0) > 0:
                logger.info({"event": "log_auto_cleanup_done", **result})
        except Exception as exc:
            logger.error({"event": "log_auto_cleanup_failed", "error": str(exc)})


def start_log_cleanup_scheduler(stop_event: threading.Event) -> threading.Thread:
    thread = threading.Thread(target=_auto_cleanup_worker, args=(stop_event,), daemon=True, name="log-cleanup")
    thread.start()
    return thread


def _collect_urls(value: object) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "url" and isinstance(item, str):
                urls.append(item)
            elif key in {"urls", "_image_urls"} and isinstance(item, list):
                urls.extend(str(url) for url in item if isinstance(url, str))
            else:
                urls.extend(_collect_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_collect_urls(item))
    elif isinstance(value, str):
        urls.extend(match.group("url").rstrip(".,;") for match in LOG_IMAGE_URL_RE.finditer(value))
    return urls


def _collect_account_emails(value: object) -> list[str]:
    emails: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"_account_email", "account_email"} and isinstance(item, str) and item.strip():
                emails.append(item.strip())
            else:
                emails.extend(_collect_account_emails(item))
    elif isinstance(value, list):
        for item in value:
            emails.extend(_collect_account_emails(item))
    return emails


def _collect_conversation_ids(value: object) -> list[str]:
    ids: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "_conversation_id" and isinstance(item, str) and item.strip():
                ids.append(item.strip())
            else:
                ids.extend(_collect_conversation_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.extend(_collect_conversation_ids(item))
    return ids


IMAGE_TRACE_REQUEST_KEYS = {
    "n",
    "size",
    "quality",
    "response_format",
    "stream",
    "partial_images",
}


def _image_trace_metadata(body: dict[str, Any]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key in IMAGE_TRACE_REQUEST_KEYS:
        if key not in body:
            continue
        value = body.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, (str, int, float, bool)):
            metadata[key] = value
    images = body.get("images")
    if isinstance(images, list) and images:
        metadata["input_image_count"] = len(images)
    return metadata


def _image_result_metrics(value: object) -> dict[str, object]:
    metrics = {
        "result_data_count": 0,
        "result_url_count": 0,
        "result_b64_count": 0,
        "result_b64_chars": 0,
    }

    def visit(item: object) -> None:
        if isinstance(item, dict):
            if "data" in item and isinstance(item.get("data"), list):
                metrics["result_data_count"] = max(
                    int(metrics["result_data_count"]),
                    len(item.get("data") or []),
                )
            url = item.get("url")
            if isinstance(url, str) and url.strip():
                metrics["result_url_count"] = int(metrics["result_url_count"]) + 1
            b64_json = item.get("b64_json")
            if isinstance(b64_json, str) and b64_json.strip():
                metrics["result_b64_count"] = int(metrics["result_b64_count"]) + 1
                metrics["result_b64_chars"] = int(metrics["result_b64_chars"]) + len(b64_json)
            for nested in item.values():
                if isinstance(nested, (dict, list)):
                    visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return {
        key: value
        for key, value in metrics.items()
        if value
    }


def _strip_internal_response_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_internal_response_fields(item)
            for key, item in value.items()
            if key not in INTERNAL_RESPONSE_KEYS
        }
    if isinstance(value, list):
        return [_strip_internal_response_fields(item) for item in value]
    return value


def _request_excerpt(text: object, limit: int = REQUEST_TEXT_EXCERPT_LIMIT) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _request_full_text(text: object, limit: int = REQUEST_TEXT_FULL_LIMIT) -> tuple[str, bool]:
    value = str(text or "").strip()
    if not value:
        return "", False
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized, False
    return normalized[: limit - 1].rstrip() + "…", True


def _exception_log_fields(exc: Exception) -> dict[str, object]:
    return exception_diagnostic_fields(exc, include_status_code=True)

def _image_error_response(exc: Exception) -> JSONResponse:
    from services.protocol.conversation import public_image_error_message

    raw_message = str(exc)
    message = public_image_error_message(raw_message)
    raw_lower = raw_message.lower()
    if hasattr(exc, "to_openai_error") and hasattr(exc, "status_code"):
        return JSONResponse(status_code=int(exc.status_code), content=exc.to_openai_error())
    if "quota_exhausted" in raw_lower or "insufficient_quota" in raw_lower:
        return openai_error_response(
            {
                "error": {
                    "message": message,
                    "type": "insufficient_quota",
                    "param": None,
                    "code": "insufficient_quota",
                }
            },
            429,
        )
    return openai_error_response(message, 502)


def _protocol_error_response(exc: Exception, status_code: int, sse: str) -> JSONResponse:
    message = str(exc)
    if sse == "anthropic":
        return anthropic_error_response(message, status_code)
    return openai_error_response(message, status_code)


def _next_item(items):
    try:
        return True, next(items)
    except StopIteration:
        return False, None


@dataclass
class LoggedCall:
    identity: dict[str, object]
    endpoint: str
    model: str
    summary: str
    started: float = field(default_factory=time.time)
    request_text: str = ""
    request_shape: dict[str, int] | None = None
    call_id: str = field(default_factory=lambda: uuid4().hex[:16])
    perf_timings: dict[str, int] = field(default_factory=dict)
    trace_metadata: dict[str, object] = field(default_factory=dict)

    async def run(self, handler, *args, sse: str = "openai"):
        from services.protocol.conversation import ImageGenerationError

        if args and isinstance(args[0], dict):
            self.attach_trace_metadata(args[0])
        trace_perf = self._trace_image_perf()
        if trace_perf:
            realtime_monitor_service.start(
                self.call_id,
                endpoint=self.endpoint,
                model=self.model,
                summary=self.summary,
                role=str(self.identity.get("role") or ""),
                key_name=str(self.identity.get("name") or ""),
            )
        handler_submitted = time.perf_counter()

        def _call_handler():
            handler_started = time.perf_counter()
            queue_ms = int((handler_started - handler_submitted) * 1000)
            if trace_perf:
                self.perf_timings["handler_queue_ms"] = queue_ms
                realtime_monitor_service.stage(
                    self.call_id,
                    "handler_started",
                    handler_queue_ms=queue_ms,
                    endpoint=self.endpoint,
                    model=self.model,
                )
            if trace_perf and queue_ms >= PERF_WAIT_WARN_MS:
                logger.warning({
                    "event": "api_handler_threadpool_wait_slow",
                    "call_id": self.call_id,
                    "endpoint": self.endpoint,
                    "model": self.model,
                    "queue_ms": queue_ms,
                })
            try:
                return handler(*args)
            finally:
                if trace_perf:
                    self.perf_timings["handler_exec_ms"] = int((time.perf_counter() - handler_started) * 1000)

        try:
            result = await run_in_threadpool(_call_handler)
        except ImageGenerationError as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""),
                     conversation_id=getattr(exc, "conversation_id", ""), extra=_exception_log_fields(exc))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""),
                     extra=_exception_log_fields(exc))
            if self.endpoint.startswith("/v1/images"):
                return _image_error_response(exc)
            return _protocol_error_response(exc, 502, sse)

        if isinstance(result, dict):
            self.log("调用完成", result)
            return _strip_internal_response_fields(result)

        if self.endpoint.startswith("/v1/images"):
            sender = image_sse_stream
        else:
            sender = anthropic_sse_stream if sse == "anthropic" else sse_json_stream
        first_item_submitted = time.perf_counter()

        def _next_item_with_timing():
            first_item_started = time.perf_counter()
            queue_ms = int((first_item_started - first_item_submitted) * 1000)
            if trace_perf:
                self.perf_timings["stream_first_queue_ms"] = queue_ms
                realtime_monitor_service.stage(
                    self.call_id,
                    "stream_first_item",
                    stream_first_queue_ms=queue_ms,
                    endpoint=self.endpoint,
                    model=self.model,
                )
            if trace_perf and queue_ms >= PERF_WAIT_WARN_MS:
                logger.warning({
                    "event": "api_stream_first_item_threadpool_wait_slow",
                    "call_id": self.call_id,
                    "endpoint": self.endpoint,
                    "model": self.model,
                    "queue_ms": queue_ms,
                })
            try:
                return _next_item(result)
            finally:
                if trace_perf:
                    self.perf_timings["stream_first_exec_ms"] = int((time.perf_counter() - first_item_started) * 1000)

        try:
            has_first, first = await run_in_threadpool(_next_item_with_timing)
        except ImageGenerationError as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""),
                     conversation_id=getattr(exc, "conversation_id", ""), extra=_exception_log_fields(exc))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""),
                     extra=_exception_log_fields(exc))
            if self.endpoint.startswith("/v1/images"):
                return _image_error_response(exc)
            return _protocol_error_response(exc, 502, sse)
        if not has_first:
            self.log("流式调用结束")
            return StreamingResponse(sender(()), media_type="text/event-stream")
        return StreamingResponse(sender(self.stream(itertools.chain([first], result))), media_type="text/event-stream")

    def _trace_image_perf(self) -> bool:
        model = str(self.model or "").strip().lower()
        if self.endpoint.startswith("/v1/images"):
            return True
        if self.endpoint in {"/v1/chat/completions", "/v1/responses"}:
            return "image" in model
        return False

    def attach_trace_metadata(self, body: dict[str, Any]) -> None:
        if not isinstance(body, dict):
            return
        if not self._trace_image_perf():
            return
        body["_call_id"] = self.call_id
        body["_trace_image_perf"] = True
        self.trace_metadata.update(_image_trace_metadata(body))

    def stream(self, items):
        urls: list[str] = []
        account_emails: list[str] = []
        conversation_ids: list[str] = []
        failed = False
        try:
            for item in items:
                urls.extend(_collect_urls(item))
                account_emails.extend(_collect_account_emails(item))
                conversation_ids.extend(_collect_conversation_ids(item))
                yield _strip_internal_response_fields(item)
        except Exception as exc:
            failed = True
            self.log(
                "流式调用失败",
                status="failed",
                error=str(exc),
                urls=urls,
                account_email=(account_emails[0] if account_emails else getattr(exc, "account_email", "")),
                conversation_id=(conversation_ids[0] if conversation_ids else getattr(exc, "conversation_id", "")),
                extra=_exception_log_fields(exc),
            )
            if self.endpoint.startswith("/v1/images") and not hasattr(exc, "to_openai_error"):
                from services.protocol.conversation import ImageGenerationError, public_image_error_message

                raise ImageGenerationError(public_image_error_message(str(exc))) from exc
            raise
        finally:
            if not failed:
                self.log("流式调用结束", urls=urls, account_email=account_emails[0] if account_emails else "",
                         conversation_id=conversation_ids[0] if conversation_ids else "")

    def log(self, suffix: str, result: object = None, status: str = "success", error: str = "",
            urls: list[str] | None = None, account_email: str = "", conversation_id: str = "",
            extra: dict[str, object] | None = None) -> None:
        detail = {
            "key_id": self.identity.get("id"),
            "key_name": self.identity.get("name"),
            "role": self.identity.get("role"),
            "endpoint": self.endpoint,
            "model": self.model,
            "call_id": self.call_id,
            "started_at": beijing_from_timestamp(self.started),
            "ended_at": beijing_now_str(),
            "duration_ms": int((time.time() - self.started) * 1000),
            "status": status,
        }
        if self.perf_timings:
            detail["perf"] = dict(self.perf_timings)
        request_excerpt = _request_excerpt(self.request_text)
        if request_excerpt:
            detail["request_text"] = request_excerpt
            request_full, request_full_truncated = _request_full_text(self.request_text)
            if request_full and request_full != request_excerpt:
                detail["request_text_full"] = request_full
                detail["request_text_truncated"] = request_full_truncated
        if self.request_shape:
            detail["request_shape"] = self.request_shape
        if self.trace_metadata:
            detail["request_meta"] = dict(self.trace_metadata)
        if error:
            detail["error"] = error
        if extra:
            for key, value in extra.items():
                if value in (None, ""):
                    continue
                detail[key] = value
        email = str(account_email or "").strip()
        if not email:
            emails = _collect_account_emails(result)
            email = emails[0] if emails else ""
        if email:
            detail["account_email"] = email
        conv_id = str(conversation_id or "").strip()
        if not conv_id:
            conv_ids = _collect_conversation_ids(result)
            conv_id = conv_ids[0] if conv_ids else ""
        if conv_id:
            detail["conversation_id"] = conv_id
        collected_urls = [*(urls or []), *_collect_urls(result)]
        if collected_urls and not self.endpoint.startswith("/v1/search"):
            detail["urls"] = list(dict.fromkeys(collected_urls))
        if self._trace_image_perf():
            image_metrics = _image_result_metrics(result)
            if image_metrics:
                detail.update(image_metrics)
        if self._trace_image_perf():
            realtime_monitor_service.finish(detail)
        log_service.add(LOG_TYPE_CALL, f"{self.summary}{suffix}", detail)
