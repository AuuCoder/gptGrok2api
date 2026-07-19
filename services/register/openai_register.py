from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from curl_cffi import requests

from services.account_service import account_service
from services.json_file import read_json_object
from services.openai_checkout_service import CheckoutSessionError, openai_checkout_service
from services.proxy_service import ClearanceBundle, proxy_settings
from services.register import mail_provider
from services.sub2api_service import normalize_sync_config, sub2api_config, sync_openai_account
from utils.timezone import TIME_FORMAT, beijing_now_str

base_dir = Path(__file__).resolve().parent
config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 30,
        "wait_interval": 2,
        "api_use_register_proxy": True,
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 3,
    "checkout": {
        "enabled": True,
        "channel": "upi",
        "pix_protocol": "enhanced",
        "country": "IN",
        "currency": "INR",
        "checkout_ui_mode": "custom",
        "threads": 5,
        "checkout_proxy_enabled": False,
        "checkout_proxy_url": "",
        "promotion_proxy_enabled": False,
        "promotion_proxy_url": "",
        "provider_proxy_enabled": False,
        "provider_proxy_url": "",
    },
    "sub2api_sync": {
        "enabled": False,
        "server_id": "",
        "group_mode": "existing",
        "group_id": "",
        "group_name": "",
    },
}
register_config_file = base_dir.parents[1] / "data" / "register.json"
try:
    saved_config = read_json_object(register_config_file, name="register.json")
    config.update({
        key: saved_config[key]
        for key in ("mail", "proxy", "total", "threads", "checkout", "sub2api_sync")
        if key in saved_config
    })
except Exception:
    pass

auth_base = "https://auth.openai.com"
chatgpt_base = "https://chatgpt.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
platform_oauth_redirect_uri = f"{platform_base}/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
REGISTER_BROWSER_PROFILES: tuple[dict[str, str], ...] = (
    {
        "impersonate": "chrome142",
        "major": "142",
        "full_version": "142.0.0.0",
        "platform_version": "10.0.0",
        "accept_language": "en-US,en;q=0.9",
    },
    {
        "impersonate": "chrome136",
        "major": "136",
        "full_version": "136.0.0.0",
        "platform_version": "10.0.0",
        "accept_language": "en-US,en;q=0.9",
    },
    {
        "impersonate": "chrome131",
        "major": "131",
        "full_version": "131.0.0.0",
        "platform_version": "10.0.0",
        "accept_language": "en-US,en;q=0.9",
    },
)


def _chrome_user_agent(major: str, full_version: str) -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{full_version} Safari/537.36"
    )


def _chrome_sec_ch_ua(major: str) -> str:
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="99"'


def _chrome_sec_ch_ua_full_version_list(major: str, full_version: str) -> str:
    return (
        f'"Chromium";v="{full_version}", '
        f'"Google Chrome";v="{full_version}", '
        '"Not_A Brand";v="99.0.0.0"'
    )


def _complete_browser_fingerprint(profile: dict[str, str]) -> dict[str, str]:
    major = str(profile.get("major") or "142").strip()
    full_version = str(profile.get("full_version") or f"{major}.0.0.0").strip()
    return {
        **profile,
        "major": major,
        "full_version": full_version,
        "user_agent": str(profile.get("user_agent") or _chrome_user_agent(major, full_version)),
        "sec_ch_ua": str(profile.get("sec_ch_ua") or _chrome_sec_ch_ua(major)),
        "sec_ch_ua_full_version_list": str(
            profile.get("sec_ch_ua_full_version_list") or _chrome_sec_ch_ua_full_version_list(major, full_version)
        ),
        "accept_language": str(profile.get("accept_language") or "en-US,en;q=0.9"),
        "platform_version": str(profile.get("platform_version") or "10.0.0"),
        "impersonate": str(profile.get("impersonate") or "chrome"),
    }


DEFAULT_BROWSER_FINGERPRINT = _complete_browser_fingerprint(REGISTER_BROWSER_PROFILES[0])
user_agent = DEFAULT_BROWSER_FINGERPRINT["user_agent"]
sec_ch_ua = DEFAULT_BROWSER_FINGERPRINT["sec_ch_ua"]
sec_ch_ua_full_version_list = DEFAULT_BROWSER_FINGERPRINT["sec_ch_ua_full_version_list"]
default_timeout = 30
print_lock = threading.Lock()
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None
register_checkout_log_sink = None
register_checkout_task_sink = None
# RegisterService owns the retry queue.  The registration module only emits a
# short-lived in-memory request after the account has been persisted.
register_checkout_retry_sink: Callable[[dict[str, Any]], None] | None = None
# Assigned by RegisterService when a new registration run starts.  Checkout
# workers include it in every structured update so reset/stale workers cannot
# re-populate a cleared task table.
register_checkout_task_run_id = ""
OPENAI_EXISTING_EMAIL_RETRY_LIMIT = 20
CF_MAILBOX_WAIT_TIMEOUT_SECONDS = 60.0


class PasswordlessSignupUnavailable(RuntimeError):
    """OpenAI explicitly disabled passwordless signup for the current flow."""


class OpenAIEmailAlreadyRegistered(RuntimeError):
    """OpenAI routed a signup address into the existing-account login flow."""

    def __init__(self, email: str) -> None:
        self.email = str(email or "").strip()
        super().__init__(f"当前邮箱已进入 OpenAI 登录验证码分支，不能作为新账号继续注册: {self.email}")


class OpenAIMailboxDeliveryTimeout(RuntimeError):
    """The current mailbox never received an OTP for its active challenge."""

    def __init__(self, mailbox: dict[str, Any], reason: str = "") -> None:
        self.email = str(mailbox.get("address") or "").strip()
        self.provider = str(mailbox.get("provider") or "").strip()
        self.provider_ref = str(mailbox.get("provider_ref") or "").strip()
        self.label = str(mailbox.get("label") or self.provider or "邮箱来源").strip()
        self.reason = str(reason or "未收到验证码").strip()
        super().__init__(f"[{self.label}] {self.reason}")


common_headers = {
    "accept": "application/json",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "content-type": "application/json",
    "dnt": "1",
    "origin": auth_base,
    "priority": "u=1, i",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": user_agent,
}

navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "connection": "keep-alive",
    "dnt": "1",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": user_agent,
}


def _browser_fingerprint(fingerprint: dict[str, str] | None = None) -> dict[str, str]:
    return _complete_browser_fingerprint(fingerprint or DEFAULT_BROWSER_FINGERPRINT)


def _header_fingerprint(headers: dict[str, str], fingerprint: dict[str, str] | None = None) -> dict[str, str]:
    fp = _browser_fingerprint(fingerprint)
    next_headers = dict(headers)
    next_headers["user-agent"] = fp["user_agent"]
    next_headers["sec-ch-ua"] = fp["sec_ch_ua"]
    if "sec-ch-ua-full-version-list" in next_headers:
        next_headers["sec-ch-ua-full-version-list"] = fp["sec_ch_ua_full_version_list"]
    if "sec-ch-ua-platform-version" in next_headers:
        next_headers["sec-ch-ua-platform-version"] = f'"{fp["platform_version"]}"'
    if "accept-language" in next_headers:
        next_headers["accept-language"] = fp["accept_language"]
    return next_headers


def _extract_chrome_version_from_user_agent(value: str) -> tuple[str, str]:
    ua = str(value or "")
    for marker in ("Chrome/", "Chromium/", "Edg/"):
        if marker not in ua:
            continue
        tail = ua.split(marker, 1)[1]
        version = tail.split(" ", 1)[0].strip()
        major = version.split(".", 1)[0].strip()
        if major.isdigit():
            return major, version or f"{major}.0.0.0"
    return "", ""


def _fingerprint_with_user_agent(fingerprint: dict[str, str] | None, value: str) -> dict[str, str]:
    ua = str(value or "").strip()
    if not ua:
        return _browser_fingerprint(fingerprint)
    fp = _browser_fingerprint(fingerprint)
    major, full_version = _extract_chrome_version_from_user_agent(ua)
    major = major or fp["major"]
    full_version = full_version or f"{major}.0.0.0"
    return {
        **fp,
        "major": major,
        "full_version": full_version,
        "user_agent": ua,
        "sec_ch_ua": _chrome_sec_ch_ua(major),
        "sec_ch_ua_full_version_list": _chrome_sec_ch_ua_full_version_list(major, full_version),
    }


def _make_browser_fingerprint() -> dict[str, str]:
    return _complete_browser_fingerprint(secrets.choice(REGISTER_BROWSER_PROFILES))


def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{beijing_now_str(TIME_FORMAT)} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


def checkout_log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_checkout_log_sink:
        try:
            register_checkout_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{beijing_now_str(TIME_FORMAT)} [Checkout] {text}{suffix}")


def checkout_step(index: int, text: str, color: str = "") -> None:
    checkout_log(f"[任务{index}] {text}", color)


def checkout_task_update(
    index: int,
    task_id: str,
    *,
    email: str | None = None,
    status: str | None = None,
    stage: str | None = None,
    payment_link: str | None = None,
    error_short: str | None = None,
    progress_detail: str | None = None,
    channel: str | None = None,
) -> None:
    """Publish a credential-free Checkout task update to the admin runtime."""
    if register_checkout_task_sink is None:
        return
    payload: dict[str, Any] = {
        "task_id": str(task_id or "").strip(),
        "index": int(index),
        "run_id": str(register_checkout_task_run_id or "").strip(),
    }
    for key, value in {
        "email": email,
        "status": status,
        "stage": stage,
        "payment_link": payment_link,
        "error_short": error_short,
        "progress_detail": progress_detail,
        "channel": channel,
    }.items():
        if value is not None:
            payload[key] = str(value)
    try:
        register_checkout_task_sink(payload)
    except Exception:
        # Task telemetry must never affect a successful account registration.
        pass


