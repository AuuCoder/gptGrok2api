from __future__ import annotations

import asyncio
import hashlib
import io
import json
import random
import threading
import time
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from services.account_service import account_service
from services import cpa_service
from services.config import DATA_DIR
from services.cpa_service import normalize_cpa_delivery_config
from services.json_file import read_json_object, write_json_file
from services.openai_checkout_service import CheckoutSessionError, openai_checkout_service
from services.proxy_service import normalize_proxy_url_list
from services.register import mail_provider, openai_register
from services.register.grok_account_store import grok_account_store
from services.sub2api_service import build_xai_oauth_export_account, normalize_sync_config
from services.xai_oauth_delivery_service import (
    DEFAULT_XAI_OAUTH_DELIVERY_CONFIG,
    normalize_xai_oauth_delivery_config,
)
from services.xai_cli_oauth_store import xai_cli_oauth_store


REGISTER_FILE = DATA_DIR / "register.json"
REGISTER_TARGETS = {"openai", "grok"}
CHECKOUT_CHANNELS = {"upi", "pix"}
DEFAULT_GROK_PROBE_CONFIG = {
    "enabled": False,
    "interval_minutes": 60,
    "batch_size": 50,
    "last_started_at": "",
    "last_finished_at": "",
    "oauth_recovery_last_sweep_at": "",
    "last_result": {},
    "last_error": "",
    "events": [],
}
DEFAULT_GROK_CONFIG = {
    "signup_flow": "xconsole",
    "max_mail_retries": 3,
    "provider": "yescaptcha",
    "api_key": "",
    "api_base": "",
    "action": "",
    "sitekey": "",
    "action_id": "",
    "base_url": "https://accounts.x.ai",
    "request_timeout": 30,
    "captcha_timeout": 180,
    "captcha_poll_interval": 3,
    "local_concurrency": 2,
    "local_attempt_timeout": 45,
    "local_queue_timeout": 60,
    "local_max_attempts": 3,
    "castle_timeout": 20,
    "castle_pk": "",
    "castle_sdk_url": "",
    "next_router_state_tree": "",
    "create_path": "/createTask",
    "result_path": "/getTaskResult",
    "custom_headers": {},
    "xai_cli_oauth_enabled": True,
    "xai_cli_oauth_flow": "device",
    "xai_cli_pkce_reference_dir": "",
    "oauth_delivery": DEFAULT_XAI_OAUTH_DELIVERY_CONFIG,
    "grok2api_enabled": True,
    "grok2api_api_base": "",
    "grok2api_admin_key": "",
    "grok2api_pool": "auto",
    "grok2api_auto_nsfw": False,
    "grok2api_verify_on_import": True,
    "grok2api_timeout": 30,
    "probe_scheduler": DEFAULT_GROK_PROBE_CONFIG,
}

_GROK_QUOTA_MODES = ("auto", "fast", "expert", "heavy", "console")
_GROK_RECOVERY_BASE_DELAY_MINUTES = 60
_GROK_RECOVERY_MAX_DELAY_MINUTES = 24 * 60
_GROK_OAUTH_RECOVERY_STALE_MINUTES = 24 * 60
_GROK_PERMISSION_RETRY_DELAY_SECONDS = 15 * 60
_GROK_PERMISSION_RETRY_BATCH_SIZE = 3
_GROK_PENDING_STATUSES = {
    "submitting",
    "pending_submit",
    "pending_sso",
    "submission_unknown",
    "submission_unconfirmed",
}
_GROK_FAILED_STATUSES = {"submission_failed"}


class _GrokProbeCancelled(RuntimeError):
    pass


class _GrokProbeStopSignal:
    def __init__(self, *events: threading.Event | None):
        self._events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


def _raise_if_grok_probe_cancelled(stop_event: Any | None) -> None:
    if stop_event is not None and stop_event.is_set():
        raise _GrokProbeCancelled()


async def _await_grok_probe_with_cancel(
    awaitable: Awaitable[Any],
    stop_event: Any | None,
) -> Any:
    task = asyncio.create_task(awaitable)
    try:
        while not task.done():
            _raise_if_grok_probe_cancelled(stop_event)
            await asyncio.wait({task}, timeout=0.1)
        return await task
    except BaseException:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise


