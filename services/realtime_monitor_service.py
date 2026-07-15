from __future__ import annotations

import math
import os
import time
from collections import Counter, deque
from threading import Lock
from typing import Any

from services.request_cancel_service import request_cancel_service
from utils.timezone import beijing_from_timestamp, beijing_now_str


def _env_int(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    try:
        value = int(str(os.getenv(name, "") or default).strip())
    except (TypeError, ValueError):
        value = default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _int_ms(value: object) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _trim(value: object, limit: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


RAW_DIAGNOSTIC_FIELDS = (
    "error",
    "raw_error",
    "upstream_error",
    "upstream_message",
)


def _trim_raw(value: object, limit: int = 4000) -> str:
    return _trim(value, limit)


def _mask_email(value: object) -> str:
    return str(value or "").strip()


STAGE_LABELS = {
    "handler_submitted": "等待入口",
    "handler_started": "入口执行",
    "stream_first_item": "读取首包",
    "image_getting_account": "等待账号",
    "image_account_lookup": "等待账号",
    "image_account_wait_slow": "等待账号",
    "image_egress_waiting": "等待出口",
    "image_egress_ready": "出口就绪",
    "image_uploading": "上传图片",
    "image_bootstrapping": "初始化上游",
    "image_getting_token": "获取令牌",
    "image_preparing_conversation": "准备会话",
    "image_starting_generation": "等待上游首包",
    "image_generating": "上游生成中",
    "image_stream_failed": "上游断流",
    "image_egress_fallback_retry": "切换备用出口",
    "image_stream_resolve_start": "解析上游结果",
    "image_resolve_done": "解析图片",
    "image_resolve_failed": "解析失败",
    "image_text_reply": "上游文本回复",
    "image_download_done": "下载图片",
    "image_download_failed": "下载失败",
    "image_retry_wait": "重试等待",
    "image_codex_response_done": "Codex 响应",
    "image_single_stream_done": "生成返回",
    "image_single_done": "单图完成",
    "image_local_rejected": "本地拒绝/繁忙",
    "image_cancelled": "手动终止",
    "completed": "完成",
    "failed": "失败",
}


ACTIVE_STAGE_GROUPS = {
    "handler_submitted": "等待入口",
    "handler_started": "等待入口",
    "stream_first_item": "等待入口",
    "image_getting_account": "等待账号",
    "image_account_lookup": "等待账号",
    "image_account_wait_slow": "等待账号",
    "image_egress_waiting": "等待出口",
    "image_egress_ready": "等待出口",
    "image_uploading": "上游准备",
    "image_bootstrapping": "上游准备",
    "image_getting_token": "上游准备",
    "image_preparing_conversation": "上游准备",
    "image_starting_generation": "上游准备",
    "image_generating": "上游生成中",
    "image_stream_failed": "上游断流",
    "image_egress_fallback_retry": "等待出口",
    "image_stream_resolve_start": "解析/轮询",
    "image_resolve_done": "解析/轮询",
    "image_resolve_failed": "解析/轮询",
    "image_text_reply": "上游文本回复",
    "image_download_done": "下载图片",
    "image_download_failed": "下载图片",
    "image_retry_wait": "重试等待",
    "image_local_rejected": "本地拒绝/繁忙",
    "image_cancelled": "手动终止",
}


METRIC_LABELS = {
    "handler_queue_ms": "等待入口",
    "stream_first_queue_ms": "首包线程等待",
    "account_wait_ms": "等待账号",
    "egress_wait_ms": "等待出口",
    "egress_acquire_ms": "出口租约",
    "upload_ms": "图片上传",
    "bootstrap_ms": "上游初始化",
    "requirements_ms": "获取令牌",
    "prepare_conversation_ms": "准备会话",
    "generation_start_ms": "等待上游首包",
    "http_dns_ms": "HTTP DNS",
    "http_tcp_ms": "HTTP TCP",
    "http_tls_ms": "HTTP TLS",
    "http_wait_ms": "HTTP 等待",
    "http_ttfb_ms": "HTTP 首包",
    "http_total_ms": "HTTP 总耗时",
    "sse_first_event_ms": "SSE 首事件",
    "sse_max_gap_ms": "SSE 最大空窗",
    "sse_last_gap_ms": "SSE 收尾空窗",
    "sse_stream_ms": "SSE 流耗时",
    "conversation_stream_ms": "上游生成中",
    "stream_error_ms": "上游断流",
    "resolve_ms": "图片解析",
    "download_ms": "图片下载",
    "retry_wait_ms": "重试等待",
    "response_ms": "Codex 响应",
    "stream_ms": "单图生成流",
    "total_ms": "单图总耗时",
}


LOCAL_REJECT_PATTERNS = (
    "image_account_selection:",
    "no available image quota",
    "no account in the pool",
    "unsupported image model",
    "rate-limit status",
    "account concurrency",
    "image quota",
    "server busy",
    "local busy",
)


class RealtimeMonitorService:
    def __init__(self) -> None:
        completed_limit = _env_int("CHATGPT2API_MONITOR_COMPLETED_LIMIT", 500, 50, 5000)
        event_limit = _env_int("CHATGPT2API_MONITOR_EVENT_LIMIT", 1000, 100, 10000)
        self._lock = Lock()
        self._active: dict[str, dict[str, Any]] = {}
        self._completed: deque[dict[str, Any]] = deque(maxlen=completed_limit)
        self._events: deque[dict[str, Any]] = deque(maxlen=event_limit)
        self._threadpool: dict[str, int] = {
            "tokens": _env_int("CHATGPT2API_THREAD_TOKENS", 80, 1),
            "previous_tokens": 0,
        }

    def set_threadpool(self, *, tokens: int, previous_tokens: int = 0) -> None:
        with self._lock:
            self._threadpool = {
                "tokens": _int_ms(tokens),
                "previous_tokens": _int_ms(previous_tokens),
            }

    def start(
        self,
        call_id: str,
        *,
        endpoint: str,
        model: str,
        summary: str = "",
        role: str = "",
        key_name: str = "",
    ) -> None:
        call_id = str(call_id or "").strip()
        if not call_id:
            return
        now = time.time()
        record = {
            "call_id": call_id,
            "endpoint": str(endpoint or ""),
            "model": str(model or ""),
            "summary": str(summary or ""),
            "role": str(role or ""),
            "key_name": str(key_name or ""),
            "status": "running",
            "stage": "handler_submitted",
            "stage_label": STAGE_LABELS["handler_submitted"],
            "started_ts": now,
            "stage_started_ts": now,
            "started_at": beijing_from_timestamp(now),
            "updated_at": beijing_now_str(),
            "metrics": {},
            "perf": {},
            "images": {},
        }
        with self._lock:
            self._active[call_id] = record
            self._events.append(self._event(call_id, "handler_submitted", record))

    def stage(self, call_id: str, event: str, **data: Any) -> None:
        call_id = str(call_id or "").strip()
        if not call_id:
            return
        event = str(event or "").strip()
        if not event:
            return
        now = time.time()
        with self._lock:
            record = self._active.get(call_id)
            if record is None:
                record = {
                    "call_id": call_id,
                    "endpoint": "",
                    "model": str(data.get("model") or ""),
                    "summary": "",
                    "role": "",
                    "key_name": "",
                    "status": "running",
                    "stage": event,
                    "stage_label": STAGE_LABELS.get(event, event),
                    "started_ts": now,
                    "stage_started_ts": now,
                    "started_at": beijing_from_timestamp(now),
                    "updated_at": beijing_now_str(),
                    "metrics": {},
                    "perf": {},
                    "images": {},
                }
                self._active[call_id] = record

            if record.get("stage") != event:
                record["stage_started_ts"] = now
            record["stage"] = event
            record["stage_label"] = STAGE_LABELS.get(event, event)
            record["updated_at"] = beijing_now_str()
            self._merge_stage_data(record, data)
            self._events.append(self._event(call_id, event, record, data))

    def finish(self, detail: dict[str, Any]) -> None:
        call_id = str(detail.get("call_id") or "").strip()
        if not call_id:
            return
        request_cancel_service.clear(call_id)
        status = str(detail.get("status") or "success").strip().lower() or "success"
        with self._lock:
            record = self._active.pop(call_id, None)
            if record is None:
                now = time.time()
                record = {
                    "call_id": call_id,
                    "endpoint": str(detail.get("endpoint") or ""),
                    "model": str(detail.get("model") or ""),
                    "summary": "",
                    "role": str(detail.get("role") or ""),
                    "key_name": str(detail.get("key_name") or ""),
                    "status": status,
                    "stage": "completed" if status == "success" else "failed",
                    "stage_label": STAGE_LABELS["completed"] if status == "success" else STAGE_LABELS["failed"],
                    "started_ts": time.time() - (_int_ms(detail.get("duration_ms")) / 1000),
                    "stage_started_ts": time.time(),
                    "started_at": str(detail.get("started_at") or ""),
                    "updated_at": str(detail.get("ended_at") or beijing_now_str()),
                    "metrics": {},
                    "perf": {},
                    "images": {},
                }

            record["status"] = status
            record["stage"] = "completed" if status == "success" else "failed"
            record["stage_label"] = STAGE_LABELS["completed"] if status == "success" else STAGE_LABELS["failed"]
            record["ended_at"] = str(detail.get("ended_at") or beijing_now_str())
            record["updated_at"] = record["ended_at"]
            record["duration_ms"] = _int_ms(detail.get("duration_ms"))
            record["endpoint"] = str(detail.get("endpoint") or record.get("endpoint") or "")
            record["model"] = str(detail.get("model") or record.get("model") or "")
            record["role"] = str(detail.get("role") or record.get("role") or "")
            record["key_name"] = str(detail.get("key_name") or record.get("key_name") or "")
            if detail.get("account_email"):
                record["account_email"] = _mask_email(detail.get("account_email"))
            if detail.get("conversation_id"):
                record["conversation_id"] = str(detail.get("conversation_id") or "")
            for key in RAW_DIAGNOSTIC_FIELDS:
                if detail.get(key):
                    record[key] = _trim_raw(detail.get(key))
            urls = detail.get("urls")
            if isinstance(urls, list):
                record["url_count"] = len(urls)
            perf = detail.get("perf")
            if isinstance(perf, dict):
                self._merge_metric_dict(record.setdefault("perf", {}), perf)
            events = [dict(item) for item in self._events if item.get("call_id") == call_id][-60:]
            diagnostic = self._detail_diagnostic(record, events)
            if diagnostic:
                detail["monitor"] = diagnostic
                for key in (
                    "proxy_source",
                    "proxy_hash",
                    "has_proxy",
                    "egress_mode",
                    "egress_key",
                    "egress_label",
                    "proxy_group_id",
                    "proxy_node_id",
                    "proxy_node_name",
                    "image_egress_limit",
                    "local_reason",
                ):
                    if key in diagnostic and key not in detail:
                        detail[key] = diagnostic[key]
            self._completed.append(self._copy_record(record))
            self._events.append(self._event(call_id, str(record["stage"]), record))

    def detail(self, call_id: str) -> dict[str, Any]:
        call_id = str(call_id or "").strip()
        if not call_id:
            return {}
        with self._lock:
            record = self._active.get(call_id)
            if record is None:
                record = next((item for item in reversed(self._completed) if item.get("call_id") == call_id), None)
            if record is None:
                return {}
            item = self._copy_record(record)
            events = [dict(event) for event in self._events if event.get("call_id") == call_id][-100:]
        now = time.time()
        if str(item.get("status") or "").lower() in {"running", "cancelling"}:
            item["elapsed_ms"] = _int_ms((now - float(item.get("started_ts") or now)) * 1000)
            item["stage_elapsed_ms"] = _int_ms((now - float(item.get("stage_started_ts") or now)) * 1000)
        item = self._public_record(item)
        item["events"] = events
        item["cancelled"] = bool(item.get("cancel_requested_at")) or request_cancel_service.is_cancelled(call_id)
        return item

    def cancel(self, call_id: str) -> dict[str, Any]:
        call_id = str(call_id or "").strip()
        if not call_id:
            return {"ok": False, "error": "call_id is required"}
        with self._lock:
            record = self._active.get(call_id)
            if record is None:
                return {"ok": False, "error": "request is not active"}
            request_cancel_service.cancel(call_id)
            now = time.time()
            record["status"] = "cancelling"
            record["stage"] = "image_cancelled"
            record["stage_label"] = STAGE_LABELS["image_cancelled"]
            record["updated_at"] = beijing_now_str()
            record["cancel_requested_at"] = record["updated_at"]
            record["stage_started_ts"] = now
            self._events.append(self._event(call_id, "image_cancelled", record, {"status": "cancelling"}))
            item = self._copy_record(record)
        now = time.time()
        item["elapsed_ms"] = _int_ms((now - float(item.get("started_ts") or now)) * 1000)
        item["stage_elapsed_ms"] = _int_ms((now - float(item.get("stage_started_ts") or now)) * 1000)
        return {"ok": True, "record": self._public_record(item)}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = [self._copy_record(item) for item in self._active.values()]
            completed = [self._copy_record(item) for item in self._completed]
            events = [dict(item) for item in self._events]
            threadpool = dict(self._threadpool)

        now = time.time()
        for item in active:
            item["elapsed_ms"] = _int_ms((now - float(item.get("started_ts") or now)) * 1000)
            item["stage_elapsed_ms"] = _int_ms((now - float(item.get("stage_started_ts") or now)) * 1000)
        active.sort(key=lambda item: float(item.get("started_ts") or 0))
        completed_latest = list(reversed(completed))
        slow = sorted(
            completed,
            key=lambda item: max(
                _int_ms(item.get("duration_ms")),
                self._metric_value(item, "handler_queue_ms") * 4,
                self._metric_value(item, "stream_first_queue_ms") * 4,
                self._metric_value(item, "account_wait_ms") * 2,
            ),
            reverse=True,
        )
        return {
            "updated_at": beijing_now_str(),
            "threadpool": threadpool,
            "window": {
                "completed": len(completed),
                "completed_capacity": self._completed.maxlen,
                "events": len(events),
                "event_capacity": self._events.maxlen,
            },
            "summary": self._summary(active, completed),
            "active": [self._public_record(item) for item in active[:100]],
            "recent": [self._public_record(item) for item in completed_latest[:80]],
            "slow": [self._public_record(item) for item in slow[:50]],
            "events": list(reversed(events[-80:])),
            "metric_labels": METRIC_LABELS,
        }

    def _merge_stage_data(self, record: dict[str, Any], data: dict[str, Any]) -> None:
        metrics = record.setdefault("metrics", {})
        metric_data = {key: value for key, value in data.items() if key.endswith("_ms")}
        self._merge_metric_dict(metrics, metric_data)
        if data.get("account_email"):
            record["account_email"] = _mask_email(data.get("account_email"))
        if data.get("conversation_id"):
            record["conversation_id"] = str(data.get("conversation_id") or "")
        if data.get("model") and not record.get("model"):
            record["model"] = str(data.get("model") or "")
        for key in (
            "proxy_source",
            "proxy_hash",
            "egress_mode",
            "egress_key",
            "egress_label",
            "proxy_group_id",
            "proxy_node_id",
            "proxy_node_name",
            "image_egress_limit",
            "local_reason",
        ):
            if key in data:
                record[key] = str(data.get(key) or "")
        for key in RAW_DIAGNOSTIC_FIELDS:
            if key in data and data.get(key):
                record[key] = _trim_raw(data.get(key))
        if "has_proxy" in data:
            record["has_proxy"] = bool(data.get("has_proxy"))

        index = str(data.get("index") or "")
        if index:
            images = record.setdefault("images", {})
            image = images.setdefault(index, {"index": _int_ms(index), "metrics": {}})
            image["stage"] = str(record.get("stage") or "")
            image["stage_label"] = str(record.get("stage_label") or "")
            image["updated_at"] = str(record.get("updated_at") or "")
            if data.get("total"):
                image["total"] = _int_ms(data.get("total"))
            if data.get("status"):
                image["status"] = str(data.get("status") or "")
            if data.get("returned_result") is not None:
                image["returned_result"] = bool(data.get("returned_result"))
            if data.get("returned_message") is not None:
                image["returned_message"] = bool(data.get("returned_message"))
            for key in (
                "proxy_source",
                "proxy_hash",
                "egress_mode",
                "egress_key",
                "egress_label",
                "proxy_group_id",
                "proxy_node_id",
                "proxy_node_name",
                "image_egress_limit",
                "local_reason",
            ):
                if key in data:
                    image[key] = str(data.get(key) or "")
            for key in RAW_DIAGNOSTIC_FIELDS:
                if key in data and data.get(key):
                    image[key] = _trim_raw(data.get(key))
            if "has_proxy" in data:
                image["has_proxy"] = bool(data.get("has_proxy"))
            self._merge_metric_dict(image.setdefault("metrics", {}), metric_data)

    def _merge_metric_dict(self, target: dict[str, int], values: dict[str, Any]) -> None:
        for key, value in values.items():
            if not str(key).endswith("_ms"):
                continue
            ms = _int_ms(value)
            target[key] = max(_int_ms(target.get(key)), ms)

    def _summary(self, active: list[dict[str, Any]], completed: list[dict[str, Any]]) -> dict[str, Any]:
        success = sum(1 for item in completed if str(item.get("status") or "").lower() == "success")
        failed = len(completed) - success
        durations = [_int_ms(item.get("duration_ms")) for item in completed if _int_ms(item.get("duration_ms"))]
        metric_p95 = {key: self._percentile(self._metric_values(completed, key), 95) for key in METRIC_LABELS}
        bottleneck_key = max(
            (
                "handler_queue_ms",
                "stream_first_queue_ms",
                "account_wait_ms",
                "egress_wait_ms",
                "egress_acquire_ms",
                "upload_ms",
                "bootstrap_ms",
                "requirements_ms",
                "prepare_conversation_ms",
                "generation_start_ms",
                "http_ttfb_ms",
                "http_wait_ms",
                "sse_first_event_ms",
                "sse_max_gap_ms",
                "conversation_stream_ms",
                "stream_error_ms",
                "resolve_ms",
                "download_ms",
                "retry_wait_ms",
            ),
            key=lambda key: metric_p95.get(key, 0),
            default="",
        )
        if metric_p95.get(bottleneck_key, 0) <= 0:
            bottleneck_key = ""
        models = Counter(str(item.get("model") or "unknown") for item in completed if item.get("model"))
        active_models = Counter(str(item.get("model") or "unknown") for item in active if item.get("model"))
        active_egress = Counter(self._egress_label(item) for item in active)
        active_stages = Counter(self._active_stage_group(item) for item in active)
        return {
            "active": len(active),
            "completed": len(completed),
            "success": success,
            "failed": failed,
            "success_rate": round(success * 100 / len(completed), 1) if completed else 0,
            "avg_duration_ms": round(sum(durations) / len(durations)) if durations else 0,
            "p95_duration_ms": self._percentile(durations, 95),
            "metric_p95": metric_p95,
            "slow_counts": {
                "handler_queue": sum(1 for item in completed if self._metric_value(item, "handler_queue_ms") >= 1000),
                "stream_first_queue": sum(1 for item in completed if self._metric_value(item, "stream_first_queue_ms") >= 1000),
                "account_wait": sum(1 for item in completed if self._metric_value(item, "account_wait_ms") >= 5000),
                "egress_wait": sum(1 for item in completed if self._metric_value(item, "egress_wait_ms") >= 1000),
                "total_over_120s": sum(1 for item in completed if _int_ms(item.get("duration_ms")) >= 120000),
                "local_reject_or_busy": sum(1 for item in completed if self._is_local_reject_or_busy(item)),
            },
            "bottleneck": {
                "key": bottleneck_key,
                "label": METRIC_LABELS.get(bottleneck_key, ""),
                "value_ms": metric_p95.get(bottleneck_key, 0),
            },
            "by_model": dict(models.most_common(10)),
            "active_by_model": dict(active_models.most_common(10)),
            "active_by_egress": dict(active_egress.most_common(8)),
            "active_by_stage": dict(active_stages.most_common(10)),
        }

    def _metric_values(self, records: list[dict[str, Any]], key: str) -> list[int]:
        return [value for value in (self._metric_value(item, key) for item in records) if value > 0]

    def _metric_value(self, record: dict[str, Any], key: str) -> int:
        perf = record.get("perf") if isinstance(record.get("perf"), dict) else {}
        metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
        return max(_int_ms(perf.get(key)), _int_ms(metrics.get(key)))

    def _percentile(self, values: list[int], percentile: int) -> int:
        items = sorted(value for value in values if value > 0)
        if not items:
            return 0
        index = min(len(items) - 1, max(0, math.ceil(len(items) * percentile / 100) - 1))
        return items[index]

    def _is_local_reject_or_busy(self, record: dict[str, Any]) -> bool:
        if str(record.get("stage") or "") == "image_local_rejected":
            return True
        if str(record.get("local_reason") or ""):
            return True
        error = str(record.get("error") or "").lower()
        return any(pattern in error for pattern in LOCAL_REJECT_PATTERNS)

    def _egress_label(self, record: dict[str, Any]) -> str:
        source = str(record.get("proxy_source") or "direct").strip() or "direct"
        egress_label = str(record.get("egress_label") or "").strip()
        if (
            egress_label
            and egress_label != "direct"
            and egress_label != source
            and egress_label != f"{source}_profile"
            and not egress_label.startswith("proxy:")
        ):
            return f"{source}:{egress_label}"
        proxy_hash = str(record.get("proxy_hash") or "").strip()
        if proxy_hash and proxy_hash != "direct":
            return f"{source}:{proxy_hash}"
        return source

    def _active_stage_group(self, record: dict[str, Any]) -> str:
        stage = str(record.get("stage") or "").strip()
        if stage in ACTIVE_STAGE_GROUPS:
            return ACTIVE_STAGE_GROUPS[stage]
        return str(record.get("stage_label") or STAGE_LABELS.get(stage) or stage or "运行中")

    def _detail_diagnostic(self, record: dict[str, Any], events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        diagnostic: dict[str, Any] = {}
        for key in (
            "stage",
            "stage_label",
            "proxy_source",
            "proxy_hash",
            "egress_mode",
            "egress_key",
            "egress_label",
            "proxy_group_id",
            "proxy_node_id",
            "proxy_node_name",
            "image_egress_limit",
            "local_reason",
        ):
            value = str(record.get(key) or "").strip()
            if value:
                diagnostic[key] = value
        for key in RAW_DIAGNOSTIC_FIELDS:
            value = str(record.get(key) or "").strip()
            if value:
                diagnostic[key] = value
        if "has_proxy" in record:
            diagnostic["has_proxy"] = bool(record.get("has_proxy"))

        metrics = {
            key: _int_ms(value)
            for key, value in dict(record.get("metrics") or {}).items()
            if str(key).endswith("_ms") and _int_ms(value) > 0
        }
        perf_metrics = {
            key: _int_ms(value)
            for key, value in dict(record.get("perf") or {}).items()
            if str(key).endswith("_ms") and _int_ms(value) > 0
        }
        metrics.update({key: max(metrics.get(key, 0), value) for key, value in perf_metrics.items()})
        if metrics:
            diagnostic["metrics"] = metrics

        images = record.get("images")
        if isinstance(images, dict):
            image_items: dict[str, Any] = {}
            for key, value in images.items():
                if not isinstance(value, dict):
                    continue
                item: dict[str, Any] = {}
                for field in (
                    "index",
                    "total",
                    "stage",
                    "stage_label",
                    "status",
                    "returned_result",
                    "returned_message",
                    "proxy_source",
                    "proxy_hash",
                    "has_proxy",
                    "egress_mode",
                    "egress_key",
                    "egress_label",
                    "proxy_group_id",
                    "proxy_node_id",
                    "proxy_node_name",
                    "image_egress_limit",
                    "local_reason",
                    *RAW_DIAGNOSTIC_FIELDS,
                ):
                    if field in value and value[field] not in ("", None):
                        item[field] = value[field]
                image_metrics = {
                    metric_key: _int_ms(metric_value)
                    for metric_key, metric_value in dict(value.get("metrics") or {}).items()
                    if str(metric_key).endswith("_ms") and _int_ms(metric_value) > 0
                }
                if image_metrics:
                    item["metrics"] = image_metrics
                if item:
                    image_items[str(key)] = item
            if image_items:
                diagnostic["images"] = image_items

        if events:
            diagnostic["events"] = [
                {
                    key: value
                    for key, value in event.items()
                    if key in {"time", "event", "label", "index", "total", "status"}
                    or key in RAW_DIAGNOSTIC_FIELDS
                    or (str(key).endswith("_ms") and _int_ms(value) > 0)
                }
                for event in events
            ]

        return diagnostic

    def _public_record(self, record: dict[str, Any]) -> dict[str, Any]:
        item = self._copy_record(record)
        item.pop("started_ts", None)
        item.pop("stage_started_ts", None)
        return item

    def _copy_record(self, record: dict[str, Any]) -> dict[str, Any]:
        copied = dict(record)
        copied["metrics"] = dict(record.get("metrics") or {})
        copied["perf"] = dict(record.get("perf") or {})
        images = record.get("images")
        if isinstance(images, dict):
            copied["images"] = {
                key: {**value, "metrics": dict(value.get("metrics") or {})}
                for key, value in images.items()
                if isinstance(value, dict)
            }
        else:
            copied["images"] = {}
        return copied

    def _event(
        self,
        call_id: str,
        event: str,
        record: dict[str, Any],
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "time": beijing_now_str(),
            "call_id": call_id,
            "event": event,
            "label": STAGE_LABELS.get(event, event),
            "model": str(record.get("model") or data.get("model") if data else record.get("model") or ""),
        }
        if data:
            for key in (
                "index",
                "total",
                "status",
                "account_wait_ms",
                "egress_wait_ms",
                "upload_ms",
                "bootstrap_ms",
                "requirements_ms",
                "prepare_conversation_ms",
                "generation_start_ms",
                "http_dns_ms",
                "http_tcp_ms",
                "http_tls_ms",
                "http_wait_ms",
                "http_ttfb_ms",
                "http_total_ms",
                "sse_first_event_ms",
                "sse_max_gap_ms",
                "sse_last_gap_ms",
                "sse_stream_ms",
                "sse_event_count",
                "conversation_stream_ms",
                "stream_error_ms",
                "resolve_ms",
                "download_ms",
                "retry_wait_ms",
                "response_ms",
                "stream_ms",
                "total_ms",
                "proxy_source",
                "proxy_hash",
                "egress_key",
                "egress_label",
                "proxy_group_id",
                "proxy_node_id",
                "proxy_node_name",
                "image_egress_limit",
                "egress_mode",
                "has_proxy",
                "local_reason",
                *RAW_DIAGNOSTIC_FIELDS,
            ):
                if key in data:
                    payload[key] = _trim_raw(data[key], 1000) if key in RAW_DIAGNOSTIC_FIELDS else data[key]
        return payload


realtime_monitor_service = RealtimeMonitorService()