def _checkout_failure_short(error: Exception, checkout_channel: str = "upi") -> str:
    """Map protocol diagnostics to a concise, operator-facing Chinese cause.

    The raw exception can include upstream HTML, request URLs, or proxy
    details.  It remains available in the legacy runtime log, while the task
    table deliberately stores only this small classified message.
    """
    text = str(error or "").lower()
    try:
        status = int(getattr(error, "upstream_status", 0) or getattr(error, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    code = str(getattr(error, "code", "") or "").strip().lower()
    channel_label = "Pix" if _checkout_channel(checkout_channel) == "pix" else "UPI"
    inactive_session = (
        code == "checkout_session_inactive"
        or "checkout_not_active_session" in text
        or "checkout session is no longer active" in text
    )
    if code == "checkout_amount_mismatch" or "checkout_amount_mismatch" in text or "不是 0 元试用资格" in text:
        return "当前代理无 0 元试用资格"
    if code == "upi_unavailable":
        return "当前 Checkout 不支持 UPI"
    if code == "pix_unavailable":
        return "当前 Checkout 不支持 Pix"
    if "未包含可改写的 country/region 选择器" in text:
        return "代理地区格式不支持"
    if "轮询代理连接超时" in text:
        return "Stripe 轮询代理超时"
    if "代理连接失败" in text or "proxy" in text or "connect aborted" in text or "connection refused" in text:
        return "代理连接失败"
    if "超时" in text or "timeout" in text or "timed out" in text or status in {408, 504}:
        return "上游请求超时"
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return "上游请求过于频繁"
    if status in {401, 403} or "permission" in text or "access denied" in text:
        return "上游拒绝访问"
    if "we're sorry" in text or "unable to serve" in text:
        return "上游支付页面拒绝请求"
    if "stripe_init" in text:
        return "Stripe Checkout 初始化失败：Session 已失效" if inactive_session else "Stripe Checkout 初始化失败"
    if "stripe_elements" in text:
        return "Stripe Elements 会话失败：Session 已失效" if inactive_session else "Stripe Elements 会话失败"
    if "stripe_tax_1" in text:
        return "Stripe 税务地区第一次更新失败：Session 已失效" if inactive_session else "Stripe 税务地区第一次更新失败"
    if "stripe_tax_2" in text:
        return "Stripe 税务地区第二次更新失败：Session 已失效" if inactive_session else "Stripe 税务地区第二次更新失败"
    if "stripe_payment_method" in text:
        return "Stripe 支付方式创建失败"
    if "stripe_confirm_1" in text:
        return "Stripe 首次确认失败"
    if "stripe_confirm_2" in text:
        return "Stripe 审批后二次确认失败"
    if inactive_session:
        return "Stripe Checkout Session 已失效"
    if "stripe" in text or "checkout" in text:
        return "支付页面创建失败"
    return f"{channel_label} 提链失败，请稍后重试"


def _checkout_stage_code(message: object) -> str:
    """Collapse verbose protocol progress into stable UI stage identifiers."""
    text = str(message or "").strip().lower()
    tagged = re.match(r"^\[([a-z0-9_]{1,32})\]", text)
    if tagged:
        return tagged.group(1)
    if "优惠" in text or "promotion" in text:
        return "promotion"
    if "更新 checkout" in text or "checkout update" in text:
        return "checkout_update"
    if "paymentmethod" in text or "支付方式" in text:
        return "payment_method"
    if "初始化 stripe" in text:
        return "stripe_init"
    if "elements" in text:
        return "stripe_elements"
    if "税务" in text or "tax" in text:
        return "stripe_tax"
    if "账单快照" in text or "snapshot" in text:
        return "snapshot"
    if "轮询" in text or "poll" in text:
        return "poll"
    if "解析" in text or "最终授权链接" in text or "最终支付链接" in text:
        return "extract"
    if "确认" in text or "审批" in text or "approve" in text:
        return "confirm"
    if "stripe" in text or "税务" in text:
        return "stripe"
    return "checkout"


def _checkout_progress_detail(message: object) -> str:
    """Remove the optional stage marker from a credential-free progress message."""
    return re.sub(r"^\[[a-z0-9_]{1,32}\]\s*", "", str(message or "").strip(), count=1)


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


from utils.pkce import generate_pkce as _generate_pkce  # noqa: F401


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _response_debug_detail(resp, limit: int = 800) -> str:
    if resp is None:
        return ""
    data = _response_json(resp)
    parts = [
        f"url={str(getattr(resp, 'url', '') or '')[:300]}",
        f"content_type={str(getattr(resp, 'headers', {}).get('content-type') or '')}",
    ]
    for key in ("cf-ray", "x-request-id", "openai-processing-ms"):
        value = str(getattr(resp, "headers", {}).get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    if data:
        parts.append(f"json={json.dumps(data, ensure_ascii=False)[:limit]}")
    else:
        parts.append(f"body={str(getattr(resp, 'text', '') or '')[:limit]}")
    return ", ".join(parts)


def _is_cloudflare_challenge(resp) -> bool:
    if resp is None:
        return False
    try:
        status_code = int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code not in (403, 503):
        return False
    text = str(getattr(resp, "text", "") or "").lower()
    return (
        "<title>just a moment" in text
        or "<title>attention required! | cloudflare" in text
        or "cf-chl-" in text
        or "__cf_chl_" in text
        or "cf-browser-verification" in text
    )


def _truthy(value: object, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return fallback


def _checkout_channel(value: object) -> str:
    channel = str(value or "").strip().lower()
    return channel if channel in {"upi", "pix"} else "upi"


def _passwordless_signup_explicitly_unavailable(data: object) -> bool:
    """Only accept explicit upstream opt-out signals as a legacy-flow fallback."""
    if not isinstance(data, dict):
        return False

    candidates: list[dict[str, Any]] = [data]
    page = data.get("page")
    if isinstance(page, dict):
        candidates.append(page)
        payload = page.get("payload")
        if isinstance(payload, dict):
            candidates.append(payload)

    for candidate in candidates:
        if _truthy(candidate.get("passwordless_disabled")):
            return True

    error = data.get("error")
    if not isinstance(error, dict):
        return False
    code = str(error.get("code") or "").strip().lower()
    return code in {
        "passwordless_disabled",
        "passwordless_signup_disabled",
        "passwordless_signup_unavailable",
    }


def _mail_config(register_proxy: str = "") -> dict:
    mail = config["mail"] if isinstance(config.get("mail"), dict) else {}
    use_register_proxy = _truthy(mail.get("api_use_register_proxy"), True)
    proxy = str(register_proxy or "").strip() if use_register_proxy else ""
    return {**mail, "api_use_register_proxy": use_register_proxy, "proxy": proxy}


def _checkout_config() -> dict[str, Any]:
    defaults = {
        "enabled": True,
        "channel": "upi",
        "country": "IN",
        "currency": "INR",
        "checkout_ui_mode": "custom",
        "threads": 5,
        "checkout_proxy_enabled": False,
        "checkout_proxy_url": "",
        "promotion_proxy_enabled": False,
        "promotion_proxy_url": "",
        "provider_proxy_enabled": False,
        "provider_proxy_url": "",
        "continuous_retry": True,
    }
    configured = config.get("checkout") if isinstance(config.get("checkout"), dict) else {}
    merged = {**defaults, **configured}
    # `residential_proxy_*` was the single pre-stage field. Existing configs
    # retain it as the shared IN egress when they are first read.
    checkout_proxy_enabled = _truthy(
        configured.get("checkout_proxy_enabled"),
        _truthy(configured.get("residential_proxy_enabled"), False),
    )
    checkout_proxy_url = str(
        (
            configured.get("checkout_proxy_url")
            if "checkout_proxy_url" in configured
            else configured.get("residential_proxy_url")
        )
        or ""
    ).strip()
    promotion_proxy_enabled = _truthy(configured.get("promotion_proxy_enabled"), False)
    promotion_proxy_url = str(configured.get("promotion_proxy_url") or "").strip()
    continuous_retry = _truthy(merged.get("continuous_retry"), True)
    channel = _checkout_channel(merged.get("channel"))
    country, currency = (("BR", "BRL") if channel == "pix" else ("IN", "INR"))
    try:
        checkout_threads = max(1, int(merged.get("threads") or defaults["threads"]))
    except (TypeError, ValueError):
        checkout_threads = defaults["threads"]
    return {
        "enabled": _truthy(merged.get("enabled"), True),
        "channel": channel,
        "pix_protocol": (
            str(merged.get("pix_protocol") or "").strip().lower()
            if str(merged.get("pix_protocol") or "").strip().lower() in {"enhanced", "reference", "standalone"}
            else "enhanced"
        ),
        "country": country,
        "currency": currency,
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


def _queue_checkout_retry(
    *,
    index: int,
    task_id: str,
    email: str,
    access_token: str,
    checkout: dict[str, Any],
    next_proxy_rotation: int,
    attempt: int = 1,
) -> bool:
    """Hand an already-saved final-link account to the independent queue."""
    sink = register_checkout_retry_sink
    if sink is None:
        return False
    try:
        sink(
            {
                "index": int(index),
                "task_id": str(task_id or "").strip(),
                "run_id": str(register_checkout_task_run_id or "").strip(),
                "email": str(email or "").strip(),
                "access_token": str(access_token or "").strip(),
                "checkout": dict(checkout),
                # Every account owns its rotation seed. A direct queue starts
                # at attempt zero; a post-failure retry starts at attempt one.
                "next_proxy_rotation": max(1, int(next_proxy_rotation)),
                "attempt": max(0, int(attempt)),
            }
        )
    except Exception:
        return False
    return True


# Older callers/tests may still refer to the UK-specific helper.  The queue is
# now channel-aware, so retain this lightweight compatibility alias.
def _queue_paypal_gb_checkout_retry(**kwargs: Any) -> bool:
    return _queue_checkout_retry(**kwargs)


def _chatgpt_cookie_header(session: object) -> str:
    """Serialize only the ChatGPT cookie jar for later explicit checkout use."""
    cookies = getattr(session, "cookies", None)
    jar = getattr(cookies, "jar", None)
    pairs: dict[str, str] = {}
    try:
        iterator = iter(jar) if jar is not None else iter(())
        for cookie in iterator:
            domain = str(getattr(cookie, "domain", "") or "").lstrip(".").lower()
            if domain and domain != "chatgpt.com" and not domain.endswith(".chatgpt.com"):
                continue
            name = str(getattr(cookie, "name", "") or "").strip()
            value = str(getattr(cookie, "value", "") or "").strip()
            if name and value:
                pairs[name] = value
    except Exception:
        pairs = {}

    if not pairs:
        try:
            values = cookies.get_dict() if cookies is not None else {}
        except Exception:
            values = {}
        if isinstance(values, dict):
            for name, value in values.items():
                name = str(name or "").strip()
                value = str(value or "").strip()
                if name and value:
                    pairs[name] = value
    return "; ".join(f"{name}={value}" for name, value in pairs.items())


def _authorize_landed_page(resp) -> str:
    """诊断用：粗判 authorize 之后落在哪个页面。返回 signup / login / "" 仅供日志。

    注意：email-verification / email_otp_verification 在注册和登录流程里都会出现，
    无法据此可靠区分，所以这里只用于打日志，绝不据此中断注册流程。
    """
    if resp is None:
        return ""
    final_url = str(getattr(resp, "url", "") or "").lower()
    data = _response_json(resp)
    page_type = ""
    page = data.get("page") if isinstance(data, dict) else None
    if isinstance(page, dict):
        page_type = str(page.get("type") or "").lower()
    if "create-account" in final_url or "signup" in final_url or "create_account" in page_type:
        return "signup"
    if "/log-in" in final_url or "/login" in final_url or page_type in {"login", "password_verification"}:
        return "login"
    return ""


def create_mailbox(
    username: str | None = None,
    register_proxy: str = "",
    excluded_provider_refs: set[str] | None = None,
) -> dict:
    return mail_provider.create_mailbox(
        _mail_config(register_proxy),
        username,
        excluded_provider_refs=excluded_provider_refs,
    )


def wait_for_code(
    mailbox: dict,
    register_proxy: str = "",
    *,
    wait_timeout: float | None = None,
) -> str | None:
    return mail_provider.wait_for_code(
        _mail_config(register_proxy),
        mailbox,
        wait_timeout=wait_timeout,
    )


def _configured_mail_wait_timeout() -> float:
    mail = config.get("mail") if isinstance(config.get("mail"), dict) else {}
    try:
        return max(1.0, float(mail.get("wait_timeout") or 30))
    except (TypeError, ValueError):
        return 30.0


def _wait_for_chatgpt_registration_code(
    mailbox: dict[str, Any],
    index: int,
    register_proxy: str,
    *,
    resend: Callable[[], None] | None = None,
) -> str:
    is_cf_mail = str(mailbox.get("provider") or "").strip() == "cloudflare_temp_email"
    wait_timeout = min(_configured_mail_wait_timeout(), CF_MAILBOX_WAIT_TIMEOUT_SECONDS) if is_cf_mail else None
    if is_cf_mail:
        step(index, f"CF 邮箱等待验证码（最多 {int(wait_timeout or CF_MAILBOX_WAIT_TIMEOUT_SECONDS)} 秒）")

    def poll() -> str | None:
        if wait_timeout is None:
            return wait_for_code(mailbox, register_proxy=register_proxy)
        return wait_for_code(mailbox, register_proxy=register_proxy, wait_timeout=wait_timeout)

    try:
        code = poll()
    except Exception as error:
        if is_cf_mail:
            raise OpenAIMailboxDeliveryTimeout(
                mailbox,
                f"查询邮件失败：{type(error).__name__}: {error}",
            ) from error
        raise
    if code:
        return code

    if is_cf_mail and resend is not None:
        step(index, "CF 邮箱首次未收到验证码，正在重发一次", "yellow")
        resend()
        mailbox["_received_after"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        step(index, f"验证码已重发，继续等待 CF 邮箱（最多 {int(wait_timeout or CF_MAILBOX_WAIT_TIMEOUT_SECONDS)} 秒）")
        try:
            code = poll()
        except Exception as error:
            raise OpenAIMailboxDeliveryTimeout(mailbox, f"重发后查询邮件失败：{type(error).__name__}: {error}") from error
        if code:
            return code

    raise OpenAIMailboxDeliveryTimeout(mailbox, "验证码投递超时：邮箱服务未返回新邮件")


from utils.sentinel import (
    SentinelTokenGenerator,
    build_sentinel_token as _build_sentinel_token_tuple,
    build_sentinel_with_so_token,
)


def build_sentinel_token(
    session: requests.Session,
    device_id: str,
    flow: str,
    fingerprint: dict[str, str] | None = None,
) -> str:
    """请求 sentinel token，返回 sentinel header 字符串（兼容旧接口）。"""
    fp = _browser_fingerprint(fingerprint)
    sentinel_val, oai_sc_value = _build_sentinel_token_tuple(
        session,
        device_id,
        flow,
        user_agent=fp["user_agent"],
        sec_ch_ua=fp["sec_ch_ua"],
    )
    _set_oai_sc_cookie(session, oai_sc_value)
    return sentinel_val


def _set_oai_sc_cookie(session: requests.Session, value: str) -> None:
    value = str(value or "").strip()
    if not value:
        return
    for domain in (".auth.openai.com", "auth.openai.com"):
        try:
            session.cookies.set("oai-sc", value, domain=domain)
        except Exception:
            continue


def _cookie_presence(session: requests.Session, names: tuple[str, ...]) -> str:
    """Return only cookie presence for diagnostics; never log cookie values."""
    expected = {str(name) for name in names if str(name)}
    found: set[str] = set()
    try:
        get_dict = getattr(session.cookies, "get_dict", None)
        if callable(get_dict):
            found.update(str(name) for name in get_dict() if str(name) in expected)
        cookie_jar = getattr(session.cookies, "jar", session.cookies)
        for cookie in cookie_jar:
            name = str(getattr(cookie, "name", "") or "")
            if name in expected:
                found.add(name)
    except Exception:
        pass
    return ",".join(f"{name}={'yes' if name in found else 'no'}" for name in names)


def create_session(proxy: str = "", fingerprint: dict[str, str] | None = None) -> Any:
    fp = _browser_fingerprint(fingerprint)
    kwargs = proxy_settings.build_session_kwargs(
        proxy=proxy,
        upstream=True,
        impersonate=fp["impersonate"],
        verify=False,
    )
    session = requests.Session(**kwargs)
    try:
        # Explicit proxy settings are supplied above. Do not let unrelated
        # HTTP(S)_PROXY environment variables split this auth state machine.
        session.trust_env = False
    except Exception:
        pass
    session.headers.update({"user-agent": fp["user_agent"]})
    return session


def _apply_clearance_to_session(session: requests.Session, bundle: ClearanceBundle | None) -> None:
    if bundle is None:
        return
    if bundle.user_agent:
        session.headers["user-agent"] = bundle.user_agent
    for name, value in bundle.cookies.items():
        try:
            session.cookies.set(name, value, domain=f".{bundle.target_host or 'openai.com'}")
            session.cookies.set(name, value, domain=bundle.target_host or "auth.openai.com")
        except Exception:
            continue


def _headers_with_clearance(
    headers: dict[str, str],
    target_url: str,
    proxy: str = "",
    user_agent_override: str = "",
) -> dict[str, str]:
    merged = proxy_settings.build_headers(
        headers=headers,
        target_url=target_url,
        proxy=proxy,
        upstream=True,
    )
    normalized = {str(key): str(value) for key, value in merged.items()}
    if user_agent_override:
        ua_key = next((key for key in normalized if key.lower() == "user-agent"), "user-agent")
        normalized[ua_key] = user_agent_override
    return normalized


def _cloudflare_block_message(resp, prefix: str = "被 Cloudflare 拦截", reason: str = "") -> str:
    status = getattr(resp, "status_code", "unknown")
    debug = _response_debug_detail(resp)
    reason = reason or "clearance 刷新失败或重试后仍失败，请更换 IP/代理重试"
    return f"{prefix}，{reason}: status={status}, {debug}"


def request_with_local_retry(session: requests.Session, method: str, url: str, retry_attempts: int = 3, **kwargs):
    last_error = ""
    for _ in range(max(1, retry_attempts)):
        try:
            return session.request(method.upper(), url, timeout=default_timeout, **kwargs), ""
        except Exception as error:
            last_error = str(error)
            time.sleep(1)
    return None, last_error


def validate_otp(
    session: requests.Session,
    device_id: str,
    code: str,
    fingerprint: dict[str, str] | None = None,
):
    """Submit an email OTP once against the current authorization state.

    The upstream challenge is single-use. Retrying the same code with a
    different Sentinel context turns an actionable response into an
    ``invalid_state`` and makes the original failure impossible to diagnose.
    """
    headers = _header_fingerprint(common_headers, fingerprint)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    return request_with_local_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/email-otp/validate",
        retry_attempts=1,
        json={"code": code},
        headers=headers,
        verify=False,
    )


def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}


def extract_continue_url(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    direct = str(data.get("continue_url") or data.get("continueUrl") or "").strip()
    if direct:
        return direct
    page = data.get("page")
    if isinstance(page, dict):
        payload = page.get("payload")
        if isinstance(payload, dict):
            nested = str(
                payload.get("continue_url")
                or payload.get("continueUrl")
                or payload.get("next_url")
                or payload.get("nextUrl")
                or ""
            ).strip()
            if nested:
                return nested
    session_info = data.get("oai-client-auth-session")
    if isinstance(session_info, dict):
        return str(session_info.get("continue_url") or session_info.get("continueUrl") or "").strip()
    return ""


def _absolute_auth_url(url: str) -> str:
    value = str(url or "").strip()
    if value.startswith("/"):
        return f"{auth_base}{value}"
    return value


def _safe_url_for_log(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return "-"
    try:
        parsed = urlparse(value)
    except Exception:
        return value[:160]
    if parsed.scheme and parsed.netloc:
        return f"{parsed.netloc}{parsed.path}"[:160]
    return parsed.path[:160] if parsed.path else value[:160]


def _url_path(url: str) -> str:
    value = _absolute_auth_url(url)
    try:
        return urlparse(value).path.rstrip("/") or "/"
    except Exception:
        return ""


def _append_exchange_error(errors: list[str] | None, message: str) -> None:
    if errors is not None and message:
        errors.append(message)


def request_platform_oauth_token(
    session: requests.Session,
    code: str,
    code_verifier: str,
    errors: list[str] | None = None,
    fingerprint: dict[str, str] | None = None,
) -> dict | None:
    fp = _browser_fingerprint(fingerprint)
    headers = {
        "accept": "*/*",
        "accept-language": fp["accept_language"],
        "auth0-client": platform_auth0_client,
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": platform_base,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": f"{platform_base}/",
        "sec-ch-ua": fp["sec_ch_ua"],
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": fp["user_agent"],
    }
    try:
        resp = session.post(
            f"{auth_base}/api/accounts/oauth/token",
            headers=headers,
            json={
                "client_id": platform_oauth_client_id,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": platform_oauth_redirect_uri,
            },
            verify=False,
            timeout=60,
        )
    except Exception as error:
        _append_exchange_error(errors, f"api token 请求异常: {str(error)[:300]}")
        return None
    if resp.status_code != 200:
        _append_exchange_error(errors, f"api token 接口拒绝: status={resp.status_code}, {_response_debug_detail(resp, 500)}")
        return None
    data = _response_json(resp)
    missing = [key for key in ("access_token", "refresh_token") if not data.get(key)]
    if missing:
        _append_exchange_error(errors, f"api token 返回缺少字段: {', '.join(missing)}")
        return None
    return data


def request_platform_oauth_token_legacy(
    session: requests.Session,
    code: str,
    code_verifier: str,
    proxy: str = "",
    errors: list[str] | None = None,
    fresh_session: bool = False,
    fingerprint: dict[str, str] | None = None,
) -> dict | None:
    token_session = create_session(proxy, fingerprint) if fresh_session else session
    resp = None
    try:
        resp = token_session.post(
            f"{auth_base}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": platform_oauth_redirect_uri,
                "client_id": platform_oauth_client_id,
                "code_verifier": code_verifier,
            },
            verify=False,
            timeout=60,
        )
    except Exception as error:
        _append_exchange_error(errors, f"legacy token 请求异常: {str(error)[:300]}")
        return None
    finally:
        if fresh_session:
            try:
                token_session.close()
            except Exception:
                pass
    if resp is None:
        _append_exchange_error(errors, "legacy token 未返回响应")
        return None
    data = _response_json(resp)
    if resp.status_code != 200:
        _append_exchange_error(errors, f"legacy token 接口拒绝: status={resp.status_code}, {_response_debug_detail(resp, 500)}")
        return None
    missing = [key for key in ("access_token", "refresh_token", "id_token") if not data.get(key)]
    if missing:
        _append_exchange_error(errors, f"legacy token 返回缺少字段: {', '.join(missing)}")
        return None
    return data


def extract_callback_via_consent(
    session: requests.Session,
    consent_url: str,
    device_id: str,
    proxy: str = "",
    user_agent_override: str = "",
    fingerprint: dict[str, str] | None = None,
) -> dict[str, str] | None:
    current = _absolute_auth_url(consent_url)
    if not current:
        return None
    fp = _fingerprint_with_user_agent(fingerprint, user_agent_override) if user_agent_override else _browser_fingerprint(fingerprint)
    for _ in range(10):
        headers = _headers_with_clearance(_header_fingerprint(navigate_headers, fp), current, proxy, user_agent_override)
        resp, _error = request_with_local_retry(session, "get", current, headers=headers, verify=False, allow_redirects=False)
        if resp is None:
            return None
        callback = extract_oauth_callback_params_from_url(str(getattr(resp, "url", "") or ""))
        callback = callback or extract_oauth_callback_params_from_url(str(getattr(resp, "headers", {}).get("Location") or ""))
        if callback:
            return callback
        location = str(getattr(resp, "headers", {}).get("Location") or "").strip()
        if getattr(resp, "status_code", 0) not in (301, 302, 303, 307, 308) or not location:
            break
        current = _absolute_auth_url(location)

    raw = session.cookies.get("oai-client-auth-session", domain=".auth.openai.com") or session.cookies.get("oai-client-auth-session")
    if not raw:
        return None
    try:
        first = raw.split(".")[0]
        pad = 4 - len(first) % 4
        if pad != 4:
            first += "=" * pad
        payload = json.loads(base64.urlsafe_b64decode(first))
        workspace_id = payload["workspaces"][0]["id"]
    except Exception:
        return None

    url = f"{auth_base}/api/accounts/workspace/select"
    headers = _header_fingerprint(common_headers, fp)
    headers["referer"] = current
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    headers = _headers_with_clearance(headers, url, proxy, user_agent_override)
    ws_resp, _error = request_with_local_retry(session, "post", url, json={"workspace_id": workspace_id}, headers=headers, verify=False, allow_redirects=False)
    if ws_resp is None:
        return None
    callback = extract_oauth_callback_params_from_url(str(getattr(ws_resp, "headers", {}).get("Location") or ""))
    if callback:
        return callback

    ws_data = _response_json(ws_resp)
    orgs = ((ws_data.get("data") or {}).get("orgs") or []) if isinstance(ws_data, dict) else []
    if not orgs:
        return None
    org_id = str((orgs[0] or {}).get("id") or "").strip()
    project_id = str(((orgs[0] or {}).get("projects") or [{}])[0].get("id") or "").strip()
    if not org_id:
        return None
    org_url = f"{auth_base}/api/accounts/organization/select"
    org_headers = _header_fingerprint(common_headers, fp)
    org_headers["referer"] = str(ws_data.get("continue_url") or current)
    org_headers["oai-device-id"] = device_id
    org_headers.update(_make_trace_headers())
    org_headers = _headers_with_clearance(org_headers, org_url, proxy, user_agent_override)
    body = {"org_id": org_id}
    if project_id:
        body["project_id"] = project_id
    org_resp, _error = request_with_local_retry(session, "post", org_url, json=body, headers=org_headers, verify=False, allow_redirects=False)
    if org_resp is None:
        return None
    return extract_oauth_callback_params_from_url(str(getattr(org_resp, "headers", {}).get("Location") or ""))


def exchange_tokens_from_continue_url(
    session: requests.Session,
    device_id: str,
    code_verifier: str,
    continue_url: str,
    proxy: str = "",
    user_agent_override: str = "",
    errors: list[str] | None = None,
    fingerprint: dict[str, str] | None = None,
) -> dict | None:
    callback = extract_oauth_callback_params_from_url(continue_url)
    fp = _fingerprint_with_user_agent(fingerprint, user_agent_override) if user_agent_override else _browser_fingerprint(fingerprint)
    callback = callback or extract_callback_via_consent(
        session,
        continue_url,
        device_id,
        proxy,
        user_agent_override,
        fp,
    )
    if not callback:
        url = _absolute_auth_url(continue_url)
        try:
            headers = _headers_with_clearance(_header_fingerprint(navigate_headers, fp), url, proxy, user_agent_override)
            resp = session.get(url, headers=headers, allow_redirects=True, verify=False, timeout=30)
            callback = extract_oauth_callback_params_from_url(str(getattr(resp, "url", "") or ""))
            if not callback:
                for history in getattr(resp, "history", []) or []:
                    callback = extract_oauth_callback_params_from_url(str(history.headers.get("Location") or ""))
                    if callback:
                        break
            if not callback:
                _append_exchange_error(
                    errors,
                    f"跟随 continue_url 后仍未拿到 callback: status={getattr(resp, 'status_code', 'unknown')}, final={_safe_url_for_log(str(getattr(resp, 'url', '') or ''))}",
                )
        except Exception as error:
            _append_exchange_error(errors, f"跟随 continue_url 异常: {str(error)[:300]}")
            callback = None
    code = str((callback or {}).get("code") or "").strip()
    if not code:
        _append_exchange_error(errors, f"未拿到 OAuth callback code: continue={_safe_url_for_log(continue_url)}")
        return None
    return request_platform_oauth_token_legacy(session, code, code_verifier, proxy, errors, fresh_session=True, fingerprint=fp)


class PlatformRegistrar:
    def __init__(self, proxy: str = "") -> None:
        self.proxy = str(proxy or "").strip()
        self.fingerprint = _make_browser_fingerprint()
        self.session = create_session(self.proxy, self.fingerprint)
        self.clearance_user_agent = ""
        self.clearance_failure_reason = ""
        self.device_id = str(uuid.uuid4())
        self.code_verifier = ""
        self.platform_auth_code = ""
        self._platform_authorize_final_url = ""
        self.last_otp_continue_url = ""
        self.passwordless_signup = False
        self.excluded_mail_provider_refs: set[str] = set()

    def close(self) -> None:
        self.session.close()

    def _create_registration_mailbox(self) -> dict:
        excluded = {
            str(item or "").strip()
            for item in getattr(self, "excluded_mail_provider_refs", set())
            if str(item or "").strip()
        }
        return create_mailbox(
            register_proxy=self.proxy,
            excluded_provider_refs=excluded,
        )

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = _header_fingerprint(navigate_headers, self.fingerprint)
        if referer:
            headers["referer"] = referer
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = _header_fingerprint(common_headers, self.fingerprint)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _refresh_cloudflare_clearance(self, target_url: str, index: int) -> ClearanceBundle | None:
        self.clearance_failure_reason = ""
        profile = proxy_settings.get_profile(proxy=self.proxy, upstream=True)
        if not profile.clearance_enabled:
            self.clearance_failure_reason = (
                "可尝试使用 FlareSolverr 清障方式，注意需要 Docker 部署 flaresolverr、privoxy、warp-proxy 等相关容器"
            )
            step(index, f"检测到 Cloudflare 拦截，{self.clearance_failure_reason}", "yellow")
            return None
        step(index, "检测到 Cloudflare 拦截，尝试刷新 clearance", "yellow")
        bundle = proxy_settings.refresh_clearance(
            target_url=target_url,
            proxy=self.proxy,
            force=True,
            upstream=True,
        )
        if bundle is not None:
            _apply_clearance_to_session(self.session, bundle)
            self.clearance_user_agent = bundle.user_agent or self.clearance_user_agent
            if bundle.user_agent:
                self.fingerprint = _fingerprint_with_user_agent(self.fingerprint, bundle.user_agent)
            step(index, "Cloudflare clearance 刷新完成，重试当前请求", "yellow")
        else:
            self.clearance_failure_reason = "clearance 刷新未返回可用 Cookie，请检查 FlareSolverr URL、代理和出口 IP"
            step(index, f"Cloudflare clearance 刷新失败：{self.clearance_failure_reason}", "yellow")
        return bundle

    def _platform_authorize(self, email: str, index: int, screen_hint: str = "login_or_signup") -> str:
        step(index, "开始 platform authorize")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        self.code_verifier, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            # 官网当前的新账号流程使用 passwordless signup。
            "screen_hint": screen_hint,
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        target_url = f"{auth_base}/api/accounts/authorize?{urlencode(params)}"
        headers = self._navigate_headers(f"{platform_base}/")
        headers = _headers_with_clearance(headers, target_url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "get", target_url, headers=headers, allow_redirects=True, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            retry_headers = _headers_with_clearance(self._navigate_headers(f"{platform_base}/"), target_url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "get", target_url, headers=retry_headers, allow_redirects=True, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            err = _response_json(resp).get("error", {}) if resp is not None else {}
            detail = f": {err.get('code', '')} - {err.get('message', '')}".strip(" -") if err else ""
            debug = _response_debug_detail(resp)
            status = getattr(resp, "status_code", "unknown")
            raise RuntimeError(error or f"platform_authorize_http_{status}{detail}, {debug}")
        landed = _authorize_landed_page(resp)
        final_url = str(getattr(resp, "url", "") or "")
        self._platform_authorize_final_url = final_url
        self.passwordless_signup = "/email-verification" in final_url.lower()
        entry_path = _url_path(final_url) or "/"
        step(
            index,
            f"platform authorize 完成[{landed or '?'}] entry={entry_path} "
            f"passwordless_active={self.passwordless_signup}",
        )
        return landed

    def _reset_auth_cookies(self) -> None:
        jar = getattr(self.session.cookies, "jar", self.session.cookies)
        for cookie in list(jar):
            domain = str(getattr(cookie, "domain", "") or "")
            if "auth.openai.com" not in domain:
                continue
            try:
                self.session.cookies.delete(
                    str(getattr(cookie, "name", "") or ""),
                    domain=domain,
                    path=str(getattr(cookie, "path", "/") or "/"),
                )
            except Exception:
                continue
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")

    def _authorize_continue_login(self, email: str, index: int) -> dict:
        step(index, "提交 Microsoft 邮箱进入登录验证")
        url = f"{auth_base}/api/accounts/authorize/continue"

        def send():
            headers = self._json_headers(f"{auth_base}/log-in?usernameKind=email")
            headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "authorize_continue", self.fingerprint)
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            return request_with_local_retry(
                self.session,
                "post",
                url,
                json={"username": {"kind": "email", "value": email}},
                headers=headers,
                allow_redirects=False,
                verify=False,
            )

        resp, error = send()
        if resp is not None and resp.status_code == 409:
            step(index, "登录会话过期，重新发起登录授权", "yellow")
            self._reset_auth_cookies()
            self._platform_authorize(email, index, screen_hint="login_or_signup")
            resp, error = send()
        if resp is None or resp.status_code != 200:
            detail = _response_json(resp) if resp is not None else {}
            raise RuntimeError(error or f"login_continue_http_{getattr(resp, 'status_code', 'unknown')}, detail={json.dumps(detail, ensure_ascii=False)[:300]}")
        data = _response_json(resp)
        if ((data.get("page") or {}).get("payload") or {}).get("passwordless_disabled"):
            raise RuntimeError("当前 OpenAI 登录流不支持 passwordless，请换邮箱或使用已有密码重登")
        return data

    def _send_passwordless_otp(self, index: int) -> None:
        step(index, "发送 OpenAI 登录验证码")
        url = f"{auth_base}/api/accounts/passwordless/send-otp"
        headers = self._json_headers(f"{auth_base}/log-in/password")
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={}, headers=headers, allow_redirects=False, verify=False)
        if resp is None or resp.status_code not in (200, 201, 204):
            detail = _response_json(resp) if resp is not None else {}
            raise RuntimeError(error or f"passwordless_send_otp_http_{getattr(resp, 'status_code', 'unknown')}, detail={json.dumps(detail, ensure_ascii=False)[:300]}")

    @staticmethod
    def _is_passwordless_invalid_state(resp) -> bool:
        if resp is None or getattr(resp, "status_code", None) != 409:
            return False
        data = _response_json(resp)
        error = data.get("error") if isinstance(data, dict) else None
        if not isinstance(error, dict):
            return False
        code = str(error.get("code") or "").strip().lower()
        message = str(error.get("message") or "").strip().lower()
        return code == "invalid_state" or "sign-in session is no longer valid" in message

    def _passwordless_login(self, email: str, mailbox: dict, index: int) -> dict:
        supports_login = (
            str(mailbox.get("provider") or "") == "outlook_token"
            or bool(mailbox.get("supports_passwordless_login"))
        )
        if not supports_login:
            raise RuntimeError("OpenAI 返回登录流，当前邮箱来源无法读取登录验证码")
        step(index, "OpenAI 返回登录流，转入 passwordless 登录", "yellow")
        for attempt in range(2):
            if attempt:
                step(index, "OpenAI 登录会话失效，重新发起 passwordless 登录", "yellow")
                self._reset_auth_cookies()
                self._platform_authorize(email, index, screen_hint="login_or_signup")
            self._authorize_continue_login(email, index)
            mailbox["_received_after"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
            self._send_passwordless_otp(index)
            step(index, "开始等待 OpenAI 登录验证码")
            code = wait_for_code(mailbox, register_proxy=self.proxy)
            if not code:
                raise RuntimeError("等待 OpenAI 登录验证码超时")
            step(index, f"收到 OpenAI 登录验证码: {code}")
            resp, error = validate_otp(self.session, self.device_id, code, self.fingerprint)
            if resp is not None and resp.status_code == 200:
                break
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            if attempt == 0 and self._is_passwordless_invalid_state(resp):
                continue
            raise RuntimeError(error or f"passwordless_validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}")
        data = _response_json(resp)
        continue_url = str(data.get("continue_url") or "").strip() or f"{auth_base}/sign-in-with-chatgpt/platform/consent"
        if _url_path(continue_url) == "/about-you":
            first_name, last_name = _random_name()
            step(index, "OpenAI 登录验证完成，需要完善账号资料")
            self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
            return self._exchange_registered_tokens(index)
        step(index, "OpenAI 登录验证完成，开始换 token")
        exchange_errors: list[str] = []
        tokens = exchange_tokens_from_continue_url(
            self.session,
            self.device_id,
            self.code_verifier,
            continue_url,
            self.proxy,
            self.clearance_user_agent,
            exchange_errors,
            self.fingerprint,
        )
        if not tokens:
            detail = "；".join(exchange_errors[-4:]) if exchange_errors else "未返回 token"
            raise RuntimeError(f"Microsoft passwordless token 换取失败: {detail}")
        step(index, "Microsoft passwordless token 换取完成")
        return tokens

    def _start_passwordless_signup(self, index: int) -> None:
        step(index, "开始切换 passwordless signup 并发送验证码")
        url = f"{auth_base}/api/accounts/passwordless/send-otp"

        def send():
            headers = self._json_headers(f"{auth_base}/create-account/password")
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            return request_with_local_retry(self.session, "post", url, headers=headers, verify=False)

        resp, error = send()
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            resp, error = send()
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        data = _response_json(resp) if resp is not None else {}
        if _passwordless_signup_explicitly_unavailable(data):
            raise PasswordlessSignupUnavailable("OpenAI explicitly disabled passwordless signup")
        if resp is None or resp.status_code not in (200, 201, 204):
            detail = f", detail={json.dumps(data, ensure_ascii=False)[:300]}" if data else ""
            raise RuntimeError(error or f"passwordless_send_otp_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        self.passwordless_signup = True
        step(index, "passwordless signup 验证码发送完成")

    def _register_user(self, email: str, password: str, index: int) -> None:
        step(index, "开始提交注册密码")
        url = f"{auth_base}/api/accounts/user/register"
        headers = self._json_headers(f"{auth_base}/create-account/password")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "username_password_create", self.fingerprint)
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/create-account/password")
            headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "username_password_create", self.fingerprint)
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "提交注册密码完成")

    def _send_otp(self, index: int) -> None:
        step(index, "开始发送验证码")
        url = f"{auth_base}/api/accounts/email-otp/send"
        headers = _headers_with_clearance(self._navigate_headers(f"{auth_base}/create-account/password"), url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = _headers_with_clearance(self._navigate_headers(f"{auth_base}/create-account/password"), url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=True, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "发送验证码完成")

    def _validate_otp(self, code: str, index: int) -> None:
        step(index, f"开始校验验证码 {code}")
        resp, error = validate_otp(self.session, self.device_id, code, self.fingerprint)
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}")
        data = _response_json(resp)
        continue_url = extract_continue_url(data)
        if continue_url:
            self.last_otp_continue_url = continue_url
            self._authorize_continue(continue_url, index)
        step(index, "验证码校验完成")

    def _authorize_continue(self, continue_url: str, index: int) -> None:
        url = str(continue_url or "").strip()
        if not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            url = urljoin(f"{auth_base}/", url.lstrip("/"))

        def send():
            headers = self._navigate_headers(f"{auth_base}/email-verification")
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            return request_with_local_retry(
                self.session,
                "get",
                url,
                headers=headers,
                allow_redirects=True,
                verify=False,
            )

        step(index, "开始继续注册授权")
        resp, error = send()
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            resp, error = send()
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            debug = _response_debug_detail(resp)
            raise RuntimeError(
                error
                or f"authorize_continue_http_{getattr(resp, 'status_code', 'unknown')}, {debug}"
            )
        step(index, f"继续注册授权完成 url={str(getattr(resp, 'url', '') or '')[:160]}")

    def _create_account(self, name: str, birthdate: str, index: int) -> None:
        step(index, "开始创建账号资料")
        url = f"{auth_base}/api/accounts/create_account"
        headers = self._json_headers(f"{auth_base}/about-you")

        # 使用新的 Sentinel 函数，同时获取 Sentinel Token 和 SO Token
        fp = _browser_fingerprint(self.fingerprint)
        sentinel_token, so_token, _oai_sc = build_sentinel_with_so_token(
            self.session,
            self.device_id,
            "oauth_create_account",
            user_agent=fp["user_agent"],
            sec_ch_ua=fp["sec_ch_ua"],
        )
        _set_oai_sc_cookie(self.session, _oai_sc)

        headers["openai-sentinel-token"] = sentinel_token
        if so_token:
            headers["openai-sentinel-so-token"] = so_token

        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/about-you")

            # 重新生成 Sentinel Token 和 SO Token
            sentinel_token, so_token, _oai_sc = build_sentinel_with_so_token(
                self.session,
                self.device_id,
                "oauth_create_account",
                user_agent=fp["user_agent"],
                sec_ch_ua=fp["sec_ch_ua"],
            )
            _set_oai_sc_cookie(self.session, _oai_sc)

            headers["openai-sentinel-token"] = sentinel_token
            if so_token:
                headers["openai-sentinel-so-token"] = so_token

            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "创建账号失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        data = _response_json(resp)
        callback_params = (
            extract_oauth_callback_params_from_url(str(data.get("continue_url") or "").strip())
            or extract_oauth_callback_params_from_url(str(getattr(resp, "headers", {}).get("Location") or "").strip())
            or extract_oauth_callback_params_from_url(str(getattr(resp, "url", "") or "").strip())
        )
        self.platform_auth_code = str((callback_params or {}).get("code") or "").strip()
        if not self.platform_auth_code:
            continue_hint = str(data.get("continue_url") or getattr(resp, "headers", {}).get("Location") or getattr(resp, "url", "") or "")
            raise RuntimeError(f"create_account_missing_callback: continue={_safe_url_for_log(continue_hint)}")
        step(index, "创建账号资料完成")

    def _exchange_registered_tokens(self, index: int) -> dict:
        step(index, "开始换 token")
        if not self.platform_auth_code:
            raise RuntimeError("token换取失败: 缺少 OAuth callback code")
        exchange_errors: list[str] = []
        tokens = request_platform_oauth_token(self.session, self.platform_auth_code, self.code_verifier, exchange_errors, self.fingerprint)
        if not tokens:
            detail = "；".join(exchange_errors[-3:]) if exchange_errors else "未返回 token"
            raise RuntimeError(f"token换取失败: {detail}")
        step(index, "token 换取完成")
        return tokens

    def extract_platform_oauth_credentials(self, email: str, index: int) -> dict:
        """Derive durable Platform OAuth tokens from an authenticated web session.

        The normal ChatGPT registration path intentionally preserves its web
        session for checkout work.  When a downstream Sub2API sync is enabled,
        this additional PKCE authorization produces the refreshable OAuth
        credential set that Sub2API expects without replacing the local web
        session account.
        """
        if not self.device_id:
            self.device_id = str(uuid.uuid4())
            for domain in (".auth.openai.com", "auth.openai.com"):
                try:
                    self.session.cookies.set("oai-did", self.device_id, domain=domain)
                except Exception:
                    continue

        self.platform_auth_code = ""
        self._platform_authorize_final_url = ""
        self._platform_authorize(email, index, screen_hint="login")
        continue_url = str(self._platform_authorize_final_url or "").strip()
        callback = extract_oauth_callback_params_from_url(continue_url)
        exchange_errors: list[str] = []
        tokens = None
        if callback and callback.get("code"):
            tokens = request_platform_oauth_token(
                self.session,
                str(callback["code"]),
                self.code_verifier,
                exchange_errors,
                self.fingerprint,
            )
        if not tokens and continue_url:
            tokens = exchange_tokens_from_continue_url(
                self.session,
                self.device_id,
                self.code_verifier,
                continue_url,
                self.proxy,
                self.clearance_user_agent,
                exchange_errors,
                self.fingerprint,
            )
        if not tokens or not str(tokens.get("access_token") or "").strip() or not str(tokens.get("refresh_token") or "").strip():
            raise RuntimeError("未能从当前 ChatGPT 会话提取可同步的 OAuth 凭据")
        return tokens

    def register(self, index: int) -> dict:
        step(index, "开始创建邮箱")
        mailbox = self._create_registration_mailbox()
        email = str(mailbox.get("address") or "").strip()
        if not email:
            mail_provider.release_mailbox(mailbox)
            raise RuntimeError("邮箱服务未返回 address")
        label = str(mailbox.get("label") or "")
        step(index, f"邮箱创建完成[{label}]: {email}")
        try:
            first_name, last_name = _random_name()
            password = ""
            # authorize 可能直接发送 OTP，先记录收信边界，避免慢跳转后漏掉验证码。
            mailbox["_received_after"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
            landed = self._platform_authorize(email, index)
            source_type = "web"
            if landed == "login":
                tokens = self._passwordless_login(email, mailbox, index)
                source_type = "microsoft"
            else:
                if not self.passwordless_signup:
                    mailbox["_received_after"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
                    try:
                        self._start_passwordless_signup(index)
                    except PasswordlessSignupUnavailable:
                        password = _random_password()
                        step(index, "上游明确禁用 passwordless signup，切换传统密码注册", "yellow")
                        self._register_user(email, password, index)
                        mailbox["_received_after"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
                        self._send_otp(index)
                if self.passwordless_signup:
                    step(index, "已进入 passwordless signup，不创建本地不可用的随机密码")
                else:
                    step(index, "传统密码注册已提交，开始等待注册验证码")
                step(index, "开始等待注册验证码")
                code = wait_for_code(mailbox, register_proxy=self.proxy)
                if not code:
                    raise RuntimeError("等待注册验证码超时")
                step(index, f"收到注册验证码: {code}")
                self._validate_otp(code, index)
                self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
                tokens = self._exchange_registered_tokens(index)
        except Exception as error:
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
            raise
        mail_provider.mark_mailbox_result(mailbox, success=True)
        return {
            "email": email,
            "password": password,
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "source_type": source_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


class ChatGPTWebRegistrar(PlatformRegistrar):
    """Use the real ChatGPT web client OAuth flow instead of Platform OAuth."""

    _CHATGPT_CALLBACK_PATH = "/api/auth/callback/openai"
    _CHATGPT_SESSION_PATH = "/api/auth/session"

    def _chatgpt_headers(
        self,
        target_path: str,
        target_route: str | None = None,
        *,
        referer: str = "",
        content_type: str = "application/json",
    ) -> dict[str, str]:
        headers = _header_fingerprint(common_headers, self.fingerprint)
        headers["origin"] = chatgpt_base
        headers["referer"] = referer or f"{chatgpt_base}/"
        headers["content-type"] = content_type
        headers["x-openai-target-path"] = target_path
        headers["x-openai-target-route"] = target_route or target_path
        return headers

    def _chatgpt_request(self, method: str, path: str, index: int, **kwargs):
        url = f"{chatgpt_base}{path}"
        headers = kwargs.pop("headers", {})
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, method, url, headers=headers, verify=False, **kwargs)
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(chatgpt_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, method, url, headers=headers, verify=False, **kwargs)
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "ChatGPT clearance 重试仍被拦截"))
        return resp, error

    def _chatgpt_csrf(self, index: int) -> str:
        path = "/api/auth/csrf"
        resp, error = self._chatgpt_request(
            "get",
            path,
            index,
            headers=self._chatgpt_headers(path, referer=f"{chatgpt_base}/"),
        )
        data = _response_json(resp)
        token = str(data.get("csrfToken") or "").strip()
        if resp is None or resp.status_code != 200 or not token:
            raise RuntimeError(
                error
                or f"chatgpt_csrf_http_{getattr(resp, 'status_code', 'unknown')}, {_response_debug_detail(resp, 400)}"
            )
        return token

    def _chatgpt_begin_signin(self, csrf_token: str, index: int) -> str:
        path = "/api/auth/signin/openai?prompt=login&screen_hint=login_or_signup"
        resp, error = self._chatgpt_request(
            "post",
            path,
            index,
            headers=self._chatgpt_headers(
                "/api/auth/signin/openai",
                "/api/auth/signin/{provider}",
                referer=f"{chatgpt_base}/",
                content_type="application/x-www-form-urlencoded",
            ),
            data={
                "csrfToken": csrf_token,
                "callbackUrl": f"{chatgpt_base}/",
                "json": "true",
            },
        )
        data = _response_json(resp)
        if resp is None or resp.status_code != 200:
            raise RuntimeError(
                error
                or f"chatgpt_signin_http_{getattr(resp, 'status_code', 'unknown')}, {_response_debug_detail(resp, 400)}"
            )
        return str(data.get("url") or "").strip()

    def _chatgpt_web_authorize(self, index: int) -> None:
        """Perform NextAuth's two CSRF/sign-in handshakes and land on auth.openai.com."""
        step(index, "初始化 ChatGPT 网页授权")
        self._chatgpt_begin_signin(self._chatgpt_csrf(index), index)
        authorize_url = self._chatgpt_begin_signin(self._chatgpt_csrf(index), index)
        parsed = urlparse(authorize_url)
        if parsed.scheme != "https" or parsed.netloc != "auth.openai.com" or "/api/accounts/authorize" not in parsed.path:
            raise RuntimeError(f"ChatGPT 未返回有效 authorize URL: {_safe_url_for_log(authorize_url)}")
        device_id = str((parse_qs(parsed.query).get("device_id") or [""])[0]).strip()
        if not device_id:
            raise RuntimeError("ChatGPT authorize URL 缺少 device_id")
        self.device_id = device_id
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")

        headers = _headers_with_clearance(
            self._navigate_headers(f"{chatgpt_base}/"), authorize_url, self.proxy, self.clearance_user_agent
        )
        resp, error = request_with_local_retry(
            self.session, "get", authorize_url, headers=headers, allow_redirects=True, verify=False
        )
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = _headers_with_clearance(
                self._navigate_headers(f"{chatgpt_base}/"), authorize_url, self.proxy, self.clearance_user_agent
            )
            resp, error = request_with_local_retry(
                self.session, "get", authorize_url, headers=headers, allow_redirects=True, verify=False
            )
        if resp is None or resp.status_code != 200:
            raise RuntimeError(
                error
                or f"chatgpt_authorize_http_{getattr(resp, 'status_code', 'unknown')}, {_response_debug_detail(resp, 400)}"
            )
        if urlparse(str(getattr(resp, "url", "") or "")).netloc != "auth.openai.com":
            raise RuntimeError(f"ChatGPT authorize 跳转异常: {_safe_url_for_log(str(getattr(resp, 'url', '') or ''))}")
        step(index, "ChatGPT 网页授权上下文已建立")

    def _chatgpt_continue_username(self, email: str, index: int) -> dict[str, Any]:
        step(index, "提交 ChatGPT 注册邮箱")
        url = f"{auth_base}/api/accounts/authorize/continue"
        body = {
            "username": {"kind": "email", "value": email},
            "screen_hint": "login_or_signup",
        }
        sentinel_context = ("", "")

        def send():
            nonlocal sentinel_context
            headers = self._json_headers(f"{auth_base}/log-in-or-create-account")
            fp = _browser_fingerprint(self.fingerprint)
            sentinel_token, so_token, oai_sc_value = build_sentinel_with_so_token(
                self.session,
                self.device_id,
                "authorize_continue",
                user_agent=fp["user_agent"],
                sec_ch_ua=fp["sec_ch_ua"],
            )
            _set_oai_sc_cookie(self.session, oai_sc_value)
            sentinel_context = (sentinel_token, so_token)
            headers["openai-sentinel-token"] = sentinel_token
            if so_token:
                headers["openai-sentinel-so-token"] = so_token
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            return request_with_local_retry(
                self.session, "post", url, json=body, headers=headers, allow_redirects=False, verify=False
            )

        resp, error = send()
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            resp, error = send()
        data = _response_json(resp)
        if resp is None or resp.status_code != 200:
            raise RuntimeError(
                error
                or f"chatgpt_authorize_continue_http_{getattr(resp, 'status_code', 'unknown')}, "
                f"{json.dumps(data, ensure_ascii=False)[:400]}"
            )
        page_type = str((data.get("page") or {}).get("type") or "").strip()
        if page_type != "email_otp_verification":
            raise RuntimeError(f"ChatGPT 注册未进入邮箱验证页: page_type={page_type or '?'}")
        self._chatgpt_otp_sentinel_token, self._chatgpt_otp_sentinel_so_token = sentinel_context
        step(
            index,
            "ChatGPT 授权 Cookie 状态: "
            + _cookie_presence(
                self.session,
                ("oai-did", "oai-sc", "oai-client-auth-session", "auth-session-minimized-client-checksum"),
            ),
        )
        return data

    def _send_chatgpt_otp(self, index: int) -> None:
        step(index, "发送 ChatGPT 注册验证码")
        url = f"{auth_base}/api/accounts/email-otp/send"
        sentinel_token = str(getattr(self, "_chatgpt_otp_sentinel_token", "") or "").strip()
        so_token = str(getattr(self, "_chatgpt_otp_sentinel_so_token", "") or "").strip()
        if not sentinel_token:
            raise RuntimeError("ChatGPT 发码缺少 authorize/continue 的 Sentinel 上下文")

        def send():
            # email-otp/send belongs to the authorize/continue challenge. Do
            # not refresh Sentinel or oai-sc here: that creates a new local
            # context while the server still expects the original challenge.
            headers = self._json_headers(f"{auth_base}/email-verification")
            headers["openai-sentinel-token"] = sentinel_token
            if so_token:
                headers["openai-sentinel-so-token"] = so_token
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            return request_with_local_retry(
                self.session, "get", url, headers=headers, allow_redirects=True, verify=False
            )

        resp, error = send()
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            resp, error = send()
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(
                error
                or f"chatgpt_send_otp_http_{getattr(resp, 'status_code', 'unknown')}, "
                f"{_response_debug_detail(resp, 400)}"
            )
        data = _response_json(resp)
        error_data = data.get("error") if isinstance(data.get("error"), dict) else {}
        error_message = str(
            error_data.get("message")
            or (data.get("message") if data.get("success") is False else "")
            or ""
        ).strip()
        if error_message:
            raise RuntimeError(f"ChatGPT 发码请求被拒绝: {error_message}")
        request_id = str(getattr(resp, "headers", {}).get("x-request-id") or "").strip()
        response_keys = ",".join(sorted(str(key) for key in data)[:8]) or "none"
        page = data.get("page") if isinstance(data.get("page"), dict) else {}
        page_type = str(page.get("type") or "none").strip() or "none"
        payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
        payload_keys = ",".join(sorted(str(key) for key in payload)[:8]) or "none"
        email_verification_mode = str(payload.get("email_verification_mode") or "none").strip()[:80] or "none"
        continue_path = urlparse(str(data.get("continue_url") or "")).path or "none"
        response_note = f" request_id={request_id}" if request_id else ""
        step(
            index,
            f"ChatGPT 发码响应: HTTP {resp.status_code}, page={page_type}, continue={continue_path}, "
            f"fields={response_keys}, payload_fields={payload_keys}, email_mode={email_verification_mode}, "
            + _cookie_presence(
                self.session,
                ("oai-did", "oai-sc", "oai-client-auth-session", "auth-session-minimized-client-checksum"),
            )
            + response_note,
        )

    def _validate_chatgpt_otp(self, code: str, index: int) -> str:
        self._validate_otp(code, index)
        return self.last_otp_continue_url

    def _create_chatgpt_profile(self, name: str, birthdate: str, index: int) -> str:
        step(index, "提交 ChatGPT 账号资料")
        url = f"{auth_base}/api/accounts/user/profile"
        fp = _browser_fingerprint(self.fingerprint)

        def send():
            headers = self._json_headers(f"{auth_base}/about-you")
            sentinel_token, so_token, _oai_sc = build_sentinel_with_so_token(
                self.session,
                self.device_id,
                "oauth_create_account",
                user_agent=fp["user_agent"],
                sec_ch_ua=fp["sec_ch_ua"],
            )
            _set_oai_sc_cookie(self.session, _oai_sc)
            headers["openai-sentinel-token"] = sentinel_token
            if so_token:
                headers["openai-sentinel-so-token"] = so_token
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            return request_with_local_retry(
                self.session,
                "post",
                url,
                json={"name": name, "birthdate": birthdate},
                headers=headers,
                allow_redirects=False,
                verify=False,
            )

        resp, error = send()
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            resp, error = send()
        data = _response_json(resp)
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(
                error
                or f"chatgpt_profile_http_{getattr(resp, 'status_code', 'unknown')}, {json.dumps(data, ensure_ascii=False)[:400]}"
            )
        callback_url = (
            extract_continue_url(data)
            or str(getattr(resp, "headers", {}).get("Location") or "").strip()
            or str(getattr(resp, "url", "") or "").strip()
        )
        parsed = urlparse(callback_url)
        if parsed.scheme != "https" or parsed.netloc != "chatgpt.com" or parsed.path != self._CHATGPT_CALLBACK_PATH:
            raise RuntimeError(f"ChatGPT 资料提交未返回回调地址: {_safe_url_for_log(callback_url)}")
        return callback_url

    def _complete_chatgpt_callback(self, callback_url: str, index: int) -> dict[str, Any]:
        step(index, "完成 ChatGPT 网页登录回调")
        parsed = urlparse(callback_url)
        if parsed.scheme != "https" or parsed.netloc != "chatgpt.com" or parsed.path != self._CHATGPT_CALLBACK_PATH:
            raise RuntimeError(f"拒绝非 ChatGPT 回调地址: {_safe_url_for_log(callback_url)}")
        callback_headers = _headers_with_clearance(
            self._navigate_headers(f"{auth_base}/about-you"), callback_url, self.proxy, self.clearance_user_agent
        )
        callback_resp, error = request_with_local_retry(
            self.session, "get", callback_url, headers=callback_headers, allow_redirects=True, verify=False
        )
        if callback_resp is None or callback_resp.status_code != 200:
            raise RuntimeError(
                error
                or f"chatgpt_callback_http_{getattr(callback_resp, 'status_code', 'unknown')}, "
                f"{_response_debug_detail(callback_resp, 400)}"
            )
        session_path = self._CHATGPT_SESSION_PATH
        session_resp = None
        session_data: dict[str, Any] = {}
        error = ""
        for attempt in range(1, 5):
            session_resp, error = self._chatgpt_request(
                "get",
                session_path,
                index,
                headers=self._chatgpt_headers(session_path, referer=f"{chatgpt_base}/"),
            )
            session_data = _response_json(session_resp)
            access_token = str(
                session_data.get("accessToken") or session_data.get("access_token") or ""
            ).strip()
            if session_resp is not None and session_resp.status_code == 200 and access_token:
                break
            if session_resp is None or session_resp.status_code != 200:
                raise RuntimeError(
                    error
                    or f"chatgpt_session_http_{getattr(session_resp, 'status_code', 'unknown')}, "
                    f"{_response_debug_detail(session_resp, 400)}"
                )
            if attempt < 4:
                step(index, f"ChatGPT 网页会话尚未就绪，第 {attempt + 1} 次重试")
                time.sleep(attempt)
        else:
            raise RuntimeError("ChatGPT 登录回调完成，但 /api/auth/session 未返回 accessToken")
        step(index, "ChatGPT 网页会话已建立")
        return session_data

    def _chatgpt_session_result(self, email: str, session_data: dict[str, Any]) -> dict[str, Any]:
        user = session_data.get("user") if isinstance(session_data.get("user"), dict) else {}
        access_token = str(session_data.get("accessToken") or session_data.get("access_token") or "").strip()
        result: dict[str, Any] = {
            "email": str(user.get("email") or email).strip(),
            "access_token": access_token,
            "source_type": "chatgpt_web",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "fp": {
                "user-agent": self.fingerprint["user_agent"],
                "impersonate": self.fingerprint["impersonate"],
                "oai-device-id": self.device_id,
                "sec-ch-ua": self.fingerprint["sec_ch_ua"],
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        }
        for camel_key, snake_key in (
            ("refreshToken", "refresh_token"),
            ("idToken", "id_token"),
            ("sessionToken", "session_token"),
            ("expires", "expired"),
        ):
            value = str(session_data.get(camel_key) or session_data.get(snake_key) or "").strip()
            if value:
                result[snake_key] = value
        user_id = str(user.get("id") or user.get("user_id") or "").strip()
        if user_id:
            result["user_id"] = user_id
        cookie_header = _chatgpt_cookie_header(self.session)
        if cookie_header:
            # This stays server-side.  It is needed by later explicit payment
            # actions, but never returned by the account list API.
            result["cookie_header"] = cookie_header
        return result

    def register(self, index: int) -> dict:
        step(index, "开始创建邮箱")
        mailbox = self._create_registration_mailbox()
        email = str(mailbox.get("address") or "").strip()
        if not email:
            mail_provider.release_mailbox(mailbox)
            raise RuntimeError("邮箱服务未返回 address")
        label = str(mailbox.get("label") or "")
        step(index, f"邮箱创建完成[{label}]: {email}")
        try:
            first_name, last_name = _random_name()
            # iCloud Privacy Mail treats a missing keyword as "OpenAI".  The
            # ChatGPT web client currently sends a localized ChatGPT subject,
            # so this flow must ask that mailbox API for ChatGPT explicitly.
            mailbox["_icloud_keyword"] = "ChatGPT"
            mailbox["_received_after"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
            self._chatgpt_web_authorize(index)
            self._chatgpt_continue_username(email, index)
            mailbox["_received_after"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
            self._send_chatgpt_otp(index)
            step(index, "开始等待 ChatGPT 注册验证码")
            code = _wait_for_chatgpt_registration_code(
                mailbox,
                index,
                self.proxy,
                resend=lambda: self._send_chatgpt_otp(index),
            )
            step(index, f"收到 ChatGPT 注册验证码: {code}")
            self._validate_chatgpt_otp(code, index)
            callback_url = self._create_chatgpt_profile(
                f"{first_name} {last_name}", _random_birthdate(), index
            )
            session_data = self._complete_chatgpt_callback(callback_url, index)
            result = self._chatgpt_session_result(email, session_data)
            if not str(result.get("access_token") or "").strip():
                raise RuntimeError("ChatGPT 注册完成但未获得对话 access token")
        except Exception as error:
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
            raise
        mail_provider.mark_mailbox_result(mailbox, success=True)
        return result


class TraditionalChatGPTRegistrar(ChatGPTWebRegistrar):
    """Traditional ChatGPT signup flow adapted from gpt-outlook-register.

    This intentionally uses the password-registration state machine instead
    of the passwordless ``login_or_signup`` onboarding branch.
    """

    _REFERENCE_USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    )

    def __init__(self, proxy: str = "") -> None:
        self.proxy = str(proxy or "").strip()
        self.fingerprint = _complete_browser_fingerprint(
            {
                "impersonate": "chrome136",
                "major": "136",
                "full_version": "136.0.0.0",
                "platform_version": "10.15.7",
                "user_agent": self._REFERENCE_USER_AGENT,
                "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not_A Brand";v="99"',
                "sec_ch_ua_full_version_list": (
                    '"Chromium";v="136.0.0.0", "Google Chrome";v="136.0.0.0", '
                    '"Not_A Brand";v="99.0.0.0"'
                ),
            }
        )
        self.session = create_session(self.proxy, self.fingerprint)
        self.clearance_user_agent = ""
        self.clearance_failure_reason = ""
        self.device_id = ""
        self.code_verifier = ""
        self.platform_auth_code = ""
        self._platform_authorize_final_url = ""
        self.last_otp_continue_url = ""
        self.passwordless_signup = False
        self._reference_signup_mode = ""
        self._reference_email_verification_mode = ""
        self._reference_authorize_sentinel = ""

    @staticmethod
    def _reference_password(email: str) -> str:
        password = str(email or "").replace("@", "")
        return password if len(password) >= 8 else f"{password}2026OpenAI"

    def _reference_nextauth_headers(
        self,
        referer: str,
        content_type: str = "",
    ) -> dict[str, str]:
        """Reference flow uses plain NextAuth headers, without app routing hints."""
        headers = {
            "accept": "application/json",
            "origin": chatgpt_base,
            "referer": referer,
            "user-agent": self.fingerprint["user_agent"],
        }
        if content_type:
            headers["content-type"] = content_type
        headers.update(_make_trace_headers())
        return headers

    def _reference_auth_headers(self, referer: str, content_type: str = "application/json") -> dict[str, str]:
        parsed = urlparse(referer)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else auth_base
        headers = {
            "accept": "application/json",
            "content-type": content_type,
            "origin": origin,
            "referer": referer,
            "user-agent": self.fingerprint["user_agent"],
        }
        if "auth.openai.com" in parsed.netloc and self.device_id:
            headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _reference_navigation_headers(self, referer: str) -> dict[str, str]:
        return {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "referer": referer,
            "user-agent": self.fingerprint["user_agent"],
        }

    def _reference_start_authorization(self, index: int) -> None:
        def begin_signin() -> tuple[str, object | None, str]:
            csrf_path = "/api/auth/csrf"
            csrf_resp, csrf_error = self._chatgpt_request(
                "get",
                csrf_path,
                index,
                headers=self._reference_nextauth_headers(f"{chatgpt_base}/auth/login"),
            )
            csrf_data = _response_json(csrf_resp)
            csrf_token = str(csrf_data.get("csrfToken") or "").strip()
            if csrf_resp is None or csrf_resp.status_code != 200 or not csrf_token:
                raise RuntimeError(
                    csrf_error
                    or f"chatgpt_csrf_http_{getattr(csrf_resp, 'status_code', 'unknown')}, "
                    f"{_response_debug_detail(csrf_resp, 400)}"
                )

            signin_path = "/api/auth/signin/openai"
            signin_resp, signin_error = self._chatgpt_request(
                "post",
                signin_path,
                index,
                headers=self._reference_nextauth_headers(
                    f"{chatgpt_base}/auth/login",
                    content_type="application/x-www-form-urlencoded",
                ),
                data={
                    "csrfToken": csrf_token,
                    "callbackUrl": f"{chatgpt_base}/",
                    "json": "true",
                },
            )
            signin_data = _response_json(signin_resp)
            return str(signin_data.get("url") or "").strip(), signin_resp, signin_error

        auth_url, signin_resp, error = begin_signin()
        parsed = urlparse(auth_url)
        is_authorize_url = (
            parsed.scheme == "https"
            and parsed.netloc == "auth.openai.com"
            and "/api/accounts/authorize" in parsed.path
        )
        if not is_authorize_url and parsed.netloc == "chatgpt.com" and parsed.path == "/api/auth/signin":
            auth_url, signin_resp, error = begin_signin()
            parsed = urlparse(auth_url)
            is_authorize_url = (
                parsed.scheme == "https"
                and parsed.netloc == "auth.openai.com"
                and "/api/accounts/authorize" in parsed.path
            )
        if signin_resp is None or signin_resp.status_code != 200 or not is_authorize_url:
            raise RuntimeError(
                error
                or f"chatgpt_signin_http_{getattr(signin_resp, 'status_code', 'unknown')}, "
                f"url={_safe_url_for_log(auth_url)}, {_response_debug_detail(signin_resp, 400)}"
            )

        def authorize():
            headers = _headers_with_clearance(
                self._reference_navigation_headers(f"{chatgpt_base}/auth/login"),
                auth_url,
                self.proxy,
                self.clearance_user_agent,
            )
            return request_with_local_retry(
                self.session,
                "get",
                auth_url,
                headers=headers,
                allow_redirects=True,
                verify=False,
            )

        auth_resp, error = authorize()
        if _is_cloudflare_challenge(auth_resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(auth_resp, reason=self.clearance_failure_reason))
            auth_resp, error = authorize()
        if auth_resp is None or auth_resp.status_code != 200:
            raise RuntimeError(
                error
                or f"chatgpt_authorize_http_{getattr(auth_resp, 'status_code', 'unknown')}, "
                f"{_response_debug_detail(auth_resp, 400)}"
            )

        device_id = ""
        try:
            device_id = str(self.session.cookies.get("oai-did", "") or "").strip()
        except Exception:
            pass
        if not device_id:
            device_id = str((parse_qs(parsed.query).get("device_id") or [""])[0]).strip()
        if not device_id:
            device_id = str(uuid.uuid4())
            step(index, "授权响应未携带 oai-did，使用新的设备标识", "yellow")
        self.device_id = device_id
        for domain in (".auth.openai.com", "auth.openai.com"):
            try:
                self.session.cookies.set("oai-did", self.device_id, domain=domain)
            except Exception:
                continue

    def _reference_signup(self, email: str, index: int) -> str:
        """Advance the reference signup flow and return ``password`` or ``otp``.

        OpenAI can route a fresh signup directly to email verification.  That
        state belongs to the current authorize challenge; it must not be sent
        to ``user/register`` merely because the older password-page branch was
        not selected.
        """
        step(index, "提交 ChatGPT 注册邮箱（signup）")
        url = f"{auth_base}/api/accounts/authorize/continue"
        sentinel_token = build_sentinel_token(
            self.session,
            self.device_id,
            "authorize_continue",
            self.fingerprint,
        )
        self._reference_authorize_sentinel = sentinel_token
        headers = self._reference_auth_headers(f"{auth_base}/create-account")
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(
            self.session,
            "post",
            url,
            json={"username": {"value": email, "kind": "email"}, "screen_hint": "signup"},
            headers=headers,
            allow_redirects=False,
            verify=False,
        )
        if resp is None or resp.status_code != 200:
            raise RuntimeError(
                error
                or f"reference_authorize_continue_http_{getattr(resp, 'status_code', 'unknown')}, "
                f"{_response_debug_detail(resp, 400)}"
            )
        data = _response_json(resp)
        page = data.get("page") if isinstance(data.get("page"), dict) else {}
        page_type = str(page.get("type") or "").strip()
        payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
        continue_url = extract_continue_url(data)
        page_mode = {
            "create_account_password": "password",
            "email_otp_verification": "otp",
        }.get(page_type)
        continue_mode = {
            "/create-account/password": "password",
            "/email-verification": "otp",
        }.get(_url_path(continue_url))
        if page_mode and continue_mode and page_mode != continue_mode:
            raise RuntimeError(
                "ChatGPT 参考注册状态冲突: "
                f"page_type={page_type}, continue={_safe_url_for_log(continue_url)}"
            )
        signup_mode = page_mode or continue_mode
        if signup_mode == "password":
            self._reference_signup_mode = "password"
            self._reference_email_verification_mode = ""
            step(index, "已进入传统密码注册流程")
            return "password"
        if signup_mode == "otp":
            self._reference_signup_mode = "otp"
            self._reference_email_verification_mode = str(
                payload.get("email_verification_mode") or ""
            ).strip().lower()
            return "otp"
        raise RuntimeError(
            "ChatGPT 参考注册返回未知页面: "
            f"page_type={page_type or '?'}, continue={_safe_url_for_log(continue_url)}"
        )

    def _reference_register_password(self, email: str, password: str, index: int) -> None:
        step(index, "建立密码注册页面状态")
        page_url = f"{auth_base}/create-account/password"
        page_headers = _headers_with_clearance(
            self._reference_auth_headers(f"{auth_base}/create-account"),
            page_url,
            self.proxy,
            self.clearance_user_agent,
        )
        page_resp, error = request_with_local_retry(
            self.session,
            "get",
            page_url,
            headers=page_headers,
            allow_redirects=True,
            verify=False,
        )
        if page_resp is None or page_resp.status_code not in (200, 302):
            raise RuntimeError(
                error
                or f"reference_password_page_http_{getattr(page_resp, 'status_code', 'unknown')}, "
                f"{_response_debug_detail(page_resp, 400)}"
            )

        step(index, "提交 ChatGPT 注册密码")
        url = f"{auth_base}/api/accounts/user/register"
        sentinel_token = build_sentinel_token(
            self.session,
            self.device_id,
            "username_password_create",
            self.fingerprint,
        )
        self._reference_password_sentinel = sentinel_token
        headers = self._reference_auth_headers(page_url)
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(
            self.session,
            "post",
            url,
            json={"password": password, "username": email},
            headers=headers,
            verify=False,
        )
        if resp is None or resp.status_code != 200:
            raise RuntimeError(
                error
                or f"reference_user_register_http_{getattr(resp, 'status_code', 'unknown')}, "
                f"{_response_debug_detail(resp, 400)}"
            )
        step(index, "传统密码注册完成")

    def _reference_send_otp(self, index: int) -> None:
        step(index, "发送 ChatGPT 注册验证码")
        sentinel_token = str(getattr(self, "_reference_password_sentinel", "") or "").strip()
        if not sentinel_token:
            raise RuntimeError("ChatGPT 发码缺少密码注册阶段的 Sentinel 上下文")
        url = f"{auth_base}/api/accounts/email-otp/send"
        headers = self._reference_auth_headers(f"{auth_base}/create-account/password")
        headers["openai-sentinel-token"] = sentinel_token
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(
            self.session,
            "get",
            url,
            headers=headers,
            allow_redirects=True,
            verify=False,
        )
        if resp is None or resp.status_code != 200:
            raise RuntimeError(
                error
                or f"reference_send_otp_http_{getattr(resp, 'status_code', 'unknown')}, "
                f"{_response_debug_detail(resp, 400)}"
            )

    def _reference_resend_otp(self, index: int) -> None:
        """Resend an OTP without replacing the active authorize challenge."""
        step(index, "重发 ChatGPT 注册验证码")
        sentinel_token = str(getattr(self, "_reference_authorize_sentinel", "") or "").strip()
        if not sentinel_token:
            raise RuntimeError("ChatGPT OTP 分支缺少 authorize/continue 的 Sentinel 上下文")
        url = f"{auth_base}/api/accounts/email-otp/resend"
        headers = self._reference_auth_headers(f"{auth_base}/email-verification")
        headers["openai-sentinel-token"] = sentinel_token
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        def send():
            # Resend changes the active OTP.  Retrying after an uncertain
            # timeout can invalidate the code delivered by the first request.
            return request_with_local_retry(
                self.session,
                "post",
                url,
                retry_attempts=1,
                headers=headers,
                allow_redirects=False,
                verify=False,
            )

        resp, error = send()
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            resp, error = send()
        if resp is None or resp.status_code != 200:
            raise RuntimeError(
                error
                or f"reference_resend_otp_http_{getattr(resp, 'status_code', 'unknown')}, "
                f"{_response_debug_detail(resp, 400)}"
            )
        data = _response_json(resp)
        error_data = data.get("error")
        if isinstance(error_data, dict):
            error_message = str(error_data.get("message") or error_data.get("code") or "").strip()
        else:
            error_message = str(error_data or "").strip()
        if not error_message and data.get("success") is False:
            error_message = str(data.get("message") or "unknown error").strip()
        if error_message:
            raise RuntimeError(f"ChatGPT 验证码重发请求被拒绝: {error_message}")
        step(index, "ChatGPT 验证码重发请求已接受")

    def _reference_validate_otp(self, code: str, index: int) -> None:
        resp, error = validate_otp(self.session, self.device_id, code, self.fingerprint)
        if resp is None or resp.status_code != 200:
            body = str(getattr(resp, "text", "") or "")[:500] if resp is not None else ""
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}")
        step(index, "ChatGPT 注册验证码校验完成")

    def _reference_create_account(self, name: str, birthdate: str, index: int) -> str:
        step(index, "创建 ChatGPT 账号资料")
        url = f"{auth_base}/api/accounts/create_account"
        sentinel_token = build_sentinel_token(
            self.session,
            self.device_id,
            "create_account",
            self.fingerprint,
        )
        headers = self._reference_auth_headers(f"{auth_base}/about-you")
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(
            self.session,
            "post",
            url,
            json={"name": name, "birthdate": birthdate},
            headers=headers,
            allow_redirects=False,
            verify=False,
        )
        if resp is None or resp.status_code != 200:
            raise RuntimeError(
                error
                or f"reference_create_account_http_{getattr(resp, 'status_code', 'unknown')}, "
                f"{_response_debug_detail(resp, 400)}"
            )
        data = _response_json(resp)
        continue_url = (
            extract_continue_url(data)
            or str(getattr(resp, "headers", {}).get("Location") or "").strip()
            or str(getattr(resp, "url", "") or "").strip()
        )
        if not continue_url:
            raise RuntimeError("ChatGPT create_account 未返回 continue_url")
        return _absolute_auth_url(continue_url)

    def _reference_capture_callback(self, continue_url: str, index: int) -> str:
        step(index, "跟踪 ChatGPT 注册回调")
        current_url = _absolute_auth_url(continue_url)
        for _ in range(12):
            parsed = urlparse(current_url)
            if parsed.scheme == "https" and parsed.netloc == "chatgpt.com" and parsed.path == self._CHATGPT_CALLBACK_PATH:
                return current_url
            headers = _headers_with_clearance(
                self._reference_navigation_headers(f"{auth_base}/about-you"),
                current_url,
                self.proxy,
                self.clearance_user_agent,
            )
            resp, error = request_with_local_retry(
                self.session,
                "get",
                current_url,
                headers=headers,
                allow_redirects=False,
                verify=False,
            )
            if resp is None:
                raise RuntimeError(error or "ChatGPT 回调重定向请求失败")
            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            location = str(getattr(resp, "headers", {}).get("Location") or "").strip()
            if not location:
                break
            current_url = urljoin(current_url, location)
        raise RuntimeError(f"ChatGPT 注册未捕获有效 callback: {_safe_url_for_log(current_url)}")

    def register(self, index: int) -> dict:
        step(index, "开始创建邮箱")
        mailbox = self._create_registration_mailbox()
        email = str(mailbox.get("address") or "").strip()
        if not email:
            mail_provider.release_mailbox(mailbox)
            raise RuntimeError("邮箱服务未返回 address")
        label = str(mailbox.get("label") or "")
        step(index, f"邮箱创建完成[{label}]: {email}")
        try:
            first_name, last_name = _random_name()
            password = self._reference_password(email)
            mailbox["_icloud_keyword"] = "ChatGPT"
            self._reference_start_authorization(index)
            signup_mode = self._reference_signup(email, index)
            if signup_mode == "password":
                self._reference_register_password(email, password, index)
                mailbox["_received_after"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
                self._reference_send_otp(index)
                step(index, "开始等待 ChatGPT 注册验证码")
                code = _wait_for_chatgpt_registration_code(
                    mailbox,
                    index,
                    self.proxy,
                    resend=lambda: self._reference_resend_otp(index),
                )
                step(index, f"收到 ChatGPT 注册验证码: {code}")
                self._reference_validate_otp(code, index)
                continue_url = self._reference_create_account(
                    f"{first_name} {last_name}",
                    _random_birthdate(),
                    index,
                )
                callback_url = self._reference_capture_callback(continue_url, index)
            else:
                verification_mode = str(
                    getattr(self, "_reference_email_verification_mode", "") or ""
                ).strip().lower()
                if verification_mode == "passwordless_login":
                    raise OpenAIEmailAlreadyRegistered(email)
                if verification_mode != "passwordless_signup":
                    raise RuntimeError(
                        "ChatGPT OTP 注册分支缺少可确认的新账号模式: "
                        f"email_verification_mode={verification_mode or 'unknown'}"
                    )
                # The endpoint already created this challenge.  A fresh
                # email-otp/send here would invalidate the email that is in
                # flight, so only resend against the same challenge.
                self._reference_resend_otp(index)
                # Prefer the response to this resend over an OTP emitted by
                # authorize/continue before the mailbox polling window.
                mailbox["_received_after"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
                step(index, "开始等待 ChatGPT 注册验证码")
                code = _wait_for_chatgpt_registration_code(
                    mailbox,
                    index,
                    self.proxy,
                    resend=lambda: self._reference_resend_otp(index),
                )
                step(index, f"收到 ChatGPT 注册验证码: {code}")
                self._reference_validate_otp(code, index)
                continue_url = self._reference_create_account(
                    f"{first_name} {last_name}",
                    _random_birthdate(),
                    index,
                )
                callback_url = self._reference_capture_callback(continue_url, index)
                password = ""
            session_data = self._complete_chatgpt_callback(callback_url, index)
            result = self._chatgpt_session_result(email, session_data)
            if password:
                result["password"] = password
            if not str(result.get("access_token") or "").strip():
                raise RuntimeError("ChatGPT 注册完成但未获得对话 access token")
        except OpenAIEmailAlreadyRegistered:
            # OpenAI has positively identified this address as an existing
            # account.  Keep it out of future GPT signup claims even though
            # this worker did not create a new account with it.
            mail_provider.mark_mailbox_result(mailbox, success=True)
            raise
        except Exception as error:
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
            raise
        mail_provider.mark_mailbox_result(mailbox, success=True)
        return result


def _safe_sub2api_sync_error(error: Exception, account: dict) -> str:
    """Return one short registration-log message without OAuth credentials."""
    message = " ".join(str(error or "Sub2API 同步失败").replace("\n", " ").split())[:300]
    for key in ("access_token", "refresh_token", "id_token", "cookie_header", "password"):
        value = str(account.get(key) or "").strip()
        if value:
            message = message.replace(value, "***")
    return message or "Sub2API 同步失败"


def _sub2api_sync_account_payload(registrar: Any, account: dict, index: int) -> dict:
    """Build a durable OAuth payload while retaining the local web account."""
    if str(account.get("refresh_token") or "").strip():
        return dict(account)
    extractor = getattr(registrar, "extract_platform_oauth_credentials", None)
    if not callable(extractor):
        raise RuntimeError("当前注册会话无法提取 Sub2API 所需的 OAuth 凭据")
    step(index, "正在提取 Sub2API OAuth 凭据")
    tokens = extractor(str(account.get("email") or "").strip(), index)
    payload = dict(account)
    for key in ("access_token", "refresh_token", "id_token", "expires_at"):
        value = str(tokens.get(key) or "").strip() if isinstance(tokens, dict) else ""
        if value:
            payload[key] = value
    if not str(payload.get("refresh_token") or "").strip():
        raise RuntimeError("未能提取 Sub2API 所需的 OAuth refresh_token")
    return payload


def _sync_registered_account_to_sub2api(index: int, account: dict, registrar: Any) -> None:
    """Best-effort post-registration sync.  It must never fail the GPT task."""
    sync_settings = normalize_sync_config(config.get("sub2api_sync"))
    if not sync_settings["enabled"]:
        return

    access_token = str(account.get("access_token") or "").strip()
    server_id = str(sync_settings.get("server_id") or "").strip()
    sync_time = datetime.now(timezone.utc).isoformat()
    sync_account = dict(account)
    try:
        if not server_id:
            raise RuntimeError("未选择 Sub2API 连接")
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise RuntimeError("选定的 Sub2API 连接不存在或已删除")

        step(index, "正在同步账号到 Sub2API")
        sync_account = _sub2api_sync_account_payload(registrar, account, index)
        sync_result = sync_openai_account(server, sync_account, sync_settings)
        group_name = str(sync_result.get("group_name") or sync_result.get("group_id") or "默认分组").strip()
        server_name = str(sync_result.get("server_name") or server.get("name") or server_id).strip()
        account_service.update_account(
            access_token,
            {
                "sub2api_sync_status": "success",
                "sub2api_sync_server_id": server_id,
                "sub2api_sync_server_name": server_name,
                "sub2api_sync_group_id": str(sync_result.get("group_id") or "").strip(),
                "sub2api_sync_group_name": group_name,
                "sub2api_sync_account_id": str(sync_result.get("account_id") or "").strip(),
                "sub2api_sync_at": sync_time,
                "sub2api_sync_error": None,
            },
            quiet=True,
        )
        account["sub2api_sync_status"] = "success"
        step(index, f"已同步到 Sub2API：{server_name} / {group_name}", "green")
    except Exception as error:
        safe_error = _safe_sub2api_sync_error(error, sync_account)
        account_service.update_account(
            access_token,
            {
                "sub2api_sync_status": "failed",
                "sub2api_sync_server_id": server_id,
                "sub2api_sync_group_id": str(sync_settings.get("group_id") or "").strip(),
                "sub2api_sync_group_name": str(sync_settings.get("group_name") or "").strip(),
                "sub2api_sync_at": sync_time,
                "sub2api_sync_error": safe_error,
            },
            quiet=True,
        )
        account["sub2api_sync_status"] = "failed"
        step(index, f"Sub2API 同步未成功，本地账号已保留：{safe_error}", "yellow")


def _enabled_mail_provider_count() -> int:
    mail = config.get("mail") if isinstance(config.get("mail"), dict) else {}
    providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
    return sum(
        1
        for item in providers
        if isinstance(item, dict) and _truthy(item.get("enable"), False)
    )


def _register_with_fresh_email(index: int) -> tuple[PlatformRegistrar, dict]:
    skipped = 0
    delivery_failures = 0
    excluded_provider_refs: set[str] = set()
    provider_count = max(1, _enabled_mail_provider_count())
    while True:
        registrar = PlatformRegistrar(config["proxy"])
        registrar.excluded_mail_provider_refs = set(excluded_provider_refs)
        try:
            return registrar, registrar.register(index)
        except OpenAIMailboxDeliveryTimeout as error:
            registrar.close()
            delivery_failures += 1
            if error.provider_ref:
                excluded_provider_refs.add(error.provider_ref)
            remaining = provider_count - max(delivery_failures, len(excluded_provider_refs))
            if remaining <= 0:
                raise RuntimeError(
                    f"所有启用邮箱来源均未收到 ChatGPT 验证码，最后失败来源：{error.label}"
                ) from error
            step(
                index,
                f"{error.label} 未收到验证码，正在切换下一个邮箱来源（剩余 {remaining} 个）",
                "yellow",
            )
        except OpenAIEmailAlreadyRegistered as error:
            registrar.close()
            skipped += 1
            step(
                index,
                f"{error.email} 已存在 GPT 账号，已标记 GPT；自动更换邮箱（已跳过 {skipped} 个）",
                "yellow",
            )
            if skipped >= OPENAI_EXISTING_EMAIL_RETRY_LIMIT:
                raise RuntimeError(
                    f"连续 {skipped} 个邮箱已存在 GPT 账号，本任务停止；请检查邮箱池标签"
                ) from error
        except Exception:
            registrar.close()
            raise


def worker(index: int) -> dict:
    start = time.time()
    registrar: PlatformRegistrar | None = None
    try:
        step(index, "任务启动")
        registrar, result = _register_with_fresh_email(index)
        cost = time.time() - start
        access_token = str(result["access_token"])
        account_service.add_account_items([result])
        _sync_registered_account_to_sub2api(index, result, registrar)
        checkout_settings = _checkout_config()
        if checkout_settings["enabled"]:
            checkout_channel = checkout_settings["channel"]
            channel_label = checkout_channel.upper()
            continuous_proxy_retry = bool(checkout_settings["continuous_retry"])
            # Different newly registered accounts should not all begin on the
            # first proxy line.  The final-link service maps this seed into
            # each country-specific pool and advances it after every failure.
            proxy_rotation = random.randrange(0, 2**31) if continuous_proxy_retry else None
            checkout_task_id = f"checkout-{index}-{uuid.uuid4().hex}"
            checkout_email = str(result.get("email") or "").strip()
            # Account persistence deliberately precedes task creation.  This
            # makes a failed payment-link extraction retryable from account
            # management without losing the freshly registered account.
            checkout_task_update(
                index,
                checkout_task_id,
                email=checkout_email,
                status="queued" if checkout_channel == "pix" and continuous_proxy_retry else "running",
                stage="queued" if checkout_channel == "pix" and continuous_proxy_retry else "checkout",
                payment_link="",
                error_short="",
                channel=checkout_channel,
            )
            queue_pix_directly = checkout_channel == "pix" and continuous_proxy_retry
            if queue_pix_directly:
                queued = _queue_checkout_retry(
                    index=index,
                    task_id=checkout_task_id,
                    email=checkout_email,
                    access_token=access_token,
                    checkout=checkout_settings,
                    next_proxy_rotation=proxy_rotation or 1,
                    attempt=0,
                )
                if not queued:
                    raise RuntimeError("Pix 独立提链队列未就绪")
                result["checkout_link_status"] = "pending"
                account_service.update_account(
                    access_token,
                    {"checkout_link_status": "pending"},
                    quiet=True,
                )
                checkout_task_update(
                    index,
                    checkout_task_id,
                    status="queued",
                    stage="queued",
                    error_short="等待 Pix 独立提链线程",
                )
                checkout_step(index, "账号已保存，已追加到 Pix 独立提链队列", "yellow")
            else:
                checkout_step(index, f"账号已保存，开始 {channel_label} 最终支付链接提取")

                def checkout_progress(message: str) -> None:
                    checkout_step(index, message)
                    checkout_task_update(
                        index,
                        checkout_task_id,
                        stage=_checkout_stage_code(message),
                        progress_detail=_checkout_progress_detail(message),
                    )

                try:
                    if checkout_settings["checkout_proxy_enabled"] and not checkout_settings["checkout_proxy_url"]:
                        raise CheckoutSessionError("已启用 Checkout 代理，但未填写代理 URL", status_code=400)
                    if (
                        (
                            checkout_channel == "upi"
                            or (
                                checkout_channel == "pix"
                                and checkout_settings["pix_protocol"] == "standalone"
                            )
                        )
                        and checkout_settings["promotion_proxy_enabled"]
                        and not checkout_settings["promotion_proxy_url"]
                    ):
                        raise CheckoutSessionError("已启用 Promotion 代理，但未填写代理 URL", status_code=400)
                    checkout = openai_checkout_service.extract_and_store_checkout_link(
                        access_token,
                        checkout_channel=checkout_channel,
                        **(
                            {"pix_protocol": checkout_settings["pix_protocol"]}
                            if checkout_channel == "pix"
                            else {}
                        ),
                        checkout_proxy=checkout_settings["checkout_proxy_url"],
                        promotion_proxy=checkout_settings["promotion_proxy_url"],
                        provider_proxy=checkout_settings["checkout_proxy_url"],
                        **(
                            {"proxy_rotation": proxy_rotation}
                            if proxy_rotation is not None
                            else {}
                        ),
                        progress=checkout_progress,
                    )
                    result["checkout_link_status"] = "ready"
                    payment_link = str(checkout.get("checkout_final_url") or checkout.get("checkout_url") or "").strip()
                    result["checkout_url"] = payment_link
                    if checkout.get("checkout_final_url"):
                        result["checkout_final_url"] = checkout.get("checkout_final_url")
                    checkout_task_update(
                        index,
                        checkout_task_id,
                        status="success",
                        stage="completed",
                        payment_link=payment_link,
                        error_short="",
                    )
                    checkout_step(index, f"{channel_label} 最终支付链接已保存", "green")
                except CheckoutSessionError as exc:
                    should_retry_continuously = (
                        continuous_proxy_retry
                        and _queue_checkout_retry(
                            index=index,
                            task_id=checkout_task_id,
                            email=checkout_email,
                            access_token=access_token,
                            checkout=checkout_settings,
                            next_proxy_rotation=(proxy_rotation or 0) + 1,
                        )
                    )
                    if should_retry_continuously:
                        result["checkout_link_status"] = "pending"
                        result["checkout_last_error"] = str(exc)
                        account_service.update_account(
                            access_token,
                            {
                                "checkout_link_status": "pending",
                                "checkout_last_error": str(exc)[:500],
                            },
                            quiet=True,
                        )
                        checkout_task_update(
                            index,
                            checkout_task_id,
                            status="retrying",
                            stage="checkout",
                            error_short="正在轮换代理持续重试",
                        )
                        checkout_step(index, "账号已保存，Checkout 提链未成功，已进入持续换代理重试", "yellow")
                    else:
                        result["checkout_link_status"] = "failed"
                        result["checkout_last_error"] = str(exc)
                        checkout_task_update(
                            index,
                            checkout_task_id,
                            status="failed",
                            stage="failed",
                            error_short=_checkout_failure_short(exc, checkout_channel),
                        )
                        checkout_step(index, f"账号已保存，Checkout 提链暂未成功，可稍后重试: {exc}", "yellow")
                except Exception as exc:
                    checkout_task_update(
                        index,
                        checkout_task_id,
                        status="failed",
                        stage="failed",
                        error_short=_checkout_failure_short(exc, checkout_channel),
                    )
                    raise
        refresh_result = account_service.refresh_accounts([access_token])
        if refresh_result.get("errors"):
            step(index, f"账号已保存，刷新状态暂未成功，稍后可重试: {refresh_result['errors']}", "yellow")
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            avg = (time.time() - stats["start_time"]) / stats["success"]
        log(f'{result["email"]} 注册成功，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
        return {"ok": True, "index": index, "result": result}
    except Exception as e:
        cost = time.time() - start
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        log(f"任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        return {"ok": False, "index": index, "error": str(e)}
    finally:
        if registrar is not None:
            registrar.close()
