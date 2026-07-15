from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from services.config import DATA_DIR, config
from services.content_filter import request_text
from services.json_file import read_json_file, write_json_file
from services.log_service import LOG_TYPE_CALL, log_service
from services.protocol import openai_v1_image_edit, openai_v1_image_generations
from services.realtime_monitor_service import realtime_monitor_service
from services.register.grok_account_store import grok_account_store
from utils.diagnostics import exception_diagnostic_fields
from utils.timezone import beijing_from_timestamp, beijing_now_str

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_ERROR = "error"
TERMINAL_STATUSES = {TASK_STATUS_SUCCESS, TASK_STATUS_ERROR}
UNFINISHED_STATUSES = {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING}
GROK_IMAGE_EDIT_MODEL = "grok-imagine-image-edit"
TASK_DETAIL_KEYS = (
    "error_code",
    "raw_error",
    "upstream_error",
    "upstream_error_type",
    "upstream_request_id",
    "can_resume_poll",
    "raw_upstream_message",
    "raw_upstream_message_len",
    "raw_upstream_message_truncated",
    "upstream_message_preview",
    "upstream_message_len",
    "upstream_message_truncated",
    "tool_invoked",
    "terminal_message",
    "blocked",
    "diagnosis",
    "poll_attempts",
    "poll_timeout_secs",
    "stream_timeout_secs",
    "last_task_error",
)


def _copy_task_details(source: dict[str, Any], target: dict[str, Any]) -> None:
    for key in TASK_DETAIL_KEYS:
        value = source.get(key)
        if value in (None, ""):
            continue
        target[key] = value


def _task_detail_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if key in TASK_DETAIL_KEYS}


def _clear_task_details() -> dict[str, str]:
    return {key: "" for key in TASK_DETAIL_KEYS}


def _now_iso() -> str:
    return beijing_now_str()


def _timestamp(value: object) -> float:
    if not isinstance(value, str) or not value.strip():
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:26], fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _clean(value: object, default: str = "") -> str:
    return str(value or default).strip()


def _image_count(value: object) -> int:
    try:
        count = int(value or 1)
    except (TypeError, ValueError):
        count = 1
    return min(4, max(1, count))


def _owner_id(identity: dict[str, object]) -> str:
    return _clean(identity.get("id")) or "anonymous"


def _task_key(owner_id: str, task_id: str) -> str:
    return f"{owner_id}:{task_id}"


def _collect_image_urls(data: list[Any]) -> list[str]:
    urls: list[str] = []
    for item in data:
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def _grok_model_spec(model: str):
    from app.control.model import registry as model_registry

    return model_registry.get(_clean(model))


def _is_grok_image_task(model: str) -> bool:
    spec = _grok_model_spec(model)
    return bool(spec and (spec.is_image() or spec.is_image_edit()))


def _grok_task_timeout_secs() -> float:
    return float(max(config.image_stream_timeout_secs, config.image_poll_timeout_secs) + 60)