class GrokAccountChatTestError(RuntimeError):
    """A safe, operator-facing failure from one explicit Grok chat test."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        category: str = "failed",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.category = category if category in {"blocked", "invalid", "limited", "permission", "failed"} else "failed"


def _serialize_outlook_pool(credentials: list[dict]) -> str:
    return "\n".join(
        f'{c["email"]}----{c.get("password", "")}----{c["client_id"]}----{c["refresh_token"]}' for c in credentials
    )


def _checkout_channel(value: object) -> str:
    channel = _clean_text(value).lower()
    return channel if channel in CHECKOUT_CHANNELS else "upi"


def _checkout_market(channel: str) -> tuple[str, str]:
    return ("BR", "BRL") if channel == "pix" else ("IN", "INR")


def _merge_outlook_pool(old_text: str, new_text: str) -> str:
    """合并已存邮箱池与新导入文本，按邮箱去重，新导入的同名邮箱覆盖旧凭据。"""
    merged: dict[str, dict] = {}
    for credential in mail_provider.parse_outlook_credentials(old_text or ""):
        merged[credential["email"].strip().lower()] = credential
    for credential in mail_provider.parse_outlook_credentials(new_text or ""):
        merged[credential["email"].strip().lower()] = credential
    return _serialize_outlook_pool(list(merged.values()))


def _outlook_credential_changed(old: dict | None, new: dict) -> bool:
    if not old:
        return False
    for key in ("password", "client_id", "refresh_token"):
        if str(old.get(key) or "") != str(new.get(key) or ""):
            return True
    return False


def _safe_bool(value: object, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return fallback


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _safe_int(value: object, fallback: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return fallback


def _normalize_bridge_sso(value: object) -> str:
    token = _clean_text(value)
    if token.lower().startswith("sso="):
        token = token[4:].strip()
    return "" if ";" in token else token


def _token_preview(value: object) -> str:
    token = _normalize_bridge_sso(value)
    if not token:
        return ""
    if len(token) <= 12:
        return "********"
    return f"{token[:6]}...{token[-4:]}"


def _runtime_result_key(value: object) -> str:
    token = _normalize_bridge_sso(value)
    if len(token) > 20:
        return f"{token[:8]}...{token[-8:]}"
    return token


def _quota_brief(value: object) -> dict[str, dict[str, int]]:
    source_quota = value if isinstance(value, dict) else {}
    result: dict[str, dict[str, int]] = {}
    for mode in _GROK_QUOTA_MODES:
        item = source_quota.get(mode)
        if not isinstance(item, dict):
            continue
        quota = {
            "remaining": max(0, _safe_int(item.get("remaining"))),
            "total": max(0, _safe_int(item.get("total"))),
        }
        if mode == "console":
            reset_at = _safe_int(item.get("reset_at"))
            if reset_at > 0:
                quota["reset_at"] = reset_at
            source_value = _safe_int(item.get("source"), -1)
            if source_value in {0, 1, 2}:
                quota["source"] = source_value
        result[mode] = quota
    return result


def _verify_quota_brief(value: object) -> dict[str, int] | None:
    """Return the only quota fields safe and useful for a login-state probe."""
    if not isinstance(value, dict):
        return None
    return {
        "remaining": max(0, _safe_int(value.get("remaining"))),
        "total": max(0, _safe_int(value.get("total"))),
    }


def _runtime_status_bucket(value: object) -> str:
    status = _clean_text(value).lower()
    if status == "active":
        return "active"
    if status == "cooling":
        return "cooling"
    if status == "disabled":
        return "disabled"
    return "invalid"


def _grok_oauth_display_status(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "unauthorized"
    probe = value.get("probe") if isinstance(value.get("probe"), dict) else {}
    probe_code = _clean_text(probe.get("code")).lower()
    if probe_code == "personal-team-blocked" or probe_code.startswith("personal-team-blocked:"):
        return "no_quota"
    status = _clean_text(value.get("status")).lower()
    if status == "expired":
        return "expired"
    if status in {"disabled", "invalid"}:
        return "invalid"
    probe_status = _clean_text(probe.get("status")).lower()
    if probe_status == "valid":
        return "normal"
    if probe_status == "limited":
        return "limited"
    if probe_status in {"invalid", "unknown"}:
        return "invalid"
    return "normal" if status == "active" else "invalid"


def _batch_summary(payload: object, total: int) -> dict[str, int]:
    data = payload if isinstance(payload, dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else data
    ok = max(0, _safe_int(summary.get("ok")))
    fail = max(0, _safe_int(summary.get("fail")))
    reported_total = max(0, _safe_int(summary.get("total"), total))
    if reported_total <= 0:
        reported_total = max(total, ok + fail)
    return {"total": reported_total, "ok": ok, "fail": fail}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: object) -> datetime | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_grok_probe_config(value: object) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result_source = source.get("last_result") if isinstance(source.get("last_result"), dict) else {}
    events_source = source.get("events") if isinstance(source.get("events"), list) else []
    events: list[dict[str, str]] = []
    for raw in events_source[-50:]:
        if not isinstance(raw, dict):
            continue
        message = _clean_text(raw.get("message"))[:500]
        if not message:
            continue
        level = _clean_text(raw.get("level")).lower()
        events.append(
            {
                "id": _clean_text(raw.get("id")) or uuid.uuid4().hex,
                "at": _clean_text(raw.get("at")) or _now(),
                "level": level if level in {"info", "success", "warning", "error"} else "info",
                "message": message,
            }
        )
    return {
        "enabled": _safe_bool(source.get("enabled"), False),
        "interval_minutes": max(5, min(10_080, _safe_int(source.get("interval_minutes"), 60) or 60)),
        "batch_size": max(1, min(500, _safe_int(source.get("batch_size"), 50) or 50)),
        "last_started_at": _clean_text(source.get("last_started_at")),
        "last_finished_at": _clean_text(source.get("last_finished_at")),
        "oauth_recovery_last_sweep_at": _clean_text(source.get("oauth_recovery_last_sweep_at")),
        "last_result": {
            key: max(0, _safe_int(result_source.get(key)))
            for key in (
                "eligible",
                "skipped",
                "total",
                "valid",
                "invalid",
                "unknown",
                "batches",
                "recovery_attempted",
                "recovery_succeeded",
                "recovery_failed",
                "recovery_deferred",
                "oauth_eligible",
                "oauth_skipped",
                "oauth_total",
                "oauth_valid",
                "oauth_limited",
                "oauth_invalid",
                "oauth_unknown",
                "oauth_batches",
                "oauth_recovery_attempted",
                "oauth_recovery_queued",
                "oauth_recovery_failed",
                "oauth_recovery_deferred",
                "oauth_backfill_eligible",
                "oauth_backfill_attempted",
                "oauth_backfill_queued",
                "oauth_backfill_reused",
                "oauth_backfill_failed",
                "oauth_backfill_deferred",
                "oauth_backfill_missing_credentials",
            )
        }
        if result_source
        else {},
        "last_error": _clean_text(source.get("last_error"))[:500],
        "events": events,
    }


def _checkout_task_text(value: object, *, limit: int = 160) -> str:
    """Return one compact display field for the non-secret task table."""
    text = _clean_text(value).replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())[:limit]


def _checkout_task_payment_link(value: object) -> str:
    """Only expose a user-actionable HTTPS link, never a proxy URL."""
    link = _checkout_task_text(value, limit=4_096)
    if not link:
        return ""
    try:
        parsed = urlparse(link)
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            return ""
    except (TypeError, ValueError):
        return ""
    return link


def _checkout_task_stage(value: object) -> str:
    stage = _checkout_task_text(value, limit=32).lower()
    return stage if stage in {
        "checkout",
        "checkout_created",
        "checkout_update",
        "queued",
        "retrying",
        "stripe",
        "stripe_bootstrap",
        "stripe_init",
        "stripe_provider",
        "stripe_elements",
        "stripe_tax",
        "stripe_token",
        "billing",
        "snapshot",
        "promotion",
        "payment_method",
        "confirm",
        "confirm_retry",
        "approve",
        "poll",
        "extract",
        "final_validate",
        "completed",
        "failed",
        "cancelled",
    } else "checkout"


def _registration_backend(target: str):
    if str(target or "openai").strip().lower() == "grok":
        from services.register import grok_register

        return grok_register
    return openai_register


def _provider_id(provider: dict) -> str:
    return str(provider.get("id") or provider.get("provider_id") or "").strip()


def _ensure_provider_id(provider: dict) -> str:
    provider_id = _provider_id(provider)
    if provider_id:
        provider["id"] = provider_id
        provider.pop("provider_id", None)
        return provider_id
    provider_id = f"provider-{uuid.uuid4().hex[:12]}"
    provider["id"] = provider_id
    return provider_id


def _outlook_mailbox_id(provider_id: str, email: str) -> str:
    source = f"{str(provider_id).strip()}\0{str(email).strip().lower()}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def _default_config() -> dict:
    return {
        **openai_register.config,
        "target": "openai",
        "grok": dict(DEFAULT_GROK_CONFIG),
        "mode": "total",
        "target_quota": 100,
        "target_available": 10,
        "check_interval": 5,
        "enabled": False,
        "stats": {
            "success": 0,
            "fail": 0,
            "done": 0,
            "running": 0,
            "threads": openai_register.config["threads"],
            "elapsed_seconds": 0,
            "avg_seconds": 0,
            "success_rate": 0,
            "current_quota": 0,
            "current_available": 0,
        },
    }


def _normalize(raw: dict) -> dict:
    cfg = _default_config()
    cfg.update({k: v for k, v in raw.items() if k not in {"stats", "logs"}})
    target = str(cfg.get("target") or "openai").strip().lower()
    cfg["target"] = target if target in REGISTER_TARGETS else "openai"
    cfg["total"] = max(1, int(cfg.get("total") or 1))
    cfg["threads"] = max(1, int(cfg.get("threads") or 1))
    cfg["mode"] = str(cfg.get("mode") or "total").strip() if str(cfg.get("mode") or "total").strip() in {"total", "quota", "available"} else "total"
    if cfg["target"] == "grok":
        cfg["mode"] = "total"
    cfg["target_quota"] = max(1, int(cfg.get("target_quota") or 1))
    cfg["target_available"] = max(1, int(cfg.get("target_available") or 1))
    cfg["check_interval"] = max(1, int(cfg.get("check_interval") or 5))
    cfg["proxy"] = str(cfg.get("proxy") or "").strip()
    checkout_source = cfg.get("checkout") if isinstance(cfg.get("checkout"), dict) else {}
    checkout_defaults = openai_register.config.get("checkout") if isinstance(openai_register.config.get("checkout"), dict) else {}
    checkout = {**checkout_defaults, **checkout_source}
    # Compatibility: the earlier single residential proxy now becomes the
    # shared IN egress for Checkout, Provider, Approve, polling, and redirects.
    checkout_proxy_enabled = _safe_bool(
        checkout_source.get("checkout_proxy_enabled"),
        _safe_bool(checkout_source.get("residential_proxy_enabled"), False),
    )
    checkout_proxy_url = normalize_proxy_url_list(_clean_text(
        (
            checkout_source.get("checkout_proxy_url")
            if "checkout_proxy_url" in checkout_source
            else checkout_source.get("residential_proxy_url")
        )
    ))
    promotion_proxy_enabled = _safe_bool(checkout_source.get("promotion_proxy_enabled"), False)
    promotion_proxy_url = normalize_proxy_url_list(_clean_text(checkout_source.get("promotion_proxy_url")))
    continuous_retry = _safe_bool(checkout_source.get("continuous_retry"), True)
    checkout_threads = max(1, _safe_int(checkout.get("threads"), 5) or 5)
    checkout_channel = _checkout_channel(checkout.get("channel"))
    checkout_country, checkout_currency = _checkout_market(checkout_channel)
    cfg["checkout"] = {
        "enabled": _safe_bool(checkout.get("enabled"), True),
        "channel": checkout_channel,
        "pix_protocol": (
            str(checkout.get("pix_protocol") or "").strip().lower()
            if str(checkout.get("pix_protocol") or "").strip().lower() in {"enhanced", "reference", "standalone"}
            else "enhanced"
        ),
        "country": checkout_country,
        "currency": checkout_currency,
        "checkout_ui_mode": "custom",
        "threads": checkout_threads,
        "checkout_proxy_enabled": checkout_proxy_enabled,
        "checkout_proxy_url": checkout_proxy_url if checkout_proxy_enabled else "",
        "promotion_proxy_enabled": promotion_proxy_enabled,
        "promotion_proxy_url": promotion_proxy_url if promotion_proxy_enabled else "",
        "provider_proxy_enabled": checkout_proxy_enabled,
        "provider_proxy_url": checkout_proxy_url if checkout_proxy_enabled else "",
        "continuous_retry": continuous_retry,
    }
    cfg["sub2api_sync"] = normalize_sync_config(cfg.get("sub2api_sync"))
    cfg["cpa_sync"] = normalize_cpa_delivery_config(cfg.get("cpa_sync"))
    archive_source = cfg.get("agent_identity_archive")
    archive_source = archive_source if isinstance(archive_source, dict) else {}
    cfg["agent_identity_archive"] = {
        "enabled": _safe_bool(archive_source.get("enabled"), True),
    }
    grok_source = cfg.get("grok") if isinstance(cfg.get("grok"), dict) else {}
    grok = {**DEFAULT_GROK_CONFIG, **grok_source}
    nested_bridge = grok_source.get("grok2api") if isinstance(grok_source.get("grok2api"), dict) else {}
    for nested_key, config_key in {
        "enabled": "grok2api_enabled",
        "api_base": "grok2api_api_base",
        "admin_key": "grok2api_admin_key",
        "pool": "grok2api_pool",
        "auto_nsfw": "grok2api_auto_nsfw",
        "verify_on_import": "grok2api_verify_on_import",
        "timeout": "grok2api_timeout",
    }.items():
        if config_key not in grok_source and nested_key in nested_bridge:
            grok[config_key] = nested_bridge[nested_key]
    grok.pop("grok2api", None)
    retry_value = (
        grok_source.get("max_mail_retries")
        if "max_mail_retries" in grok_source
        else grok_source.get("max_mail_retry", DEFAULT_GROK_CONFIG["max_mail_retries"])
    )
    grok["max_mail_retries"] = max(1, min(20, int(retry_value or 3)))
    grok.pop("max_mail_retry", None)
    grok_provider = str(grok.get("provider") or "yescaptcha").strip().lower()
    grok["provider"] = (
        grok_provider
        if grok_provider in {"yescaptcha", "2captcha", "local", "custom"}
        else "yescaptcha"
    )
    signup_flow = str(grok.get("signup_flow") or "xconsole").strip().lower()
    grok["signup_flow"] = signup_flow if signup_flow in {"legacy", "xconsole"} else "xconsole"
    for key in (
        "api_key",
        "api_base",
        "action",
        "sitekey",
        "action_id",
        "base_url",
        "castle_pk",
        "castle_sdk_url",
        "next_router_state_tree",
        "create_path",
        "result_path",
        "grok2api_api_base",
        "grok2api_admin_key",
        "grok2api_pool",
    ):
        grok[key] = str(grok.get(key) or DEFAULT_GROK_CONFIG[key]).strip()
    grok2api_pool = grok["grok2api_pool"].lower() or "auto"
    grok["grok2api_pool"] = grok2api_pool if grok2api_pool in {"auto", "basic", "super", "heavy"} else "auto"
    grok["xai_cli_oauth_enabled"] = _safe_bool(grok.get("xai_cli_oauth_enabled"), True)
    oauth_flow = str(grok.get("xai_cli_oauth_flow") or "device").strip().lower()
    grok["xai_cli_oauth_flow"] = oauth_flow if oauth_flow in {"device", "pkce_reference"} else "device"
    grok["xai_cli_pkce_reference_dir"] = str(grok.get("xai_cli_pkce_reference_dir") or "").strip()
    grok["oauth_delivery"] = normalize_xai_oauth_delivery_config(grok_source.get("oauth_delivery"))
    grok["grok2api_enabled"] = True
    grok["grok2api_api_base"] = ""
    grok["grok2api_admin_key"] = ""
    grok["grok2api_auto_nsfw"] = _safe_bool(grok.get("grok2api_auto_nsfw"), False)
    grok["grok2api_verify_on_import"] = _safe_bool(grok.get("grok2api_verify_on_import"), True)
    grok["probe_scheduler"] = _normalize_grok_probe_config(grok.get("probe_scheduler"))
    for key, minimum, maximum in (
        ("request_timeout", 1, 300),
        ("captcha_timeout", 10, 900),
        ("captcha_poll_interval", 1, 60),
        ("local_concurrency", 0, 64),
        ("local_attempt_timeout", 15, 120),
        ("local_queue_timeout", 0, 300),
        ("local_max_attempts", 1, 5),
        ("castle_timeout", 1, 300),
        ("grok2api_timeout", 1, 300),
    ):
        raw_value = grok.get(key)
        if raw_value is None or raw_value == "":
            raw_value = DEFAULT_GROK_CONFIG[key]
        grok[key] = max(minimum, min(maximum, int(raw_value)))
    if not isinstance(grok.get("custom_headers"), dict):
        grok["custom_headers"] = {}
    cfg["grok"] = grok
    default_mail = _default_config()["mail"] if isinstance(_default_config().get("mail"), dict) else {}
    mail = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    cfg["mail"] = {**default_mail, **mail}
    cfg["mail"]["api_use_register_proxy"] = _safe_bool(cfg["mail"].get("api_use_register_proxy"), True)
    cfg["mail"].pop("proxy", None)
    cfg["enabled"] = bool(cfg.get("enabled"))
    stats = {**_default_config()["stats"], **(raw.get("stats") if isinstance(raw.get("stats"), dict) else {}),
             "threads": cfg["threads"]}
    cfg["stats"] = stats
    return cfg


class RegisterService:
    def __init__(
        self,
        store_file: Path,
        *,
        grok_oauth_protocol_sink: Callable[..., dict[str, Any]] | None = None,
    ):
        self._store_file = store_file
        self._grok_oauth_protocol_sink = grok_oauth_protocol_sink
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        self._grok_probe_running = False
        self._grok_probe_scheduler_thread: threading.Thread | None = None
        self._grok_probe_wake_event = threading.Event()
        self._grok_probe_cancel_event = threading.Event()
        self._grok_probe_idle_event = threading.Event()
        self._grok_probe_idle_event.set()
        self._grok_runtime_snapshot_lock = threading.Lock()
        self._grok_runtime_snapshot_error = ""
        self._logs: list[dict] = []
        self._grok_oauth_logs: list[dict] = []
        self._checkout_logs: list[dict] = []
        self._checkout_tasks: list[dict] = []
        self._checkout_task_run_id = ""
        self._checkout_retry_jobs: dict[str, dict[str, Any]] = {}
        self._checkout_retry_stop_event = threading.Event()
        self._checkout_retry_condition = threading.Condition(self._lock)
        # One worker per configured registration thread.  Each worker claims a
        # different account job before leaving the queue lock, so a slow
        # Checkout flow cannot serialize every other saved account.
        self._checkout_retry_runners: list[threading.Thread] = []
        openai_register.register_log_sink = self._append_log
        openai_register.register_checkout_log_sink = self._append_checkout_log
        openai_register.register_checkout_task_sink = self._upsert_checkout_task
        openai_register.register_checkout_retry_sink = self._enqueue_checkout_retry
        openai_register.register_checkout_task_run_id = ""
        self._config = self._load()
        if self._config["enabled"]:
            self.start()

    def _load(self) -> dict:
        return _normalize(read_json_object(self._store_file, name="register.json"))

    def _save(self) -> None:
        write_json_file(self._store_file, self._config)

    def _runtime_config(self, target: str | None = None) -> dict:
        selected_target = str(target or self._config.get("target") or "openai").strip().lower()
        runtime = json.loads(json.dumps(self._config, ensure_ascii=False))
        runtime["target"] = selected_target if selected_target in REGISTER_TARGETS else "openai"
        grok = runtime.get("grok") if isinstance(runtime.get("grok"), dict) else {}
        grok["max_mail_retry"] = int(grok.get("max_mail_retries") or 3)
        runtime["grok"] = grok
        mail = runtime.get("mail") if isinstance(runtime.get("mail"), dict) else {}
        providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
        project = "grok" if runtime["target"] == "grok" else "openai"
        keyword = "xAI" if project == "grok" else "OpenAI"
        for provider in providers:
            if isinstance(provider, dict) and provider.get("type") in {"icloud_api", "icloud_local"}:
                provider["project"] = project
                provider["keyword"] = keyword
        if runtime["target"] == "grok":
            runtime["mode"] = "total"
        return runtime

    def _sync_backend_runtime(self, runtime: dict):
        backend = _registration_backend(runtime["target"])
        backend.register_log_sink = self._append_log
        backend.register_checkout_log_sink = self._append_checkout_log
        if runtime["target"] == "openai":
            backend.register_checkout_task_sink = self._upsert_checkout_task
            backend.register_checkout_retry_sink = self._enqueue_checkout_retry
        if runtime["target"] == "grok" and hasattr(backend, "account_result_sink"):
            backend.account_result_sink = self._persist_grok_account_snapshot
        config_keys = [
            "mail", "proxy", "total", "threads", "checkout", "sub2api_sync",
            "cpa_sync", "agent_identity_archive",
        ]
        if runtime["target"] == "grok":
            config_keys.extend(["target", "grok"])
        backend.config.update(
            {
                key: runtime[key]
                for key in config_keys
                if key in runtime
            }
        )
        return backend

    def _sync_backend_config(self, target: str | None = None):
        return self._sync_backend_runtime(self._runtime_config(target))

    def _sync_icloud_claims(self) -> None:
        mail = self._config.get("mail") if isinstance(self._config.get("mail"), dict) else {}
        providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
        if not any(isinstance(item, dict) and item.get("type") == "icloud_local" for item in providers):
            return
        projects = {
            "openai": [str(item.get("email") or "").strip() for item in account_service.list_accounts()],
            "grok": [str(item.get("email") or "").strip() for item in grok_account_store.list_accounts(redacted=False)],
        }
        for project, emails in projects.items():
            emails = [email for email in emails if email]
            if not emails:
                continue
            try:
                result = mail_provider.sync_icloud_claims(mail, project, emails)
                updated = int(result.get("updated") or 0) if isinstance(result, dict) else 0
                if updated:
                    self._append_log(f"已同步 {project.upper()} 邮箱注册标签：{updated} 个", "info")
            except Exception as exc:
                self._append_log(f"同步 {project.upper()} 邮箱注册标签失败：{type(exc).__name__}: {exc}", "yellow")

    def get(self) -> dict:
        with self._lock:
            active_checkout_jobs = sum(
                1
                for job in self._checkout_retry_jobs.values()
                if job.get("stop_event") is self._checkout_retry_stop_event
            )
            snapshot = json.loads(
                json.dumps(
                    {
                        **self._config,
                        "logs": self._logs[-300:],
                        "grok_oauth_logs": self._grok_oauth_logs[-300:],
                        "checkout_logs": self._checkout_logs[-300:],
                        "checkout_tasks": self._checkout_tasks[-300:],
                        "checkout_retries_active": (
                            active_checkout_jobs > 0 and not self._checkout_retry_stop_event.is_set()
                        ),
                        "checkout_retry_job_count": active_checkout_jobs,
                    },
                    ensure_ascii=False,
                )
            )
        self._redact_outlook_pools(snapshot)
        return snapshot

    @staticmethod
    def _mask_email(email: str) -> str:
        local, sep, domain = str(email or "").partition("@")
        if not sep:
            return "***"
        masked = (local[:2] + "***" + local[-1:]) if len(local) > 2 else (local[:1] + "***")
        return f"{masked}@{domain}"

    def _redact_outlook_pools(self, snapshot: dict) -> None:
        """把 outlook_token 邮箱池里的密码/refresh_token 从对外输出中抹掉，仅保留脱敏预览与统计。

        mailboxes 改为只写导入框（输出为空），避免把密码与 refresh_token 通过 GET/SSE 反复广播。
        """
        mail = snapshot.get("mail")
        if not isinstance(mail, dict):
            return
        providers = mail.get("providers")
        if not isinstance(providers, list):
            return
        stats = snapshot.get("stats") if isinstance(snapshot.get("stats"), dict) else {}
        batch_started_at = str(stats.get("mailbox_batch_started_at") or stats.get("started_at") or "")
        for index, provider in enumerate(providers):
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            provider_id = _provider_id(provider) or f"outlook-{index}"
            provider["id"] = provider_id
            pool_text = str(provider.get("mailboxes") or "")
            base_credentials = mail_provider.parse_outlook_credentials(pool_text)
            credentials = mail_provider.expand_outlook_aliases(base_credentials, provider)
            provider["mailboxes"] = ""
            provider["mailboxes_count"] = len(credentials)
            provider["mailboxes_base_count"] = len(base_credentials)
            provider["mailboxes_alias_count"] = max(0, len(credentials) - len(base_credentials))
            provider["mailboxes_preview"] = [self._mask_email(c["email"]) for c in credentials]
            provider["mailboxes_stats"] = mail_provider.outlook_token_pool_stats(credentials)
            provider["mailboxes_parse_stats"] = mail_provider.inspect_outlook_credentials(pool_text)
            provider["mailboxes_failed"] = [
                {
                    "id": _outlook_mailbox_id(provider_id, item["email"]),
                    "email": self._mask_email(item["email"]),
                    "reason": item.get("reason") or "",
                    "updated_at": item.get("updated_at") or "",
                }
                for item in mail_provider.outlook_token_pool_failures(
                    credentials,
                    since=batch_started_at,
                )
            ]

    def _drop_mail_proxy(self) -> None:
        if isinstance(self._config.get("mail"), dict):
            self._config["mail"].pop("proxy", None)

    def _merge_outlook_pools(self, updates: dict) -> None:
        """对 outlook_token provider：把前端新导入的 mailboxes 与已存池按邮箱合并去重。

        前端 mailboxes 是只写导入框，留空表示不改动；填入的新行追加/覆盖已存凭据。
        按数组下标与已存的同类型 provider 对齐。
        """
        mail = updates.get("mail")
        if not isinstance(mail, dict) or not isinstance(mail.get("providers"), list):
            return
        old_mail = self._config.get("mail") if isinstance(self._config.get("mail"), dict) else {}
        old_providers = old_mail.get("providers") if isinstance(old_mail.get("providers"), list) else []
        old_outlook_by_id = {
            _provider_id(provider): provider
            for provider in old_providers
            if isinstance(provider, dict) and provider.get("type") == "outlook_token" and _provider_id(provider)
        }
        old_outlook_by_order = [
            provider
            for provider in old_providers
            if isinstance(provider, dict) and provider.get("type") == "outlook_token"
        ]
        outlook_index = 0
        for index, provider in enumerate(mail["providers"]):
            if not isinstance(provider, dict):
                continue
            _ensure_provider_id(provider)
            if provider.get("type") != "outlook_token":
                continue
            provider_id = _provider_id(provider)
            old = old_outlook_by_id.get(provider_id) or {}
            if not old and index < len(old_providers) and isinstance(old_providers[index], dict) and old_providers[index].get("type") == "outlook_token":
                old = old_providers[index]
            if not old and outlook_index < len(old_outlook_by_order):
                old = old_outlook_by_order[outlook_index]
            outlook_index += 1
            old_text = str(old.get("mailboxes") or "") if old.get("type") == "outlook_token" else ""
            new_text = str(provider.get("mailboxes") or "")
            old_credentials = {
                credential["email"].strip().lower(): credential
                for credential in mail_provider.parse_outlook_credentials(old_text or "")
            }
            new_credentials = mail_provider.parse_outlook_credentials(new_text or "")
            if new_text.strip():
                provider["mailboxes"] = _merge_outlook_pool(old_text, new_text)
                refreshed_credentials = [
                    credential
                    for credential in new_credentials
                    if _outlook_credential_changed(old_credentials.get(credential["email"].strip().lower()), credential)
                ]
                if refreshed_credentials:
                    refreshed_addresses = [
                        item["email"]
                        for credential in refreshed_credentials
                        for item in mail_provider.expand_outlook_aliases([credential], provider)
                    ]
                    mail_provider.clear_outlook_token_states(
                        refreshed_addresses,
                        states=mail_provider.OUTLOOK_REFRESHED_CREDENTIAL_RESET_STATES,
                    )
            elif old_text:
                provider["mailboxes"] = _merge_outlook_pool(old_text, "")
            else:
                provider["mailboxes"] = ""
            for key in ("mailboxes_count", "mailboxes_base_count", "mailboxes_alias_count", "mailboxes_preview", "mailboxes_stats", "mailboxes_parse_stats", "mailboxes_failed"):
                provider.pop(key, None)

    def _prune_unused_outlook_pools(self) -> int:
        mail = self._config.get("mail")
        if not isinstance(mail, dict):
            return 0
        providers = mail.get("providers")
        if not isinstance(providers, list):
            return 0
        total_removed = 0
        for provider in providers:
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            credentials = mail_provider.parse_outlook_credentials(str(provider.get("mailboxes") or ""))
            kept, removed = mail_provider.prune_outlook_unused_credentials(credentials, provider)
            if removed:
                provider["mailboxes"] = _serialize_outlook_pool(kept)
                total_removed += removed
            for key in ("mailboxes_count", "mailboxes_base_count", "mailboxes_alias_count", "mailboxes_preview", "mailboxes_stats", "mailboxes_parse_stats", "mailboxes_failed"):
                provider.pop(key, None)
        return total_removed

    def update(self, updates: dict) -> dict:
        with self._lock:
            self._merge_outlook_pools(updates)
            self._config = _normalize({**self._config, **updates})
            if isinstance(updates.get("checkout"), dict):
                self._refresh_checkout_retry_configs_locked()
            self._drop_mail_proxy()
            self._sync_icloud_claims()
            self._sync_backend_config()
            self._save()
            return self.get()

    def _refresh_checkout_retry_configs_locked(self) -> None:
        """Apply saved Checkout changes to queued jobs on their next attempt."""
        checkout = self._config.get("checkout") if isinstance(self._config.get("checkout"), dict) else {}
        snapshot = json.loads(json.dumps(checkout, ensure_ascii=False))
        changed = False
        for job in self._checkout_retry_jobs.values():
            if job.get("stop_event") is not self._checkout_retry_stop_event:
                continue
            job["checkout"] = json.loads(json.dumps(snapshot, ensure_ascii=False))
            job["channel"] = _checkout_channel(snapshot.get("channel"))
            changed = True
        if changed:
            self._checkout_retry_condition.notify_all()

    def start(self) -> dict:
        with self._lock:
            if self._runner and self._runner.is_alive():
                self._config["enabled"] = True
                self._save()
                return self.get()
            target = str(self._config.get("target") or "openai")
            self._sync_icloud_claims()
            backend = self._sync_backend_config(target)
            self._config["enabled"] = True
            self._drop_mail_proxy()
            self._logs = []
            metrics = self._pool_metrics() if target == "openai" else {"current_quota": 0, "current_available": 0}
            job_id = uuid.uuid4().hex
            started_at = _now()
            self._config["stats"] = {
                "job_id": job_id,
                "success": 0,
                "fail": 0,
                "done": 0,
                "running": 0,
                "threads": self._config["threads"],
                **metrics,
                "started_at": started_at,
                "mailbox_batch_started_at": started_at,
                "updated_at": started_at,
            }
            if target == "openai":
                self._ensure_checkout_queue_locked()
                with openai_register.stats_lock:
                    openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})
            self._save()
            self._runner = threading.Thread(
                target=self._run,
                args=(target, backend),
                daemon=True,
                name=f"{target}-register",
            )
            if target == "grok":
                self._append_log(
                    f"Grok 注册任务已启动：共 {self._config['total']} 个，并发 {self._config['threads']}",
                    "yellow",
                )
                runtime_grok = backend.config.get("grok") if isinstance(backend.config.get("grok"), dict) else {}
                if (
                    _safe_bool(runtime_grok.get("xai_cli_oauth_enabled"), True)
                    and _clean_text(runtime_grok.get("provider")).lower() == "local"
                ):
                    self._append_log(
                        "Grok OAuth 即时上传已开启："
                        f"注册成功后立即处理，solver 总槽位 {runtime_grok.get('local_concurrency')}",
                        "yellow",
                    )
            else:
                self._append_log(
                    f"注册任务启动，平台={target}，模式={self._config['mode']}，线程数={self._config['threads']}",
                    "yellow",
                )
            self._runner.start()
            return self.get()

    def stop(self) -> dict:
        with self._lock:
            self._config["enabled"] = False
            self._config["stats"]["updated_at"] = _now()
            self._save()
            self._append_log("已请求停止注册任务，正在等待当前运行任务结束", "yellow")
            return self.get()

    def stop_checkout_retries(self) -> dict:
        """Cancel queued/retrying payment-link jobs without changing accounts."""
        with self._lock:
            self._cancel_checkout_retries_locked()
            return self.get()

    def clear_checkout_history(self) -> dict[str, Any]:
        """Remove terminal Checkout rows while preserving active queue state."""
        active_statuses = {"queued", "running", "retrying", "pending"}
        with self._lock:
            before = len(self._checkout_tasks)
            self._checkout_tasks = [
                task
                for task in self._checkout_tasks
                if str(task.get("status") or "").strip().lower() in active_statuses
            ]
            removed = before - len(self._checkout_tasks)
            return {"removed": removed, "register": self.get()}

    def enqueue_checkout_retries_for_accounts(self, access_tokens: list[str]) -> dict[str, int]:
        """Queue selected accounts for their configured final-link flow.

        This is used by the account-list batch action.  It intentionally does
        not run a synchronous extraction request: selected accounts enter the
        same concurrent queue as newly registered accounts, preserving
        country role routing and allowing a later stop.
        """
        tokens = list(dict.fromkeys(_clean_text(value) for value in access_tokens if _clean_text(value)))
        if not tokens:
            raise ValueError("未选择可提链账号")
        with self._lock:
            checkout = self._config.get("checkout") if isinstance(self._config.get("checkout"), dict) else {}
            channel = _checkout_channel(checkout.get("channel"))
            if not _safe_bool(checkout.get("continuous_retry"), True):
                raise ValueError("请先启用失败后持续换代理提链")

            run_id = self._ensure_checkout_queue_locked()
            checkout_snapshot = json.loads(json.dumps(checkout, ensure_ascii=False))
            self._append_checkout_log(
                f"已从账号管理向 {self._checkout_retry_proxy_plan(channel, checkout.get('pix_protocol'))} "
                f"提链队列追加账号，待加入 {len(tokens)} 个",
                "yellow",
            )

        queued = 0
        skipped = 0
        for offset, token in enumerate(tokens, start=1):
            resolved = account_service.resolve_access_token(token)
            account = account_service.get_account(resolved)
            if account is None:
                skipped += 1
                continue
            if account_service.ready_checkout_url(account, channel):
                skipped += 1
                continue
            with self._lock:
                if any(
                    job.get("run_id") == run_id and job.get("access_token") == resolved
                    for job in self._checkout_retry_jobs.values()
                ):
                    skipped += 1
                    continue
            account_service.update_account(resolved, {"checkout_link_status": "pending"}, quiet=True)
            self._enqueue_checkout_retry(
                {
                    "index": offset,
                    "task_id": f"manual-checkout-{uuid.uuid4().hex}",
                    "run_id": run_id,
                    "email": _clean_text(account.get("email")),
                    "access_token": resolved,
                    "checkout": checkout_snapshot,
                    "attempt": 0,
                    "next_proxy_rotation": random.randrange(1, 2**31),
                }
            )
            queued += 1
        self._append_checkout_log(
            f"账号管理提链批次已入队：{queued} 个任务，跳过 {skipped} 个账号",
            "green" if queued else "yellow",
        )
        return {"queued": queued, "skipped": skipped}

    def reset(self) -> dict:
        with self._lock:
            self._logs = []
            target = str(self._config.get("target") or "openai")
            metrics = self._pool_metrics() if target == "openai" else {"current_quota": 0, "current_available": 0}
            self._config["stats"] = {"success": 0, "fail": 0, "done": 0, "running": 0, "threads": self._config["threads"], "elapsed_seconds": 0, "avg_seconds": 0, "success_rate": 0, **metrics, "updated_at": _now()}
            if target == "openai":
                with openai_register.stats_lock:
                    openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": 0.0})
            self._save()
            return self.get()

    def reset_outlook_pool(self, scope: str = "all") -> dict:
        scope = str(scope or "all").strip().lower()
        if scope == "unused":
            with self._lock:
                removed = self._prune_unused_outlook_pools()
                self._sync_backend_config()
                self._save()
                self._append_log(f"已删除 Outlook 邮箱池未使用邮箱，移除 {removed} 个", "yellow")
            return self.get()
        scope_aliases = {"failed": "retryable", "retryable": "retryable", "invalid": "invalid", "all": "all"}
        scope = scope_aliases.get(scope, "all")
        cleared = mail_provider.reset_outlook_token_pool_state(scope)
        scope_label = {"retryable": "占用/临时失败", "invalid": "异常", "all": "全部"}[scope]
        with self._lock:
            self._append_log(
                f"已重置 Outlook 邮箱池状态（范围={scope_label}），清除 {cleared} 条记录",
                "yellow",
            )
        return self.get()

    def retry_outlook_failed(self, provider_id: str, mailbox_ids: list[str]) -> dict:
        selected_ids = list(dict.fromkeys(str(value or "").strip() for value in mailbox_ids if str(value or "").strip()))
        if not selected_ids:
            raise ValueError("请至少选择 1 个本次失败邮箱")
        requested_provider_id = str(provider_id or "").strip()
        with self._lock:
            if self._runner and self._runner.is_alive():
                raise ValueError("注册任务运行中，请等待当前任务结束")
            mail = self._config.get("mail") if isinstance(self._config.get("mail"), dict) else {}
            providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
            matched_provider: dict | None = None
            matched_provider_id = ""
            for index, item in enumerate(providers):
                if not isinstance(item, dict) or item.get("type") != "outlook_token":
                    continue
                item_id = _provider_id(item) or f"outlook-{index}"
                if item_id == requested_provider_id:
                    matched_provider = item
                    matched_provider_id = item_id
                    break
            if matched_provider is None:
                raise ValueError("Outlook 邮箱池配置不存在或已更改")

            base_credentials = mail_provider.parse_outlook_credentials(str(matched_provider.get("mailboxes") or ""))
            credentials = mail_provider.expand_outlook_aliases(base_credentials, matched_provider)
            stats = self._config.get("stats") if isinstance(self._config.get("stats"), dict) else {}
            batch_started_at = str(stats.get("mailbox_batch_started_at") or stats.get("started_at") or "")
            current_failures = {
                item["email"].strip().lower(): item
                for item in mail_provider.outlook_token_pool_failures(credentials, since=batch_started_at)
            }
            credential_by_id = {
                _outlook_mailbox_id(matched_provider_id, credential["email"]): credential
                for credential in credentials
                if credential["email"].strip().lower() in current_failures
            }
            selected_credentials: list[dict[str, str]] = []
            for mailbox_id in selected_ids:
                credential = credential_by_id.get(mailbox_id)
                if credential is None:
                    raise ValueError("所选邮箱已不在本次失败列表中，请刷新后重选")
                selected_credentials.append(credential)

            target = str(self._config.get("target") or "openai").strip().lower()
            runtime = self._runtime_config(target)
            retry_provider = json.loads(json.dumps(matched_provider, ensure_ascii=False))
            retry_provider["mailboxes"] = _serialize_outlook_pool(selected_credentials)
            retry_provider["alias_enabled"] = False
            retry_provider["alias_per_email"] = 0
            runtime_mail = runtime.get("mail") if isinstance(runtime.get("mail"), dict) else {}
            runtime_mail["providers"] = [retry_provider]
            runtime["mail"] = runtime_mail
            runtime["mode"] = "total"
            runtime["total"] = len(selected_credentials)
            runtime["threads"] = min(max(1, int(runtime.get("threads") or 1)), len(selected_credentials))
            backend = self._sync_backend_runtime(runtime)

            cleared = mail_provider.clear_outlook_token_states(
                [item["email"] for item in selected_credentials],
                states=mail_provider.OUTLOOK_RETRYABLE_STATES,
            )
            if cleared != len(selected_credentials):
                raise ValueError("部分所选邮箱状态已变更，请刷新后重选")

            started_at = _now()
            metrics = self._pool_metrics() if target == "openai" else {"current_quota": 0, "current_available": 0}
            self._config["enabled"] = True
            self._logs = []
            self._config["stats"] = {
                "job_id": uuid.uuid4().hex,
                "success": 0,
                "fail": 0,
                "done": 0,
                "running": 0,
                "threads": runtime["threads"],
                **metrics,
                "started_at": started_at,
                "mailbox_batch_started_at": batch_started_at or started_at,
                "updated_at": started_at,
                "retry_selected": len(selected_credentials),
            }
            if target == "openai":
                self._ensure_checkout_queue_locked()
                with openai_register.stats_lock:
                    openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})
            self._save()
            run_options = {"mode": "total", "total": len(selected_credentials), "threads": runtime["threads"]}
            self._runner = threading.Thread(
                target=self._run,
                args=(target, backend, run_options),
                daemon=True,
                name=f"{target}-outlook-retry",
            )
            self._append_log(
                f"已启动所选 Outlook 失败邮箱重试：{len(selected_credentials)} 个，其他邮箱保持原状态",
                "yellow",
            )
            self._runner.start()
            return self.get()

    def _mail_config_with_proxy(self) -> dict:
        mail = json.loads(json.dumps(self._config.get("mail") if isinstance(self._config.get("mail"), dict) else {}, ensure_ascii=False))
        use_register_proxy = _safe_bool(mail.get("api_use_register_proxy"), True)
        mail["api_use_register_proxy"] = use_register_proxy
        mail["proxy"] = str(self._config.get("proxy") or "").strip() if use_register_proxy else ""
        return mail

    def gptmail_status(self, provider: dict | None = None, force: bool = False) -> dict:
        with self._lock:
            mail = self._mail_config_with_proxy()
        return mail_provider.gptmail_status(mail, provider, force=force)

    def refresh_gptmail_public_key(self, provider: dict | None = None, force: bool = True) -> dict:
        with self._lock:
            mail = self._mail_config_with_proxy()
        return mail_provider.refresh_gptmail_public_key(mail, provider, force=force)

    def _grok_config_snapshot(self) -> dict[str, Any]:
        with self._lock:
            source = self._config.get("grok") if isinstance(self._config.get("grok"), dict) else {}
            return json.loads(json.dumps(source, ensure_ascii=False))

    def _grok2api_client(self):
        from services.register.grok2api_account_client import Grok2APIAccountClient

        return Grok2APIAccountClient(self._grok_config_snapshot())

    def _grok2api_error_text(self, error: Exception, secrets: list[str] | None = None) -> str:
        text = _clean_text(error) or type(error).__name__
        config = self._grok_config_snapshot()
        hidden_values = [
            _clean_text(config.get("grok2api_admin_key")),
            *[_clean_text(value) for value in (secrets or [])],
        ]
        for secret in hidden_values:
            if secret:
                text = text.replace(secret, "***")
        return text[:300]

    @staticmethod
    def _runtime_tokens(payload: object) -> list[dict[str, Any]]:
        tokens = payload.get("tokens") if isinstance(payload, dict) else []
        return [dict(item) for item in tokens if isinstance(item, dict)] if isinstance(tokens, list) else []

    def _reconcile_grok_runtime_archive(self, client) -> dict[str, int]:
        """Update the host archive from a current Grok runtime snapshot.

        The store keeps registration credentials and provenance untouched; the
        runtime is represented by its separate, non-secret ``runtime`` field.
        """
        return grok_account_store.reconcile_runtime_accounts(self._runtime_tokens(client.list()))

    def refresh_grok_runtime_snapshot(self) -> dict[str, Any]:
        """Refresh the local runtime cache without blocking account-list reads."""
        if not self._grok_runtime_snapshot_lock.acquire(blocking=False):
            return {"ok": True, "refreshed": False, "refreshing": True, "error": ""}
        try:
            client = self._grok2api_client()
            ready, readiness_error = client.readiness()
            if not ready:
                error = self._grok2api_error_text(RuntimeError(readiness_error))
                with self._lock:
                    self._grok_runtime_snapshot_error = error
                return {"ok": False, "refreshed": False, "refreshing": False, "error": error}

            try:
                result = self._reconcile_grok_runtime_archive(client)
            except Exception as exc:
                error = self._grok2api_error_text(exc)
                with self._lock:
                    self._grok_runtime_snapshot_error = error
                return {"ok": False, "refreshed": False, "refreshing": False, "error": error}

            with self._lock:
                self._grok_runtime_snapshot_error = ""
            return {
                "ok": True,
                "refreshed": True,
                "refreshing": False,
                "error": "",
                "summary": result,
            }
        finally:
            self._grok_runtime_snapshot_lock.release()

    def _sync_grok_account_to_runtime(self, item: dict[str, Any], client=None) -> dict[str, Any]:
        account_id = _clean_text(item.get("id"))
        token = _normalize_bridge_sso(item.get("sso"))
        if not token:
            raise ValueError("账号缺少可同步的裸 SSO")
        client = client or self._grok2api_client()
        add_result = client.add([token])
        added = max(0, _safe_int(add_result.get("count"))) if isinstance(add_result, dict) else 0
        skipped = max(0, _safe_int(add_result.get("skipped"))) if isinstance(add_result, dict) else 0
        if added < 1 and skipped < 1:
            raise RuntimeError("内置 Grok 运行时未确认账号新增或已存在")

        refresh_summary = None
        verification_pending = False
        verification_error = ""
        if bool(getattr(client, "verify_on_import", True)):
            try:
                refresh_result = client.refresh([token])
                refresh_summary = _batch_summary(refresh_result, 1)
                if refresh_summary["ok"] != 1 or refresh_summary["fail"] != 0:
                    verification_pending = True
                    verification_error = (
                        "Grok 账号验证待复检: "
                        f"ok={refresh_summary['ok']}, fail={refresh_summary['fail']}"
                    )
            except Exception as error:
                verification_pending = True
                verification_error = self._grok2api_error_text(error, [token])
        return {
            "id": account_id,
            "ok": True,
            "sync_state": "synced",
            "added": added,
            "skipped": skipped,
            "refresh_summary": refresh_summary,
            "verification_pending": verification_pending,
            "verification_error": verification_error,
        }

    def sync_grok_accounts(self, ids: list[str]) -> dict[str, Any]:
        ordered_ids = list(dict.fromkeys(_clean_text(value) for value in ids if _clean_text(value)))
        raw_items = grok_account_store.get_accounts_by_ids(ordered_ids)
        by_id = {_clean_text(item.get("id")): item for item in raw_items}
        results: list[dict[str, Any]] = []
        try:
            client = self._grok2api_client()
        except Exception as error:
            message = self._grok2api_error_text(error)
            return {
                "summary": {"total": len(ordered_ids), "ok": 0, "fail": len(ordered_ids)},
                "results": [
                    {"id": account_id, "ok": False, "sync_state": "failed", "error": message}
                    for account_id in ordered_ids
                ],
            }

        for account_id in ordered_ids:
            item = by_id.get(account_id)
            if item is None:
                results.append({"id": account_id, "ok": False, "sync_state": "failed", "error": "本地账号不存在"})
                continue
            token = _normalize_bridge_sso(item.get("sso"))
            try:
                results.append(self._sync_grok_account_to_runtime(item, client=client))
            except Exception as error:
                results.append(
                    {
                        "id": account_id,
                        "ok": False,
                        "sync_state": "failed",
                        "error": self._grok2api_error_text(error, [token]),
                    }
                )
        ok = sum(1 for item in results if item.get("ok"))
        return {"summary": {"total": len(results), "ok": ok, "fail": len(results) - ok}, "results": results}

    def authorize_grok_accounts_oauth(self, ids: list[str]) -> dict[str, Any]:
        ordered_ids = list(dict.fromkeys(_clean_text(value) for value in ids if _clean_text(value)))
        raw_items = grok_account_store.get_accounts_by_ids(ordered_ids)
        by_id = {_clean_text(item.get("id")): item for item in raw_items}
        linked_emails = {
            _clean_text(item.get("email")).lower()
            for item in xai_cli_oauth_store.list_accounts(redacted=False)
            if isinstance(item, dict) and _clean_text(item.get("email"))
        }
        summary = {
            "total": len(ordered_ids),
            "queued": 0,
            "reused": 0,
            "skipped": 0,
            "failed": 0,
        }
        results: list[dict[str, Any]] = []

        for account_id in ordered_ids:
            source = by_id.get(account_id)
            if source is None:
                summary["failed"] += 1
                results.append({"id": account_id, "ok": False, "status": "failed", "error": "Grok 账号不存在"})
                continue

            email = _clean_text(source.get("email"))
            if email.lower() in linked_emails:
                summary["skipped"] += 1
                results.append({"id": account_id, "ok": True, "status": "already_authorized"})
                continue

            missing = []
            if _clean_text(source.get("status")).lower() != "active":
                missing.append("注册状态不是 active")
            if not email:
                missing.append("邮箱")
            if not _clean_text(source.get("password")):
                missing.append("登录密码")
            if not _normalize_bridge_sso(source.get("sso")):
                missing.append("SSO 登录态")
            if missing:
                summary["failed"] += 1
                results.append(
                    {
                        "id": account_id,
                        "ok": False,
                        "status": "failed",
                        "error": f"无法执行 OAuth 授权：缺少或不满足 {'、'.join(missing)}",
                    }
                )
                continue

            if self._grok_oauth_protocol_sink is None:
                summary["failed"] += 1
                results.append(
                    {"id": account_id, "ok": False, "status": "failed", "error": "Grok OAuth 协议授权服务不可用"}
                )
                continue

            try:
                started = self._grok_oauth_protocol_sink(account_id, prioritize=True)
                if not isinstance(started, dict):
                    raise RuntimeError("OAuth 协议授权任务未返回有效状态")
                job = started.get("job") if isinstance(started.get("job"), dict) else {}
                job_id = _clean_text(job.get("id"))
                if bool(started.get("reused")):
                    summary["reused"] += 1
                    results.append(
                        {"id": account_id, "ok": True, "status": "reused", "job_id": job_id}
                    )
                elif bool(started.get("queued")) or job_id:
                    summary["queued"] += 1
                    results.append(
                        {"id": account_id, "ok": True, "status": "queued", "job_id": job_id}
                    )
                else:
                    raise RuntimeError("OAuth 协议授权任务未进入队列")
            except Exception as error:
                summary["failed"] += 1
                results.append(
                    {
                        "id": account_id,
                        "ok": False,
                        "status": "failed",
                        "error": _clean_text(error) or type(error).__name__,
                    }
                )

        level = "green" if summary["failed"] == 0 else "yellow"
        self._append_grok_oauth_log(
            "Grok OAuth 手动授权："
            f"新排队 {summary['queued']}，队列中 {summary['reused']}，"
            f"已授权跳过 {summary['skipped']}，失败 {summary['failed']}",
            level,
        )
        return {"summary": summary, "results": results}

    @staticmethod
    def _grok_status_matches(item: dict[str, Any], status: str) -> bool:
        status_filter = _clean_text(status).lower()
        if not status_filter or status_filter == "all":
            return True
        if status_filter == "refresh_failed":
            return _clean_text(item.get("refresh_status")).lower() == "failed"
        if status_filter == "probe_invalid":
            return _clean_text(item.get("probe_status")).lower() == "invalid"
        if status_filter == "probe_unknown":
            return _clean_text(item.get("probe_status")).lower() == "unknown"
        if status_filter.startswith("oauth_"):
            oauth_status = status_filter.removeprefix("oauth_")
            if oauth_status in {"unauthorized", "normal", "limited", "no_quota", "expired", "invalid"}:
                return _grok_oauth_display_status(item.get("oauth")) == oauth_status
        runtime_aliases = {
            "normal": "active",
            "limited": "cooling",
            "abnormal": "invalid",
            "disabled": "disabled",
        }
        runtime_filter = runtime_aliases.get(status_filter)
        if runtime_filter:
            return bool(item.get("runtime_status")) and _runtime_status_bucket(item.get("runtime_status")) == runtime_filter
        return _clean_text(item.get("status")).lower() == status_filter

    def grok_accounts_view(self, *, keyword: str = "", status: str = "all") -> dict[str, Any]:
        runtime_available = False
        runtime_error = ""
        config = self._grok_config_snapshot()
        if _safe_bool(config.get("grok2api_enabled"), False):
            try:
                runtime_available, runtime_error = self._grok2api_client().readiness()
            except Exception as error:
                runtime_error = self._grok2api_error_text(error)
        if not runtime_error:
            with self._lock:
                runtime_error = self._grok_runtime_snapshot_error

        local_items = grok_account_store.list_accounts(redacted=True)
        raw_items = grok_account_store.get_accounts_by_ids([_clean_text(item.get("id")) for item in local_items])
        raw_by_id = {_clean_text(item.get("id")): item for item in raw_items}
        oauth_raw_items = xai_cli_oauth_store.list_accounts(redacted=False)
        oauth_safe_by_id = {
            _clean_text(item.get("id")): item
            for item in xai_cli_oauth_store.list_accounts(redacted=True)
            if _clean_text(item.get("id"))
        }
        oauth_by_email = {
            _clean_text(item.get("email")).lower(): oauth_safe_by_id.get(_clean_text(item.get("id")), {})
            for item in oauth_raw_items
            if _clean_text(item.get("email")) and _clean_text(item.get("id")) in oauth_safe_by_id
        }
        keyword_ids = {
            _clean_text(item.get("id"))
            for item in grok_account_store.list_accounts(redacted=True, keyword=keyword, status="all")
        }

        merged_items: list[dict[str, Any]] = []
        for local in local_items:
            account_id = _clean_text(local.get("id"))
            raw = raw_by_id.get(account_id, {})
            token = _normalize_bridge_sso(raw.get("sso"))
            oauth = oauth_by_email.get(_clean_text(raw.get("email")).lower())
            cached_runtime = raw.get("runtime") if isinstance(raw.get("runtime"), dict) else {}
            cached_probe = raw.get("probe") if isinstance(raw.get("probe"), dict) else {}
            cached_recovery = raw.get("recovery") if isinstance(raw.get("recovery"), dict) else {}
            cached_status = _clean_text(cached_runtime.get("status")) if cached_runtime else ""
            cached_runtime_present = bool(cached_runtime) and cached_runtime.get("present") is not False
            merged = {
                **local,
                "token_preview": _token_preview(token),
                "pool": _clean_text(cached_runtime.get("pool")),
                "runtime_status": cached_status,
                "quota": _quota_brief(cached_runtime.get("quota")),
                "use_count": max(0, _safe_int(cached_runtime.get("use_count"))),
                "fail_count": max(0, _safe_int(cached_runtime.get("fail_count"))),
                "last_used_at": cached_runtime.get("last_used_at"),
                "tags": [_clean_text(value) for value in cached_runtime.get("tags", []) if _clean_text(value)]
                if isinstance(cached_runtime.get("tags"), list)
                else [],
                "refresh_status": _clean_text(cached_runtime.get("refresh_status")).lower(),
                "refresh_at": cached_runtime.get("refresh_at"),
                "refresh_error": _clean_text(cached_runtime.get("refresh_error"))[:300],
                "probe_status": _clean_text(cached_probe.get("status")).lower(),
                "probe_at": cached_probe.get("at"),
                "probe_quota": _verify_quota_brief(cached_probe.get("quota")),
                "probe_error": _clean_text(cached_probe.get("error"))[:300],
                "recovery_status": _clean_text(cached_recovery.get("status")).lower(),
                "recovery_last_attempt_at": cached_recovery.get("last_attempt_at"),
                "recovery_last_success_at": cached_recovery.get("last_success_at"),
                "recovery_next_attempt_at": cached_recovery.get("next_attempt_at"),
                "recovery_error": _clean_text(cached_recovery.get("error"))[:300],
                "recovery_attempts": max(0, _safe_int(cached_recovery.get("attempts"))),
                "sync_state": (
                    "not_ready"
                    if not token
                    else "synced"
                    if cached_runtime_present
                    else "not_synced"
                    if cached_runtime.get("present") is False
                    else "unknown"
                ),
                "oauth": oauth if isinstance(oauth, dict) and oauth else None,
            }
            merged_items.append(merged)

        cached_runtime_items = [item for item in merged_items if item.get("sync_state") == "synced"]
        runtime_status = {"active": 0, "cooling": 0, "invalid": 0, "disabled": 0}
        quota_summary = {mode: 0 for mode in _GROK_QUOTA_MODES}
        calls_total = 0
        for runtime_item in cached_runtime_items:
            runtime_status[_runtime_status_bucket(runtime_item.get("runtime_status"))] += 1
            calls_total += max(0, _safe_int(runtime_item.get("use_count"))) + max(
                0, _safe_int(runtime_item.get("fail_count"))
            )
            quota = runtime_item.get("quota") if isinstance(runtime_item.get("quota"), dict) else {}
            for mode in _GROK_QUOTA_MODES:
                quota_summary[mode] += max(0, _safe_int(quota.get(mode, {}).get("remaining")))

        local_statuses = [_clean_text(item.get("status")).lower() for item in local_items]
        oauth_display_statuses = [_grok_oauth_display_status(item.get("oauth")) for item in merged_items]
        summary = {
            "total": len(local_items),
            "active": sum(1 for value in local_statuses if value == "active"),
            "pending": sum(1 for value in local_statuses if value in _GROK_PENDING_STATUSES),
            "failed": sum(1 for value in local_statuses if value in _GROK_FAILED_STATUSES),
            "synced": sum(1 for item in merged_items if item.get("sync_state") == "synced"),
            "not_synced": sum(1 for item in merged_items if item.get("sync_state") == "not_synced"),
            "runtime_total": len(cached_runtime_items),
            "oauth_total": len(oauth_raw_items),
            "oauth_linked": sum(1 for item in merged_items if isinstance(item.get("oauth"), dict)),
            "oauth_status": {
                status: sum(1 for value in oauth_display_statuses if value == status)
                for status in ("unauthorized", "normal", "limited", "no_quota", "expired", "invalid")
            },
            "runtime_status": runtime_status,
            "calls_total": calls_total,
            "quota": quota_summary,
            "refresh_failed": sum(
                1 for item in merged_items if _clean_text(item.get("refresh_status")).lower() == "failed"
            ),
            "probe": {
                status: sum(
                    1 for item in merged_items if _clean_text(item.get("probe_status")).lower() == status
                )
                for status in ("valid", "invalid", "unknown")
            },
        }
        filtered = [
            item
            for item in merged_items
            if _clean_text(item.get("id")) in keyword_ids and self._grok_status_matches(item, status)
        ]
        return {
            "items": filtered,
            "all_total": len(local_items),
            "summary": summary,
            "runtime_available": runtime_available,
            "runtime_error": runtime_error,
        }

    def list_grok_accounts(self, *, keyword: str = "", status: str = "all") -> list[dict]:
        return self.grok_accounts_view(keyword=keyword, status=status)["items"]

    def grok_account_login_credentials(self, account_id: str) -> dict[str, str] | None:
        return grok_account_store.get_login_credentials(account_id)

    @staticmethod
    def _validate_grok_chat_test_request(prompt: str, model: str | None) -> tuple[str, str]:
        from app.dataplane.reverse.protocol.xai_console_chat import CONSOLE_MODELS
        from services.grok_runtime import GROK_ACCOUNT_CHAT_TEST_MODEL

        message = _clean_text(prompt)
        if not message:
            raise GrokAccountChatTestError("测试消息不能为空", status_code=400)
        if len(message) > 1_200:
            raise GrokAccountChatTestError("测试消息不能超过 1200 个字符", status_code=400)
        selected_model = _clean_text(model) or GROK_ACCOUNT_CHAT_TEST_MODEL
        if selected_model not in CONSOLE_MODELS:
            raise GrokAccountChatTestError("仅支持 Console Grok 模型进行对话测试", status_code=400)
        return message, selected_model

    def chat_test_grok_account(
        self,
        account_id: str,
        *,
        prompt: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Test one archive account by making one direct Console request.

        The archive ID is resolved locally and its SSO never leaves this
        boundary except for the internal direct upstream request.  This method
        does not add an unsynchronised account to the runtime pool.
        """
        stable_id = _clean_text(account_id)
        if not stable_id:
            raise GrokAccountChatTestError("Grok 账号 ID 不能为空", status_code=400)
        item = next(iter(grok_account_store.get_accounts_by_ids([stable_id])), None)
        if item is None:
            raise GrokAccountChatTestError("Grok 账号不存在或已删除", status_code=404)

        token = _normalize_bridge_sso(item.get("sso"))
        if not token:
            raise GrokAccountChatTestError("该 Grok 账号未保存 SSO 登录态", status_code=409)

        # The registration archive mirrors the runtime's non-secret quota
        # snapshot.  When Console quota is already known to be empty, do not
        # send a request that can only produce another upstream 429.
        runtime = item.get("runtime") if isinstance(item.get("runtime"), dict) else {}
        runtime_quota = runtime.get("quota") if isinstance(runtime.get("quota"), dict) else {}
        console_quota = runtime_quota.get("console") if isinstance(runtime_quota.get("console"), dict) else {}
        console_total = max(0, _safe_int(console_quota.get("total")))
        console_remaining = max(0, _safe_int(console_quota.get("remaining")))
        console_source = _safe_int(console_quota.get("source"), -1)
        console_reset_at = max(0, _safe_int(console_quota.get("reset_at")))
        now_ms = int(time.time() * 1000)
        # Only block on the local Console estimator.  Historic REAL values
        # could have been populated from the unrelated auto quota endpoint.
        if (
            console_total > 0
            and console_remaining <= 0
            and console_source == 2
            and (console_reset_at <= 0 or console_reset_at > now_ms)
        ):
            reset_text = ""
            if console_reset_at > now_ms:
                reset_time = datetime.fromtimestamp(console_reset_at / 1000, tz=timezone.utc).astimezone()
                reset_text = f"预计恢复时间：{reset_time.strftime('%Y-%m-%d %H:%M')}。"
            raise GrokAccountChatTestError(
                f"该账号的 Console 对话额度已耗尽（0 / {console_total}）。"
                f"{reset_text}这是额度限流，不是账号封禁。请刷新状态和额度，或选择 Console 额度大于 0 的账号。",
                status_code=409,
                category="limited",
            )

        message, selected_model = self._validate_grok_chat_test_request(prompt, model)

        try:
            result = self._grok2api_client().chat_test(
                token,
                prompt=message,
                model=selected_model,
            )
        except GrokAccountChatTestError:
            raise
        except Exception as error:
            from app.dataplane.reverse.protocol.xai_usage import invalid_credentials_error_kind

            upstream_status = _safe_int(getattr(error, "status", 0))
            credential_kind = invalid_credentials_error_kind(error)
            if credential_kind == "blocked":
                raise GrokAccountChatTestError(
                    "上游明确返回账号已封禁、暂停或邮箱域名被拒绝",
                    status_code=403,
                    category="blocked",
                ) from error
            if credential_kind == "invalid":
                raise GrokAccountChatTestError(
                    "SSO 登录态已失效或被撤销，需要重新登录后再测",
                    status_code=401,
                    category="invalid",
                ) from error
            if upstream_status == 403:
                raise GrokAccountChatTestError(
                    "Console 权限被拒绝；没有收到封禁标记，不能据此判断账号被封",
                    status_code=403,
                    category="permission",
                ) from error
            if upstream_status == 401:
                raise GrokAccountChatTestError(
                    "Console 登录请求被拒绝，但上游未返回封禁或登录态失效标记，暂时无法确认",
                    status_code=401,
                    category="permission",
                ) from error
            if upstream_status == 429:
                raise GrokAccountChatTestError(
                    "Console 对话测试触发限流；这是额度或频率限制，不是封禁标记",
                    status_code=429,
                    category="limited",
                ) from error
            if upstream_status in {408, 504}:
                raise GrokAccountChatTestError("Console 对话测试超时", status_code=504) from error
            raise GrokAccountChatTestError(
                f"Console 对话测试失败: {self._grok2api_error_text(error, [token])}",
                status_code=502,
            ) from error

        content = _clean_text(result.get("content")) if isinstance(result, dict) else ""
        if not content:
            raise GrokAccountChatTestError("Console 未返回文本回复", status_code=502)
        return {
            "id": stable_id,
            "model": _clean_text(result.get("model")) if isinstance(result, dict) else selected_model,
            "content": content,
            "elapsed_ms": max(0, _safe_int(result.get("elapsed_ms"))) if isinstance(result, dict) else 0,
        }

    def count_grok_accounts(self) -> int:
        return grok_account_store.count()

    def refresh_grok_accounts_runtime(self, ids: list[str]) -> dict[str, Any]:
        ordered_ids = list(dict.fromkeys(_clean_text(value) for value in ids if _clean_text(value)))
        raw_items = grok_account_store.get_accounts_by_ids(ordered_ids)
        by_id = {_clean_text(item.get("id")): item for item in raw_items}
        account_tokens = [
            (account_id, _normalize_bridge_sso(by_id.get(account_id, {}).get("sso")))
            for account_id in ordered_ids
        ]
        valid_tokens = list(dict.fromkeys(token for _account_id, token in account_tokens if token))
        missing = sum(1 for _account_id, token in account_tokens if not token)
        if not valid_tokens:
            return {
                "summary": {"total": len(ordered_ids), "ok": 0, "fail": len(ordered_ids)},
                "results": [
                    {
                        "id": account_id,
                        "ok": False,
                        "refresh_status": "failed",
                        "error": "账号未保存 SSO 登录态",
                    }
                    for account_id, _token in account_tokens
                ],
            }
        try:
            result = self._grok2api_client().refresh(valid_tokens)
            summary = _batch_summary(result, len(valid_tokens))
            summary["total"] += missing
            summary["fail"] += missing
            source_results = result.get("results") if isinstance(result, dict) else {}
            source_results = source_results if isinstance(source_results, dict) else {}
            results: list[dict[str, Any]] = []
            for account_id, token in account_tokens:
                if not token:
                    results.append(
                        {
                            "id": account_id,
                            "ok": False,
                            "refresh_status": "failed",
                            "error": "账号未保存 SSO 登录态",
                        }
                    )
                    continue
                detail = source_results.get(_runtime_result_key(token))
                detail = detail if isinstance(detail, dict) else {}
                error = _clean_text(detail.get("error"))
                if detail:
                    ok = not error
                elif not source_results and summary["total"] == 1:
                    ok = summary["ok"] == 1 and summary["fail"] == 0
                    if not ok:
                        error = "上游未返回真实额度数据"
                else:
                    ok = False
                results.append(
                    {
                        "id": account_id,
                        "ok": ok,
                        "refresh_status": "success" if ok else "failed",
                        "error": error or ("运行时未返回刷新结果" if not detail and not ok else ""),
                    }
                )
            return {"summary": summary, "results": results}
        except Exception as error:
            return {
                "summary": {"total": len(ordered_ids), "ok": 0, "fail": len(ordered_ids)},
                "error": self._grok2api_error_text(error, valid_tokens),
            }

    def verify_grok_accounts_runtime(
        self,
        ids: list[str],
        *,
        cancel_event: Any | None = None,
    ) -> dict[str, Any]:
        """Verify registered Grok SSO sessions with one fast quota probe each.

        This intentionally accepts archive IDs rather than raw SSO values.  The
        runtime client receives the raw tokens, but this boundary only ever
        returns the stable account ID and a redacted quota brief.
        """
        ordered_ids = list(dict.fromkeys(_clean_text(value) for value in ids if _clean_text(value)))
        raw_items = grok_account_store.get_accounts_by_ids(ordered_ids)
        by_id = {_clean_text(item.get("id")): item for item in raw_items}
        candidates: list[tuple[str, str]] = []
        results: dict[str, dict[str, Any]] = {}

        for account_id in ordered_ids:
            item = by_id.get(account_id)
            if item is None:
                results[account_id] = {
                    "id": account_id,
                    "status": "invalid",
                    "error": "本地账号不存在",
                }
                continue
            token = _normalize_bridge_sso(item.get("sso"))
            if not token:
                results[account_id] = {
                    "id": account_id,
                    "status": "invalid",
                    "error": "账号未保存 SSO 登录态",
                }
                continue
            candidates.append((account_id, token))

        remote_by_token: dict[str, dict[str, Any]] = {}
        unique_tokens = list(dict.fromkeys(token for _, token in candidates))
        if unique_tokens:
            try:
                _raise_if_grok_probe_cancelled(cancel_event)
                client = self._grok2api_client()
                payload = (
                    client.verify(unique_tokens)
                    if cancel_event is None
                    else client.verify(unique_tokens, cancel_event=cancel_event)
                )
                _raise_if_grok_probe_cancelled(cancel_event)
            except _GrokProbeCancelled:
                raise
            except Exception as error:
                if cancel_event is not None and cancel_event.is_set():
                    raise _GrokProbeCancelled() from error
                message = self._grok2api_error_text(error, unique_tokens)
                for account_id, _token in candidates:
                    results[account_id] = {
                        "id": account_id,
                        "status": "unknown",
                        "error": message,
                    }
            else:
                source = payload.get("results") if isinstance(payload, dict) else []
                if isinstance(source, list):
                    for item in source:
                        if not isinstance(item, dict):
                            continue
                        token = _normalize_bridge_sso(item.get("token"))
                        if token:
                            remote_by_token[token] = item
                elif isinstance(source, dict):
                    # Accept the legacy keyed form too, while still stripping
                    # every token before returning data from this service.
                    for token, item in source.items():
                        normalized = _normalize_bridge_sso(token)
                        if normalized and isinstance(item, dict):
                            remote_by_token[normalized] = item

                for account_id, token in candidates:
                    remote = remote_by_token.get(token)
                    if not isinstance(remote, dict):
                        results[account_id] = {
                            "id": account_id,
                            "status": "unknown",
                            "error": "运行时未返回 fast 配额探针结果",
                        }
                        continue

                    status = _clean_text(remote.get("status")).lower()
                    if status not in {"valid", "invalid", "unknown"}:
                        status = "unknown"
                    result: dict[str, Any] = {"id": account_id, "status": status}
                    quota = _verify_quota_brief(remote.get("quota"))
                    if status == "valid" and quota is not None:
                        result["quota"] = quota
                    error_text = _clean_text(remote.get("error"))
                    if error_text:
                        result["error"] = self._grok2api_error_text(RuntimeError(error_text), [token])
                    elif status == "unknown":
                        result["error"] = "未确认登录态"
                    elif status == "invalid":
                        result["error"] = "登录态已失效或账号不可用"
                    results[account_id] = result

        ordered_results = [results[account_id] for account_id in ordered_ids if account_id in results]
        summary = {
            "total": len(ordered_results),
            "valid": sum(1 for item in ordered_results if item.get("status") == "valid"),
            "invalid": sum(1 for item in ordered_results if item.get("status") == "invalid"),
            "unknown": sum(1 for item in ordered_results if item.get("status") == "unknown"),
        }
        return {"summary": summary, "results": ordered_results}

    def _grok_recovery_error_text(self, error: Exception, secrets: list[str] | None = None) -> str:
        with self._lock:
            grok = self._config.get("grok") if isinstance(self._config.get("grok"), dict) else {}
            hidden = [
                _clean_text(grok.get("api_key")),
                _clean_text(self._config.get("proxy")),
                *[_clean_text(value) for value in (secrets or [])],
            ]
        return self._grok2api_error_text(error, hidden)

    def _authorize_grok_recovery_sso(self, *, email: str, password: str) -> str:
        from services.proxy_service import proxy_settings
        from services.xai_device_oauth_protocol import XaiDeviceOAuthProtocol

        with self._lock:
            runtime = self._runtime_config("grok")
        grok_config = runtime.get("grok") if isinstance(runtime.get("grok"), dict) else {}
        profile = proxy_settings.get_profile(proxy=_clean_text(runtime.get("proxy")), upstream=True)
        proxy = _clean_text(profile.proxy_url) or "direct"
        credential = XaiDeviceOAuthProtocol(grok_config, proxy=proxy).authorize(
            email=email,
            password=password,
            sso_only=True,
        )
        sso = _normalize_bridge_sso(credential.get("sso") if isinstance(credential, dict) else "")
        if not sso:
            raise RuntimeError("邮箱密码登录成功，但未获得新的 Grok SSO")
        return sso

    @staticmethod
    def _grok_recovery_due(item: dict[str, Any], *, now: datetime) -> bool:
        recovery = item.get("recovery") if isinstance(item.get("recovery"), dict) else {}
        next_attempt = _parse_datetime(recovery.get("next_attempt_at"))
        return next_attempt is None or now >= next_attempt

    @staticmethod
    def _grok_recovery_next_attempt_at(attempts: int, *, now: datetime) -> str:
        exponent = max(0, min(10, int(attempts) - 1))
        delay_minutes = min(
            _GROK_RECOVERY_MAX_DELAY_MINUTES,
            _GROK_RECOVERY_BASE_DELAY_MINUTES * (2 ** exponent),
        )
        return (now + timedelta(minutes=delay_minutes)).isoformat()

    @staticmethod
    def _grok_verification_for_token(payload: object, token: str) -> dict[str, Any]:
        normalized = _normalize_bridge_sso(token)
        source = payload.get("results") if isinstance(payload, dict) else []
        if isinstance(source, list):
            for item in source:
                if isinstance(item, dict) and _normalize_bridge_sso(item.get("token")) == normalized:
                    return dict(item)
        elif isinstance(source, dict):
            for key, item in source.items():
                if _normalize_bridge_sso(key) == normalized and isinstance(item, dict):
                    return dict(item)
        return {}

    def _recover_grok_account(
        self,
        account_id: str,
        client,
        *,
        stop_event: Any | None = None,
    ) -> dict[str, Any]:
        _raise_if_grok_probe_cancelled(stop_event)
        current_items = grok_account_store.get_accounts_by_ids([account_id])
        if not current_items:
            return {"status": "failed", "error": "本地账号不存在"}
        item = current_items[0]
        if _clean_text(item.get("status")).lower() != "active":
            return {"status": "deferred"}

        now = datetime.now(timezone.utc)
        if not self._grok_recovery_due(item, now=now):
            return {"status": "deferred"}

        credentials = grok_account_store.get_login_credentials(account_id) or {}
        email = _clean_text(credentials.get("email"))
        password = _clean_text(credentials.get("password"))
        old_sso = _normalize_bridge_sso(item.get("sso"))
        if not email or not password or not old_sso:
            error = "账号缺少已保存邮箱、密码或 SSO，无法自动恢复"
            grok_account_store.update_recovery_state(
                account_id,
                status="failed",
                next_attempt_at="",
                error=error,
            )
            return {"status": "failed", "error": error}

        recovery = item.get("recovery") if isinstance(item.get("recovery"), dict) else {}
        attempts = max(0, _safe_int(recovery.get("attempts"))) + 1
        attempted_at = now.isoformat()
        grok_account_store.update_recovery_state(
            account_id,
            status="running",
            last_attempt_at=attempted_at,
            next_attempt_at="",
            error="",
            attempts=attempts,
        )

        new_sso = ""
        added_new = False
        old_deleted = False

        def rollback_runtime_changes() -> None:
            nonlocal old_deleted
            if old_deleted:
                try:
                    restored = client.add([old_sso])
                    old_restored = not isinstance(restored, dict) or (
                        max(0, _safe_int(restored.get("count"))) > 0
                        or max(0, _safe_int(restored.get("skipped"))) > 0
                    )
                except Exception:
                    old_restored = False
                if old_restored and added_new:
                    try:
                        client.delete([new_sso])
                    except Exception:
                        pass
            elif added_new:
                try:
                    client.delete([new_sso])
                except Exception:
                    pass

        try:
            new_sso = self._authorize_grok_recovery_sso(email=email, password=password)
            _raise_if_grok_probe_cancelled(stop_event)
            if new_sso != old_sso:
                add_result = client.add([new_sso])
                added = max(0, _safe_int(add_result.get("count"))) if isinstance(add_result, dict) else 0
                skipped = max(0, _safe_int(add_result.get("skipped"))) if isinstance(add_result, dict) else 0
                if added < 1 and skipped < 1:
                    raise RuntimeError("内置 Grok 运行时未确认新 SSO 已加入")
                added_new = added > 0
                _raise_if_grok_probe_cancelled(stop_event)

            verify_payload = (
                client.verify([new_sso])
                if stop_event is None
                else client.verify([new_sso], cancel_event=stop_event)
            )
            _raise_if_grok_probe_cancelled(stop_event)
            verification = self._grok_verification_for_token(verify_payload, new_sso)
            verify_status = _clean_text(verification.get("status")).lower()
            if verify_status != "valid":
                detail = _clean_text(verification.get("error"))
                raise RuntimeError(detail or "新 Grok SSO 验证未通过")
            quota = _verify_quota_brief(verification.get("quota"))

            if new_sso != old_sso:
                _raise_if_grok_probe_cancelled(stop_event)
                delete_result = client.delete([old_sso])
                if isinstance(delete_result, dict) and "deleted" in delete_result:
                    if max(0, _safe_int(delete_result.get("deleted"))) < 1:
                        raise RuntimeError("内置 Grok 运行时未确认旧 SSO 已删除")
                old_deleted = True
                _raise_if_grok_probe_cancelled(stop_event)

            recovered_at = _now()
            grok_account_store.replace_sso_after_recovery(
                account_id,
                expected_sso=old_sso,
                new_sso=new_sso,
                recovered_at=recovered_at,
                quota=quota,
            )
            try:
                self._reconcile_grok_runtime_archive(client)
            except Exception:
                pass
            return {"status": "success"}
        except _GrokProbeCancelled:
            rollback_runtime_changes()
            grok_account_store.update_recovery_state(
                account_id,
                status="pending",
                last_attempt_at=attempted_at,
                next_attempt_at="",
                error="",
                attempts=max(0, attempts - 1),
            )
            raise
        except Exception as error:
            rollback_runtime_changes()

            safe_error = self._grok_recovery_error_text(
                error,
                [email, password, old_sso, new_sso],
            )
            failed_at = datetime.now(timezone.utc)
            grok_account_store.update_recovery_state(
                account_id,
                status="failed",
                last_attempt_at=attempted_at,
                next_attempt_at=self._grok_recovery_next_attempt_at(attempts, now=failed_at),
                error=safe_error,
                attempts=attempts,
            )
            return {"status": "failed", "error": safe_error}

    @staticmethod
    def _grok_oauth_recovery_due(
        item: dict[str, Any],
        *,
        now: datetime,
        reclaim_inflight: bool = False,
    ) -> bool:
        recovery = item.get("recovery") if isinstance(item.get("recovery"), dict) else {}
        status = _clean_text(recovery.get("status")).lower()
        if reclaim_inflight and status in {"pending", "running"}:
            return True
        last_attempt = _parse_datetime(recovery.get("last_attempt_at"))
        if status in {"pending", "running"} and last_attempt is not None:
            if now < last_attempt + timedelta(minutes=_GROK_OAUTH_RECOVERY_STALE_MINUTES):
                return False
        next_attempt = _parse_datetime(recovery.get("next_attempt_at"))
        return next_attempt is None or now >= next_attempt

    def _recover_grok_oauth_account(
        self,
        oauth_item: dict[str, Any],
        source_by_email: dict[str, dict[str, Any]],
        *,
        reclaim_inflight: bool = False,
    ) -> dict[str, Any]:
        oauth_account_id = _clean_text(oauth_item.get("id"))
        if not oauth_account_id or _clean_text(oauth_item.get("status")).lower() == "disabled":
            return {"status": "deferred"}
        probe = oauth_item.get("probe") if isinstance(oauth_item.get("probe"), dict) else {}
        if _clean_text(probe.get("status")).lower() != "invalid":
            return {"status": "deferred"}

        now = datetime.now(timezone.utc)
        if not self._grok_oauth_recovery_due(
            oauth_item,
            now=now,
            reclaim_inflight=reclaim_inflight,
        ):
            return {"status": "deferred"}

        email = _clean_text(oauth_item.get("email"))
        source = source_by_email.get(email.lower()) if email else None
        source = source if isinstance(source, dict) else {}
        source_account_id = _clean_text(source.get("id"))
        password = _clean_text(source.get("password"))
        recovery = oauth_item.get("recovery") if isinstance(oauth_item.get("recovery"), dict) else {}
        attempts = max(0, _safe_int(recovery.get("attempts"))) + 1
        attempted_at = now.isoformat()

        if not source_account_id or not email or not password:
            error = "未找到同邮箱且保存了密码的 Grok 注册账号，无法自动重新授权"
            xai_cli_oauth_store.update_recovery_state(
                oauth_account_id,
                status="failed",
                source_account_id=source_account_id,
                last_attempt_at=attempted_at,
                next_attempt_at=self._grok_recovery_next_attempt_at(attempts, now=now),
                attempts=attempts,
                error=error,
            )
            return {"status": "failed", "error": error}
        if self._grok_oauth_protocol_sink is None:
            return {"status": "deferred"}

        try:
            started = self._grok_oauth_protocol_sink(source_account_id, retry=True)
            job = started.get("job") if isinstance(started, dict) and isinstance(started.get("job"), dict) else {}
            job_id = _clean_text(job.get("id"))
            if not job_id:
                raise RuntimeError("OAuth 重新授权任务未返回任务 ID")
            xai_cli_oauth_store.update_recovery_state(
                oauth_account_id,
                status="pending",
                job_id=job_id,
                source_account_id=source_account_id,
                last_attempt_at=attempted_at,
                next_attempt_at="",
                attempts=attempts,
                error="",
            )
            return {"status": "queued", "reused": bool(started.get("reused")), "job_id": job_id}
        except Exception as error:
            safe_error = self._grok_recovery_error_text(error, [email, password])
            failed_at = datetime.now(timezone.utc)
            xai_cli_oauth_store.update_recovery_state(
                oauth_account_id,
                status="failed",
                source_account_id=source_account_id,
                last_attempt_at=attempted_at,
                next_attempt_at=self._grok_recovery_next_attempt_at(attempts, now=failed_at),
                attempts=attempts,
                error=safe_error,
            )
            return {"status": "failed", "error": safe_error}

    def _grok_probe_config_locked(self) -> dict[str, Any]:
        grok = self._config.get("grok") if isinstance(self._config.get("grok"), dict) else {}
        probe = _normalize_grok_probe_config(grok.get("probe_scheduler"))
        grok["probe_scheduler"] = probe
        self._config["grok"] = grok
        return probe

    def _grok_probe_due_locked(self, probe: dict[str, Any]) -> bool:
        last_finished = _parse_datetime(probe.get("last_finished_at"))
        if last_finished is None:
            return True
        interval = max(5, _safe_int(probe.get("interval_minutes"), 60) or 60)
        return datetime.now(timezone.utc) >= last_finished + timedelta(minutes=interval)

    def _grok_probe_next_run_at_locked(self, probe: dict[str, Any]) -> str:
        last_finished = _parse_datetime(probe.get("last_finished_at"))
        if last_finished is None:
            return _now()
        interval = max(5, _safe_int(probe.get("interval_minutes"), 60) or 60)
        return (last_finished + timedelta(minutes=interval)).isoformat()

    @staticmethod
    def _grok_oauth_recovery_sweep_due(probe: dict[str, Any]) -> bool:
        last_finished = _parse_datetime(probe.get("last_finished_at"))
        if last_finished is None:
            return False
        last_sweep = _parse_datetime(probe.get("oauth_recovery_last_sweep_at"))
        return last_sweep is None or last_sweep < last_finished

    @staticmethod
    def _grok_oauth_has_inflight_recovery() -> bool:
        try:
            items = xai_cli_oauth_store.list_accounts(redacted=True)
        except Exception:
            return False
        return any(
            _clean_text(item.get("status")).lower() != "disabled"
            and _clean_text(
                item.get("probe", {}).get("status") if isinstance(item.get("probe"), dict) else ""
            ).lower()
            == "invalid"
            and _clean_text(
                item.get("recovery", {}).get("status") if isinstance(item.get("recovery"), dict) else ""
            ).lower()
            in {"pending", "running"}
            for item in items
            if isinstance(item, dict)
        )

    @staticmethod
    def _grok_oauth_unlinked_sources() -> tuple[list[dict[str, Any]], int]:
        oauth_emails = {
            _clean_text(item.get("email")).lower()
            for item in xai_cli_oauth_store.list_accounts(redacted=False)
            if isinstance(item, dict) and _clean_text(item.get("email"))
        }
        sources_by_email: dict[str, dict[str, Any]] = {}
        missing_credentials = 0
        for item in grok_account_store.list_accounts(redacted=False):
            if not isinstance(item, dict):
                continue
            email = _clean_text(item.get("email")).lower()
            if not email or email in oauth_emails or email in sources_by_email:
                continue
            if _clean_text(item.get("status")).lower() != "active" or not _clean_text(item.get("sso")):
                continue
            if not _clean_text(item.get("id")) or not _clean_text(item.get("password")):
                missing_credentials += 1
                continue
            sources_by_email[email] = item
        sources = list(sources_by_email.values())
        sources.sort(key=lambda item: (_clean_text(item.get("created_at")), _clean_text(item.get("id"))))
        return sources, missing_credentials

    def _grok_oauth_has_unlinked_accounts(self) -> bool:
        try:
            sources, _missing_credentials = self._grok_oauth_unlinked_sources()
        except Exception:
            return False
        return bool(sources)

    def _queue_unlinked_grok_oauth_accounts(
        self,
        *,
        limit: int | None = None,
        stop_event: Any | None = None,
    ) -> dict[str, int]:
        summary = {
            "eligible": 0,
            "attempted": 0,
            "queued": 0,
            "reused": 0,
            "failed": 0,
            "deferred": 0,
            "missing_credentials": 0,
        }
        with self._lock:
            grok = self._config.get("grok") if isinstance(self._config.get("grok"), dict) else {}
            enabled = _safe_bool(grok.get("xai_cli_oauth_enabled"), True)
        if not enabled or self._grok_oauth_protocol_sink is None:
            return summary

        sources, missing_credentials = self._grok_oauth_unlinked_sources()
        summary["eligible"] = len(sources)
        summary["missing_credentials"] = missing_credentials
        max_attempts = len(sources) if limit is None else max(0, int(limit))
        for index, source in enumerate(sources):
            _raise_if_grok_probe_cancelled(stop_event)
            if summary["attempted"] >= max_attempts:
                summary["deferred"] += len(sources) - index
                break
            account_id = _clean_text(source.get("id"))
            try:
                started = self._grok_oauth_protocol_sink(account_id)
                if not isinstance(started, dict):
                    raise RuntimeError("OAuth 协议授权任务未返回有效状态")
            except Exception:
                summary["attempted"] += 1
                summary["failed"] += 1
                continue
            summary["attempted"] += 1
            if bool(started.get("reused")):
                summary["reused"] += 1
            else:
                summary["queued"] += 1
        return summary

    def _append_grok_probe_event_locked(self, message: str, level: str = "info") -> None:
        probe = self._grok_probe_config_locked()
        events = probe.get("events") if isinstance(probe.get("events"), list) else []
        normalized_level = _clean_text(level).lower()
        events.append(
            {
                "id": uuid.uuid4().hex,
                "at": _now(),
                "level": normalized_level
                if normalized_level in {"info", "success", "warning", "error"}
                else "info",
                "message": _clean_text(message)[:500],
            }
        )
        probe["events"] = events[-50:]

    def grok_probe_scheduler_status(self) -> dict[str, Any]:
        with self._lock:
            probe = json.loads(json.dumps(self._grok_probe_config_locked(), ensure_ascii=False))
            probe.update(
                {
                    "running": self._grok_probe_running,
                    "next_run_at": self._grok_probe_next_run_at_locked(probe) if probe["enabled"] else "",
                }
            )
            return probe

    def set_grok_probe_scheduler_enabled(self, enabled: bool) -> dict[str, Any]:
        target = bool(enabled)
        with self._lock:
            probe = self._grok_probe_config_locked()
            changed = bool(probe.get("enabled")) != target
            probe["enabled"] = target
            if changed and target:
                # An explicit start should not wait for a stale interval deadline.
                probe["last_finished_at"] = ""
                # A new run gets a new signal.  An old cancelled run keeps its
                # own set event and cannot resume after a rapid off/on toggle.
                self._grok_probe_cancel_event = threading.Event()
            elif not target:
                self._grok_probe_cancel_event.set()
            if changed:
                self._append_grok_probe_event_locked(
                    "Grok 账号自动轮询已开启" if target else "Grok 账号自动轮询已关闭",
                    "success" if target else "warning",
                )
            self._save()
        self._grok_probe_wake_event.set()
        if not target:
            # Cancellable HTTP probes observe the signal within 100 ms.  Do not
            # hold the configuration lock while the worker unwinds.
            self._grok_probe_idle_event.wait(timeout=1.0)
        return self.grok_probe_scheduler_status()

    def start_grok_probe_scheduler(self, stop_event: threading.Event) -> threading.Thread:
        with self._lock:
            thread = self._grok_probe_scheduler_thread
            if thread is not None and thread.is_alive():
                return thread
            self._grok_probe_wake_event.clear()
            thread = threading.Thread(
                target=self._run_grok_probe_scheduler,
                args=(stop_event,),
                daemon=True,
                name="grok-account-probe-scheduler",
            )
            self._grok_probe_scheduler_thread = thread
            thread.start()
            return thread

    def stop_grok_probe_scheduler(self) -> None:
        self._grok_probe_cancel_event.set()
        self._grok_probe_wake_event.set()
        with self._lock:
            thread = self._grok_probe_scheduler_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _run_grok_probe_scheduler(self, stop_event: threading.Event) -> None:
        try:
            startup_checked = False
            while not stop_event.is_set():
                with self._lock:
                    probe = self._grok_probe_config_locked()
                    enabled = bool(probe.get("enabled"))
                    cancel_event = self._grok_probe_cancel_event
                if not enabled:
                    self._grok_probe_wake_event.wait(30)
                    self._grok_probe_wake_event.clear()
                    continue

                cycle_stop = _GrokProbeStopSignal(stop_event, cancel_event)
                if not startup_checked:
                    recovery_sweep_due = self._grok_oauth_recovery_sweep_due(probe)
                    recovery_sweep_due = (
                        recovery_sweep_due
                        or self._grok_oauth_has_inflight_recovery()
                        or self._grok_oauth_has_unlinked_accounts()
                    )
                    if recovery_sweep_due and not cycle_stop.is_set():
                        self._run_grok_oauth_recovery_sweep(
                            reclaim_inflight=True,
                            stop_event=cycle_stop,
                        )
                    startup_checked = True

                permission_retry = self._run_grok_permission_retry_once(cycle_stop)
                if cycle_stop.is_set():
                    continue
                with self._lock:
                    probe = self._grok_probe_config_locked()
                    should_run = bool(probe.get("enabled")) and self._grok_probe_due_locked(probe)
                if should_run:
                    try:
                        self._run_grok_probe_once(cycle_stop)
                    except _GrokProbeCancelled:
                        pass
                    continue
                if permission_retry.get("tested", 0) > 0:
                    continue
                self._grok_probe_wake_event.wait(30)
                self._grok_probe_wake_event.clear()
        finally:
            with self._lock:
                self._grok_probe_running = False
                self._grok_probe_idle_event.set()
                self._grok_probe_scheduler_thread = None

    def _run_grok_permission_retry_once(
        self,
        stop_event: Any | None = None,
    ) -> dict[str, int]:
        summary = {"eligible": 0, "tested": 0, "valid": 0, "pending": 0, "failed": 0, "uploaded": 0}
        if stop_event is not None and stop_event.is_set():
            return summary

        due_before = datetime.now(timezone.utc) - timedelta(seconds=_GROK_PERMISSION_RETRY_DELAY_SECONDS)
        candidates: list[dict[str, Any]] = []
        for item in xai_cli_oauth_store.list_accounts(redacted=False):
            if not isinstance(item, dict) or _clean_text(item.get("status")).lower() == "disabled":
                continue
            probe = item.get("probe") if isinstance(item.get("probe"), dict) else {}
            if _clean_text(probe.get("code")).lower() != "permission-denied":
                continue
            probed_at = _parse_datetime(probe.get("at"))
            if probed_at is None or probed_at > due_before:
                continue
            candidates.append(item)
        candidates.sort(
            key=lambda item: _clean_text(
                item.get("probe", {}).get("at") if isinstance(item.get("probe"), dict) else ""
            )
        )
        summary["eligible"] = len(candidates)
        selected = candidates[:_GROK_PERMISSION_RETRY_BATCH_SIZE]
        if not selected:
            return summary

        try:
            from services.xai_cli_oauth_service import xai_cli_oauth_service

            payload = asyncio.run(
                _await_grok_probe_with_cancel(
                    xai_cli_oauth_service.probe_accounts(
                        [_clean_text(item.get("id")) for item in selected],
                        concurrency=_GROK_PERMISSION_RETRY_BATCH_SIZE,
                    ),
                    stop_event,
                )
            )
            results = payload.get("results") if isinstance(payload, dict) else []
            results = results if isinstance(results, list) else []
            summary["tested"] = len(results)
            summary["valid"] = sum(
                1 for item in results if _clean_text(item.get("status")).lower() == "valid"
            )
            summary["pending"] = sum(
                1 for item in results if _clean_text(item.get("code")).lower() == "permission-denied"
            )
            summary["failed"] = max(0, summary["tested"] - summary["valid"] - summary["pending"])
            result_summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
            summary["uploaded"] = max(0, _safe_int(result_summary.get("delivery_success")))
            level = "green" if summary["valid"] else "yellow"
            self._append_grok_oauth_log(
                "Grok OAuth 权限延迟复检完成："
                f"恢复 {summary['valid']}，待生效 {summary['pending']}，"
                f"其他失败 {summary['failed']}，已上传 {summary['uploaded']}",
                level,
            )
        except _GrokProbeCancelled:
            return summary
        except Exception as error:
            summary["failed"] = len(selected)
            self._append_grok_oauth_log(
                f"Grok OAuth 权限延迟复检失败：{self._grok2api_error_text(error)}",
                "red",
            )
        return summary

    def _run_grok_oauth_recovery_sweep(
        self,
        *,
        limit: int | None = None,
        reclaim_inflight: bool = False,
        stop_event: Any | None = None,
    ) -> dict[str, int]:
        summary = {
            "eligible": 0,
            "attempted": 0,
            "queued": 0,
            "failed": 0,
            "deferred": 0,
            "backfill_eligible": 0,
            "backfill_attempted": 0,
            "backfill_queued": 0,
            "backfill_reused": 0,
            "backfill_failed": 0,
            "backfill_deferred": 0,
            "backfill_missing_credentials": 0,
        }
        error_text = ""
        cancelled = False
        try:
            _raise_if_grok_probe_cancelled(stop_event)
            oauth_items = xai_cli_oauth_store.list_accounts(redacted=False)
            candidates = [
                item
                for item in oauth_items
                if _clean_text(item.get("status")).lower() != "disabled"
                and _clean_text(
                    item.get("probe", {}).get("status") if isinstance(item.get("probe"), dict) else ""
                ).lower()
                == "invalid"
                and _clean_text(
                    item.get("probe", {}).get("code") if isinstance(item.get("probe"), dict) else ""
                ).lower()
                != "permission-denied"
            ]
            candidates.sort(
                key=lambda item: 0
                if _clean_text(
                    item.get("recovery", {}).get("status")
                    if isinstance(item.get("recovery"), dict)
                    else ""
                ).lower()
                in {"pending", "running"}
                else 1
            )
            summary["eligible"] = len(candidates)
            source_by_email = {
                _clean_text(item.get("email")).lower(): item
                for item in grok_account_store.list_accounts(redacted=False)
                if _clean_text(item.get("email"))
                and _clean_text(item.get("password"))
                and _clean_text(item.get("status")).lower() == "active"
            }
            max_attempts = len(candidates) if limit is None else max(0, int(limit))
            for index, candidate in enumerate(candidates):
                _raise_if_grok_probe_cancelled(stop_event)
                if summary["attempted"] >= max_attempts:
                    summary["deferred"] += len(candidates) - index
                    break
                recovery = self._recover_grok_oauth_account(
                    candidate,
                    source_by_email,
                    reclaim_inflight=reclaim_inflight,
                )
                status = _clean_text(recovery.get("status")).lower()
                if status == "queued":
                    summary["attempted"] += 1
                    summary["queued"] += 1
                elif status == "failed":
                    summary["attempted"] += 1
                    summary["failed"] += 1
                else:
                    summary["deferred"] += 1
            backfill = self._queue_unlinked_grok_oauth_accounts(stop_event=stop_event)
            for key, value in backfill.items():
                summary[f"backfill_{key}"] = value
        except _GrokProbeCancelled:
            cancelled = True
        except Exception as error:
            error_text = self._grok_recovery_error_text(error)
        finally:
            if not cancelled:
                swept_at = _now()
                with self._lock:
                    probe = self._grok_probe_config_locked()
                    probe["oauth_recovery_last_sweep_at"] = swept_at
                    if error_text:
                        message = f"Grok OAuth 恢复扫描失败：{error_text}"
                        level = "error"
                    else:
                        message = (
                            "Grok OAuth 恢复扫描完成："
                            f"失效恢复排队 {summary['queued']}，启动失败 {summary['failed']}，延后 {summary['deferred']}；"
                            f"未授权新排队 {summary['backfill_queued']}，队列中 {summary['backfill_reused']}，"
                            f"启动失败 {summary['backfill_failed']}，延后 {summary['backfill_deferred']}"
                        )
                        level = "warning" if (
                            summary["failed"]
                            or summary["deferred"]
                            or summary["backfill_failed"]
                            or summary["backfill_deferred"]
                            or summary["backfill_missing_credentials"]
                        ) else "success"
                    self._append_grok_probe_event_locked(message, level)
                    self._save()
                self._append_grok_oauth_log(
                    message,
                    "red" if level == "error" else "yellow" if level == "warning" else "green",
                )
        return summary

    def _run_grok_probe_once(self, stop_event: Any | None = None) -> dict[str, int]:
        _raise_if_grok_probe_cancelled(stop_event)
        with self._lock:
            if self._grok_probe_running:
                return {}
            self._grok_probe_running = True
            self._grok_probe_idle_event.clear()
            probe = self._grok_probe_config_locked()
            probe["last_started_at"] = _now()
            probe["last_error"] = ""
            batch_size = max(1, min(500, _safe_int(probe.get("batch_size"), 50) or 50))
            self._append_grok_probe_event_locked("Grok 账号探测开始", "info")
            self._save()

        summary = {
            "eligible": 0,
            "skipped": 0,
            "total": 0,
            "valid": 0,
            "invalid": 0,
            "unknown": 0,
            "batches": 0,
            "recovery_attempted": 0,
            "recovery_succeeded": 0,
            "recovery_failed": 0,
            "recovery_deferred": 0,
            "oauth_eligible": 0,
            "oauth_skipped": 0,
            "oauth_total": 0,
            "oauth_valid": 0,
            "oauth_limited": 0,
            "oauth_invalid": 0,
            "oauth_unknown": 0,
            "oauth_batches": 0,
            "oauth_recovery_attempted": 0,
            "oauth_recovery_queued": 0,
            "oauth_recovery_failed": 0,
            "oauth_recovery_deferred": 0,
            "oauth_backfill_eligible": 0,
            "oauth_backfill_attempted": 0,
            "oauth_backfill_queued": 0,
            "oauth_backfill_reused": 0,
            "oauth_backfill_failed": 0,
            "oauth_backfill_deferred": 0,
            "oauth_backfill_missing_credentials": 0,
        }
        errors: list[str] = []
        cancelled = False
        try:
            try:
                _raise_if_grok_probe_cancelled(stop_event)
                client = self._grok2api_client()
                list_payload = (
                    client.list()
                    if stop_event is None
                    else client.list(cancel_event=stop_event)
                )
                remote_items = self._runtime_tokens(list_payload)
                _raise_if_grok_probe_cancelled(stop_event)
                grok_account_store.reconcile_runtime_accounts(remote_items)
                remote_by_token = {
                    token: item
                    for item in remote_items
                    if (token := _normalize_bridge_sso(item.get("token")))
                }
                raw_items = grok_account_store.list_accounts(redacted=False)
                raw_by_id = {
                    _clean_text(item.get("id")): item
                    for item in raw_items
                    if _clean_text(item.get("id"))
                }
                eligible_ids: list[str] = []
                for item in raw_items:
                    account_id = _clean_text(item.get("id"))
                    token = _normalize_bridge_sso(item.get("sso"))
                    remote = remote_by_token.get(token) if token else None
                    remote_status = _clean_text(remote.get("status")).lower() if isinstance(remote, dict) else ""
                    if account_id and token and isinstance(remote, dict) and remote_status != "disabled":
                        eligible_ids.append(account_id)
                summary["eligible"] = len(eligible_ids)
                summary["skipped"] = max(0, len(raw_items) - len(eligible_ids))

                for offset in range(0, len(eligible_ids), batch_size):
                    _raise_if_grok_probe_cancelled(stop_event)
                    batch_ids = eligible_ids[offset:offset + batch_size]
                    payload = self.verify_grok_accounts_runtime(
                        batch_ids,
                        cancel_event=stop_event,
                    )
                    _raise_if_grok_probe_cancelled(stop_event)
                    results = payload.get("results") if isinstance(payload, dict) else []
                    results = results if isinstance(results, list) else []
                    probed_at = _now()
                    grok_account_store.update_probe_results(results, probed_at=probed_at)
                    summary["batches"] += 1
                    summary["total"] += len(results)
                    for result in results:
                        status = _clean_text(result.get("status")).lower() if isinstance(result, dict) else "unknown"
                        summary[status if status in {"valid", "invalid", "unknown"} else "unknown"] += 1
                        if status != "invalid" or not isinstance(result, dict):
                            continue
                        account_id = _clean_text(result.get("id"))
                        candidate = raw_by_id.get(account_id)
                        if not isinstance(candidate, dict):
                            continue
                        _raise_if_grok_probe_cancelled(stop_event)
                        recovery = self._recover_grok_account(
                            account_id,
                            client,
                            stop_event=stop_event,
                        )
                        recovery_status = _clean_text(recovery.get("status")).lower()
                        if recovery_status == "success":
                            summary["recovery_attempted"] += 1
                            summary["recovery_succeeded"] += 1
                        elif recovery_status == "failed":
                            summary["recovery_attempted"] += 1
                            summary["recovery_failed"] += 1
                        else:
                            summary["recovery_deferred"] += 1
            except _GrokProbeCancelled:
                raise
            except Exception as error:
                errors.append(f"SSO: {self._grok2api_error_text(error)}")

            try:
                _raise_if_grok_probe_cancelled(stop_event)
                from services.xai_cli_oauth_service import XaiCliOAuthService, xai_cli_oauth_service

                oauth_items = xai_cli_oauth_store.list_accounts(redacted=False)
                oauth_by_id = {
                    _clean_text(item.get("id")): item
                    for item in oauth_items
                    if _clean_text(item.get("id"))
                }
                source_by_email = {
                    _clean_text(item.get("email")).lower(): item
                    for item in grok_account_store.list_accounts(redacted=False)
                    if _clean_text(item.get("email"))
                    and _clean_text(item.get("password"))
                    and _clean_text(item.get("status")).lower() == "active"
                }
                oauth_service = (
                    xai_cli_oauth_service
                    if xai_cli_oauth_service.store is xai_cli_oauth_store
                    else XaiCliOAuthService(xai_cli_oauth_store)
                )
                oauth_eligible_ids = [
                    _clean_text(item.get("id"))
                    for item in oauth_items
                    if _clean_text(item.get("id"))
                    and _clean_text(item.get("status")).lower() != "disabled"
                    and _clean_text(item.get("refresh_token"))
                ]
                summary["oauth_eligible"] = len(oauth_eligible_ids)
                summary["oauth_skipped"] = max(0, len(oauth_items) - len(oauth_eligible_ids))
                for offset in range(0, len(oauth_eligible_ids), batch_size):
                    _raise_if_grok_probe_cancelled(stop_event)
                    batch_ids = oauth_eligible_ids[offset:offset + batch_size]
                    payload = asyncio.run(
                        _await_grok_probe_with_cancel(
                            oauth_service.probe_accounts(
                                batch_ids,
                                concurrency=min(10, len(batch_ids)),
                            ),
                            stop_event,
                        )
                    )
                    _raise_if_grok_probe_cancelled(stop_event)
                    results = payload.get("results") if isinstance(payload, dict) else []
                    results = results if isinstance(results, list) else []
                    summary["oauth_batches"] += 1
                    summary["oauth_total"] += len(results)
                    for result in results:
                        status = _clean_text(result.get("status")).lower() if isinstance(result, dict) else "unknown"
                        key = status if status in {"valid", "limited", "invalid", "unknown"} else "unknown"
                        summary[f"oauth_{key}"] += 1
                        if status != "invalid" or not isinstance(result, dict):
                            continue
                        oauth_account_id = _clean_text(result.get("account_id"))
                        candidate = oauth_by_id.get(oauth_account_id)
                        if not isinstance(candidate, dict):
                            summary["oauth_recovery_deferred"] += 1
                            continue
                        _raise_if_grok_probe_cancelled(stop_event)
                        recovery = self._recover_grok_oauth_account(
                            {**candidate, "status": "invalid", "probe": {"status": "invalid"}},
                            source_by_email,
                        )
                        recovery_status = _clean_text(recovery.get("status")).lower()
                        if recovery_status == "queued":
                            summary["oauth_recovery_attempted"] += 1
                            summary["oauth_recovery_queued"] += 1
                        elif recovery_status == "failed":
                            summary["oauth_recovery_attempted"] += 1
                            summary["oauth_recovery_failed"] += 1
                        else:
                            summary["oauth_recovery_deferred"] += 1
            except _GrokProbeCancelled:
                raise
            except Exception as error:
                errors.append(f"OAuth: {self._grok2api_error_text(error)}")

            try:
                _raise_if_grok_probe_cancelled(stop_event)
                backfill = self._queue_unlinked_grok_oauth_accounts(stop_event=stop_event)
                for key, value in backfill.items():
                    summary[f"oauth_backfill_{key}"] = value
            except _GrokProbeCancelled:
                raise
            except Exception as error:
                errors.append(f"OAuth 回填: {self._grok2api_error_text(error)}")
        except _GrokProbeCancelled:
            cancelled = True
        finally:
            error_text = "；".join(errors)
            finished_at = _now()
            with self._lock:
                probe = self._grok_probe_config_locked()
                if not cancelled:
                    probe["last_finished_at"] = finished_at
                    probe["oauth_recovery_last_sweep_at"] = finished_at
                probe["last_result"] = dict(summary)
                probe["last_error"] = "" if cancelled else error_text[:500]
                self._grok_probe_running = False
                self._grok_probe_idle_event.set()
                if cancelled:
                    message = "Grok 账号探测已停止"
                    level = "warning"
                elif error_text:
                    message = f"Grok 账号探测失败：{error_text}"
                    level = "error"
                else:
                    unresolved_invalid = max(0, summary["invalid"] - summary["recovery_succeeded"])
                    message = (
                        "Grok 账号探测完成："
                        f"SSO 有效 {summary['valid']}，失效 {summary['invalid']}，未知 {summary['unknown']}；"
                        f"OAuth 有效 {summary['oauth_valid']}，限流 {summary['oauth_limited']}，"
                        f"失效 {summary['oauth_invalid']}，未知 {summary['oauth_unknown']}；"
                        f"SSO 自动恢复 {summary['recovery_succeeded']}/{summary['recovery_attempted']}，"
                        f"OAuth 恢复排队 {summary['oauth_recovery_queued']}/{summary['oauth_recovery_attempted']}，"
                        f"未授权回填新排队 {summary['oauth_backfill_queued']}，"
                        f"队列中 {summary['oauth_backfill_reused']}，失败 {summary['oauth_backfill_failed']}；"
                        f"跳过 SSO {summary['skipped']} / OAuth {summary['oauth_skipped']}"
                    )
                    level = (
                        "warning"
                        if unresolved_invalid
                        or summary["unknown"]
                        or summary["recovery_failed"]
                        or summary["oauth_invalid"]
                        or summary["oauth_unknown"]
                        or summary["oauth_recovery_failed"]
                        or summary["oauth_backfill_failed"]
                        or summary["oauth_backfill_deferred"]
                        or summary["oauth_backfill_missing_credentials"]
                        else "success"
                    )
                self._append_grok_probe_event_locked(message, level)
                self._save()
        return summary

    def set_grok_accounts_disabled(self, ids: list[str], disabled: bool) -> dict[str, Any]:
        ordered_ids = list(dict.fromkeys(_clean_text(value) for value in ids if _clean_text(value)))
        raw_items = grok_account_store.get_accounts_by_ids(ordered_ids)
        tokens = list(
            dict.fromkeys(
                token
                for item in raw_items
                if (token := _normalize_bridge_sso(item.get("sso")))
            )
        )
        missing = len(ordered_ids) - len(tokens)
        if not tokens:
            return {"disabled": bool(disabled), "summary": {"total": len(ordered_ids), "ok": 0, "fail": len(ordered_ids)}}
        try:
            result = self._grok2api_client().set_disabled(tokens, bool(disabled))
            summary = _batch_summary(result, len(tokens))
            summary["total"] += missing
            summary["fail"] += missing
            return {"disabled": bool(disabled), "summary": summary}
        except Exception as error:
            return {
                "disabled": bool(disabled),
                "summary": {"total": len(ordered_ids), "ok": 0, "fail": len(ordered_ids)},
                "error": self._grok2api_error_text(error, tokens),
            }

    def delete_grok_accounts(self, ids: list[str], *, delete_upstream: bool = False) -> dict[str, int]:
        upstream_deleted = 0
        if delete_upstream:
            raw_items = grok_account_store.get_accounts_by_ids(ids)
            tokens = list(
                dict.fromkeys(
                    token
                    for item in raw_items
                    if (token := _normalize_bridge_sso(item.get("sso")))
                )
            )
            if tokens:
                try:
                    upstream = self._grok2api_client().delete(tokens)
                except Exception as error:
                    raise RuntimeError(self._grok2api_error_text(error, tokens)) from error
                upstream_deleted = max(0, _safe_int(upstream.get("deleted"))) if isinstance(upstream, dict) else 0
        result = grok_account_store.delete_accounts(ids)
        return {**result, "upstream_deleted": upstream_deleted}

    def export_grok_accounts(self) -> list[dict]:
        return grok_account_store.list_accounts(redacted=False)

    def export_grok_accounts_text(self) -> str:
        return grok_account_store.export_text()

    def _grok_oauth_export_accounts(self, ids: list[str] | None = None) -> list[dict[str, Any]]:
        selected_ids = list(dict.fromkeys(_clean_text(value) for value in (ids or []) if _clean_text(value)))
        selected = ids is not None
        source_accounts = (
            grok_account_store.get_accounts_by_ids(selected_ids)
            if selected
            else grok_account_store.list_accounts(redacted=False)
        )
        selected_emails = {
            _clean_text(item.get("email")).lower()
            for item in source_accounts
            if _clean_text(item.get("email"))
        }
        sso_by_email = {
            _clean_text(item.get("email")).lower(): _normalize_bridge_sso(item.get("sso"))
            for item in source_accounts
            if _clean_text(item.get("email")) and _normalize_bridge_sso(item.get("sso"))
        }
        result: list[dict[str, Any]] = []
        for account in xai_cli_oauth_store.list_accounts(redacted=False):
            if not _clean_text(account.get("access_token")) or not _clean_text(account.get("refresh_token")):
                continue
            exported = dict(account)
            email_key = _clean_text(exported.get("email")).lower()
            if selected and email_key not in selected_emails:
                continue
            if email_key and sso_by_email.get(email_key):
                exported["sso_token"] = sso_by_email[email_key]
            result.append(exported)
        return result

    def export_grok_accounts_sub2api(self, ids: list[str] | None = None) -> dict[str, Any]:
        accounts = [
            build_xai_oauth_export_account(account)
            for account in self._grok_oauth_export_accounts(ids)
        ]
        if not accounts:
            raise ValueError("暂无可导出的 Grok OAuth 账号")
        return {
            "exported_at": _now(),
            "proxies": [],
            "accounts": accounts,
        }

    def export_grok_accounts_cpa(self, ids: list[str] | None = None) -> bytes:
        accounts = self._grok_oauth_export_accounts(ids)
        if not accounts:
            raise ValueError("暂无可导出的 Grok OAuth 账号")
        buffer = io.BytesIO()
        used_names: set[str] = set()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for account in accounts:
                file_name, payload = cpa_service.build_xai_oauth_file(account)
                stem, suffix = file_name.rsplit(".", 1)
                unique_name = file_name
                duplicate = 2
                while unique_name in used_names:
                    unique_name = f"{stem}-{duplicate}.{suffix}"
                    duplicate += 1
                used_names.add(unique_name)
                archive.writestr(
                    unique_name,
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                )
        return buffer.getvalue()

    def _persist_grok_account_snapshot(self, payload: dict) -> dict:
        saved = grok_account_store.upsert(payload)
        item = saved.get("item") if isinstance(saved.get("item"), dict) else {}
        email = str(item.get("email") or "")
        status = str(item.get("status") or "active")
        if status == "active":
            config = self._grok_config_snapshot()
            runtime_synced = False
            if _safe_bool(config.get("grok2api_enabled"), False):
                token = _normalize_bridge_sso(item.get("sso"))
                try:
                    sync_result = self._sync_grok_account_to_runtime(item)
                except Exception as error:
                    self._append_log(
                        "导入内置 Grok 账号池失败: "
                        f"{email}，原因: {self._grok2api_error_text(error, [token])}",
                        "red",
                    )
                else:
                    runtime_synced = True
                    if bool(sync_result.get("verification_pending")):
                        verification_error = _clean_text(sync_result.get("verification_error")) or "上游暂未返回可用性结果"
                        self._append_log(
                            "Grok 账号已加入内置账号池，验证待复检: "
                            f"{email}，原因: {verification_error}",
                            "yellow",
                        )
            self._append_log(
                f"Grok 账号已保存{'并加入账号池' if runtime_synced else ''}：{email}",
                "green",
            )
        return saved

    def _persist_grok_worker_result(self, worker_result: dict) -> dict:
        payload = worker_result.get("result")
        if not isinstance(payload, dict):
            payload = worker_result.get("account")
        if not isinstance(payload, dict):
            payload = {
                key: value
                for key, value in worker_result.items()
                if key not in {"ok", "index", "error"}
            }
        nested_account = payload.get("account") if isinstance(payload.get("account"), dict) else None
        if nested_account and not any(payload.get(key) for key in ("email", "sso", "sso_token")):
            payload = nested_account
        return self._persist_grok_account_snapshot(payload)

    def _enqueue_grok_oauth_protocol(self, saved: dict[str, Any]) -> None:
        config = self._grok_config_snapshot()
        if not _safe_bool(config.get("xai_cli_oauth_enabled"), True):
            return
        item = saved.get("item") if isinstance(saved.get("item"), dict) else {}
        account_id = _clean_text(item.get("id"))
        email = _clean_text(item.get("email"))
        log_email = email or "未知账号"
        if not account_id or self._grok_oauth_protocol_sink is None:
            return
        try:
            started = self._grok_oauth_protocol_sink(account_id, prioritize=True)
        except Exception as error:
            self._append_grok_oauth_log(
                f"Grok OAuth 协议授权启动失败: {log_email}，原因: {self._grok2api_error_text(error)}",
                "red",
            )
            return
        if bool(started.get("reused")):
            state = "已接入"
        elif bool(started.get("queued")):
            state = "已进入即时上传队列"
        else:
            state = "已启动"
        self._append_grok_oauth_log(
            f"Grok OAuth 授权{state}：{log_email}",
            "yellow",
        )

    def handle_grok_oauth_protocol_event(self, event: dict[str, Any]) -> None:
        email = _clean_text(event.get("email"))
        log_email = email or "未知账号"
        job_id = _clean_text(event.get("job_id"))
        recovery_account = xai_cli_oauth_store.find_by_recovery_job_id(job_id) if job_id else None
        recovery_account_id = _clean_text(recovery_account.get("id")) if isinstance(recovery_account, dict) else ""
        if _clean_text(event.get("status")) == "permission_pending":
            self._append_grok_oauth_log(
                f"Grok OAuth 授权完成，Grok 4.5 权限待生效，已进入延迟复检：{log_email}",
                "yellow",
            )
            return
        if _clean_text(event.get("status")) != "authorized":
            error = self._grok2api_error_text(RuntimeError(_clean_text(event.get("error")) or "未知错误"))
            if recovery_account_id:
                recovery = (
                    recovery_account.get("recovery")
                    if isinstance(recovery_account.get("recovery"), dict)
                    else {}
                )
                attempts = max(1, _safe_int(recovery.get("attempts")))
                failed_at = datetime.now(timezone.utc)
                xai_cli_oauth_store.update_recovery_state(
                    recovery_account_id,
                    status="failed",
                    job_id="",
                    next_attempt_at=self._grok_recovery_next_attempt_at(attempts, now=failed_at),
                    attempts=attempts,
                    error=error,
                )
            else:
                self._append_grok_oauth_log(
                    f"Grok OAuth 协议授权失败：{log_email}，原因: {error}",
                    "red",
                )
            return

        if recovery_account_id:
            recovered_at = _now()
            xai_cli_oauth_store.update_recovery_state(
                recovery_account_id,
                status="success",
                job_id="",
                last_success_at=recovered_at,
                next_attempt_at="",
                attempts=0,
                error="",
            )

        delivery = event.get("delivery") if isinstance(event.get("delivery"), dict) else {}
        uploaded = [
            name
            for name, result in delivery.items()
            if isinstance(result, dict) and _clean_text(result.get("status")) == "success"
        ]
        failed = [
            name
            for name, result in delivery.items()
            if isinstance(result, dict) and _clean_text(result.get("status")) == "failed"
        ]
        if uploaded:
            targets = "、".join("NovaApi" if name == "sub2api" else name.upper() for name in uploaded)
            suffix = f"；{len(failed)} 个投递目标失败" if failed else ""
            action = "自动恢复完成" if recovery_account_id else "授权完成"
            self._append_grok_oauth_log(
                f"Grok OAuth {action}，已上传到 {targets}：{log_email}{suffix}",
                "green",
            )
        elif failed:
            targets = "、".join("NovaApi" if name == "sub2api" else name.upper() for name in failed)
            action = "自动恢复完成" if recovery_account_id else "授权完成"
            self._append_grok_oauth_log(
                f"Grok OAuth {action}，但上传到 {targets} 失败：{log_email}",
                "red",
            )
        else:
            action = "自动恢复完成" if recovery_account_id else "授权完成"
            self._append_grok_oauth_log(
                f"Grok OAuth {action}，未启用外部投递：{log_email}",
                "yellow",
            )

    def _append_log(self, text: str, color: str = "") -> None:
        with self._lock:
            self._logs.append({"time": _now(), "text": str(text), "level": str(color or "info")})
            self._logs = self._logs[-300:]

    def _append_grok_oauth_log(self, text: str, color: str = "") -> None:
        with self._lock:
            self._grok_oauth_logs.append({"time": _now(), "text": str(text), "level": str(color or "info")})
            self._grok_oauth_logs = self._grok_oauth_logs[-300:]

    def _append_checkout_log(self, text: str, color: str = "") -> None:
        with self._lock:
            self._checkout_logs.append({"time": _now(), "text": str(text), "level": str(color or "info")})
            self._checkout_logs = self._checkout_logs[-300:]

    def _upsert_checkout_task(self, payload: dict[str, Any]) -> None:
        """Store one credential-free progress record for the Checkout table.

        The registration workers run concurrently, so all update matching is
        by the generated task id rather than task index.  A run id gate also
        prevents a worker that outlives a reset from restoring stale rows.
        """
        if not isinstance(payload, dict):
            return
        task_id = _checkout_task_text(payload.get("task_id"), limit=96)
        run_id = _checkout_task_text(payload.get("run_id"), limit=96)
        if not task_id or not run_id:
            return
        status = _checkout_task_text(payload.get("status"), limit=16).lower()
        stage = _checkout_task_stage(payload.get("stage"))
        email = _checkout_task_text(payload.get("email"), limit=320)
        channel = _checkout_task_text(payload.get("channel"), limit=32).lower()
        error_short = _checkout_task_text(payload.get("error_short"), limit=160)
        progress_detail = _checkout_task_text(payload.get("progress_detail"), limit=160)
        payment_link = _checkout_task_payment_link(payload.get("payment_link"))
        attempt = max(0, _safe_int(payload.get("attempt"), 0))
        next_retry_at = _checkout_task_text(payload.get("next_retry_at"), limit=64)

        with self._lock:
            if not self._checkout_task_run_id or run_id != self._checkout_task_run_id:
                return
            existing = next(
                (item for item in self._checkout_tasks if item.get("task_id") == task_id),
                None,
            )
            now = _now()
            if existing is None:
                try:
                    task_index = max(1, int(payload.get("index") or 0))
                except (TypeError, ValueError):
                    task_index = 0
                existing = {
                    "id": task_id,
                    "task_id": task_id,
                    "index": task_index,
                    "email": email,
                    "status": status if status in {"queued", "running", "retrying", "success", "failed", "cancelled"} else "running",
                    "stage": stage,
                    "channel": channel,
                    "payment_link": payment_link,
                    "error_short": error_short,
                    "progress_detail": progress_detail,
                    "attempt": attempt,
                    "next_retry_at": next_retry_at,
                    "created_at": now,
                    "updated_at": now,
                    "finished_at": None,
                }
                self._checkout_tasks.append(existing)
            else:
                if "index" in payload:
                    try:
                        existing["index"] = max(1, int(payload.get("index") or 0))
                    except (TypeError, ValueError):
                        pass
                if "email" in payload:
                    existing["email"] = email
                if "channel" in payload:
                    existing["channel"] = channel
                if "stage" in payload:
                    existing["stage"] = stage
                if "payment_link" in payload:
                    existing["payment_link"] = payment_link
                if "error_short" in payload:
                    existing["error_short"] = error_short
                if "progress_detail" in payload:
                    existing["progress_detail"] = progress_detail
                if "attempt" in payload:
                    existing["attempt"] = attempt
                if "next_retry_at" in payload:
                    existing["next_retry_at"] = next_retry_at
                if status in {"queued", "running", "retrying", "success", "failed", "cancelled"}:
                    existing["status"] = status
                existing["updated_at"] = now

            if existing["status"] in {"success", "failed", "cancelled"}:
                existing["finished_at"] = now
            self._checkout_tasks = self._checkout_tasks[-300:]

    @staticmethod
    def _checkout_retry_error_is_terminal(error: Exception) -> bool:
        """Avoid endlessly retrying errors a different proxy cannot repair."""
        text = _clean_text(error).lower()
        permanent_markers = (
            "account not found",
            "access_token is required",
            "账号没有可用邮箱",
            "需要 in checkout 代理",
            "需要 vn promotion 代理",
            "需要 in provider 代理",
            "未填写代理 url",
            "必须是有效的 http",
            "未包含可改写的 country/region 选择器",
            "invalid access token",
            "token has been revoked",
        )
        return any(marker in text for marker in permanent_markers)

    @staticmethod
    def _checkout_retry_error_is_trial_ineligible(error: Exception) -> bool:
        """Return whether the current proxy did not expose a free trial.

        The account remains usable: the trial offer is determined while the
        Checkout is created, so a new GB/TR/VN proxy rotation can legitimately
        produce a different result.  Prefer the propagated protocol code and
        retain the message checks for older persisted/runtime errors.
        """
        code = _clean_text(getattr(error, "code", "")).lower()
        text = _clean_text(error).lower()
        return (
            code == "checkout_amount_mismatch"
            or "checkout_amount_mismatch" in text
            or "不是 0 元试用资格" in text
        )

    @staticmethod
    def _checkout_retry_delay_seconds(error: Exception) -> int:
        if RegisterService._checkout_retry_error_is_trial_ineligible(error):
            # This is not a transient transport failure.  The next proxy
            # rotation should start straight away instead of waiting on the
            # normal backoff used for a failed upstream request.
            return 0
        text = _clean_text(error).lower()
        code = _clean_text(getattr(error, "code", "")).lower()
        try:
            status = int(getattr(error, "upstream_status", 0) or getattr(error, "status_code", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        if (
            code == "cloudflare_challenge"
            or status == 403
            or "cloudflare_challenge" in text
            or "cloudflare" in text
        ):
            return 60
        if status == 429 or "rate limit" in text or "too many requests" in text:
            return 30
        if "generic_decline" in text:
            return 30
        return 4

    @staticmethod
    def _checkout_retry_task_key(run_id: str, task_id: str) -> str:
        return f"{run_id}:{task_id}"

    @staticmethod
    def _checkout_retry_proxy_plan(channel: str, pix_protocol: object = "") -> str:
        if _checkout_channel(channel) != "pix":
            return "IN / VN / IN"
        return "BR / VN / BR" if _clean_text(pix_protocol).lower() == "standalone" else "BR 共享出口"

    @staticmethod
    def _checkout_retry_channel(job: dict[str, Any]) -> str:
        checkout = job.get("checkout") if isinstance(job.get("checkout"), dict) else {}
        return _checkout_channel(job.get("channel") or checkout.get("channel"))

    def _checkout_retry_task_update(
        self,
        job: dict[str, Any],
        *,
        status: str,
        stage: str,
        error_short: str = "",
        progress_detail: str = "",
        payment_link: str = "",
        next_retry_at: str = "",
    ) -> None:
        self._upsert_checkout_task(
            {
                "task_id": job["task_id"],
                "run_id": job["run_id"],
                "index": job["index"],
                "email": job["email"],
                "channel": self._checkout_retry_channel(job),
                "status": status,
                "stage": stage,
                "error_short": error_short,
                "progress_detail": progress_detail,
                "payment_link": payment_link,
                "attempt": job["attempt"],
                "next_retry_at": next_retry_at,
            }
        )

    def _enqueue_checkout_retry(self, payload: dict[str, Any]) -> None:
        """Queue an already-persisted final-link account for proxy rotation.

        The input originates only from the in-process registration worker.  It
        deliberately remains memory-only because it contains an access token.
        """
        if not isinstance(payload, dict):
            return
        run_id = _checkout_task_text(payload.get("run_id"), limit=96)
        task_id = _checkout_task_text(payload.get("task_id"), limit=96)
        access_token = _clean_text(payload.get("access_token"))
        checkout = payload.get("checkout") if isinstance(payload.get("checkout"), dict) else {}
        channel = _checkout_channel(payload.get("channel") or checkout.get("channel"))
        if not run_id or not task_id or not access_token:
            return

        with self._lock:
            if run_id != self._checkout_task_run_id or self._checkout_retry_stop_event.is_set():
                return
            key = self._checkout_retry_task_key(run_id, task_id)
            if key in self._checkout_retry_jobs:
                return
            if any(
                job.get("run_id") == run_id and job.get("access_token") == access_token
                for job in self._checkout_retry_jobs.values()
                if job.get("stop_event") is self._checkout_retry_stop_event
            ):
                return
            job = {
                "key": key,
                "run_id": run_id,
                "task_id": task_id,
                "index": max(1, _safe_int(payload.get("index"), 1)),
                "email": _checkout_task_text(payload.get("email"), limit=320),
                "access_token": access_token,
                "checkout": json.loads(json.dumps(checkout, ensure_ascii=False)),
                "channel": channel,
                "attempt": max(0, _safe_int(payload.get("attempt"), 0)),
                "next_proxy_rotation": max(1, _safe_int(payload.get("next_proxy_rotation"), 1)),
                "next_retry_monotonic": time.monotonic(),
                "stop_event": self._checkout_retry_stop_event,
                "in_flight": False,
            }
            self._checkout_retry_jobs[key] = job
            self._checkout_retry_task_update(
                job,
                status="queued",
                stage="queued",
                error_short="等待轮换代理重试",
            )
            self._ensure_checkout_retry_workers_locked()
            self._checkout_retry_condition.notify_all()

    def _ensure_checkout_queue_locked(self) -> str:
        """Return the independent task-table generation, reopening a stopped queue."""
        if self._checkout_retry_stop_event.is_set():
            self._checkout_retry_stop_event = threading.Event()
            self._checkout_retry_condition = threading.Condition(self._lock)
            self._checkout_retry_runners = []
        if not self._checkout_task_run_id:
            self._checkout_task_run_id = f"checkout-runtime-{uuid.uuid4().hex}"
        openai_register.register_checkout_task_run_id = self._checkout_task_run_id
        return self._checkout_task_run_id

    def _cancel_checkout_retries_locked(self) -> None:
        """Stop future retry attempts; in-flight HTTP requests finish safely."""
        stop_event = self._checkout_retry_stop_event
        stop_event.set()
        self._checkout_retry_condition.notify_all()
        cancelled = [job for job in self._checkout_retry_jobs.values() if job.get("stop_event") is stop_event]
        self._checkout_retry_jobs = {
            key: job
            for key, job in self._checkout_retry_jobs.items()
            if job.get("stop_event") is not stop_event
        }
        for job in cancelled:
            self._checkout_retry_task_update(
                job,
                status="cancelled",
                stage="cancelled",
                error_short="已停止持续提链",
            )
        if cancelled:
            self._append_checkout_log(f"已停止 {len(cancelled)} 个持续提链任务", "yellow")

    def _checkout_retry_worker_limit_locked(self) -> int:
        """Use the independent Checkout thread setting as the retry concurrency cap."""
        checkout = self._config.get("checkout") if isinstance(self._config.get("checkout"), dict) else {}
        return max(1, _safe_int(checkout.get("threads"), 5) or 5)

    def _ensure_checkout_retry_workers_locked(self) -> None:
        """Keep enough workers alive for the due Checkout jobs in this run."""
        stop_event = self._checkout_retry_stop_event
        pending_count = sum(
            1
            for job in self._checkout_retry_jobs.values()
            if job.get("stop_event") is stop_event
        )
        desired_workers = min(self._checkout_retry_worker_limit_locked(), pending_count)
        self._checkout_retry_runners = [
            worker for worker in self._checkout_retry_runners if worker.is_alive()
        ]
        while len(self._checkout_retry_runners) < desired_workers:
            worker_index = len(self._checkout_retry_runners) + 1
            worker = threading.Thread(
                target=self._run_checkout_retry_queue,
                args=(stop_event, self._checkout_retry_condition),
                daemon=True,
                name=f"checkout-final-link-retry-{worker_index}",
            )
            self._checkout_retry_runners.append(worker)
            worker.start()

    def _claim_next_checkout_retry_job(
        self,
        stop_event: threading.Event,
        condition: threading.Condition,
    ) -> dict[str, Any] | None:
        """Wait for and atomically claim one due retry job for this generation."""
        with condition:
            while (
                not stop_event.is_set()
                and self._checkout_retry_stop_event is stop_event
                and self._checkout_retry_condition is condition
            ):
                jobs = [
                    job
                    for job in self._checkout_retry_jobs.values()
                    if job.get("stop_event") is stop_event and not job.get("in_flight")
                ]
                if not jobs:
                    condition.wait()
                    continue
                now = time.monotonic()
                job = min(jobs, key=lambda item: float(item.get("next_retry_monotonic") or now))
                wait_seconds = max(0.0, float(job.get("next_retry_monotonic") or now) - now)
                if wait_seconds > 0:
                    condition.wait(wait_seconds)
                    continue
                # Claim while holding the queue lock.  Multiple workers can
                # never submit the same account's Checkout flow at once.
                job["in_flight"] = True
                return job
        return None

    def _release_checkout_retry_claim(self, job: dict[str, Any], stop_event: threading.Event) -> None:
        with self._checkout_retry_condition:
            current = self._checkout_retry_jobs.get(_clean_text(job.get("key")))
            if current is job and current.get("stop_event") is stop_event:
                current["in_flight"] = False
                self._checkout_retry_condition.notify_all()

    def _remove_checkout_retry_job(self, job: dict[str, Any], stop_event: threading.Event) -> None:
        """Delete a completed claim only when it still belongs to this run."""
        with self._checkout_retry_condition:
            key = _clean_text(job.get("key"))
            current = self._checkout_retry_jobs.get(key)
            if current is job and current.get("stop_event") is stop_event:
                self._checkout_retry_jobs.pop(key, None)
                self._checkout_retry_condition.notify_all()

    def _run_checkout_retry_queue(self, stop_event: threading.Event, condition: threading.Condition) -> None:
        """Run claimed final-link jobs concurrently, up to the thread cap."""
        while not stop_event.is_set():
            job = self._claim_next_checkout_retry_job(stop_event, condition)
            if job is None:
                return
            try:
                self._run_checkout_retry_attempt(job, stop_event)
            finally:
                self._release_checkout_retry_claim(job, stop_event)

    def _run_checkout_retry_attempt(self, job: dict[str, Any], stop_event: threading.Event) -> None:
        if stop_event.is_set():
            return
        channel = self._checkout_retry_channel(job)
        job["channel"] = channel
        account = account_service.get_account(job["access_token"])
        payment_link = account_service.ready_checkout_url(account, channel)
        if payment_link:
            self._checkout_retry_task_update(
                job,
                status="success",
                stage="completed",
                payment_link=payment_link,
                error_short="",
            )
            self._append_checkout_log(
                f"[任务{job['index']}] 已有 {channel.upper()} 最终支付链接，跳过重复提链",
                "green",
            )
            self._remove_checkout_retry_job(job, stop_event)
            return
        attempt = int(job.get("attempt") or 1) + 1
        rotation = int(job.get("next_proxy_rotation") or 1)
        job["attempt"] = attempt
        checkout = job["checkout"]
        role_plan = self._checkout_retry_proxy_plan(channel, checkout.get("pix_protocol"))
        self._checkout_retry_task_update(
            job,
            status="running",
            stage="checkout",
            error_short="",
            progress_detail=f"第 {attempt} 轮：准备 {role_plan} 代理链",
        )
        self._append_checkout_log(
            f"[任务{job['index']}] 第 {attempt} 轮持续提链：已选定本轮 {role_plan} 代理",
            "yellow",
        )
        account_service.update_account(
            job["access_token"],
            {"checkout_link_status": "pending"},
            quiet=True,
        )

        last_stage = "checkout"
        last_detail = ""

        def progress(message: str) -> None:
            nonlocal last_stage, last_detail
            if stop_event.is_set():
                return
            stage = openai_register._checkout_stage_code(message)
            detail = openai_register._checkout_progress_detail(message)
            if stage == last_stage and detail == last_detail:
                return
            last_stage = stage
            last_detail = detail
            self._checkout_retry_task_update(
                job,
                status="running",
                stage=stage,
                error_short="",
                progress_detail=detail,
            )

        try:
            checkout_result = openai_checkout_service.extract_and_store_checkout_link(
                job["access_token"],
                checkout_channel=channel,
                **(
                    {
                        "pix_protocol": (
                            str(checkout.get("pix_protocol") or "").strip().lower()
                            if str(checkout.get("pix_protocol") or "").strip().lower()
                            in {"enhanced", "reference", "standalone"}
                            else "enhanced"
                        )
                    }
                    if channel == "pix"
                    else {}
                ),
                checkout_proxy=_clean_text(checkout.get("checkout_proxy_url")),
                promotion_proxy=_clean_text(checkout.get("promotion_proxy_url")),
                provider_proxy=_clean_text(checkout.get("checkout_proxy_url")),
                proxy_rotation=rotation,
                progress=progress,
            )
        except Exception as error:
            if stop_event.is_set():
                return
            short_error = openai_register._checkout_failure_short(error, channel)
            if self._checkout_retry_error_is_terminal(error):
                self._checkout_retry_task_update(
                    job,
                    status="failed",
                    stage="failed",
                    error_short=short_error,
                )
                self._append_checkout_log(
                    f"[任务{job['index']}] 持续提链停止：{short_error}",
                    "red",
                )
                self._remove_checkout_retry_job(job, stop_event)
                return

            trial_ineligible = self._checkout_retry_error_is_trial_ineligible(error)
            delay = self._checkout_retry_delay_seconds(error)
            job["next_proxy_rotation"] = rotation + 1
            job["next_retry_monotonic"] = time.monotonic() + delay
            next_retry_at = (
                (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                if delay > 0
                else ""
            )
            # The protocol storage boundary records a failed attempt before it
            # raises.  A queued retry is not an account failure, so restore the
            # visible account state immediately while the next IP rotation is
            # prepared.
            account_service.update_account(
                job["access_token"],
                {"checkout_link_status": "pending"},
                quiet=True,
            )
            retry_message = (
                f"第 {attempt} 轮：当前代理无 0 元试用资格，立即切换 {role_plan} 代理"
                if trial_ineligible
                else f"第 {attempt} 轮失败：{short_error}；将轮换 {role_plan} 代理"
            )
            self._checkout_retry_task_update(
                job,
                status="retrying",
                stage="retrying",
                error_short=retry_message,
                next_retry_at=next_retry_at,
            )
            if trial_ineligible:
                self._append_checkout_log(
                    f"[任务{job['index']}] 第 {attempt} 轮当前 IP 无 0 元试用资格，立即轮换 {role_plan} 代理重试",
                    "yellow",
                )
            else:
                self._append_checkout_log(
                    f"[任务{job['index']}] 第 {attempt} 轮提链未成功：{short_error}；{delay} 秒后轮换 {role_plan} 代理重试",
                    "yellow",
                )
            with self._checkout_retry_condition:
                if self._checkout_retry_stop_event is stop_event:
                    self._checkout_retry_condition.notify_all()
            return

        if stop_event.is_set():
            return
        payment_link = _clean_text(checkout_result.get("checkout_final_url") or checkout_result.get("checkout_url"))
        self._checkout_retry_task_update(
            job,
            status="success",
            stage="completed",
            payment_link=payment_link,
            error_short="",
        )
        self._append_checkout_log(
            f"[任务{job['index']}] 第 {attempt} 轮持续提链成功，已保存最终支付链接",
            "green",
        )
        self._remove_checkout_retry_job(job, stop_event)

    def _pool_metrics(
        self,
        *,
        refresh_stale: bool = False,
        target_quota: int | None = None,
        target_available: int | None = None,
    ) -> dict:
        return account_service.evaluate_account_pool(
            refresh_stale=refresh_stale,
            target_quota=target_quota,
            target_available=target_available,
        )

    def _target_reached(self, cfg: dict, submitted: int) -> bool:
        mode = str(cfg.get("mode") or "total")
        if str(cfg.get("target") or "openai") == "grok":
            return submitted >= int(cfg.get("total") or 1)
        metrics = self._pool_metrics(
            refresh_stale=mode in {"quota", "available"},
            target_quota=int(cfg.get("target_quota") or 1) if mode == "quota" else None,
            target_available=int(cfg.get("target_available") or 1) if mode == "available" else None,
        )
        self._bump(**metrics)
        if mode == "quota":
            reached = metrics["current_quota"] >= int(cfg.get("target_quota") or 1)
            self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，当前剩余额度={metrics['current_quota']}，目标额度={cfg.get('target_quota')}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        if mode == "available":
            reached = metrics["current_available"] >= int(cfg.get("target_available") or 1)
            self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，目标账号={cfg.get('target_available')}，当前剩余额度={metrics['current_quota']}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        return submitted >= int(cfg.get("total") or 1)

    def _bump(self, **updates) -> None:
        with self._lock:
            self._config["stats"].update(updates)
            stats = self._config["stats"]
            started_at = str(stats.get("started_at") or "")
            if started_at:
                try:
                    elapsed = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
                except Exception:
                    elapsed = 0.0
                done = int(stats.get("done") or 0)
                success = int(stats.get("success") or 0)
                fail = int(stats.get("fail") or 0)
                stats["elapsed_seconds"] = round(elapsed, 1)
                stats["avg_seconds"] = round(elapsed / success, 1) if success else 0
                stats["success_rate"] = round(success * 100 / max(1, success + fail), 1)
            self._config["stats"]["updated_at"] = _now()
            self._save()

    def _run(self, target: str, backend, run_options: dict[str, Any] | None = None) -> None:
        options = dict(run_options or {})
        threads = max(1, int(options.get("threads") or self.get()["threads"]))
        submitted, done, success, fail = 0, 0, 0, 0
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = set()
            while True:
                cfg = self.get()
                cfg["target"] = target
                cfg.update(options)
                if target == "grok":
                    cfg["mode"] = "total"
                while self.get()["enabled"] and not self._target_reached(cfg, submitted) and len(futures) < threads:
                    submitted += 1
                    futures.add(executor.submit(backend.worker, submitted))
                self._bump(running=len(futures), done=done, success=success, fail=fail)
                if not futures and (not self.get()["enabled"] or str(cfg.get("mode") or "total") == "total"):
                    break
                if not futures:
                    time.sleep(max(1, int(cfg.get("check_interval") or 5)))
                    continue
                finished, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in finished:
                    done += 1
                    try:
                        result = future.result()
                        ok = bool(result.get("ok")) if isinstance(result, dict) else False
                        has_grok_account = bool(
                            isinstance(result, dict)
                            and (
                                isinstance(result.get("result"), dict)
                                or isinstance(result.get("account"), dict)
                            )
                        )
                        already_persisted = bool(result.get("account_persisted")) if isinstance(result, dict) else False
                        if target == "grok" and has_grok_account and (ok or not already_persisted):
                            saved = self._persist_grok_worker_result(result)
                            if ok:
                                self._enqueue_grok_oauth_protocol(saved)
                        success += 1 if ok else 0
                        fail += 0 if ok else 1
                    except Exception as exc:
                        fail += 1
                        self._append_log(f"任务结果处理失败: {type(exc).__name__}: {exc}", "red")
        self._bump(running=0, done=done, success=success, fail=fail, finished_at=_now())
        with self._lock:
            self._config["enabled"] = False
            self._save()
        if target == "grok":
            self._append_log(f"Grok 注册任务已结束：成功 {success}，失败 {fail}", "yellow")
        else:
            self._append_log(f"注册任务结束，平台={target}，成功{success}，失败{fail}", "yellow")
        if target == "grok" and hasattr(backend, "account_result_sink"):
            backend.account_result_sink = None


def _start_xai_cli_oauth_protocol(
    account_id: str,
    *,
    prioritize: bool = False,
    retry: bool = False,
) -> dict[str, Any]:
    from services.xai_cli_oauth_service import xai_cli_oauth_service

    return xai_cli_oauth_service.start_protocol_authorization_background(
        account_id,
        prioritize=prioritize,
        retry=retry,
    )


register_service = RegisterService(
    REGISTER_FILE,
    grok_oauth_protocol_sink=_start_xai_cli_oauth_protocol,
)

from services.xai_cli_oauth_service import xai_cli_oauth_service

xai_cli_oauth_service.protocol_event_sink = register_service.handle_grok_oauth_protocol_event
