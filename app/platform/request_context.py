"""Per-request values shared by embedded product adapters."""

from __future__ import annotations

import hashlib
from contextvars import ContextVar, Token
from typing import Any


_media_base_url: ContextVar[str] = ContextVar("media_base_url", default="")
_grok_log_trace: ContextVar[dict[str, Any] | None] = ContextVar(
    "grok_log_trace",
    default=None,
)


def set_media_base_url(value: str | None) -> Token[str]:
    return _media_base_url.set(str(value or "").rstrip("/"))


def reset_media_base_url(token: Token[str]) -> None:
    _media_base_url.reset(token)


def get_media_base_url() -> str:
    return _media_base_url.get()


def set_grok_log_trace(trace: dict[str, Any] | None = None) -> Token[dict[str, Any] | None]:
    return _grok_log_trace.set(trace if trace is not None else {})


def reset_grok_log_trace(token: Token[dict[str, Any] | None]) -> None:
    _grok_log_trace.reset(token)


def get_grok_log_trace() -> dict[str, Any] | None:
    return _grok_log_trace.get()


def _record_grok_metric(trace: dict[str, Any], key: str, value_ms: int) -> None:
    if value_ms <= 0:
        return
    metrics = trace.setdefault("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
        trace["metrics"] = metrics
    metrics[key] = max(int(metrics.get(key) or 0), value_ms)


def _record_grok_event(trace: dict[str, Any], event: str, **data: object) -> None:
    events = trace.setdefault("events", [])
    if not isinstance(events, list):
        events = []
        trace["events"] = events
    events.append(
        {
            "event": event,
            **{
                key: value
                for key, value in data.items()
                if value not in (None, "")
            },
        }
    )


def _notify_grok_progress(trace: dict[str, Any], step: str) -> None:
    callback = trace.get("progress_callback")
    if not callable(callback):
        return
    try:
        callback(step)
    except Exception:
        pass


def record_grok_account_selection(
    token: str,
    *,
    pool_id: int,
    mode_id: int,
    account_wait_ms: int,
) -> None:
    trace = get_grok_log_trace()
    normalized_token = str(token or "").strip()
    if trace is None or not normalized_token:
        return
    trace["account_token"] = normalized_token
    trace["account_pool_id"] = int(pool_id)
    trace["account_mode_id"] = int(mode_id)
    _record_grok_metric(trace, "account_wait_ms", account_wait_ms)
    _record_grok_event(
        trace,
        "image_getting_account",
        account_wait_ms=account_wait_ms,
    )
    _notify_grok_progress(trace, "image_egress_waiting")


def record_grok_proxy_lease(
    proxy_url: str | None,
    *,
    egress_mode: object,
    scope: object,
    kind: object,
    egress_acquire_ms: int,
) -> None:
    trace = get_grok_log_trace()
    if trace is None:
        return

    normalized_proxy = str(proxy_url or "").strip()
    mode = str(getattr(egress_mode, "value", egress_mode) or "direct").strip()
    scope_value = str(getattr(scope, "value", scope) or "app").strip()
    kind_value = str(getattr(kind, "value", kind) or "http").strip()
    proxy_hash = (
        hashlib.sha256(normalized_proxy.encode("utf-8")).hexdigest()[:12]
        if normalized_proxy
        else ""
    )
    egress_key = f"grok:{scope_value}:{proxy_hash or 'direct'}"
    proxy_detail: dict[str, object] = {
        "proxy_source": "grok_runtime",
        "has_proxy": bool(normalized_proxy),
        "egress_mode": mode,
        "egress_key": egress_key,
        "egress_label": f"Grok {scope_value} {'代理 ' + proxy_hash if proxy_hash else '直连'}",
        "proxy_scope": scope_value,
        "proxy_kind": kind_value,
    }
    if proxy_hash:
        proxy_detail["proxy_hash"] = proxy_hash
    existing_proxy = trace.get("proxy")
    existing_scope = (
        str(existing_proxy.get("proxy_scope") or "")
        if isinstance(existing_proxy, dict)
        else ""
    )
    primary_egress = scope_value == "app" or not existing_scope
    if primary_egress:
        trace["proxy"] = proxy_detail
    _record_grok_metric(trace, "egress_acquire_ms", egress_acquire_ms)
    if primary_egress:
        _record_grok_event(
            trace,
            "image_egress_ready",
            egress_acquire_ms=egress_acquire_ms,
            **proxy_detail,
        )
        _notify_grok_progress(trace, "image_egress_ready")


__all__ = [
    "get_grok_log_trace",
    "get_media_base_url",
    "record_grok_account_selection",
    "record_grok_proxy_lease",
    "reset_grok_log_trace",
    "reset_media_base_url",
    "set_grok_log_trace",
    "set_media_base_url",
]