def _run_grok_generation(payload: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    from services.grok_runtime import grok_runtime

    return grok_runtime.run_sync(
        lambda: grok_runtime.generate_image(payload, trace=trace),
        timeout=_grok_task_timeout_secs(),
    )


def _run_grok_edit(payload: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    from services.grok_runtime import grok_runtime

    return grok_runtime.run_sync(
        lambda: grok_runtime.edit_image(payload, trace=trace),
        timeout=_grok_task_timeout_secs(),
    )


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": task.get("id"),
        "status": task.get("status"),
        "mode": task.get("mode"),
        "model": task.get("model"),
        "n": _image_count(task.get("n")),
        "size": task.get("size"),
        "quality": task.get("quality"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }
    if task.get("conversation_id"):
        item["conversation_id"] = task.get("conversation_id")
    if task.get("data") is not None:
        item["data"] = task.get("data")
    if task.get("usage") is not None:
        item["usage"] = task.get("usage")
    if task.get("error"):
        item["error"] = task.get("error")
    _copy_task_details(task, item)
    if task.get("progress"):
        item["progress"] = task.get("progress")
    if task.get("duration_ms") is not None:
        item["duration_ms"] = task.get("duration_ms")
    if task.get("status") in (TASK_STATUS_RUNNING, TASK_STATUS_QUEUED):
        if task.get("status") == TASK_STATUS_RUNNING:
            # RUNNING 状态仅在 started_ts 被设置后（image_stream_resolve_start）才计时
            base_ts = task.get("started_ts")
        else:
            # QUEUED 状态从 created_ts 开始计时（排队等待中）
            base_ts = task.get("created_ts") or task.get("updated_ts")
        if base_ts:
            item["elapsed_secs"] = round(time.time() - base_ts, 1)
    return item


class ImageTaskService:
    def __init__(
        self,
        path: Path,
        *,
        generation_handler: Callable[[dict[str, Any]], dict[str, Any]] = openai_v1_image_generations.handle,
        edit_handler: Callable[[dict[str, Any]], dict[str, Any]] = openai_v1_image_edit.handle,
        grok_generation_handler: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] = _run_grok_generation,
        grok_edit_handler: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] = _run_grok_edit,
        retention_days_getter: Callable[[], int] | None = None,
    ):
        self.path = path
        self.generation_handler = generation_handler
        self.edit_handler = edit_handler
        self.grok_generation_handler = grok_generation_handler
        self.grok_edit_handler = grok_edit_handler
        self.retention_days_getter = retention_days_getter or (lambda: config.image_retention_days)
        self._lock = threading.RLock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._tasks = self._load_locked()
            changed = self._recover_unfinished_locked()
            changed = self._cleanup_locked() or changed
            if changed:
                self._save_locked()

    def submit_generation(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        n: int = 1,
        size: str | None = None,
        quality: str = "auto",
        base_url: str = "",
    ) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "model": model,
            "n": _image_count(n),
            "size": size,
            "quality": quality,
            "response_format": "url",
            "base_url": base_url,
        }
        return self._submit(identity, client_task_id=client_task_id, mode="generate", payload=payload)

    def submit_edit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        n: int = 1,
        size: str | None = None,
        quality: str = "auto",
        base_url: str = "",
        images: list[tuple[bytes, str, str]] | None = None,
        masks: list[tuple[bytes, str, str]] | None = None,
    ) -> dict[str, Any]:
        selected_model = _clean(model, "gpt-image-2")
        spec = _grok_model_spec(selected_model)
        if spec and spec.is_image():
            selected_model = GROK_IMAGE_EDIT_MODEL
            spec = _grok_model_spec(selected_model)
        grok_edit = bool(spec and spec.is_image_edit())
        payload = {
            "prompt": prompt,
            "images": images or [],
            "mask": masks or [],
            "model": selected_model,
            "n": min(2, _image_count(n)) if grok_edit else _image_count(n),
            "size": "1024x1024" if grok_edit else size,
            "quality": quality,
            "response_format": "url",
            "base_url": base_url,
        }
        return self._submit(identity, client_task_id=client_task_id, mode="edit", payload=payload)

    def list_tasks(self, identity: dict[str, object], task_ids: list[str]) -> dict[str, Any]:
        owner = _owner_id(identity)
        requested_ids = [_clean(task_id) for task_id in task_ids if _clean(task_id)]
        with self._lock:
            if self._cleanup_locked():
                self._save_locked()
            items = []
            missing_ids = []
            for task_id in requested_ids:
                task = self._tasks.get(_task_key(owner, task_id))
                if task is None:
                    missing_ids.append(task_id)
                else:
                    items.append(_public_task(task))
            if not requested_ids:
                items = [
                    _public_task(task)
                    for task in self._tasks.values()
                    if task.get("owner_id") == owner
                ]
                items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
                missing_ids = []
            return {"items": items, "missing_ids": missing_ids}

    def _submit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        mode: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = _clean(client_task_id)
        if not task_id:
            raise ValueError("client_task_id is required")
        owner = _owner_id(identity)
        key = _task_key(owner, task_id)
        now = _now_iso()
        should_start = False
        with self._lock:
            cleaned = self._cleanup_locked()
            task = self._tasks.get(key)
            if task is not None:
                if cleaned:
                    self._save_locked()
                return _public_task(task)
            task = {
                "id": task_id,
                "owner_id": owner,
                "status": TASK_STATUS_QUEUED,
                "mode": mode,
                "model": _clean(payload.get("model"), "gpt-image-2"),
                "n": _image_count(payload.get("n")),
                "size": _clean(payload.get("size")),
                "quality": _clean(payload.get("quality"), "auto"),
                "created_at": now,
                "updated_at": now,
                "created_ts": time.time(),
            }
            self._tasks[key] = task
            self._save_locked()
            should_start = True

        if should_start:
            thread = threading.Thread(
                target=self._run_task,
                args=(key, mode, payload, dict(identity), _clean(payload.get("model"), "gpt-image-2")),
                name=f"image-task-{task_id[:16]}",
                daemon=True,
            )
            thread.start()
        return _public_task(task)

    def _run_task(
        self,
        key: str,
        mode: str,
        payload: dict[str, Any],
        identity: dict[str, object],
        model: str,
    ) -> None:
        started = time.time()
        call_id = uuid4().hex[:16]
        endpoint = "/v1/images/edits" if mode == "edit" else "/v1/images/generations"
        summary_prefix = "图生图" if mode == "edit" else "文生图"
        perf_timings: dict[str, int] = {"handler_queue_ms": 0}
        grok_trace: dict[str, Any] = {}
        grok_account_email = ""
        grok_log_extra: dict[str, Any] = {}
        realtime_monitor_service.start(
            call_id,
            endpoint=endpoint,
            model=model,
            summary=summary_prefix,
            role=str(identity.get("role") or ""),
            key_name=str(identity.get("name") or ""),
        )
        self._update_task(key, status=TASK_STATUS_RUNNING, error="")
        # 创建进度回调，每个步骤完成后更新任务状态
        def progress_callback(step: str) -> None:
            if step == "image_stream_resolve_start":
                self._update_task(key, started_ts=time.time())
            self._update_task(key, progress=step)
        # 将进度回调添加到 payload 中（handler 会提取并传递给 ConversationRequest）
        payload_with_progress = {
            **payload,
            "progress_callback": progress_callback,
            "_call_id": call_id,
            "_trace_image_perf": True,
        }
        if _is_grok_image_task(model):
            grok_trace["progress_callback"] = progress_callback
            self._update_task(key, progress="getting_account")
        handler_started = time.perf_counter()
        realtime_monitor_service.stage(
            call_id,
            "handler_started",
            handler_queue_ms=0,
            endpoint=endpoint,
            model=model,
        )
        try:
            if _is_grok_image_task(model):
                handler = self.grok_edit_handler if mode == "edit" else self.grok_generation_handler
                result = handler(payload_with_progress, grok_trace)
            else:
                handler = self.edit_handler if mode == "edit" else self.generation_handler
                result = handler(payload_with_progress)
            perf_timings["handler_exec_ms"] = int((time.perf_counter() - handler_started) * 1000)
            if _is_grok_image_task(model):
                perf_timings["conversation_stream_ms"] = max(1, perf_timings["handler_exec_ms"])
            grok_account_email, grok_log_extra = self._apply_grok_trace(
                call_id,
                grok_trace,
                perf_timings,
            )
            if not isinstance(result, dict):
                raise RuntimeError("image task returned streaming result unexpectedly")
            data = result.get("data")
            account_email = _clean(
                result.get("_account_email")
                or result.get("account_email")
                or grok_account_email
            )
            if not isinstance(data, list) or not data:
                upstream = _clean(result.get("message"))
                if upstream:
                    message = upstream
                else:
                    message = "号池中没有可用账号或所有账号均被限流，请检查号池状态（账号额度、是否被封禁、是否到达生图上限）"
                error = RuntimeError(message)
                if account_email:
                    setattr(error, "account_email", account_email)
                raise error
            usage = result.get("usage")
            duration_ms = int((time.time() - started) * 1000)
            self._update_task(
                key,
                status=TASK_STATUS_SUCCESS,
                data=data,
                usage=usage,
                error="",
                duration_ms=duration_ms,
                **_clear_task_details(),
            )
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用完成",
                request_preview=request_text(payload.get("prompt")),
                urls=_collect_image_urls(data),
                account_email=account_email,
                call_id=call_id,
                perf=perf_timings,
                extra=grok_log_extra,
            )
        except Exception as exc:
            perf_timings["handler_exec_ms"] = int((time.perf_counter() - handler_started) * 1000)
            if _is_grok_image_task(model):
                perf_timings["conversation_stream_ms"] = max(1, perf_timings["handler_exec_ms"])
            traced_email, traced_extra = self._apply_grok_trace(
                call_id,
                grok_trace,
                perf_timings,
            )
            grok_account_email = grok_account_email or traced_email
            grok_log_extra = {**grok_log_extra, **traced_extra}
            error_message = str(exc) or "image task failed"
            account_email = _clean(getattr(exc, "account_email", "") or grok_account_email)
            conversation_id = _clean(getattr(exc, "conversation_id", ""))
            error_details = exception_diagnostic_fields(exc)
            duration_ms = int((time.time() - started) * 1000)
            self._update_task(key, status=TASK_STATUS_ERROR, error=error_message, data=[],
                              duration_ms=duration_ms,
                              **({"conversation_id": conversation_id} if conversation_id else {}),
                              **_task_detail_fields(error_details))
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用失败",
                request_preview=request_text(payload.get("prompt")),
                status="failed",
                error=error_message,
                account_email=account_email,
                conversation_id=conversation_id,
                call_id=call_id,
                perf=perf_timings,
                extra={**grok_log_extra, **error_details},
            )

    def _apply_grok_trace(
        self,
        call_id: str,
        trace: dict[str, Any],
        perf_timings: dict[str, int],
    ) -> tuple[str, dict[str, Any]]:
        if not trace:
            return "", {}

        metrics = trace.get("metrics")
        if isinstance(metrics, dict):
            for key, value in metrics.items():
                if not str(key).endswith("_ms"):
                    continue
                try:
                    value_ms = max(0, int(value or 0))
                except (TypeError, ValueError):
                    continue
                if value_ms:
                    perf_timings[str(key)] = max(
                        int(perf_timings.get(str(key)) or 0),
                        value_ms,
                    )

        events = trace.get("events")
        if isinstance(events, list):
            flushed = int(trace.get("_flushed_events") or 0)
            for item in events[flushed:]:
                if not isinstance(item, dict):
                    continue
                event = _clean(item.get("event"))
                if not event:
                    continue
                realtime_monitor_service.stage(
                    call_id,
                    event,
                    **{
                        str(key): value
                        for key, value in item.items()
                        if key != "event" and value not in (None, "")
                    },
                )
            trace["_flushed_events"] = len(events)

        identity = grok_account_store.runtime_identity_for_token(
            _clean(trace.get("account_token"))
        )
        account_email = _clean(identity.get("account_email"))
        account_id = _clean(identity.get("account_id"))
        extra: dict[str, Any] = {}
        if account_email or account_id:
            extra["provider"] = "grok_sso"
        if account_id:
            extra["provider_account_id"] = account_id

        proxy = trace.get("proxy")
        if isinstance(proxy, dict):
            for key in (
                "proxy_source",
                "proxy_hash",
                "has_proxy",
                "egress_mode",
                "egress_key",
                "egress_label",
                "proxy_scope",
                "proxy_kind",
            ):
                if key in proxy and proxy[key] not in (None, ""):
                    extra[key] = proxy[key]
        return account_email, extra

    def _log_call(
        self,
        identity: dict[str, object],
        mode: str,
        model: str,
        started: float,
        suffix: str,
        *,
        request_preview: str = "",
        status: str = "success",
        error: str = "",
        urls: list[str] | None = None,
        account_email: str = "",
        conversation_id: str = "",
        call_id: str = "",
        perf: dict[str, int] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        endpoint = "/v1/images/edits" if mode == "edit" else "/v1/images/generations"
        summary_prefix = "图生图" if mode == "edit" else "文生图"
        detail = {
            "key_id": identity.get("id"),
            "key_name": identity.get("name"),
            "role": identity.get("role"),
            "endpoint": endpoint,
            "model": model,
            "call_id": call_id,
            "started_at": beijing_from_timestamp(started),
            "ended_at": _now_iso(),
            "duration_ms": int((time.time() - started) * 1000),
            "status": status,
        }
        if perf:
            detail["perf"] = dict(perf)
        if request_preview:
            detail["request_text"] = request_preview
        if error:
            detail["error"] = error
        if extra:
            for key, value in extra.items():
                if value in (None, ""):
                    continue
                detail[key] = value
        if account_email:
            detail["account_email"] = account_email
        if conversation_id:
            detail["conversation_id"] = conversation_id
        if urls:
            detail["urls"] = list(dict.fromkeys(urls))
        try:
            if call_id:
                realtime_monitor_service.finish(detail)
            log_service.add(LOG_TYPE_CALL, f"{summary_prefix}{suffix}", detail)
        except Exception:
            pass

    def _update_task(self, key: str, **updates: Any) -> None:
        with self._lock:
            task = self._tasks.get(key)
            if task is None:
                return
            for field, value in updates.items():
                if field in TASK_DETAIL_KEYS and value in (None, ""):
                    task.pop(field, None)
                    continue
                task[field] = value
            task["updated_at"] = _now_iso()
            task["updated_ts"] = time.time()
            self._save_locked()

    def _load_locked(self) -> dict[str, dict[str, Any]]:
        raw = read_json_file(
            self.path,
            name=self.path.name,
            default_factory=dict,
            expected_types=(dict, list),
        )
        raw_items = raw.get("tasks") if isinstance(raw, dict) else raw
        if not isinstance(raw_items, list):
            return {}
        tasks: dict[str, dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            task_id = _clean(item.get("id"))
            owner = _clean(item.get("owner_id"))
            if not task_id or not owner:
                continue
            status = _clean(item.get("status"))
            if status not in {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING, TASK_STATUS_SUCCESS, TASK_STATUS_ERROR}:
                status = TASK_STATUS_ERROR
            task = {
                "id": task_id,
                "owner_id": owner,
                "status": status,
                "mode": "edit" if item.get("mode") == "edit" else "generate",
                "model": _clean(item.get("model"), "gpt-image-2"),
                "n": _image_count(item.get("n")),
                "size": _clean(item.get("size")),
                "quality": _clean(item.get("quality"), "auto"),
                "created_at": _clean(item.get("created_at"), _now_iso()),
                "updated_at": _clean(item.get("updated_at"), _clean(item.get("created_at"), _now_iso())),
                "created_ts": item.get("created_ts"),
                "updated_ts": item.get("updated_ts"),
                "started_ts": item.get("started_ts"),
                "duration_ms": item.get("duration_ms"),
            }
            data = item.get("data")
            if isinstance(data, list):
                task["data"] = data
            usage = item.get("usage")
            if isinstance(usage, dict):
                task["usage"] = usage
            error = _clean(item.get("error"))
            if error:
                task["error"] = error
            _copy_task_details(item, task)
            tasks[_task_key(owner, task_id)] = task
        return tasks

    def _save_locked(self) -> None:
        items = sorted(self._tasks.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        write_json_file(self.path, {"tasks": items})

    def _recover_unfinished_locked(self) -> bool:
        changed = False
        for task in self._tasks.values():
            if task.get("status") in UNFINISHED_STATUSES:
                task["status"] = TASK_STATUS_ERROR
                task["error"] = "服务已重启，未完成的图片任务已中断"
                task["updated_at"] = _now_iso()
                changed = True
        return changed

    def _cleanup_locked(self) -> bool:
        try:
            retention_days = max(1, int(self.retention_days_getter()))
        except Exception:
            retention_days = 30
        cutoff = time.time() - retention_days * 86400
        removed_keys = [
            key
            for key, task in self._tasks.items()
            if task.get("status") in TERMINAL_STATUSES and _timestamp(task.get("updated_at")) < cutoff
        ]
        for key in removed_keys:
            self._tasks.pop(key, None)
        return bool(removed_keys)

    def resume_poll(
        self,
        identity: dict[str, object],
        task_id: str,
        extra_timeout_secs: float = 30.0,
    ) -> dict[str, Any]:
        """恢复对已超时任务的轮询，额外等待 extra_timeout_secs 秒。"""
        owner = _owner_id(identity)
        key = _task_key(owner, _clean(task_id))
        with self._lock:
            task = self._tasks.get(key)
            if task is None:
                raise ValueError("task not found")
            if task.get("status") != TASK_STATUS_ERROR:
                raise ValueError("task is not in error state")
            error_msg = _clean(task.get("error"))
            if "超时" not in error_msg:
                raise ValueError("task error is not a timeout error")
            conversation_id = _clean(task.get("conversation_id"))
            if not conversation_id:
                raise ValueError("task has no conversation_id")
            mode = task.get("mode", "generate")
            model = task.get("model", "gpt-image-2")
            # 将任务状态重置为 running
            self._update_task(key, status=TASK_STATUS_RUNNING, error="", **_clear_task_details())

        # 启动新线程继续轮询
        thread = threading.Thread(
            target=self._run_resume_poll,
            args=(key, conversation_id, extra_timeout_secs, dict(identity), mode, model),
            name=f"image-resume-{_clean(task_id)[:16]}",
            daemon=True,
        )
        thread.start()
        return _public_task(task)

    def _run_resume_poll(
        self,
        key: str,
        conversation_id: str,
        extra_timeout_secs: float,
        identity: dict[str, object],
        mode: str,
        model: str,
    ) -> None:
        """后台线程：继续轮询已有 conversation_id 的图片结果。"""
        started = time.time()
        backend = None
        try:
            from services.openai_backend_api import OpenAIBackendAPI
            from services.protocol.conversation import format_image_result

            backend = OpenAIBackendAPI()
            file_ids, sediment_ids = backend._poll_image_results(
                conversation_id,
                extra_timeout_secs,
            )
            if not file_ids and not sediment_ids:
                raise RuntimeError(
                    f"继续等待 {extra_timeout_secs} 秒后仍未找到图片结果。"
                )

            image_urls = backend.resolve_conversation_image_urls(
                conversation_id, file_ids, sediment_ids, poll=False,
            )
            if not image_urls:
                raise RuntimeError("图片 URL 解析失败")

            image_items = [
                {"b64_json": __import__("base64").b64encode(image_data).decode("ascii")}
                for image_data in backend.download_image_bytes(image_urls)
            ]
            # 获取 task 的原始 prompt（从 _public_task 的 mode 判断）
            with self._lock:
                task = self._tasks.get(key)
                quality = _clean(task.get("quality"), "auto") if task else "auto"
                size = _clean(task.get("size")) if task else None
            data = format_image_result(
                image_items,
                "",  # prompt 已不重要，结果已经拿到了
                "b64_json",
                "",
                int(time.time()),
            )["data"]
            self._update_task(
                key,
                status=TASK_STATUS_SUCCESS,
                data=data,
                error="",
                duration_ms=int((time.time() - started) * 1000),
                **_clear_task_details(),
            )
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用完成（续轮询）",
                status="success",
                urls=_collect_image_urls(data),
            )
        except Exception as exc:
            error_message = str(exc) or "resume poll failed"
            error_details = exception_diagnostic_fields(exc)
            duration_ms = int((time.time() - started) * 1000)
            self._update_task(
                key,
                status=TASK_STATUS_ERROR,
                error=error_message,
                data=[],
                duration_ms=duration_ms,
                **_task_detail_fields(error_details),
            )
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用失败（续轮询）",
                status="failed",
                error=error_message,
                extra=error_details,
            )
        finally:
            if backend is not None:
                backend.close()


image_task_service = ImageTaskService(DATA_DIR / "image_tasks.json")
