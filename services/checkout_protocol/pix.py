"""
\tPIX 最终支付 URL 提取脚本。

说明：
- 按真实 Stripe Elements 网页固定为 BR/BRL，一条 BR sticky 出口贯穿全链路。
- Pix 资料直接内联到 confirm，同时发送当前 Stripe JS 生成的 token 字段。
- 成功时只返回 Stripe Hosted Pix instructions URL 及可选 QR 元数据。

用法：
  1) 环境变量方式：
     PIX_TOKEN="..." python pix_extract.py

  2) 文件方式：
     把 token 放到当前目录 token.txt
     python pix_extract.py

常用环境变量：
  PIX_MAX_RETRY=5
  PIX_PROVIDER_PER_CHECKOUT=1
  PIX_MAX_APPROVE_BLOCKED=5
  PIX_WORKERS=1
  PIX_WORKERS_MAX=1
  PIX_APPROVE_RETRY_MAX=5  # approve 同一 sticky 出口串行重试上限
  PIX_APPROVE_REQUEST_TIMEOUT=30 # approve 单次请求超时
  PIX_FOLLOW_REDIRECT=1
  PIX_DUMP_LIMIT=6000       # 抓包响应保存长度
  PIX_PROXY_SKIP_FAILED=1   # 普通流程失败下次软跳过
  PIX_PROXY_REMOVE_FAILED=1 # 明确的代理失败会从 proxy_seeds.txt 移除
  PIX_PROXY_DEFAULT_SCHEME=http # 裸代理默认协议；Mars SOCKS5 可设 socks5h
  PIX_PROXY_FAIL_COOLDOWN=180 # 失败代理冷却秒数，0 表示按旧逻辑一直跳过
  PIX_PROXY_REMOVE_AFTER_FAILS=3 # 已复用代理健康类失败累计 3 次移除；普通代理失败 1 次移除
  PIX_ZERO_CACHE=1          # 兼容旧变量名：记录 checkout 的目标金额观察结果
  PIX_ZERO_CACHE_SCHEDULING=0 # 显式设为 1 才按目标金额观察结果筛选/优先调度
"""

from __future__ import annotations

import json
import hashlib
import os
import random
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from threading import Event, RLock, local
from typing import Any, Mapping
from urllib.parse import quote, urlencode, unquote, urljoin, urlparse, urlsplit, urlunsplit

import requests

from services.proxy_format import colon_proxy_to_url

from . import stripe_token
from . import pix_scheme3
from utils.sentinel import build_sentinel_token

try:
    from curl_cffi import CurlOpt
    from curl_cffi.requests import Session as CurlCffiSession
except ImportError:
    CurlOpt = None
    CurlCffiSession = None


SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"
DUMP_DIR = SCRIPT_DIR / "dumps"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DUMP_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_TIMEOUT = 30
CHATGPT_TIMEOUT = 45
PIX_UNAVAILABLE_ERROR = "当前账号支付方式不支持 PIX"
STRIPE_VERSION_FULL = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
DEFAULT_STRIPE_RUNTIME_VERSION = "299e1ea907"
REFERENCE_STRIPE_RUNTIME_VERSION = "6f8494a281"
REFERENCE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
CHATGPT_CLIENT_VERSION = "prod-5770480481b504de382d29aeef289957df290dd5"
CHATGPT_CLIENT_BUILD_NUMBER = "8230030"
DEFAULT_STRIPE_PK = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

PIX_TAX_NAME = "Joao Silva"
PIX_TAX_LINE1 = "Avenida Paulista 1000"
PIX_TAX_CITY = "Sao Paulo"
PIX_TAX_STATE = "SP"
PIX_TAX_POSTAL_CODE = "01310-100"
PIX_MAX_AMOUNT_MINOR = 50


@dataclass(frozen=True)
class PixFlowResult:
    redirect_url: str
    qr_urls: list[str]
    amount_minor: int
    intent_kind: str
    intent_status: str = ""
    qr_png: str = ""
    qr_svg: str = ""
    expires_at: int | None = None


PIX_BOOTSTRAP_COUNTRY = "BR"
PIX_PROMOTION_COUNTRIES = ["BR"]
PIX_PROMOTION_COUNTRY = PIX_PROMOTION_COUNTRIES[0]
PIX_PROVIDER_COUNTRY = "BR"

COUNTRY_CURRENCY = {
    "BR": "BRL",
    "NL": "EUR",
    "BE": "EUR",
    "DE": "EUR",
    "FR": "EUR",
    "US": "USD",
    "IN": "INR",
    "JP": "JPY",
    "VN": "VND",
}

DEFAULT_PIX_BILLING = {
    "email": "redacted@example.invalid",
    "name": "Lucas Silva",
    "country": "BR",
    "line1": "Avenida Paulista, 1578",
    "line2": "Bela Vista",
    "city": "Sao Paulo",
    "postal_code": "01310-200",
    "state": "SP",
}

BR_BILLING_NAMES = [
    ("Gabriel", "Almeida"),
    ("Lucas", "Oliveira"),
    ("Mariana", "Santos"),
    ("Beatriz", "Costa"),
]

BR_BILLING_ADDRESSES = [
    ("Avenida Paulista 1000", "", "Sao Paulo", "01310-100", "SP"),
    ("Rua das Flores 120", "", "Curitiba", "80020-090", "PR"),
    ("Avenida Atlantica 500", "", "Rio de Janeiro", "22010-000", "RJ"),
    ("Rua da Bahia 900", "", "Belo Horizonte", "30160-011", "MG"),
]

_log_file = LOG_DIR / f"pix_{time.strftime('%Y%m%d-%H%M%S')}.log"
_dump_counter = 0
_proxy_state: dict[str, Any] | None = None
_proxy_state_lock = RLock()
_log_lock = RLock()
_dump_lock = RLock()
_proxy_file_lock = RLock()
_proxy_redaction_lock = RLock()
_proxy_redaction_values: set[str] = set()
_log_context = local()


def redact_log_text(text: str) -> str:
    text = str(text or "")
    with _proxy_redaction_lock:
        values = sorted(_proxy_redaction_values, key=len, reverse=True)
    for value in values:
        if value:
            try:
                label = proxy_label(value)
            except (TypeError, ValueError):
                label = f"proxy#{hashlib.sha256(value.encode()).hexdigest()[:10]}"
            if label == "direct":
                label = f"proxy#{hashlib.sha256(value.encode()).hexdigest()[:10]}"
            text = text.replace(value, label)
    return text


def log(message: str, prefix: str = "") -> None:
    context = getattr(_log_context, "prefix", "")
    line = redact_log_text(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {prefix}{context}{message}")
    with _log_lock:
        print(line, flush=True)
        with open(_log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def emit_progress(progress: Any | None, stage: str, message: object) -> None:
    """Emit a safe tagged progress update without making UI failures fatal."""
    if progress is None:
        return
    detail = redact_log_text(str(message or "").strip())
    if not detail:
        return
    try:
        progress(f"[{stage}] {detail}")
    except Exception:
        pass


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, int(raw))
    except ValueError:
        return max(minimum, default)


def pix_confirm_mode() -> str:
    """Select the Pix PaymentMethod submission protocol."""
    configured = os.environ.get("PIX_CONFIRM_MODE", "").strip().lower()
    if not configured and "PIX_CONFIRM_INLINE_PM" in os.environ:
        configured = "inline" if env_bool("PIX_CONFIRM_INLINE_PM", True) else "explicit"
    mode = configured or "inline"
    if mode not in {"inline", "explicit", "auto"}:
        raise RuntimeError("PIX_CONFIRM_MODE 只允许 inline, explicit, auto")
    return mode


def pix_protocol_mode(value: object = "") -> str:
    """Normalize the per-task Pix protocol selection without global state."""
    mode = str(value or "enhanced").strip().lower()
    if mode not in {"enhanced", "reference", "standalone"}:
        raise RuntimeError("Pix 提链方案仅允许 enhanced、reference 或 standalone")
    return mode


def pix_target_amount_minor() -> int:
    """Compatibility accessor for the maximum accepted Pix amount."""
    return PIX_MAX_AMOUNT_MINOR


def pix_amount_is_eligible(value: int) -> bool:
    """Match the successful Rust flow: accept any amount from 0 through 50 centavos."""
    return 0 <= int(value) <= PIX_MAX_AMOUNT_MINOR


def is_checkout_not_active_error(value: Any) -> bool:
    return "checkout_not_active_session" in str(value)


def is_user_already_paid_error(value: Any) -> bool:
    return "user is already paid" in str(value or "").lower()


def is_pix_unavailable_error(value: Any) -> bool:
    text = str(value or "")
    return PIX_UNAVAILABLE_ERROR in text or "当前 checkout 不支持 PIX" in text


def random_user_agent() -> str:
    return DEFAULT_USER_AGENT


def stripe_browser_id() -> str:
    return f"{uuid.uuid4()}{uuid.uuid4().hex[:8]}"


def normalize_country(country: str) -> str:
    value = str(country or "").strip().upper()
    return value if value in COUNTRY_CURRENCY else "BR"


def currency_for_country(country: str) -> str:
    return COUNTRY_CURRENCY.get(normalize_country(country), "BRL")


def payment_browser_locale() -> str:
    return os.environ.get("PIX_BROWSER_LOCALE", "pt-BR").strip() or "pt-BR"


def payment_elements_locale() -> str:
    return os.environ.get("PIX_ELEMENTS_LOCALE", "pt-BR").strip() or "pt-BR"


def payment_browser_timezone() -> str:
    return os.environ.get("PIX_BROWSER_TIMEZONE", "America/Sao_Paulo").strip() or "America/Sao_Paulo"


def payment_accept_language() -> str:
    locale = payment_browser_locale()
    if locale.lower().startswith("en"):
        return "en-US,en;q=0.9"
    return f"{locale},{locale.split('-', 1)[0]};q=0.9,en;q=0.8"


def normalize_proxy_url(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = colon_proxy_to_url(proxy)
        if "://" not in proxy:
            proxy = f"{default_proxy_scheme()}://{proxy}"
        elif default_proxy_scheme() != "http" and proxy.lower().startswith("http://"):
            proxy = f"{default_proxy_scheme()}://{proxy[len('http://') :]}"

    parsed = urlsplit(proxy)
    if parsed.username is None and parsed.password is None:
        return proxy

    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    username = quote(unquote(parsed.username or ""), safe="-._~")
    auth = username
    if parsed.password is not None:
        auth = f"{auth}:{quote(unquote(parsed.password), safe='-._~')}"
    return urlunsplit((parsed.scheme, f"{auth}@{host}", parsed.path, parsed.query, parsed.fragment))


def register_proxy_for_redaction(proxy: str) -> None:
    raw = str(proxy or "").strip()
    if not raw:
        return
    normalized = normalize_proxy_url(raw)
    values = {raw}
    if normalized:
        values.add(normalized)
        decoded = unquote(normalized)
        values.add(decoded)
        parsed = urlsplit(decoded)
        if parsed.netloc:
            values.add(parsed.netloc)
        if parsed.hostname:
            host = parsed.hostname
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            try:
                port = parsed.port
            except ValueError:
                port = None
            values.add(f"{host}:{port}" if port else host)
    with _proxy_redaction_lock:
        _proxy_redaction_values.update(values)


def default_proxy_scheme() -> str:
    raw = os.environ.get("PIX_PROXY_DEFAULT_SCHEME", "http").strip().lower()
    raw = raw[:-3] if raw.endswith("://") else raw
    if raw in ("socks5", "socks5h"):
        return "socks5h"
    if raw in ("http", "https"):
        return raw
    return "http"


def proxy_short(proxy: str) -> str:
    proxy = normalize_proxy_url(proxy)
    if not proxy:
        return "direct"
    digest = hashlib.sha256(proxy.encode()).hexdigest()[:10]
    return f"proxy#{digest}"


def proxy_label(proxy: str) -> str:
    return proxy_short(proxy)


def proxy_key(proxy: str) -> str:
    proxy = normalize_proxy_url(proxy)
    return hashlib.sha256(proxy.encode()).hexdigest() if proxy else ""


_PROXY_COUNTRY_SELECTOR_RE = re.compile(
    r"(?i)(?P<name>country|region)(?P<separator>[-_=])(?P<value>[a-z]{2}(?:,[a-z]{2})*)"
)
_STICKY_SESSION_RE = re.compile(
    r"(?i)(?P<prefix>(?:^|[-_])(?:session|sid)[-_])(?P<value>[A-Za-z0-9]+)(?=(?:[-_]|$))"
)


def proxy_chain_key(proxy: str) -> str:
    """Return a redacted identity that stays stable across country rewrites."""
    proxy = unquote(normalize_proxy_url(proxy))
    normalized = _PROXY_COUNTRY_SELECTOR_RE.sub(
        lambda match: f"{match.group('name')}{match.group('separator')}*",
        proxy,
    )
    return hashlib.sha256(normalized.encode()).hexdigest()[:10] if normalized else ""


def proxy_for_country(proxy: str, country: str) -> str:
    """Rewrite only a proxy auth country selector while retaining its sticky session."""
    proxy = normalize_proxy_url(proxy)
    target_country = normalize_country(country).lower()
    if not proxy:
        raise RuntimeError("代理为空，无法派生地区链路")

    parsed = urlsplit(proxy)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    replacements = 0

    def replace_country(match: re.Match[str]) -> str:
        nonlocal replacements
        replacements += 1
        current = match.group("value")
        value = target_country.upper() if current.isupper() else target_country
        return f"{match.group('name')}{match.group('separator')}{value}"

    username = _PROXY_COUNTRY_SELECTOR_RE.sub(replace_country, username)
    password = _PROXY_COUNTRY_SELECTOR_RE.sub(replace_country, password)
    if not replacements:
        raise RuntimeError(
            f"代理未包含可改写的 country/region 选择器: {proxy_label(proxy)}"
        )

    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    auth = quote(username, safe="-._~")
    if parsed.password is not None:
        auth = f"{auth}:{quote(password, safe='-._~')}"
    derived = urlunsplit((parsed.scheme, f"{auth}@{host}", parsed.path, parsed.query, parsed.fragment))
    register_proxy_for_redaction(derived)
    return derived


def proxy_with_fresh_sid(proxy: str) -> str:
    """Refresh a known sticky-session marker while preserving proxy region and credentials."""
    proxy = normalize_proxy_url(proxy)
    parsed = urlsplit(proxy)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    fresh_sid = str(random.randint(10_000_000, 99_999_999))
    username_next = _STICKY_SESSION_RE.sub(
        lambda match: f"{match.group('prefix')}{fresh_sid}", username, count=1
    )
    password_next = _STICKY_SESSION_RE.sub(
        lambda match: f"{match.group('prefix')}{fresh_sid}", password, count=1
    )
    if username_next == username and password_next == password:
        return proxy
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    auth = quote(username_next, safe="-._~")
    if parsed.password is not None:
        auth = f"{auth}:{quote(password_next, safe='-._~')}"
    refreshed = urlunsplit((parsed.scheme, f"{auth}@{host}", parsed.path, parsed.query, parsed.fragment))
    register_proxy_for_redaction(refreshed)
    return refreshed


def pix_proxy_chain(proxy_seed: str) -> tuple[str, str, str]:
    """Keep one sticky seed across configured checkout, promotion, and provider stages."""
    checkout_proxy = proxy_for_country(proxy_seed, PIX_BOOTSTRAP_COUNTRY)
    promotion_proxies = [
        proxy_for_country(proxy_seed, country) for country in PIX_PROMOTION_COUNTRIES
    ]
    provider_proxy = proxy_for_country(proxy_seed, PIX_PROVIDER_COUNTRY)
    chain_key = proxy_chain_key(proxy_seed)
    if not chain_key or any(
        proxy_chain_key(proxy) != chain_key
        for proxy in (checkout_proxy, *promotion_proxies, provider_proxy)
    ):
        raise RuntimeError("代理地区改写改变了 sticky seed，已拒绝混用代理链")
    return checkout_proxy, promotion_proxies[0], provider_proxy


def log_pix_proxy_chain(proxy_seed: str, checkout_proxy: str, promotion_proxy: str, provider_proxy: str) -> None:
    promotion_steps = " -> ".join(
        f"{country} promotion={proxy_label(proxy_for_country(promotion_proxy, country))}"
        for country in PIX_PROMOTION_COUNTRIES
    )
    log(
        "派生代理链: "
        f"chain={proxy_chain_key(proxy_seed)}; seed={proxy_label(proxy_seed)}; "
        f"{PIX_BOOTSTRAP_COUNTRY} checkout={proxy_label(checkout_proxy)}; "
        f"{promotion_steps}; "
        f"{PIX_PROVIDER_COUNTRY} provider/approve={proxy_label(provider_proxy)}"
    )


def normalize_pre_proxy_url(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = f"socks5h://{proxy}"
    return normalize_proxy_url(proxy)


def proxy_state_path() -> Path:
    raw = os.environ.get("PIX_PROXY_STATE_FILE", "").strip()
    return Path(raw) if raw else SCRIPT_DIR / "proxy_state.json"


def load_proxy_state() -> dict[str, Any]:
    global _proxy_state
    with _proxy_state_lock:
        if _proxy_state is not None:
            return _proxy_state
        path = proxy_state_path()
        if not path.exists():
            _proxy_state = {"seed": {}, "checkout": {}, "promotion": {}, "provider": {}, "pair": {}}
            return _proxy_state
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("seed", {})
        data.setdefault("checkout", {})
        data.setdefault("promotion", {})
        data.setdefault("provider", {})
        data.setdefault("pair", {})
        _proxy_state = data
        return _proxy_state


def save_proxy_state() -> None:
    with _proxy_state_lock:
        if _proxy_state is None:
            return
        path = proxy_state_path()
        path.write_text(json.dumps(_proxy_state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def proxy_state_key(group: str, proxy: str) -> str:
    if group == "seed":
        return proxy_chain_key(proxy)
    return proxy_key(proxy)


def prune_proxy_seed_state(proxy_seeds: list[str]) -> None:
    with _proxy_state_lock:
        state = load_proxy_state()
        seed_state = state.setdefault("seed", {})
        active_keys = {proxy_chain_key(proxy) for proxy in proxy_seeds if proxy_chain_key(proxy)}
        stale_keys = [key for key in seed_state if key not in active_keys]
        for key in stale_keys:
            del seed_state[key]
        if stale_keys:
            save_proxy_state()
    if stale_keys:
        log(f"Seed 代理状态清理完成: {len(stale_keys)}")


def prune_proxy_state(checkout_proxies: list[str], promotion_proxies: list[str], provider_proxies: list[str]) -> None:
    removed_counts: dict[str, int] = {}
    with _proxy_state_lock:
        state = load_proxy_state()
        active_keys_by_group: dict[str, set[str]] = {}
        for group, proxies in (
            ("checkout", checkout_proxies),
            ("promotion", promotion_proxies),
            ("provider", provider_proxies),
        ):
            group_state = state.get(group)
            if not isinstance(group_state, dict):
                continue
            active_keys = {proxy_key(proxy) for proxy in proxies if proxy}
            active_keys_by_group[group] = active_keys
            stale_keys = [key for key in group_state if key not in active_keys]
            for key in stale_keys:
                del group_state[key]
            if stale_keys:
                removed_counts[group] = len(stale_keys)
        pair_state = state.get("pair")
        if isinstance(pair_state, dict):
            active_checkout = active_keys_by_group.get("checkout", set())
            active_provider = active_keys_by_group.get("provider", set())
            stale_pair_keys = [
                key
                for key, record in pair_state.items()
                if not isinstance(record, dict)
                or record.get("checkout") not in active_checkout
                or record.get("provider") not in active_provider
            ]
            for key in stale_pair_keys:
                del pair_state[key]
            if stale_pair_keys:
                removed_counts["pair"] = len(stale_pair_keys)
        if removed_counts:
            save_proxy_state()
    if removed_counts:
        summary = ", ".join(f"{group}={count}" for group, count in removed_counts.items())
        log(f"代理状态清理完成: {summary}")


def proxy_record(group: str, proxy: str) -> dict[str, Any]:
    with _proxy_state_lock:
        state = load_proxy_state()
        group_state = state.setdefault(group, {})
        key = proxy_state_key(group, proxy)
        if not key:
            return {}
        record = group_state.setdefault(key, {})
        record.setdefault("success", 0)
        record.setdefault("fail", 0)
        return record


def proxy_pair_key(checkout_proxy: str, provider_proxy: str) -> str:
    checkout_key = proxy_key(checkout_proxy)
    provider_key = proxy_key(provider_proxy)
    return f"{checkout_key}:{provider_key}" if checkout_key and provider_key else ""


def record_proxy_result(group: str, proxy: str, success: bool, reason: str = "") -> dict[str, Any]:
    if not proxy or not env_bool("PIX_PROXY_SCORE", True):
        return {}
    record = proxy_record(group, proxy)
    if not record:
        return {}
    now = int(time.time())
    if success:
        record["success"] = int(record.get("success") or 0) + 1
        record["fail"] = 0
        record["last_success"] = now
        record["last_reason"] = "success"
    else:
        record["fail"] = int(record.get("fail") or 0) + 1
        record["last_fail"] = now
        record["last_reason"] = str(reason or "failed")[:160]
    save_proxy_state()
    return record


def proxy_remove_after_fails() -> int:
    return env_int("PIX_PROXY_REMOVE_AFTER_FAILS", 3)


def is_reused_proxy_record(group: str, record: dict[str, Any]) -> bool:
    return int(record.get("success") or 0) > 0


def record_proxy_health_failure(group: str, proxy: str, reason: str) -> None:
    record = record_proxy_result(group, proxy, False, reason)
    fail_count = int(record.get("fail") or 0)
    remove_after = proxy_remove_after_fails() if is_reused_proxy_record(group, record) else 1
    if fail_count >= remove_after:
        remove_failed_proxy(group, proxy, reason)


def checkout_zero_cache_ttl() -> int:
    return env_int("PIX_ZERO_CACHE_TTL", 86400, minimum=0)


def zero_cache_scheduling_enabled() -> bool:
    return env_bool("PIX_ZERO_CACHE_SCHEDULING", False)


def checkout_zero_cache_status(proxy: str, country: str) -> tuple[str, int, int]:
    if not proxy or not env_bool("PIX_ZERO_CACHE", True):
        return "", 0, 0
    record = proxy_record("seed", proxy)
    if not record:
        return "", 0, 0
    checked_at = int(record.get("zero_checked_at") or 0)
    if not checked_at:
        return "", 0, 0
    ttl = checkout_zero_cache_ttl()
    if ttl > 0 and int(time.time()) - checked_at > ttl:
        return "", 0, checked_at
    if normalize_country(str(record.get("zero_country") or country)) != normalize_country(country):
        return "", 0, checked_at
    amount = int(record.get("zero_amount") or 0)
    amount_ok = pix_amount_is_eligible(amount)
    if record.get("zero_ok") is True and amount_ok:
        return "ok", amount, checked_at
    if record.get("zero_ok") is not None:
        return "bad", amount, checked_at
    return "", amount, checked_at


def record_checkout_zero_result(proxy: str, country: str, amount: int) -> None:
    if not proxy or not env_bool("PIX_ZERO_CACHE", True):
        return
    record = proxy_record("seed", proxy)
    if not record:
        return
    amount = int(amount or 0)
    amount_ok = pix_amount_is_eligible(amount)
    record["zero_ok"] = amount_ok
    record["zero_amount"] = amount
    record["zero_country"] = normalize_country(country)
    record["zero_checked_at"] = int(time.time())
    if amount_ok:
        record["zero_success"] = int(record.get("zero_success") or 0) + 1
    save_proxy_state()


def record_proxy_pair_result(checkout_proxy: str, provider_proxy: str, success: bool, reason: str = "") -> None:
    record_proxy_result("checkout", checkout_proxy, success, reason)
    record_proxy_result("provider", provider_proxy, success, reason)
    if not checkout_proxy or not provider_proxy or not env_bool("PIX_PROXY_SCORE", True):
        return
    key = proxy_pair_key(checkout_proxy, provider_proxy)
    if not key:
        return
    with _proxy_state_lock:
        state = load_proxy_state()
        pair_state = state.setdefault("pair", {})
        record = pair_state.setdefault(
            key,
            {"checkout": proxy_key(checkout_proxy), "provider": proxy_key(provider_proxy)},
        )
        now = int(time.time())
        if success:
            record["success"] = int(record.get("success") or 0) + 1
            record["fail"] = 0
            record["last_success"] = now
            record["last_reason"] = "success"
        else:
            record["fail"] = int(record.get("fail") or 0) + 1
            record["last_fail"] = now
            record["last_reason"] = str(reason or "failed")[:160]
        save_proxy_state()


def record_proxy_pair_approve_success(checkout_proxy: str, provider_proxy: str, approve_proxy: str) -> None:
    if not checkout_proxy or not provider_proxy or not approve_proxy or not env_bool("PIX_PROXY_SCORE", True):
        return
    key = proxy_pair_key(checkout_proxy, provider_proxy)
    approve_key = proxy_key(approve_proxy)
    if not key or not approve_key:
        return
    record_proxy_result("provider", approve_proxy, True, "approve_success")
    with _proxy_state_lock:
        state = load_proxy_state()
        pair_state = state.setdefault("pair", {})
        record = pair_state.setdefault(
            key,
            {"checkout": proxy_key(checkout_proxy), "provider": proxy_key(provider_proxy)},
        )
        now = int(time.time())
        record["approve"] = approve_key
        record["approve_success"] = int(record.get("approve_success") or 0) + 1
        record["approve_last_success"] = now
        record["approve_last_reason"] = "success"
        save_proxy_state()


def successful_approve_preferences(checkout_proxy: str, provider_proxy: str, approve_pool: list[str]) -> list[str]:
    if not env_bool("PIX_PROXY_SCORE", True):
        return []
    pair_state = load_proxy_state().get("pair", {})
    if not isinstance(pair_state, dict):
        return []
    record = pair_state.get(proxy_pair_key(checkout_proxy, provider_proxy))
    if not isinstance(record, dict):
        return []
    approve_key = str(record.get("approve") or "")
    if not approve_key:
        return []
    approve_by_key = {proxy_key(proxy): proxy for proxy in approve_pool}
    approve_proxy = approve_by_key.get(approve_key)
    return [approve_proxy] if approve_proxy else []


def record_failure_by_stage(
    reason: str,
    checkout_proxy: str,
    provider_proxy: str,
    promotion_proxy: str = "",
) -> None:
    def record_seed_failure(proxy: str) -> None:
        if not proxy:
            return
        if is_direct_remove_proxy_error(reason):
            remove_failed_proxy("seed", proxy, reason)
            record_proxy_result("seed", proxy, False, reason)
        elif is_proxy_health_failure(reason):
            record_proxy_health_failure("seed", proxy, reason)
        else:
            record_proxy_result("seed", proxy, False, reason)

    if "checkout 阶段失败" in reason or "checkout 创建失败" in reason:
        record_seed_failure(checkout_proxy)
        return
    if is_pix_unavailable_error(reason):
        return
    if "0 元优惠未生效" in reason or "PIX 目标金额未命中" in reason:
        return
    if "approve blocked" in reason:
        return
    if "promotion 阶段失败" in reason or "checkout/update" in reason:
        record_seed_failure(promotion_proxy)
        return
    record_seed_failure(provider_proxy)


def order_proxy_group(group: str, proxies: list[str]) -> list[str]:
    if not env_bool("PIX_PROXY_SCORE", True):
        return proxies
    state = load_proxy_state().get(group, {})
    skip_failed = env_bool("PIX_PROXY_SKIP_FAILED", True)
    fail_threshold = env_int("PIX_PROXY_FAIL_SKIP_AFTER", 1)
    fail_cooldown = env_int("PIX_PROXY_FAIL_COOLDOWN", 180, minimum=0)
    zero_ttl = checkout_zero_cache_ttl()
    zero_scheduling = zero_cache_scheduling_enabled()
    now = int(time.time())
    kept: list[str] = []
    cooldown_skipped = 0
    zero_skipped = 0
    zero_seen = 0
    success_seen = 0
    for proxy in proxies:
        record = state.get(proxy_state_key(group, proxy), {}) if isinstance(state, dict) else {}
        success_count = int(record.get("success") or 0)
        fail_count = int(record.get("fail") or 0)
        last_fail = int(record.get("last_fail") or 0)
        if success_count > 0:
            success_seen += 1
        zero_checked_at = int(record.get("zero_checked_at") or 0)
        zero_cache_valid = zero_checked_at and (zero_ttl <= 0 or now - zero_checked_at <= zero_ttl)
        if group == "checkout" and zero_scheduling and zero_cache_valid and record.get("zero_ok") is True:
            zero_seen += 1
        if (
            group == "checkout"
            and zero_scheduling
            and env_bool("PIX_ZERO_CACHE_SKIP_BAD", True)
            and zero_cache_valid
            and record.get("zero_ok") is False
        ):
            zero_skipped += 1
            continue
        if skip_failed and fail_count >= fail_threshold:
            in_cooldown = fail_cooldown <= 0 or not last_fail or now - last_fail <= fail_cooldown
            if in_cooldown:
                cooldown_skipped += 1
                continue
        kept.append(proxy)

    if not kept and proxies:
        log(f"{group} 代理状态过滤后为空，已全部跳过", "[WARN] ")

    def rank(proxy: str) -> tuple[int, int, int, int, int]:
        record = state.get(proxy_state_key(group, proxy), {}) if isinstance(state, dict) else {}
        zero_checked_at = int(record.get("zero_checked_at") or 0)
        zero_cache_valid = zero_checked_at and (zero_ttl <= 0 or now - zero_checked_at <= zero_ttl)
        zero_rank = 1 if group == "checkout" and zero_scheduling and zero_cache_valid and record.get("zero_ok") is True else 0
        return (
            zero_rank,
            int(record.get("success") or 0),
            int(record.get("last_success") or 0),
            -int(record.get("fail") or 0),
            -int(record.get("last_fail") or 0),
        )

    ordered = sorted(kept, key=rank, reverse=True)
    if cooldown_skipped or success_seen or zero_seen or zero_skipped:
        log(
            f"{group} 代理状态: 成功优先={success_seen}，目标金额命中={zero_seen}，"
            f"冷却跳过={cooldown_skipped}，目标金额失败跳过={zero_skipped}"
        )
    return ordered


def set_proxy(session: Any, proxy: str) -> None:
    register_proxy_for_redaction(proxy)
    proxy = normalize_proxy_url(proxy)
    if hasattr(session, "trust_env"):
        session.trust_env = False
    session.proxies = {"http": proxy, "https": proxy} if proxy else {}


def pre_proxy_url() -> str:
    """本机前置代理：本机代理 -> 文件代理 -> 目标站。"""
    for name in ("PIX_PRE_PROXY", "PP_PRE_PROXY", "PP_LOCAL_PROXY"):
        if name in os.environ:
            raw = os.environ.get(name, "").strip()
            if raw.lower() in {"", "0", "off", "none", "direct", "disabled"}:
                return ""
            proxy = normalize_pre_proxy_url(raw)
            register_proxy_for_redaction(proxy)
            return proxy
    raw = ""
    return normalize_pre_proxy_url(raw) if raw else ""


def load_proxy_file(path: Path) -> list[str]:
    proxies: list[str] = []
    if not path.exists():
        return proxies
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            register_proxy_for_redaction(line)
            proxy = normalize_proxy_url(line)
            if proxy:
                proxies.append(proxy)
    random.shuffle(proxies)
    return proxies


def proxy_seed_file() -> Path:
    raw = (
        os.environ.get("PIX_PROXY_SEED_FILE", "").strip()
        or os.environ.get("PP_PROXY_SEED_FILE", "").strip()
    )
    return Path(raw).expanduser() if raw else SCRIPT_DIR / "proxy_seeds.txt"


def is_direct_remove_proxy_error(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "proxy authentication",
            "proxy auth",
            "resolve proxy",
            "could not resolve proxy",
            "invalid proxy",
            "malformed proxy",
            "unsupported proxy",
            "http 407",
            "status 407",
        )
    )


def is_proxy_health_failure(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "目标站不可达",
            "proxy-server",
            "connection reset",
            "recv failure",
            "timed out",
            "timeout",
            "connect tunnel failed",
            "proxy connect aborted",
            "proxy tunneling",
            "proxy handshake",
            "connection refused",
            "ssl connect",
            "tls connect",
            "curl: (28)",
            "curl: (35)",
            "curl: (56)",
            "http_502",
            "http_503",
            "http_504",
        )
    )


def remove_failed_proxies(group: str, failures: list[tuple[str, str]]) -> int:
    if not failures or not env_bool("PIX_PROXY_REMOVE_FAILED", True):
        return 0
    for proxy, _reason in failures:
        register_proxy_for_redaction(proxy)
    path = proxy_seed_file()
    if not path.is_file():
        return 0
    reasons = {proxy_chain_key(proxy): reason for proxy, reason in failures if proxy_chain_key(proxy)}
    if not reasons:
        return 0
    with _proxy_file_lock:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        removed = [line for line in lines if proxy_chain_key(line) in reasons]
        if not removed:
            return 0
        kept = [line for line in lines if proxy_chain_key(line) not in reasons]
        quarantine = SCRIPT_DIR / "removed_proxies.jsonl"
        with open(quarantine, "a", encoding="utf-8") as f:
            for line in removed:
                chain_key = proxy_chain_key(line)
                f.write(
                    json.dumps(
                        {
                            "time": int(time.time()),
                            "group": group,
                            "proxy": proxy_label(line.strip()),
                            "reason": redact_log_text(str(reasons.get(chain_key) or ""))[:300],
                            "source": path.name,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        temp_path = path.with_name(f".{path.name}.tmp")
        temp_path.write_text("".join(kept), encoding="utf-8")
        os.replace(temp_path, path)
        return len(removed)


def remove_failed_proxy(group: str, proxy: str, reason: str) -> bool:
    return remove_failed_proxies(group, [(proxy, reason)]) > 0


def unique_proxy_seeds(proxy_seeds: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    duplicates = 0
    for proxy_seed in proxy_seeds:
        chain_key = proxy_chain_key(proxy_seed)
        if not chain_key or chain_key in seen:
            duplicates += 1
            continue
        seen.add(chain_key)
        unique.append(proxy_seed)
    if duplicates:
        log(f"代理 Seed 去重: 忽略相同 sticky session {duplicates} 条", "[WARN] ")
    return unique


def load_proxy_seeds() -> list[str]:
    path = proxy_seed_file()
    if not path.is_file():
        raise RuntimeError("代理 Seed 文件不存在")
    proxy_seeds = unique_proxy_seeds(load_proxy_file(path))
    if not proxy_seeds:
        raise RuntimeError("代理 Seed 为空")
    prune_proxy_seed_state(proxy_seeds)
    proxy_seeds = order_proxy_group("seed", proxy_seeds)
    if not proxy_seeds:
        raise RuntimeError("代理 Seed 已全部处于失败冷却")
    log(f"加载代理 Seed {len(proxy_seeds)} 条")
    log(
        "严格代理策略: 每轮取一条 seed，派生 "
        f"{PIX_BOOTSTRAP_COUNTRY} Checkout → {' → '.join(PIX_PROMOTION_COUNTRIES)} checkout/update → "
        f"{PIX_PROVIDER_COUNTRY} Stripe/PIX/approve"
    )
    log(f"裸代理默认协议: {default_proxy_scheme()}://")
    log(f"本机前置代理: {proxy_short(pre_proxy_url())}")
    return proxy_seeds


def new_session(proxy: str = "", use_pre_proxy: bool = True) -> Any:
    pre_proxy = pre_proxy_url() if use_pre_proxy else ""
    register_proxy_for_redaction(pre_proxy)
    if CurlCffiSession is not None:
        kwargs: dict[str, Any] = {"impersonate": "chrome136"}
        if pre_proxy:
            if CurlOpt is None:
                raise RuntimeError("本机前置代理需要 curl_cffi 支持")
            kwargs["curl_options"] = {CurlOpt.PRE_PROXY: pre_proxy}
        session = CurlCffiSession(**kwargs)
    else:
        if pre_proxy:
            raise RuntimeError("本机前置代理需要 curl_cffi：python3 -m pip install curl_cffi")
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    if proxy:
        set_proxy(session, proxy)
    return session


def _redact_text(text: str, limit: int | None = None) -> str:
    text = text or ""
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._=-]+", r"\1***", text)
    text = re.sub(r"(__Secure-next-auth\.session-token=)[^;\\s]+", r"\1***", text)
    text = re.sub(r"(accessToken|access_token|sessionToken|token)(['\"]?\s*[:=]\s*['\"])[^'\"]+", r"\1\2***", text)
    text = redact_log_text(text)
    if limit is None:
        limit = env_int("PIX_DUMP_LIMIT", 6000, minimum=500)
    return text[:limit]


def dump_http(
    response: requests.Response | None,
    stage: str,
    request_body: Any = None,
    request_method: str = "",
    request_url: str = "",
    force: bool = False,
) -> None:
    if not force and not env_bool("PIX_DUMP", False):
        return
    global _dump_counter
    with _dump_lock:
        _dump_counter += 1
        name = f"{time.strftime('%Y%m%d-%H%M%S')}_{_dump_counter:04d}_{stage}.txt"
    path = DUMP_DIR / re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    lines = [
        f"stage: {stage}",
        f"request: {request_method} {request_url}",
        "",
        "request_body:",
        _redact_text(json.dumps(request_body, ensure_ascii=False, indent=2) if request_body is not None else ""),
        "",
    ]
    if response is not None:
        lines.extend(
            [
                f"status: {response.status_code}",
                f"url: {response.url}",
                "",
                "response:",
                _redact_text(response.text),
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def token_key_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def find_named_token(payload: Any, aliases: tuple[str, ...]) -> str:
    wanted = {token_key_name(item) for item in aliases}
    if isinstance(payload, dict):
        cookie_name = token_key_name(payload.get("name") or payload.get("key"))
        if cookie_name in wanted:
            for value_key in ("value", "token", "content"):
                value = str(payload.get(value_key) or "").strip()
                if value:
                    return value
        for key, value in payload.items():
            if token_key_name(key) in wanted and isinstance(value, (str, int, float)):
                found = str(value).strip()
                if found:
                    return found
        for value in payload.values():
            found = find_named_token(value, aliases)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_named_token(item, aliases)
            if found:
                return found
    return ""


def collect_strings(payload: Any, result: list[str] | None = None) -> list[str]:
    values = result if result is not None else []
    if isinstance(payload, str):
        values.append(payload)
    elif isinstance(payload, dict):
        for value in payload.values():
            collect_strings(value, values)
    elif isinstance(payload, list):
        for item in payload:
            collect_strings(item, values)
    return values


def find_session_cookie(payload: Any) -> str:
    for value in collect_strings(payload):
        match = re.search(r"(?:^|[;\s])__Secure-next-auth\.session-token=([^;\s]+)", value)
        if match:
            return unquote(match.group(1))
    return ""


def normalize_token(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    if not text:
        return "", ""
    session_token = ""
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
            access_token = find_named_token(
                data,
                (
                    "accessToken",
                    "access_token",
                    "token",
                    "bearerToken",
                    "bearer_token",
                    "jwt",
                ),
            )
            session_token = find_named_token(
                data,
                (
                    "sessionToken",
                    "session_token",
                    "nextAuthSessionToken",
                    "next_auth_session_token",
                    "__Secure-next-auth.session-token",
                    "secureNextAuthSessionToken",
                ),
            ) or find_session_cookie(data)
            text = access_token
        except json.JSONDecodeError:
            pass
    return text, session_token


def load_token() -> tuple[str, str]:
    for env_name in ("PP_TOKEN", "PIX_TOKEN"):
        value = os.environ.get(env_name, "").strip()
        if value:
            log(f"使用环境变量 {env_name}")
            token, session_token = normalize_token(value)
            env_session = os.environ.get("PP_SESSION_TOKEN", "").strip()
            if env_session or session_token:
                log("已加载 sessionToken cookie")
            return token, env_session or session_token

    candidates = [SCRIPT_DIR / "token.txt"]
    for path in candidates:
        if not path.exists():
            continue
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "utf-16", "utf-8", "ascii"):
            try:
                text = raw.decode(enc).strip()
                break
            except UnicodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="ignore").strip()
        if text:
            log("使用 token 文件")
            token, session_token = normalize_token(text)
            env_session = os.environ.get("PP_SESSION_TOKEN", "").strip()
            if env_session or session_token:
                log("已加载 sessionToken cookie")
            return token, env_session or session_token

    token = input("请输入 access_token: ").strip()
    session_token = os.environ.get("PP_SESSION_TOKEN", "").strip()
    token, parsed_session = normalize_token(token)
    return token, session_token or parsed_session


def cookie_header_values(cookie_header: str) -> dict[str, str]:
    parsed = SimpleCookie()
    try:
        parsed.load(str(cookie_header or ""))
    except Exception:
        return {}
    return {name: morsel.value for name, morsel in parsed.items() if morsel.value}


def build_chatgpt_session(
    access_token: str,
    device_id: str,
    proxy: str,
    session_token: str = "",
    cookie_header: str = "",
    fingerprint: Mapping[str, Any] | None = None,
) -> requests.Session:
    session = new_session(proxy)
    cookies = cookie_header_values(cookie_header)
    cookies["oai-did"] = cookies.get("oai-did") or device_id
    if session_token and "__Secure-next-auth.session-token" not in cookies:
        cookies["__Secure-next-auth.session-token"] = session_token
    if getattr(session, "cookies", None) is not None:
        session.cookies.update(cookies)
    browser_session_id = str(uuid.uuid4())
    fp = fingerprint if isinstance(fingerprint, Mapping) else {}
    session.headers.update(
        {
            "User-Agent": str(fp.get("user-agent") or DEFAULT_USER_AGENT),
            "Accept": "*/*",
            "Accept-Language": payment_accept_language(),
            "Authorization": f"Bearer {access_token}",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "Content-Type": "application/json",
            "oai-device-id": cookies["oai-did"],
            "oai-language": payment_browser_locale(),
            "oai-session-id": browser_session_id,
            "oai-client-version": CHATGPT_CLIENT_VERSION,
            "oai-client-build-number": CHATGPT_CLIENT_BUILD_NUMBER,
            "sec-ch-ua": str(
                fp.get("sec-ch-ua")
                or '"Chromium";v="136", "Google Chrome";v="136", "Not_A Brand";v="99"'
            ),
            "sec-ch-ua-mobile": str(fp.get("sec-ch-ua-mobile") or "?0"),
            "sec-ch-ua-platform": str(fp.get("sec-ch-ua-platform") or '"macOS"'),
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
    )
    account_id = str(cookies.get("_account") or "").strip()
    if account_id:
        session.headers["ChatGPT-Account-Id"] = account_id
    return session


def chatgpt_session_for_proxy(
    existing_session: Any | None,
    existing_proxy: str,
    target_proxy: str,
    access_token: str,
    device_id: str,
    session_token: str = "",
    cookie_header: str = "",
    fingerprint: Mapping[str, Any] | None = None,
) -> requests.Session:
    """Keep ChatGPT and Cloudflare cookies when a Pix stage stays on one route."""
    if (
        existing_session is not None
        and normalize_proxy_url(existing_proxy) == normalize_proxy_url(target_proxy)
    ):
        return existing_session
    return build_chatgpt_session(
        access_token,
        device_id,
        target_proxy,
        session_token,
        cookie_header,
        fingerprint,
    )


def is_cloudflare_challenge_response(response: Any) -> bool:
    if int(getattr(response, "status_code", 0) or 0) != 403:
        return False
    text = str(getattr(response, "text", "") or "").lower()
    return any(
        marker in text
        for marker in (
            "challenge-error-text",
            "enable javascript and cookies to continue",
            "cf-chl-",
            "__cf_chl_",
            "just a moment",
        )
    )


def raise_if_cloudflare_challenge(response: Any, stage: str) -> None:
    if is_cloudflare_challenge_response(response):
        raise RuntimeError(f"cloudflare_challenge: {stage} HTTP 403")


def checkout_response_has_promo(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    for key in (
        "scheduled_discount_preview",
        "immediate_discount_settings",
        "promo_campaign",
        "promo_credit_grant",
    ):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return True
    return False


def checkout_response_has_trial(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("one_click_trial_eligible") is True:
        return True
    subscription_data = payload.get("subscription_data")
    if isinstance(subscription_data, dict) and int(subscription_data.get("trial_period_days") or 0) > 0:
        return True
    for key in ("trial_period_days", "trial_end"):
        value = payload.get(key)
        if value not in (None, "", 0, "0", False):
            return True
    return False


def create_checkout(chatgpt: requests.Session, country: str) -> dict[str, str]:
    country = normalize_country(country)
    body: dict[str, Any] = {
        "entry_point": os.environ.get("PP_ENTRY_POINT", "all_plans_pricing_modal"),
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency_for_country(country)},
        "checkout_ui_mode": "custom",
    }
    log("Checkout bootstrap: BR/BRL without promo")

    headers = {
        "Referer": "https://chatgpt.com/",
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
    }
    resp = chatgpt.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=body,
        headers=headers,
        timeout=CHATGPT_TIMEOUT,
    )
    dump_http(resp, "checkout", body, "POST", "https://chatgpt.com/backend-api/payments/checkout", force=resp.status_code >= 400)
    raise_if_cloudflare_challenge(resp, "checkout")
    if resp.status_code >= 400:
        if is_user_already_paid_error(resp.text):
            raise RuntimeError("用户已支付: User is already paid")
        raise RuntimeError(f"checkout 创建失败 HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json() or {}

    cs_id = data.get("checkout_session_id") or data.get("session_id") or data.get("id")
    if not cs_id or not str(cs_id).startswith("cs_"):
        raise RuntimeError(f"checkout 响应缺少 cs_id: {str(data)[:500]}")

    raw_pk = (
        data.get("stripe_publishable_key")
        or data.get("publishable_key")
        or data.get("publishableKey")
        or data.get("stripePublishableKey")
        or data.get("key")
        or ""
    )
    match = re.search(r"pk_live_[A-Za-z0-9]+", str(raw_pk))
    stripe_pk = match.group(0) if match else DEFAULT_STRIPE_PK
    processor_entity = str(data.get("processor_entity") or data.get("processorEntity") or "")
    log(
        f"Checkout 创建成功: {cs_id} / {country} / {currency_for_country(country)} / "
        f"promo={checkout_response_has_promo(data)} / trial={checkout_response_has_trial(data)}"
    )
    return {
        "cs_id": str(cs_id),
        "processor_entity": processor_entity,
        "stripe_pk": stripe_pk,
        "billing_country": country,
        "currency": currency_for_country(country),
    }


def checkout_page_url(checkout: dict[str, str]) -> str:
    processor = processor_entity_for_country(
        PIX_BOOTSTRAP_COUNTRY,
        checkout.get("processor_entity") or "",
    )
    return f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}"


def update_checkout_promotion(
    chatgpt: requests.Session,
    checkout: dict[str, str],
    promotion_country: str,
) -> None:
    body: dict[str, Any] = {
        "checkout_session_id": checkout["cs_id"],
        "processor_entity": processor_entity_for_country(
            PIX_BOOTSTRAP_COUNTRY,
            checkout.get("processor_entity") or "",
        ),
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "billing_details": {
            "country": normalize_country(promotion_country),
            "currency": currency_for_country(promotion_country),
        },
        "checkout_ui_mode": "custom",
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    url = "https://chatgpt.com/backend-api/payments/checkout/update"
    resp = chatgpt.post(
        url,
        json=body,
        headers={
            "Referer": checkout_page_url(checkout),
            "x-openai-target-path": "/backend-api/payments/checkout/update",
            "x-openai-target-route": "/backend-api/payments/checkout/update",
        },
        timeout=CHATGPT_TIMEOUT,
    )
    dump_http(resp, "checkout_promotion_update", body, "POST", url, force=resp.status_code >= 400)
    raise_if_cloudflare_challenge(resp, "checkout/update")
    if resp.status_code >= 400:
        if is_checkout_not_active_error(resp.text):
            raise RuntimeError("checkout_not_active_session")
        raise RuntimeError(f"checkout/update 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    try:
        payload = resp.json() or {}
    except Exception:
        payload = {}
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"checkout/update rejected: {str(payload)[:500]}")
    log(f"{promotion_country} checkout/update 成功: promo=plus-1-month-free")


def update_pix_checkout_taxes(
    chatgpt: requests.Session,
    checkout: dict[str, str],
    email: str,
) -> None:
    url = "https://chatgpt.com/backend-api/payments/checkout/taxes"
    body = {
        "checkout_session_id": checkout["cs_id"],
        "checkout_email": email,
        "billing_country": "BR",
        "billing_name": PIX_TAX_NAME,
        "currency": "BRL",
        "tax_id": None,
        "processor_entity": processor_entity_for_country(
            PIX_BOOTSTRAP_COUNTRY,
            checkout.get("processor_entity") or "",
        ),
        "billing_address": {
            "line1": PIX_TAX_LINE1,
            "city": PIX_TAX_CITY,
            "state": PIX_TAX_STATE,
            "country": "BR",
            "postal_code": PIX_TAX_POSTAL_CODE,
        },
    }
    resp = chatgpt.post(
        url,
        json=body,
        headers={
            "Referer": checkout_page_url(checkout),
            "x-openai-target-path": "/backend-api/payments/checkout/taxes",
            "x-openai-target-route": "/backend-api/payments/checkout/taxes",
        },
        timeout=CHATGPT_TIMEOUT,
    )
    dump_http(resp, "checkout_taxes", body, "POST", url, force=resp.status_code >= 400)
    raise_if_cloudflare_challenge(resp, "checkout/taxes")
    if resp.status_code >= 400:
        if is_checkout_not_active_error(resp.text):
            raise RuntimeError("checkout_not_active_session")
        raise RuntimeError(f"checkout/taxes 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    log(f"{PIX_PROVIDER_COUNTRY} checkout/taxes 同步成功")


def stripe_api_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": random_user_agent(),
        "Accept-Language": payment_accept_language(),
    }


def stripe_init(
    cs_id: str,
    stripe_pk: str,
    proxy: str,
    session: Any | None = None,
) -> dict[str, Any]:
    stripe = session or new_session(proxy)
    stripe.headers.update({"User-Agent": random_user_agent(), "Accept-Language": payment_accept_language()})
    stripe_js_id = str(uuid.uuid4())
    body = {
        "browser_locale": payment_browser_locale(),
        "browser_timezone": payment_browser_timezone(),
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": payment_elements_locale(),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}/init"
    resp = stripe.post(
        url,
        data=body,
        headers=stripe_api_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    dump_http(resp, "stripe_init", body, "POST", url, force=resp.status_code >= 400)
    if resp.status_code >= 400:
        raise RuntimeError(f"Stripe init 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    payload = resp.json() or {}
    payload["client_stripe_js_id"] = stripe_js_id
    payload["_client_context"] = {"stripe_js_id": stripe_js_id}
    return payload


def amount_from_payload(payload: Any) -> int:
    if isinstance(payload, dict):
        total_summary = payload.get("total_summary")
        if isinstance(total_summary, dict) and total_summary.get("due") is not None:
            return int(total_summary.get("due") or 0)
        invoice = payload.get("invoice")
        if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
            return int(invoice.get("amount_due") or 0)
        line_items = payload.get("line_items")
        if isinstance(line_items, list):
            total = 0
            found = False
            for item in line_items:
                if isinstance(item, dict) and item.get("amount") is not None:
                    total += int(item.get("amount") or 0)
                    found = True
            if found:
                return total
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    for pattern in (
        r'"total"\s*:\s*(\d+)',
        r'"amount_total"\s*:\s*(\d+)',
        r'"checkout_amount"\s*:\s*(\d+)',
        r'"amount"\s*:\s*(\d+)',
    ):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return 0


def build_ctx(init_payload: dict[str, Any], checkout: dict[str, str]) -> dict[str, Any]:
    client_context = (
        init_payload.get("_client_context")
        if isinstance(init_payload.get("_client_context"), dict)
        else {}
    )
    payment_element_config_id = str(init_payload.get("config_id") or "")
    return {
        "stripe_js_id": str(
            client_context.get("stripe_js_id")
            or init_payload.get("client_stripe_js_id")
            or uuid.uuid4()
        ),
        "client_session_id": str(uuid.uuid4()),
        "guid": stripe_browser_id(),
        "muid": stripe_browser_id(),
        "sid": stripe_browser_id(),
        "elements_session_id": "",
        "elements_session_config_id": "",
        "payment_element_config_id": payment_element_config_id,
        "checkout_config_id": str(uuid.uuid4()),
        "config_id": payment_element_config_id,
        "init_checksum": init_payload.get("init_checksum") or "",
        "checkout_amount": amount_from_payload(init_payload),
        "locale": payment_elements_locale(),
        "currency": str(init_payload.get("currency") or checkout.get("currency") or "eur").lower(),
        "runtime_version": DEFAULT_STRIPE_RUNTIME_VERSION,
        "stripe_version": STRIPE_VERSION_FULL,
        "page_started_at": time.monotonic(),
    }


def stripe_elements_session_params(ctx: dict[str, Any]) -> dict[str, str]:
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or uuid.uuid4()),
        "elements_session_client[locale]": str(ctx.get("locale") or payment_elements_locale()),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
    }
    if ctx.get("elements_session_id"):
        params["elements_session_client[session_id]"] = str(ctx["elements_session_id"])
    return params


def stripe_elements_session(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    ctx: dict[str, Any],
) -> dict[str, Any]:
    amount = int(ctx.get("checkout_amount") or 0)
    currency = str(ctx.get("currency") or "brl").lower()
    params = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount),
        "deferred_intent[currency]": currency,
        "deferred_intent[setup_future_usage]": "off_session",
        "deferred_intent[payment_method_types][0]": "card",
        "deferred_intent[payment_method_types][1]": "pix",
        "currency": currency,
        "key": stripe_pk,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": str(ctx.get("stripe_js_id") or ""),
        "locale": str(ctx.get("locale") or payment_elements_locale()),
        "type": "deferred_intent",
        "checkout_session_id": cs_id,
    }
    url = "https://api.stripe.com/v1/elements/sessions"
    resp = stripe.get(
        url,
        params=params,
        headers=stripe_api_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    dump_http(resp, "stripe_elements_session", params, "GET", url, force=resp.status_code >= 400)
    if resp.status_code >= 400:
        raise RuntimeError(f"Stripe elements/sessions 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    payload = resp.json() or {}
    session_id = str(payload.get("session_id") or "")
    config_id = str(payload.get("config_id") or "")
    if not session_id or not config_id:
        raise RuntimeError(
            "Stripe elements/sessions 缺少真实会话 ID: "
            f"session_id={bool(session_id)}, config_id={bool(config_id)}"
        )
    ctx["elements_session_id"] = session_id
    ctx["elements_session_config_id"] = config_id
    return payload


def generate_cpf() -> str:
    digits = [random.randint(0, 9) for _ in range(9)]
    first = (sum(value * weight for value, weight in zip(digits, range(10, 1, -1))) * 10) % 11
    digits.append(0 if first == 10 else first)
    second = (sum(value * weight for value, weight in zip(digits, range(11, 1, -1))) * 10) % 11
    digits.append(0 if second == 10 else second)
    return "".join(str(value) for value in digits)


def normalize_br_postal_code(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if digits else str(value or "")


def format_cpf(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 11:
        return str(value or "")
    return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"


def pix_billing_snapshot(billing: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact billing values submitted during a successful Pix flow."""
    return {
        "checkout_billing_name": str(billing.get("name") or ""),
        "checkout_billing_email": str(billing.get("email") or ""),
        "checkout_billing_address": {
            "country": normalize_country(str(billing.get("country") or "BR")),
            "line1": str(billing.get("line1") or ""),
            "line2": str(billing.get("line2") or ""),
            "city": str(billing.get("city") or ""),
            "state": str(billing.get("state") or ""),
            "postal_code": normalize_br_postal_code(str(billing.get("postal_code") or "")),
        },
        "checkout_billing_tax_id": format_cpf(str(billing.get("tax_id") or "")),
    }


def pix_billing_profile() -> dict[str, str]:
    first_name, last_name = random.choice(BR_BILLING_NAMES)
    line1, line2, city, postal_code, state = random.choice(BR_BILLING_ADDRESSES)
    profile = {
        "email": f"{first_name.lower()}.{last_name.lower()}{random.randint(1000, 9999)}@example.com",
        "name": f"{first_name} {last_name}",
        "phone": f"+55119{random.randint(10000000, 99999999)}",
        "country": "BR",
        "line1": line1,
        "line2": line2,
        "city": city,
        "postal_code": postal_code,
        "state": state,
        "tax_id": generate_cpf(),
    }
    if env_bool("PIX_USE_FIXED_BILLING", False):
        profile = dict(DEFAULT_PIX_BILLING)
    env_map = {
        "email": "PIX_EMAIL",
        "name": "PIX_NAME",
        "phone": "PIX_PHONE",
        "country": "PIX_BILLING_COUNTRY",
        "line1": "PIX_LINE1",
        "line2": "PIX_LINE2",
        "city": "PIX_CITY",
        "postal_code": "PIX_POSTAL_CODE",
        "state": "PIX_STATE",
        "tax_id": "PIX_TAX_ID",
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            profile[key] = value
    profile["country"] = normalize_country(profile.get("country", "BR"))
    profile["tax_id"] = re.sub(r"\D", "", profile.get("tax_id") or generate_cpf())
    return profile


def stripe_update_tax_region(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    ctx: dict[str, Any],
    billing: dict[str, str],
    *,
    country_only: bool = False,
) -> bool:
    body: dict[str, Any] = {
        **stripe_elements_session_params(ctx),
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
        "key": stripe_pk,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
        "tax_region[country]": billing["country"],
    }
    if not country_only:
        body.update(
            {
                "tax_region[line1]": billing["line1"],
                "tax_region[city]": billing["city"],
                "tax_region[postal_code]": normalize_br_postal_code(billing["postal_code"]),
            }
        )
        if billing.get("state"):
            body["tax_region[state]"] = billing["state"]
        if billing.get("line2"):
            body["tax_region[line2]"] = billing["line2"]

    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}"
    resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
    stage = "tax_region_country" if country_only else "tax_region_full"
    dump_http(resp, stage, body, "POST", url, force=resp.status_code >= 400)
    if resp.status_code >= 400:
        if is_checkout_not_active_error(resp.text):
            raise RuntimeError("checkout_not_active_session")
        raise RuntimeError(f"Stripe Pix tax_region 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    if country_only:
        log(f"tax_region 国家已提交: {billing['country']}")
    else:
        log(
            f"tax_region 完整地址已提交: {billing['country']} / "
            f"{billing['city']} {normalize_br_postal_code(billing['postal_code'])}"
        )
    return True


def checkout_snapshot(
    chatgpt: requests.Session,
    checkout: dict[str, str],
    billing: dict[str, str],
) -> None:
    address: dict[str, str] = {
        "line1": billing["line1"],
        "city": billing["city"],
        "country": billing["country"],
        "postal_code": normalize_br_postal_code(billing["postal_code"]),
        "state": billing.get("state", ""),
    }
    if billing.get("line2"):
        address["line2"] = billing["line2"]
    body = {
        "snapshot": {
            "billing_address": {
                "name": billing["name"],
                "address": address,
            }
        }
    }
    url = "https://chatgpt.com/backend-api/payments/checkout/snapshot"
    resp = chatgpt.post(
        url,
        json=body,
        headers={
            "Referer": checkout_page_url(checkout),
            "x-openai-target-path": "/backend-api/payments/checkout/snapshot",
            "x-openai-target-route": "/backend-api/payments/checkout/snapshot",
        },
        timeout=CHATGPT_TIMEOUT,
    )
    dump_http(resp, "checkout_snapshot", body, "POST", url, force=resp.status_code >= 400)
    raise_if_cloudflare_challenge(resp, "checkout/snapshot")
    if resp.status_code >= 400:
        if is_checkout_not_active_error(resp.text):
            raise RuntimeError("checkout_not_active_session")
        raise RuntimeError(f"checkout/snapshot 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    log("checkout snapshot 已提交")


def add_inline_pix_payment_method_data(
    body: dict[str, Any],
    cs_id: str,
    billing: dict[str, str],
    ctx: dict[str, Any],
) -> None:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    page_started_at = float(ctx.get("page_started_at") or time.monotonic())
    time_on_page = max(1, int((time.monotonic() - page_started_at) * 1000))
    body.update(
        {
            "payment_method_data[billing_details][name]": billing["name"],
            "payment_method_data[billing_details][email]": billing["email"],
            "payment_method_data[billing_details][address][line1]": billing["line1"],
            "payment_method_data[billing_details][address][city]": billing["city"],
            "payment_method_data[billing_details][address][postal_code]": normalize_br_postal_code(
                billing["postal_code"]
            ),
            "payment_method_data[billing_details][address][country]": billing["country"],
            "payment_method_data[billing_details][tax_id]": format_cpf(billing["tax_id"]),
            "payment_method_data[type]": "pix",
            "payment_method_data[payment_user_agent]": (
                f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
                "payment-element; deferred-intent"
            ),
            "payment_method_data[referrer]": "https://chatgpt.com",
            "payment_method_data[time_on_page]": str(time_on_page),
            "payment_method_data[client_attribution_metadata][client_session_id]": ctx[
                "stripe_js_id"
            ],
            "payment_method_data[client_attribution_metadata][checkout_session_id]": cs_id,
            "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
            "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
            "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
            "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
            "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "automatic",
            "payment_method_data[client_attribution_metadata][elements_session_id]": ctx[
                "elements_session_id"
            ],
            "payment_method_data[client_attribution_metadata][elements_session_config_id]": ctx[
                "elements_session_config_id"
            ],
            "payment_method_data[client_attribution_metadata][checkout_config_id]": ctx[
                "payment_element_config_id"
            ],
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]": "expressCheckout",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][1]": "payment",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][2]": "address",
        }
    )
    if billing.get("state"):
        body["payment_method_data[billing_details][address][state]"] = billing["state"]
    if billing.get("line2"):
        body["payment_method_data[billing_details][address][line2]"] = billing["line2"]
    if billing.get("phone"):
        body["payment_method_data[billing_details][phone]"] = billing["phone"]


def stripe_create_pix_payment_method(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    billing: dict[str, str],
    ctx: dict[str, Any],
    *,
    reference_compat: bool = False,
) -> str:
    """Create the standalone Pix PaymentMethod used by the reference flow."""
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body: dict[str, Any] = {
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[address][country]": billing["country"],
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "billing_details[tax_id]": re.sub(r"\D", "", billing["tax_id"]),
        "type": "pix",
        "payment_user_agent": (
            f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
            "payment-element; deferred-intent"
        ),
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(30000, 90000)),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": str(ctx.get("stripe_js_id") or ""),
        "client_attribution_metadata[checkout_config_id]": str(ctx.get("config_id") or ""),
        "client_attribution_metadata[elements_session_id]": str(
            ctx.get("elements_session_id") or ""
        ),
        "client_attribution_metadata[elements_session_config_id]": str(
            ctx.get("elements_session_config_id") or ""
        ),
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_pk,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
    }
    if billing.get("phone"):
        body["billing_details[phone]"] = billing["phone"]
    if billing.get("line2"):
        body["billing_details[address][line2]"] = billing["line2"]
    if billing.get("state"):
        body["billing_details[address][state]"] = billing["state"]

    url = "https://api.stripe.com/v1/payment_methods"
    headers = reference_stripe_headers() if reference_compat else stripe_api_headers()
    resp = stripe.post(url, data=body, headers=headers, timeout=DEFAULT_TIMEOUT)
    dump_http(resp, "pix_pm", body, "POST", url, force=resp.status_code >= 400)
    if resp.status_code >= 400:
        raise RuntimeError(f"创建 PIX PM 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    pm_id = str((resp.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"创建 PIX PM 响应异常: {resp.text[:300]}")
    return pm_id


def processor_entity_for_country(country: str, processor_entity: str = "") -> str:
    if processor_entity:
        return processor_entity
    return "openai_llc" if normalize_country(country) == "US" else "openai_ie"


def stripe_checkout_long_url(cs_id: str, country: str, processor_entity: str) -> str:
    processor = processor_entity_for_country(country, processor_entity)
    success = f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={processor}&plan_type=plus"
    return (
        f"https://checkout.stripe.com/c/pay/{cs_id}"
        f"?returned_from_redirect=true&ui_mode=custom&return_url={quote(success, safe='')}"
    )


def stripe_confirm_return_url(
    cs_id: str,
    checkout: Mapping[str, Any],
    stripe_hosted_url: str,
) -> str:
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "BR"),
        str(checkout.get("processor_entity") or ""),
    )
    verify_url = "https://chatgpt.com/checkout/verify?" + urlencode(
        {
            "stripe_session_id": cs_id,
            "processor_entity": processor,
            "plan_type": "plus",
        }
    )
    hosted_url = stripe_hosted_url or stripe_checkout_long_url(
        cs_id, str(checkout.get("billing_country") or "BR"), processor
    )
    parts = urlsplit(hosted_url)
    if parts.hostname != "checkout.stripe.com":
        return hosted_url
    query = urlencode(
        {
            "returned_from_redirect": "true",
            "ui_mode": "custom",
            "return_url": verify_url,
        }
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def stripe_confirm_pix(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    init_payload: dict[str, Any],
    ctx: dict[str, Any],
    checkout: dict[str, str],
    stripe_hosted_url: str,
    billing: dict[str, str],
    *,
    token_config: stripe_token.StripeTokenConfig | None,
    pm_id: str = "",
    confirm_mode: str = "inline",
    dump_stage: str = "pix_confirm",
) -> dict[str, Any]:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    mode = str(confirm_mode or "inline").strip().lower()
    if mode not in {"inline", "explicit"}:
        raise RuntimeError(f"未知 PIX confirm mode: {confirm_mode}")
    body = {
        "expected_amount": str(ctx.get("checkout_amount") or amount_from_payload(init_payload)),
        "expected_payment_method_type": "pix",
        "return_url": stripe_confirm_return_url(cs_id, checkout, stripe_hosted_url),
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
        "guid": str(ctx.get("guid") or stripe_browser_id()),
        "muid": str(ctx.get("muid") or stripe_browser_id()),
        "sid": str(ctx.get("sid") or stripe_browser_id()),
        "key": stripe_pk,
        "version": runtime_version,
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "client_attribution_metadata[client_session_id]": str(ctx.get("stripe_js_id") or ""),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[checkout_config_id]": (
            ctx.get("config_id") if mode == "explicit" else ctx.get("checkout_config_id")
        )
        or "",
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
    }
    body.update(stripe_elements_session_params(ctx))
    if mode == "explicit":
        if not pm_id.startswith("pm_"):
            raise RuntimeError("显式 PIX confirm 缺少有效 payment_method")
        body.update(
            {
                "payment_method": pm_id,
                "consent[terms_of_service]": "accepted",
                "elements_options_client[saved_payment_method][enable_save]": "never",
                "elements_options_client[saved_payment_method][enable_redisplay]": "never",
                "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
                "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
            }
        )
    else:
        if token_config is None:
            raise RuntimeError("Stripe 动态 token 配置缺失，停止发送不完整 PIX confirm")
        body.update(
            stripe_token.build_token_fields(
                ppage_id=str(init_payload.get("id") or ""),
                config=token_config,
            )
        )
        body.update(
            {
                "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
                "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
                "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
                "link_brand": str(
                    ((init_payload.get("link_settings") or {}).get("link_brand") or "link")
                    if isinstance(init_payload.get("link_settings"), dict)
                    else "link"
                ),
            }
        )
        add_inline_pix_payment_method_data(body, cs_id, billing, ctx)
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm"
    resp = stripe.post(
        url,
        data=body,
        headers=stripe_api_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    dump_http(resp, dump_stage, body, "POST", url, force=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"PIX confirm 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json() or {}


def collect_urls(payload: Any, urls: list[str] | None = None) -> list[str]:
    found = urls if urls is not None else []
    if isinstance(payload, str):
        for match in re.findall(r"https?://[^\s\"'<>]+", payload):
            found.append(match.rstrip("),.;]"))
        for match in re.findall(r"data:image/(?:png|svg\+xml|jpeg);base64,[A-Za-z0-9+/=]+", payload):
            found.append(match)
    elif isinstance(payload, dict):
        for value in payload.values():
            collect_urls(value, found)
    elif isinstance(payload, list):
        for item in payload:
            collect_urls(item, found)
    return found


def is_resource_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if is_known_static_host(url):
        return True
    return path.endswith(
        (
            ".js",
            ".css",
            ".map",
            ".woff",
            ".woff2",
            ".ttf",
            ".otf",
            ".ico",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".webp",
        )
    )


def is_known_static_host(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return host in {
        "stripe-camo.global.ssl.fastly.net",
        "files.stripe.com",
        "js.stripe.com",
        "m.stripe.network",
        "q.stripe.com",
    }


def is_redirect_like_url(url: str, from_action_field: bool = False) -> bool:
    if not isinstance(url, str):
        return False
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False
    if is_resource_url(url):
        return False
    if from_action_field:
        return True

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    text = f"{host}{path}?{query}"
    if host in {"hooks.stripe.com", "payments.stripe.com"}:
        return True
    return any(part in text for part in ("pix", "/redirect/", "redirect_to_url", "authenticate"))


def is_qr_candidate(url: str) -> bool:
    lower = url.lower()
    parsed = urlparse(url)
    if (parsed.netloc or "").lower() == "payments.stripe.com" and "/qr/instructions/" in (parsed.path or "").lower():
        return False
    return lower.startswith("data:image/") or "qr" in lower or "qrcode" in lower or "qr-code" in lower


def extract_qr_candidates(payload: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in collect_urls(payload):
        if url in seen:
            continue
        seen.add(url)
        if is_qr_candidate(url) and not is_known_static_host(url):
            result.append(url)
    return result


def find_submission_attempt(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        value = payload.get("submission_attempt")
        if isinstance(value, dict):
            return value
        for item in payload.values():
            nested = find_submission_attempt(item)
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = find_submission_attempt(item)
            if nested:
                return nested
    return {}


def extract_redirect_url(payload: Any, path: tuple[str, ...] = ()) -> str:
    if isinstance(payload, dict):
        next_action = payload.get("next_action")
        if isinstance(next_action, dict):
            redirect = next_action.get("redirect_to_url")
            if isinstance(redirect, dict):
                url = str(redirect.get("url") or "").strip()
                if is_redirect_like_url(url, True):
                    return url
            for key in ("url", "redirect_url", "redirect_to_url", "hosted_url", "hosted_instructions_url"):
                value = next_action.get(key)
                if is_redirect_like_url(value, True):
                    return value

        for key in (
            "redirect_url",
            "redirect_to_url",
            "authorization_url",
            "authentication_url",
            "hosted_instructions_url",
        ):
            value = payload.get(key)
            if is_redirect_like_url(value, True):
                return value

        for key, value in payload.items():
            nested = extract_redirect_url(value, path + (str(key),))
            if nested:
                return nested
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            nested = extract_redirect_url(item, path + (str(index),))
            if nested:
                return nested

    return ""


def first_value_by_key(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = first_value_by_key(value, key)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = first_value_by_key(item, key)
            if found not in (None, "", [], {}):
                return found
    return None


def find_intent_payload(payload: Any, intent_kind: str) -> Any:
    """Return the first SetupIntent/PaymentIntent object or id in a nested response."""
    if isinstance(payload, dict):
        payload_id = str(payload.get("id") or "").strip()
        payload_object = str(payload.get("object") or "").strip()
        prefix = "seti_" if intent_kind == "setup_intent" else "pi_"
        if payload_object == intent_kind or payload_id.startswith(prefix):
            return payload
        direct = payload.get(intent_kind)
        if isinstance(direct, dict) or (isinstance(direct, str) and direct.startswith(prefix)):
            return direct
        for value in payload.values():
            found = find_intent_payload(value, intent_kind)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_intent_payload(value, intent_kind)
            if found not in (None, "", [], {}):
                return found
    return None


def pix_intent_details(payload: Any) -> tuple[str, str]:
    for intent_kind in ("setup_intent", "payment_intent"):
        intent = find_intent_payload(payload, intent_kind)
        if intent not in (None, "", [], {}):
            status = str(intent.get("status") or "") if isinstance(intent, dict) else ""
            return intent_kind, status
    return "", ""


def pix_display_qr_details(payload: Any) -> dict[str, Any]:
    details = first_value_by_key(payload, "pix_display_qr_code")
    if not isinstance(details, dict):
        return {}
    hosted_url = str(details.get("hosted_instructions_url") or "").strip()
    qr_png = str(details.get("image_url_png") or "").strip()
    qr_svg = str(details.get("image_url_svg") or "").strip()
    try:
        expires_at = int(details.get("expires_at")) if details.get("expires_at") is not None else None
    except (TypeError, ValueError):
        expires_at = None
    return {
        "hosted_instructions_url": hosted_url,
        "image_url_png": qr_png,
        "image_url_svg": qr_svg,
        "expires_at": expires_at,
    }


def setup_intent_last_error(payload: Any, current_pm_id: str = "") -> str:
    if isinstance(payload, dict):
        payload_id = str(payload.get("id") or "").strip()
        is_setup_intent = payload.get("object") == "setup_intent" or payload_id.startswith("seti_")
        last_error = payload.get("last_setup_error") if is_setup_intent else None
        setup_intent = payload.get("setup_intent")
        if not last_error and isinstance(setup_intent, dict):
            last_error = setup_intent.get("last_setup_error")
        if last_error:
            if current_pm_id and isinstance(last_error, dict):
                error_pm = last_error.get("payment_method")
                error_pm_id = ""
                if isinstance(error_pm, dict):
                    error_pm_id = str(error_pm.get("id") or "").strip()
                elif isinstance(error_pm, str):
                    error_pm_id = error_pm.strip()
                if error_pm_id and error_pm_id != current_pm_id:
                    last_error = None
            if last_error:
                try:
                    return json.dumps(last_error, ensure_ascii=False)[:700]
                except Exception:
                    return str(last_error)[:700]
        for value in payload.values():
            found = setup_intent_last_error(value, current_pm_id=current_pm_id)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = setup_intent_last_error(value, current_pm_id=current_pm_id)
            if found:
                return found
    return ""


def raise_if_setup_intent_blocked(payload: Any, context: str, current_pm_id: str = "") -> None:
    last_error = setup_intent_last_error(payload, current_pm_id=current_pm_id)
    if not last_error:
        return
    if "generic_decline" in last_error.lower():
        raise RuntimeError(f"Stripe 风控拒绝（generic_decline）：{context} SetupIntent 创建失败，未生成 redirect_url")
    raise RuntimeError(f"{context}: setup_intent.last_setup_error: {last_error}")


def stripe_intent_redirect_url(
    stripe: requests.Session,
    intent_payload: Any,
    stripe_pk: str,
    current_pm_id: str = "",
) -> tuple[str, dict[str, Any]]:
    if not isinstance(intent_payload, dict):
        return "", {}
    intent_id = str(intent_payload.get("id") or "").strip()
    client_secret = str(intent_payload.get("client_secret") or "").strip()
    if not intent_id or not client_secret:
        return "", {}
    intent_object = str(intent_payload.get("object") or "").strip()
    intent_path = "setup_intents" if intent_object == "setup_intent" or intent_id.startswith("seti_") else "payment_intents"
    params = {"key": stripe_pk, "client_secret": client_secret}
    url = f"https://api.stripe.com/v1/{intent_path}/{intent_id}"
    resp = stripe.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    dump_http(resp, "stripe_intent_get", params, "GET", url, force=resp.status_code >= 400)
    if resp.status_code != 200:
        return "", {}
    try:
        payload = resp.json() or {}
    except Exception:
        payload = {"_raw_text": resp.text}
    raise_if_setup_intent_blocked(payload, "stripe intent", current_pm_id=current_pm_id)
    redirect_url = extract_redirect_url(payload)
    if redirect_url:
        dump_http(resp, "stripe_intent_redirect", params, "GET", url, force=True)
        log(f"读取 Stripe intent 拿到 redirect_url: {redirect_url[:180]}")
    return redirect_url, payload


def stripe_payload_intent_redirect_url(
    stripe: requests.Session,
    payload: Any,
    stripe_pk: str,
    current_pm_id: str = "",
) -> tuple[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return "", {}
    for intent_key in ("setup_intent", "payment_intent"):
        candidates: list[Any] = []
        direct = payload.get(intent_key)
        if isinstance(direct, dict):
            candidates.append(direct)
        nested = first_value_by_key(payload, intent_key)
        if isinstance(nested, dict) and all(nested is not item for item in candidates):
            candidates.append(nested)
        for intent_payload in candidates:
            redirect_url, intent_result = stripe_intent_redirect_url(
                stripe,
                intent_payload,
                stripe_pk,
                current_pm_id=current_pm_id,
            )
            if redirect_url:
                return redirect_url, intent_result
    return "", {}


def infer_processor_entity(payload: Any) -> str:
    for value in collect_strings(payload):
        match = re.search(r"[?&]processor_entity=([A-Za-z0-9_]+)", value)
        if match:
            return match.group(1)
    return ""


def payment_page_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    elements_options = payload.get("elements_options") if isinstance(payload.get("elements_options"), dict) else {}
    submission = find_submission_attempt(payload)
    next_action = first_value_by_key(payload, "next_action")
    payment_intent = first_value_by_key(payload, "payment_intent")
    setup_intent = first_value_by_key(payload, "setup_intent")
    summary: dict[str, Any] = {
        "object": payload.get("object"),
        "id": payload.get("id"),
        "status": payload.get("status"),
        "payment_status": payload.get("payment_status"),
        "amount": elements_options.get("amount") if elements_options else first_value_by_key(payload, "amount"),
        "currency": payload.get("currency") or (elements_options.get("currency") if elements_options else None),
        "mode": elements_options.get("mode") if elements_options else payload.get("mode"),
        "payment_method_types": elements_options.get("payment_method_types") if elements_options else None,
        "submission_state": submission.get("state") if submission else None,
        "submission_status": submission.get("status") if submission else None,
        "has_next_action": isinstance(next_action, dict) and bool(next_action),
    }
    if isinstance(payment_intent, dict):
        summary["payment_intent_status"] = payment_intent.get("status")
    elif isinstance(payment_intent, str):
        summary["payment_intent"] = payment_intent
    if isinstance(setup_intent, dict):
        summary["setup_intent_status"] = setup_intent.get("status")
    elif isinstance(setup_intent, str):
        summary["setup_intent"] = setup_intent
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def log_payment_page_summary(stage: str, payload: Any) -> None:
    summary = payment_page_summary(payload)
    if not summary:
        return
    compact = format_payment_summary(summary)
    log(f"{stage} 返回摘要: {compact}")


def format_payment_summary(summary: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in summary.items())


def warmup_approve_context(
    chatgpt: requests.Session,
    checkout_page_url: str,
    device_id: str,
    fingerprint: Mapping[str, Any] | None = None,
) -> str:
    fp = fingerprint if isinstance(fingerprint, Mapping) else {}
    try:
        sentinel_token, oai_sc = build_sentinel_token(
            chatgpt,
            device_id,
            "checkout_session_approval",
            user_agent=str(fp.get("user-agent") or chatgpt.headers.get("User-Agent") or ""),
            sec_ch_ua=str(fp.get("sec-ch-ua") or chatgpt.headers.get("sec-ch-ua") or ""),
        )
        if not oai_sc:
            raise RuntimeError("Sentinel did not return oai-sc")
        chatgpt.cookies.set("oai-sc", oai_sc, domain=".chatgpt.com", path="/")
        log(f"checkout Sentinel 已就绪: flow=checkout_session_approval, oai-sc-len={len(oai_sc)}")
        resp = chatgpt.post(
            "https://chatgpt.com/backend-api/sentinel/ping",
            headers={
                "Referer": checkout_page_url,
            },
            timeout=env_int("PIX_APPROVE_REQUEST_TIMEOUT", 30),
        )
        dump_http(
            resp,
            "sentinel_ping",
            None,
            "POST",
            "https://chatgpt.com/backend-api/sentinel/ping",
            force=env_bool("PIX_DUMP_WARMUP", False) or resp.status_code >= 400,
        )
        raise_if_cloudflare_challenge(resp, "sentinel/ping")
        if resp.status_code >= 400:
            raise RuntimeError(f"sentinel/ping HTTP {resp.status_code}: {resp.text[:180]}")
        return sentinel_token
    except Exception as exc:
        log(f"approve sentinel 请求异常: {exc}", "[WARN] ")
        return ""


def extract_web_deployment_attestation(page_html: str) -> str:
    text = str(page_html or "")
    patterns = (
        r'"webDeploymentAttestation"\s*:\s*"([^"\\]+)"',
        r'\\"webDeploymentAttestation\\"\s*:\s*\\"([^"\\]+)\\"',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return ""


def chatgpt_approve(
    chatgpt: requests.Session,
    checkout: dict[str, str],
    device_id: str,
    fingerprint: Mapping[str, Any] | None = None,
) -> None:
    cs_id = checkout["cs_id"]
    processor = processor_entity_for_country(checkout.get("billing_country", "BR"), checkout.get("processor_entity", ""))
    checkout_page_url = f"https://chatgpt.com/checkout/{processor}/{cs_id}"
    sentinel_token = ""
    deployment_attestation = ""
    if env_bool("PIX_APPROVE_WARMUP", True):
        try:
            page_response = chatgpt.get(
                checkout_page_url,
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                timeout=env_int("PIX_APPROVE_REQUEST_TIMEOUT", 30),
            )
            deployment_attestation = extract_web_deployment_attestation(
                str(getattr(page_response, "text", "") or "")
            )
        except Exception as exc:
            log(f"approve checkout 预热异常: {exc}", "[WARN] ")
        sentinel_token = warmup_approve_context(
            chatgpt,
            checkout_page_url,
            device_id,
            fingerprint,
        )

    body = {"checkout_session_id": cs_id, "processor_entity": processor}
    headers = {
        "Referer": checkout_page_url,
        "x-openai-target-path": "/backend-api/payments/checkout/approve",
        "x-openai-target-route": "/backend-api/payments/checkout/approve",
        "x-oai-is-pending-updates": '{"v":3,"updates":[]}',
    }
    if sentinel_token:
        headers["OpenAI-Sentinel-Token"] = sentinel_token
    if deployment_attestation:
        headers["oai-web-deployment-attestation"] = deployment_attestation
    resp = chatgpt.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json=body,
        headers=headers,
        timeout=env_int("PIX_APPROVE_REQUEST_TIMEOUT", 30),
    )
    dump_http(resp, "approve", body, "POST", "https://chatgpt.com/backend-api/payments/checkout/approve", force=True)
    raise_if_cloudflare_challenge(resp, "checkout/approve")
    if resp.status_code >= 400:
        raise RuntimeError(f"ChatGPT approve 失败 HTTP {resp.status_code}: {resp.text[:300]}")
    result = ""
    try:
        result = str((resp.json() or {}).get("result") or "")
    except Exception:
        pass
    if result != "approved":
        raise RuntimeError(f"ChatGPT approve 未通过: {result or resp.text[:200]}")


def approve_attempt(
    access_token: str,
    device_id: str,
    checkout: dict[str, str],
    session_token: str,
    proxy: str,
    index: int,
    attempt_count: int,
    chatgpt_session: requests.Session | None = None,
    cookie_header: str = "",
    fingerprint: Mapping[str, Any] | None = None,
) -> None:
    log(f"approve 第 {index}/{attempt_count} 次 / proxy={proxy_label(proxy)}")
    chatgpt = chatgpt_session or build_chatgpt_session(
        access_token,
        device_id,
        proxy,
        session_token,
        cookie_header,
        fingerprint,
    )
    chatgpt_approve(chatgpt, checkout, device_id, fingerprint)


def log_approve_failure(error: str) -> bool:
    log(f"approve 失败: {error[:180]}", "[WARN] ")
    if "ChatGPT approve 未通过: blocked" in error:
        log("approve 返回 blocked，按账号/checkout 风控处理，不记录代理失败", "[WARN] ")
        return True
    return False


def is_approve_failure_error(error: str) -> bool:
    text = str(error or "").lower()
    return "approve" in text or "chatgpt approve" in text


def approve_with_retry(
    access_token: str,
    device_id: str,
    checkout: dict[str, str],
    proxies: list[str],
    session_token: str,
    proxy_group: str = "provider",
    initial_session: requests.Session | None = None,
    initial_proxy: str = "",
    cookie_header: str = "",
    fingerprint: Mapping[str, Any] | None = None,
    progress: Any | None = None,
    max_retry: int | None = None,
) -> str:
    max_retry = max(1, int(max_retry)) if max_retry is not None else env_int("PIX_APPROVE_RETRY_MAX", 5)
    last_error = ""
    if max_retry <= 0:
        raise RuntimeError("approve 重试次数必须大于 0")
    proxies = [proxy for proxy in dict.fromkeys(proxies) if proxy]
    if not proxies:
        raise RuntimeError("approve 代理为空")
    selected_proxies = [proxies[0]]
    for index in range(1, max_retry):
        selected_proxies.append(proxies[index % len(proxies)])
    attempt_count = len(selected_proxies)
    log(f"approve 重试策略: attempts={attempt_count}, concurrency=1")
    blocked_count = 0
    for index, proxy in enumerate(selected_proxies, start=1):
        emit_progress(progress, "confirm", f"方案1：ChatGPT approve {index}/{attempt_count}")
        try:
            reusable_session = (
                initial_session
                if normalize_proxy_url(proxy) == normalize_proxy_url(initial_proxy)
                else None
            )
            approve_attempt(
                access_token,
                device_id,
                checkout,
                session_token,
                proxy,
                index,
                attempt_count,
                reusable_session,
                cookie_header,
                fingerprint,
            )
            log("approve 成功")
            return proxy
        except Exception as exc:
            last_error = str(exc)
            if is_checkout_not_active_error(last_error):
                raise RuntimeError("checkout_not_active_session")
            if "invalid_promotion" in last_error.lower():
                raise RuntimeError("approve invalid_promotion")
            if log_approve_failure(last_error):
                blocked_count += 1
            if index < attempt_count:
                emit_progress(
                    progress,
                    "confirm",
                    f"方案1：approve {index}/{attempt_count} 未通过，1 秒后重试当前 sticky 出口",
                )
                time.sleep(1)
    if blocked_count and blocked_count == attempt_count:
        raise RuntimeError("approve blocked")
    raise RuntimeError(f"approve 重试失败: {last_error}")


def poll_payment_page(
    stripe: requests.Session,
    checkout: dict[str, str],
    stripe_pk: str,
    ctx: dict[str, Any],
    current_pm_id: str = "",
    progress: Any | None = None,
) -> tuple[str, list[str], dict[str, Any]]:
    cs_id = checkout["cs_id"]
    deadline = time.time() + env_int("PIX_POLL_TIMEOUT", 45)
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[locale]": payment_elements_locale(),
        "key": stripe_pk,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
    }
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}"
    last_error = ""
    last_payload: dict[str, Any] = {}
    last_summary = ""
    poll_attempt = 0
    while time.time() < deadline:
        poll_attempt += 1
        if poll_attempt == 1 or poll_attempt % 5 == 0:
            emit_progress(progress, "poll", f"方案1：轮询 Pix instructions，第 {poll_attempt} 次")
        resp = stripe.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        if resp.status_code >= 400:
            dump_http(resp, "poll_error", params, "GET", url, force=True)
            if is_checkout_not_active_error(resp.text):
                raise RuntimeError("checkout_not_active_session")
            last_error = f"HTTP {resp.status_code}"
            time.sleep(1)
            continue
        try:
            payload = resp.json() or {}
        except Exception:
            payload = {"_raw_text": resp.text}
        raise_if_setup_intent_blocked(payload, "stripe payment_pages", current_pm_id=current_pm_id)
        last_payload = payload
        summary = payment_page_summary(payload)
        summary_text = format_payment_summary(summary) if summary else ""
        if summary_text and summary_text != last_summary:
            last_summary = summary_text
            log(f"poll 返回摘要: {summary_text}")
        redirect_url = extract_redirect_url(payload)
        qr_urls = extract_qr_candidates(payload)
        if redirect_url or qr_urls:
            dump_http(resp, "poll_success", params, "GET", url, force=True)
            return redirect_url, qr_urls, payload
        intent_redirect, intent_payload = stripe_payload_intent_redirect_url(
            stripe,
            payload,
            stripe_pk,
            current_pm_id=current_pm_id,
        )
        if intent_redirect:
            dump_http(resp, "poll_success", params, "GET", url, force=True)
            return intent_redirect, list(dict.fromkeys(qr_urls + extract_qr_candidates(intent_payload))), intent_payload
        submission = find_submission_attempt(payload)
        if submission.get("state") == "requires_approval":
            last_error = "payment_pages 仍然 requires_approval"
            time.sleep(1)
            continue
        if submission.get("state") == "failed":
            dump_http(resp, "poll_failed", params, "GET", url, force=True)
            raise RuntimeError(f"Stripe submission failed: {submission}")
        last_error = str(submission or "waiting")
        time.sleep(1)
    if last_payload:
        dump_response = type("DumpResponse", (), {})()
        dump_response.status_code = 200
        dump_response.url = url
        dump_response.text = json.dumps(last_payload, ensure_ascii=False, indent=2)
        dump_http(dump_response, "poll_no_redirect", params, "GET", url, force=True)
    log(f"poll 结束，未发现真实 PIX redirect/QR: {last_error}", "[WARN] ")
    raise RuntimeError(f"redirect url resolution timeout: {last_error}")


def fetch_redirect_page(stripe: requests.Session, start_url: str) -> list[str]:
    if not start_url or not env_bool("PIX_FOLLOW_REDIRECT", True):
        return []
    current = start_url
    qr_urls: list[str] = []
    for hop in range(1, 6):
        resp = stripe.get(current, timeout=DEFAULT_TIMEOUT, allow_redirects=False)
        dump_http(resp, f"redirect_hop_{hop}", None, "GET", current, force=True)
        qr_urls.extend(extract_qr_candidates(resp.text))
        location = resp.headers.get("location") or resp.headers.get("Location") or ""
        if not location:
            break
        current = urljoin(current, location)
    return list(dict.fromkeys(qr_urls))


def resolve_external_redirect(stripe: requests.Session, start_url: str) -> str:
    if not start_url or not env_bool("PIX_FOLLOW_REDIRECT", True):
        return start_url
    current = start_url
    for hop in range(1, 6):
        host = (urlparse(current).netloc or "").lower()
        try:
            resp = stripe.get(current, timeout=DEFAULT_TIMEOUT, allow_redirects=False)
            dump_http(resp, f"resolve_redirect_hop_{hop}", None, "GET", current, force=True)
        except Exception as exc:
            log(f"跟随 redirect 异常: {exc}", "[WARN] ")
            return current
        location = resp.headers.get("location") or resp.headers.get("Location") or ""
        if not location:
            return current
        current = urljoin(current, location)
    return current


def fetch_pix_instruction_details(stripe: requests.Session, url: str) -> dict[str, Any]:
    """Best-effort metadata supplement for a valid Stripe-hosted Pix page."""
    try:
        resp = stripe.get(url, timeout=DEFAULT_TIMEOUT)
    except Exception as exc:
        log(f"Pix instructions 元数据请求异常: {exc}", "[WARN] ")
        return {}
    if resp.status_code >= 400:
        log(f"Pix instructions 元数据请求失败 HTTP {resp.status_code}", "[WARN] ")
        return {}
    urls = extract_qr_candidates(resp.text)
    png = next((item for item in urls if urlparse(item).path.lower().endswith(".png")), "")
    svg = next((item for item in urls if urlparse(item).path.lower().endswith(".svg")), "")
    expires_match = re.search(r"(?i)expires_at[\"'\s:=]+([0-9]{8,})", resp.text)
    return {
        "image_url_png": png,
        "image_url_svg": svg,
        "expires_at": int(expires_match.group(1)) if expires_match else None,
        "qr_urls": urls,
    }


def approve_proxy_candidates(checkout_proxy: str, provider_proxy: str, approve_pool: list[str]) -> list[str]:
    sticky_proxy = checkout_proxy or provider_proxy
    if not sticky_proxy:
        return []
    if provider_proxy and normalize_proxy_url(provider_proxy) != normalize_proxy_url(sticky_proxy):
        log("approve 强制沿用 Checkout sticky 出口，忽略不同的 Provider 代理", "[WARN] ")
    return [sticky_proxy]


def resolve_confirm_payload_pix(
    stripe: requests.Session,
    confirm_payload: dict[str, Any],
    checkout: dict[str, str],
    stripe_pk: str,
    ctx: dict[str, Any],
    pm_id: str,
    access_token: str,
    device_id: str,
    session_token: str,
    checkout_proxy: str,
    provider_proxy: str,
    approve_pool: list[str],
    chatgpt_session: requests.Session | None = None,
    cookie_header: str = "",
    fingerprint: Mapping[str, Any] | None = None,
    progress: Any | None = None,
) -> tuple[str, list[str], str, dict[str, Any]]:
    raise_if_setup_intent_blocked(confirm_payload, "stripe confirm", current_pm_id=pm_id)
    redirect_url = extract_redirect_url(confirm_payload)
    resolved_payload = confirm_payload
    if not redirect_url:
        redirect_url, intent_payload = stripe_payload_intent_redirect_url(
            stripe,
            confirm_payload,
            stripe_pk,
            current_pm_id=pm_id,
        )
        if intent_payload:
            resolved_payload = intent_payload
    qr_urls = extract_qr_candidates(confirm_payload)
    submission = find_submission_attempt(confirm_payload)

    if redirect_url:
        log(f"confirm 提取到最终支付 URL: {redirect_url[:180]}")
    if qr_urls:
        log(f"confirm 提取到 QR 候选 {len(qr_urls)} 个")

    approve_proxy = ""
    if not redirect_url and submission.get("state") == "requires_approval":
        log("需要 ChatGPT approve...")
        emit_progress(progress, "confirm", "方案1 8/8：等待 ChatGPT approve")
        approve_proxies = approve_proxy_candidates(checkout_proxy, provider_proxy, approve_pool)
        confirm_amount = int(ctx.get("checkout_amount") or 0)
        log(
            f"需要 approve：PIX BRL {confirm_amount / 100:.2f} 场景，"
            "全程沿用当前 Checkout 的 BR sticky 出口。"
        )
        approve_proxy = approve_with_retry(
            access_token,
            device_id,
            checkout,
            approve_proxies,
            session_token,
            "provider",
            initial_session=chatgpt_session,
            initial_proxy=checkout_proxy,
            cookie_header=cookie_header,
            fingerprint=fingerprint,
            progress=progress,
        )
        log("跟随跳转提取最终链...")
        emit_progress(progress, "poll", "方案1 8/8：approve 已通过，轮询 Pix instructions")
        redirect_url, poll_qr, resolved_payload = poll_payment_page(
            stripe,
            checkout,
            stripe_pk,
            ctx,
            current_pm_id=pm_id,
            progress=progress,
        )
        qr_urls.extend(poll_qr)
    elif not redirect_url and not qr_urls:
        log("confirm 未返回真实 PIX redirect/QR，继续 poll payment_pages 做最终确认", "[WARN] ")
        emit_progress(progress, "poll", "方案1 8/8：confirm 已完成，轮询 Pix instructions")
        redirect_url, poll_qr, resolved_payload = poll_payment_page(
            stripe,
            checkout,
            stripe_pk,
            ctx,
            current_pm_id=pm_id,
            progress=progress,
        )
        qr_urls.extend(poll_qr)

    return redirect_url, list(dict.fromkeys(qr_urls)), approve_proxy, resolved_payload


def run_provider_flow(
    access_token: str,
    session_token: str,
    checkout_proxy: str,
    promotion_proxy: str,
    provider_proxy: str,
    approve_pool: list[str],
    device_id: str,
    checkout: dict[str, str],
    billing: dict[str, str],
    stop_event: Event | None = None,
    chatgpt_session: requests.Session | None = None,
    cookie_header: str = "",
    fingerprint: Mapping[str, Any] | None = None,
    progress: Any | None = None,
) -> PixFlowResult:
    checkout_country = normalize_country(os.environ.get("PIX_CHECKOUT_COUNTRY", PIX_BOOTSTRAP_COUNTRY))
    stripe_pk = checkout.get("stripe_pk") or DEFAULT_STRIPE_PK
    target_amount = pix_target_amount_minor()

    def amount_is_eligible(value: int) -> bool:
        return pix_amount_is_eligible(value)

    def inspect_init(
        payload: dict[str, Any], stage: str
    ) -> tuple[dict[str, Any], int, list[str] | None]:
        current_ctx = build_ctx(payload, checkout)
        current_amount = int(current_ctx.get("checkout_amount") or 0)
        amount_major = current_amount / 100
        log(f"{stage} Stripe init 成功, 金额={checkout['currency']} {amount_major:.2f}")
        payment_method_types = first_value_by_key(payload, "payment_method_types")
        methods: list[str] | None = None
        if isinstance(payment_method_types, list):
            methods = [str(item).lower() for item in payment_method_types]
            log(f"Stripe 可用支付方式: {methods}")
        return current_ctx, current_amount, methods

    emit_progress(progress, "stripe_init", "方案1 2/8：初始化 BR Stripe Checkout")
    log(
        f"{PIX_BOOTSTRAP_COUNTRY} Bootstrap Stripe init "
        f"(PM={billing['country']}, proxy={proxy_label(checkout_proxy)})..."
    )
    init_payload = stripe_init(checkout["cs_id"], stripe_pk, checkout_proxy)
    if not checkout.get("processor_entity"):
        processor_entity = infer_processor_entity(init_payload)
        if processor_entity:
            checkout["processor_entity"] = processor_entity
            log(f"从 Stripe init 推断 processor_entity={processor_entity}")
    _bootstrap_ctx, bootstrap_amount, bootstrap_methods = inspect_init(
        init_payload, f"{PIX_BOOTSTRAP_COUNTRY} Bootstrap"
    )
    if bootstrap_methods is not None and "pix" not in bootstrap_methods:
        raise RuntimeError(
            f"{PIX_UNAVAILABLE_ERROR}: BR bootstrap amount={bootstrap_amount}; "
            f"payment_method_types={bootstrap_methods}"
        )
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")

    hosted_url = ""
    ctx: dict[str, Any] = {}
    amount = 0
    zero_setup_intent = False
    stripe = new_session(provider_proxy)
    stripe.headers.update(
        {"User-Agent": random_user_agent(), "Accept-Language": payment_accept_language()}
    )
    for promotion_index, promotion_country in enumerate(PIX_PROMOTION_COUNTRIES, start=1):
        stage_label = f"{promotion_country} checkout/update {promotion_index}/{len(PIX_PROMOTION_COUNTRIES)}"
        emit_progress(
            progress,
            "promotion",
            f"方案1 3/8：应用 {promotion_country} checkout 优惠 {promotion_index}/{len(PIX_PROMOTION_COUNTRIES)}",
        )
        try:
            current_promotion_proxy = proxy_for_country(promotion_proxy, promotion_country)
        except RuntimeError as exc:
            if "代理未包含可改写的 country/region 选择器" not in str(exc):
                raise
            current_promotion_proxy = normalize_proxy_url(promotion_proxy)
            log(
                f"{stage_label}: 显式地区代理无 selector，按传入出口原样使用 "
                f"{proxy_label(current_promotion_proxy)}",
                "[WARN] ",
            )
        log(f"{stage_label}: proxy={proxy_label(current_promotion_proxy)}")
        try:
            promotion_chatgpt = chatgpt_session_for_proxy(
                chatgpt_session,
                checkout_proxy,
                current_promotion_proxy,
                access_token,
                device_id,
                session_token,
                cookie_header,
                fingerprint,
            )
            update_checkout_promotion(promotion_chatgpt, checkout, promotion_country)
        except Exception as exc:
            if is_checkout_not_active_error(exc):
                raise
            raise RuntimeError(f"promotion 阶段失败: {exc}") from exc
        record_proxy_result("promotion", current_promotion_proxy, True, "promotion_update_success")

        log(
            f"{stage_label} 后通过 {PIX_PROVIDER_COUNTRY} 刷新 Stripe: "
            f"proxy={proxy_label(provider_proxy)}"
        )
        init_payload = stripe_init(
            checkout["cs_id"], stripe_pk, provider_proxy, session=stripe
        )
        hosted_url = str(init_payload.get("stripe_hosted_url") or hosted_url or "")
        ctx, amount, methods = inspect_init(
            init_payload, f"{promotion_country} 更新后 {PIX_PROVIDER_COUNTRY}"
        )
        if methods is not None and "pix" not in methods:
            raise RuntimeError(
                f"{PIX_UNAVAILABLE_ERROR}: BR promotion refresh amount={amount}; "
                f"payment_method_types={methods}"
            )
        record_checkout_zero_result(checkout_proxy, checkout_country, amount)
        if amount_is_eligible(amount):
            zero_setup_intent = amount == 0
            if zero_setup_intent:
                log(
                    "Promotion 后金额命中 BRL 0.00，进入 PIX SetupIntent 授权流程；"
                    "先同步 BR 税区再复查支付方式"
                )
            else:
                log(
                    f"Promotion 后金额命中 BRL {amount / 100:.2f}，"
                    "先同步 BR 税区再复查支付方式"
                )
            break
        if promotion_index < len(PIX_PROMOTION_COUNTRIES):
            log(
                f"{promotion_country} 更新后金额超过 BRL {target_amount / 100:.2f}，"
                "继续下一段 checkout/update",
                "[WARN] ",
            )
            continue
        raise RuntimeError(
            f"PIX 金额策略未命中：允许金额小单位<= {target_amount}，"
            f"当前金额小单位={amount}，已停止生成支付链"
        )

    log(
        f"{PIX_PROVIDER_COUNTRY} 最终复查通过: amount={amount}, "
        f"flow={'setup_intent' if zero_setup_intent else 'payment_intent'}, methods={methods}"
    )

    emit_progress(progress, "stripe_elements", "方案1 4/8：创建 Stripe Elements 会话")
    elements_payload = stripe_elements_session(
        stripe, checkout["cs_id"], stripe_pk, ctx
    )
    log(
        "Stripe elements/sessions 成功: "
        f"session={str(elements_payload.get('session_id') or '')[:18]}..."
    )

    snapshot_chatgpt = chatgpt_session_for_proxy(
        chatgpt_session,
        checkout_proxy,
        provider_proxy,
        access_token,
        device_id,
        session_token,
        cookie_header,
        fingerprint,
    )
    emit_progress(progress, "stripe_tax", "方案1 5/8：同步 BR 税区与账单快照")
    log(f"按网页顺序同步 {PIX_PROVIDER_COUNTRY} Stripe tax_region 与 checkout snapshot...")
    stripe_update_tax_region(
        stripe,
        checkout["cs_id"],
        stripe_pk,
        ctx,
        billing,
        country_only=True,
    )
    stripe_update_tax_region(
        stripe,
        checkout["cs_id"],
        stripe_pk,
        ctx,
        billing,
    )
    checkout_snapshot(snapshot_chatgpt, checkout, billing)

    requested_confirm_mode = pix_confirm_mode()
    effective_confirm_mode = requested_confirm_mode
    token_config: stripe_token.StripeTokenConfig | None = None
    emit_progress(progress, "stripe_token", "方案1 6/8：读取 Stripe 运行时配置")
    try:
        token_config = stripe_token.extract_config_live(
            stripe,
            log=lambda message: log(message),
            user_agent=random_user_agent(),
            accept_language=payment_accept_language(),
        )
        ctx["runtime_version"] = token_config.runtime_version
        log(
            "Stripe 动态 token 配置成功: "
            f"runtime={token_config.runtime_version}, shift={token_config.shift}, "
            f"bundle={token_config.bundle_hash[:12]}"
        )
    except Exception as exc:
        if requested_confirm_mode != "explicit":
            raise
        log(
            f"显式 PIX PM 不依赖动态 token，提取失败后使用 runtime fallback: {str(exc)[:180]}",
            "[WARN] ",
        )

    pm_id = ""
    if requested_confirm_mode in {"explicit", "auto"}:
        try:
            emit_progress(progress, "payment_method", "方案1 6/8：创建 Pix PaymentMethod")
            log(
                f"创建独立 PIX PM: {billing['name']} / {billing['city']} "
                f"{billing['postal_code']}"
            )
            pm_id = stripe_create_pix_payment_method(
                stripe,
                checkout["cs_id"],
                stripe_pk,
                billing,
                ctx,
            )
            effective_confirm_mode = "explicit"
            log(f"独立 PIX PM 创建成功: {pm_id[:18]}...")
        except Exception as exc:
            if requested_confirm_mode != "auto":
                raise
            effective_confirm_mode = "inline"
            log(f"独立 PIX PM 创建失败，回退内联协议: {str(exc)[:180]}", "[WARN] ")

    if effective_confirm_mode == "inline":
        log(
            f"PIX confirm 内联资料: {billing['name']} / "
            f"{billing['line1']} / {billing['city']} "
            f"{normalize_br_postal_code(billing['postal_code'])}"
        )

    emit_progress(progress, "confirm", f"方案1 7/8：提交 Stripe confirm（{effective_confirm_mode}）")
    log(f"Stripe confirm (expected=PIX, mode={effective_confirm_mode})...")
    confirm_payload = stripe_confirm_pix(
        stripe,
        checkout["cs_id"],
        stripe_pk,
        init_payload,
        ctx,
        checkout,
        hosted_url,
        billing,
        token_config=token_config,
        pm_id=pm_id,
        confirm_mode=effective_confirm_mode,
    )
    log("Stripe confirm 成功, 解析跳转...")
    log_payment_page_summary("confirm", confirm_payload)
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")

    approve_proxy = ""
    qr_urls: list[str] = []
    redirect_url, qr_urls, approve_proxy, resolved_payload = resolve_confirm_payload_pix(
        stripe,
        confirm_payload,
        checkout,
        stripe_pk,
        ctx,
        pm_id,
        access_token,
        device_id,
        session_token,
        checkout_proxy,
        provider_proxy,
        approve_pool,
        chatgpt_session,
        cookie_header,
        fingerprint,
        progress=progress,
    )

    qr_details = pix_display_qr_details(resolved_payload) or pix_display_qr_details(confirm_payload)
    intent_kind, intent_status = pix_intent_details(resolved_payload)
    if not intent_kind:
        intent_kind, intent_status = pix_intent_details(confirm_payload)
    hosted_instructions_url = str(qr_details.get("hosted_instructions_url") or "")
    qr_png = str(qr_details.get("image_url_png") or "")
    qr_svg = str(qr_details.get("image_url_svg") or "")
    qr_urls.extend(url for url in (qr_png, qr_svg) if url)
    if hosted_instructions_url:
        redirect_url = hosted_instructions_url

    if redirect_url and approve_proxy:
        record_proxy_pair_approve_success(checkout_proxy, provider_proxy, approve_proxy)
        log("完成 - 已记忆此 checkout/provider/approve combo")

    if redirect_url:
        final_url = resolve_external_redirect(stripe, redirect_url)
        if final_url and final_url != redirect_url:
            log(f"跟随 redirect 得到最终链: {final_url[:180]}")
            redirect_url = final_url

    if is_pix_instructions_url(redirect_url):
        emit_progress(progress, "extract", "方案1 8/8：已取得 Pix instructions，补全二维码信息")
        supplemental = fetch_pix_instruction_details(stripe, redirect_url)
        qr_urls.extend(supplemental.get("qr_urls") or [])
        qr_png = qr_png or str(supplemental.get("image_url_png") or "")
        qr_svg = qr_svg or str(supplemental.get("image_url_svg") or "")
        if qr_details.get("expires_at") is None and supplemental.get("expires_at") is not None:
            qr_details["expires_at"] = supplemental["expires_at"]

    return PixFlowResult(
        redirect_url=redirect_url,
        qr_urls=list(dict.fromkeys(qr_urls)),
        amount_minor=amount,
        intent_kind=intent_kind,
        intent_status=intent_status,
        qr_png=qr_png,
        qr_svg=qr_svg,
        expires_at=qr_details.get("expires_at"),
    )


def run_once(
    access_token: str,
    session_token: str,
    checkout_proxy: str,
    promotion_proxy: str,
    provider_proxy: str,
    approve_pool: list[str],
    attempt: int,
    max_retry: int,
    stop_event: Event | None = None,
) -> PixFlowResult:
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")
    device_id = str(uuid.uuid4())
    checkout_country = normalize_country(os.environ.get("PIX_CHECKOUT_COUNTRY", PIX_BOOTSTRAP_COUNTRY))
    billing = pix_billing_profile()
    log(f"开始 PIX 提取，第 {attempt}/{max_retry} 次")
    log(
        "组合测试: "
        f"{checkout_country} / {billing['country']} / {currency_for_country(checkout_country)} / "
        f"{payment_browser_locale()} / "
        f"{os.environ.get('PIX_PROVIDER_COUNTRY_LABEL', PIX_PROVIDER_COUNTRY).strip() or PIX_PROVIDER_COUNTRY}"
    )
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")

    try:
        proxy_seed = checkout_proxy
        checkout_proxy, promotion_proxy, provider_proxy = pix_proxy_chain(proxy_seed)
        log_pix_proxy_chain(proxy_seed, checkout_proxy, promotion_proxy, provider_proxy)
        log(f"本轮代理: checkout/资格={proxy_label(checkout_proxy)}；Stripe/PIX={proxy_label(provider_proxy)}")
        zero_status, zero_amount, _zero_checked_at = checkout_zero_cache_status(checkout_proxy, checkout_country)
        if zero_status == "ok":
            log(f"checkout 目标金额缓存命中: amount={zero_amount}")
        elif zero_status == "bad":
            log(f"checkout 目标金额失败缓存命中: 上次 amount={zero_amount}，本轮继续验证", "[WARN] ")
        chatgpt = build_chatgpt_session(access_token, device_id, checkout_proxy, session_token)
        checkout = create_checkout(chatgpt, checkout_country)
    except Exception as exc:
        if is_user_already_paid_error(exc):
            raise RuntimeError("用户已支付: User is already paid") from exc
        if is_checkout_not_active_error(exc):
            raise
        raise RuntimeError(f"checkout 阶段失败: {exc}") from exc
    return run_provider_flow(
        access_token,
        session_token,
        checkout_proxy,
        promotion_proxy,
        provider_proxy,
        [provider_proxy],
        device_id,
        checkout,
        billing,
        stop_event,
        chatgpt_session=chatgpt,
    )


def run_attempt(
    access_token: str,
    session_token: str,
    checkout_proxy: str,
    promotion_proxy: str,
    provider_proxy: str,
    approve_pool: list[str],
    attempt: int,
    max_retry: int,
    stop_event: Event | None = None,
    batch_no: int = 0,
    batch_total: int = 0,
) -> tuple[int, str, list[str], str, str, str]:
    previous_log_context = getattr(_log_context, "prefix", "")
    if batch_no > 0:
        _log_context.prefix = f"[批次 {batch_no}/{batch_total or '?'}][轮次 {attempt}/{max_retry}] "
    else:
        _log_context.prefix = f"[轮次 {attempt}/{max_retry}] "
    try:
        outcome = run_once(
            access_token,
            session_token,
            checkout_proxy,
            promotion_proxy,
            provider_proxy,
            approve_pool,
            attempt,
            max_retry,
            stop_event,
        )
        redirect_url = outcome.redirect_url
        qr_urls = outcome.qr_urls
        has_result = bool(redirect_url)
        if has_result and stop_event:
            stop_event.set()
        if has_result:
            record_proxy_pair_result(checkout_proxy, provider_proxy, True, "success")
        else:
            record_proxy_result("provider", provider_proxy, False, "no_redirect_url")
        _log_context.prefix = previous_log_context
        return attempt, redirect_url, qr_urls, checkout_proxy, provider_proxy, ""
    except Exception as exc:
        error = str(exc)
        if error.startswith("任务已停止"):
            _log_context.prefix = previous_log_context
            return attempt, "", [], checkout_proxy, provider_proxy, ""
        if is_user_already_paid_error(error):
            log("检测到 User is already paid：用户已支付，停止任务")
            if stop_event:
                stop_event.set()
            _log_context.prefix = previous_log_context
            return attempt, "", [], checkout_proxy, provider_proxy, error
        if is_pix_unavailable_error(error):
            log(f"第 {attempt}/{max_retry} 轮 checkout 未提供 PIX，保留代理并继续后续组合", "[WARN] ")
            _log_context.prefix = previous_log_context
            return attempt, "", [], checkout_proxy, provider_proxy, error
        if is_checkout_not_active_error(error):
            log(
                f"第 {attempt}/{max_retry} 轮 Session 已失效；跳过本轮，不记录代理失败，代理保留供后续使用",
                "[WARN] ",
            )
            _log_context.prefix = previous_log_context
            return attempt, "", [], checkout_proxy, provider_proxy, error
        record_failure_by_stage(error, checkout_proxy, provider_proxy, promotion_proxy)
        log(f"第 {attempt}/{max_retry} 轮失败: {error[:300]}", "[WARN] ")
        _log_context.prefix = previous_log_context
        return attempt, "", [], checkout_proxy, provider_proxy, error


def successful_pair_preferences(checkout_proxies: list[str], provider_proxies: list[str]) -> dict[str, list[str]]:
    if not env_bool("PIX_PROXY_SCORE", True):
        return {}
    checkout_by_key = {proxy_key(proxy): proxy for proxy in checkout_proxies}
    provider_by_key = {proxy_key(proxy): proxy for proxy in provider_proxies}
    pair_state = load_proxy_state().get("pair", {})
    if not isinstance(pair_state, dict):
        return {}

    candidates: list[tuple[int, int, str, str]] = []
    for record in pair_state.values():
        if not isinstance(record, dict):
            continue
        success_count = int(record.get("success") or 0)
        if success_count <= 0:
            continue
        checkout_proxy = checkout_by_key.get(str(record.get("checkout") or ""))
        provider_proxy = provider_by_key.get(str(record.get("provider") or ""))
        if checkout_proxy and provider_proxy:
            candidates.append((success_count, int(record.get("last_success") or 0), checkout_proxy, provider_proxy))

    candidates.sort(reverse=True)
    preferences: dict[str, list[str]] = {}
    for _success_count, _last_success, checkout_proxy, provider_proxy in candidates:
        providers = preferences.setdefault(checkout_proxy, [])
        if provider_proxy not in providers:
            providers.append(provider_proxy)
    return preferences


def build_attempt_batches(checkout_proxies: list[str], provider_proxies: list[str], max_attempts: int) -> list[tuple[str, list[str]]]:
    per_checkout = env_int("PIX_PROVIDER_PER_CHECKOUT", 30)
    provider_pool = provider_proxies[:]
    preferred_pairs = successful_pair_preferences(checkout_proxies, provider_proxies)
    reserved_provider_owner: dict[str, str] = {}
    for checkout_proxy, preferred_providers in preferred_pairs.items():
        for provider_proxy in preferred_providers:
            reserved_provider_owner.setdefault(provider_proxy, checkout_proxy)
    used_providers: set[str] = set()
    batches: list[tuple[str, list[str]]] = []
    provider_index = 0
    attempt_count = 0
    preferred_count = 0
    for checkout_proxy in checkout_proxies:
        batch: list[str] = []
        for provider_proxy in preferred_pairs.get(checkout_proxy, []):
            if len(batch) >= per_checkout or attempt_count >= max_attempts:
                break
            if provider_proxy in used_providers:
                continue
            batch.append(provider_proxy)
            used_providers.add(provider_proxy)
            attempt_count += 1
            preferred_count += 1
        while len(batch) < per_checkout and provider_index < len(provider_pool) and attempt_count < max_attempts:
            provider_proxy = provider_pool[provider_index]
            provider_index += 1
            if provider_proxy in used_providers:
                continue
            reserved_owner = reserved_provider_owner.get(provider_proxy)
            if reserved_owner and reserved_owner != checkout_proxy:
                continue
            batch.append(provider_proxy)
            used_providers.add(provider_proxy)
            attempt_count += 1
        if batch:
            batches.append((checkout_proxy, batch))
        if attempt_count >= max_attempts:
            break
    if preferred_count:
        log(f"调度命中成功组合优先: {preferred_count} 组")
    return batches


def is_preferred_proxy(group: str, proxy: str) -> bool:
    if not group or not env_bool("PIX_PROXY_SCORE", True):
        return False
    state = load_proxy_state().get(group, {})
    if not isinstance(state, dict):
        return False
    record = state.get(proxy_key(proxy), {})
    if not isinstance(record, dict):
        return False
    return int(record.get("success") or 0) > 0


def pick_random_proxies(proxies: list[str], limit: int, group: str = "") -> list[str]:
    if group:
        proxies = order_proxy_group(group, proxies)
    preferred = [proxy for proxy in proxies if is_preferred_proxy(group, proxy)]
    preferred_set = set(preferred)
    rest = [proxy for proxy in proxies if proxy not in preferred_set]
    if limit >= len(proxies):
        random.shuffle(rest)
        return preferred + rest
    selected = preferred[:limit]
    remain_count = limit - len(selected)
    if remain_count > 0:
        selected.extend(random.sample(rest, min(remain_count, len(rest))))
    return selected


def run_single_link_attempt(
    access_token: str,
    session_token: str,
    checkout_proxies: list[str],
    promotion_proxies: list[str],
    provider_proxies: list[str],
    attempt: int,
    pix_retry: int,
    checkout_retry: int,
    provider_retry: int,
    checkout_country: str,
    checkout_currency: str,
    stop_event: Event,
) -> tuple[int, str, str, bool]:
    previous_log_context = getattr(_log_context, "prefix", "")
    _log_context.prefix = f"[PIX {attempt}/{pix_retry}] "
    last_error = ""
    approve_blocked = False
    checkout_proxy_used = ""
    try:
        if stop_event.is_set():
            return attempt, "", "任务已停止，跳过本轮", False
        billing = pix_billing_profile()
        pm_country = billing["country"]
        device_id = str(uuid.uuid4())
        checkout_candidates = pick_random_proxies(checkout_proxies, checkout_retry, "checkout")
        checkout: dict[str, str] | None = None
        promotion_proxy = ""
        provider_proxy = ""

        log(f"开始第 {attempt}/{pix_retry} 次提链")
        log(
            f"Step 1: 创建 ChatGPT checkout... checkout账单={checkout_country}/{checkout_currency}，"
            f"第 {attempt}/{pix_retry} 次，每次随机抽取最多 {checkout_retry} 个节点"
        )
        log(f"首次 PM 国家: {pm_country}")

        for checkout_index, proxy_seed in enumerate(checkout_candidates, start=1):
            if stop_event.is_set():
                return attempt, "", "任务已停止，跳过本轮", False
            _log_context.prefix = f"[PIX {attempt}/{pix_retry}][PM={pm_country}] "
            try:
                checkout_proxy, promotion_proxy, provider_proxy = pix_proxy_chain(proxy_seed)
                log_pix_proxy_chain(proxy_seed, checkout_proxy, promotion_proxy, provider_proxy)
                log(f"Checkout {checkout_index}/{len(checkout_candidates)}: {checkout_country}/{checkout_currency}, proxy={proxy_label(checkout_proxy)}")
                zero_status, zero_amount, _zero_checked_at = checkout_zero_cache_status(checkout_proxy, checkout_country)
                if zero_status == "ok":
                    log(f"checkout 目标金额缓存命中: amount={zero_amount}")
                elif zero_status == "bad":
                    log(f"checkout 目标金额失败缓存命中: 上次 amount={zero_amount}，本轮继续验证", "[WARN] ")
                chatgpt = build_chatgpt_session(access_token, device_id, checkout_proxy, session_token)
                checkout = create_checkout(chatgpt, checkout_country)
                checkout_proxy_used = checkout_proxy
                break
            except Exception as exc:
                error = str(exc)
                last_error = error
                if is_user_already_paid_error(error):
                    log("检测到 User is already paid：用户已支付，停止任务")
                    stop_event.set()
                    return attempt, "", error, False
                if not is_checkout_not_active_error(error):
                    record_failure_by_stage(f"checkout 阶段失败: {error}", checkout_proxy, "")
                log(f"Checkout {checkout_index}/{len(checkout_candidates)} 失败: {error[:220]}", "[WARN] ")

        _log_context.prefix = f"[PIX {attempt}/{pix_retry}] "
        if not checkout or not checkout_proxy_used:
            log(f"第 {attempt}/{pix_retry} 次提链 checkout 阶段失败", "[WARN] ")
            return attempt, "", last_error or "checkout_failed", False

        stripe_pk = checkout.get("stripe_pk") or DEFAULT_STRIPE_PK
        log(f"Stripe PK: {stripe_pk[:18]}...")
        log(f"Step 2: 首次尝试 PM={pm_country}...")

        if stop_event.is_set():
            return attempt, "", "任务已停止，跳过本轮", False
        _log_context.prefix = f"[PIX {attempt}/{pix_retry}][PM={pm_country}] "
        try:
            outcome = run_provider_flow(
                access_token,
                session_token,
                checkout_proxy_used,
                promotion_proxy,
                provider_proxy,
                [provider_proxy],
                device_id,
                checkout,
                billing,
                stop_event,
                chatgpt_session=chatgpt,
            )
            redirect_url = outcome.redirect_url
            if redirect_url:
                record_proxy_pair_result(checkout_proxy_used, provider_proxy, True, "success")
                stop_event.set()
                return attempt, redirect_url, "", False
            last_error = "no_redirect_url"
            record_proxy_result("provider", provider_proxy, False, last_error)
        except Exception as exc:
            error = str(exc)
            last_error = error
            if is_checkout_not_active_error(error):
                log("Session 已失效；当前 checkout 不再继续换 provider", "[WARN] ")
            elif is_pix_unavailable_error(error):
                log("当前 checkout 未提供 PIX，换下一轮 checkout 组合", "[WARN] ")
                return attempt, "", error, False
            else:
                record_failure_by_stage(error, checkout_proxy_used, provider_proxy, promotion_proxy)
                log(f"Provider 失败: {error[:220]}", "[WARN] ")
                if is_approve_failure_error(error) and "approve blocked" in error:
                    approve_blocked = True

        _log_context.prefix = f"[PIX {attempt}/{pix_retry}] "
        log(f"第 {attempt}/{pix_retry} 次提链结束，未拿到最终 URL", "[WARN] ")
        return attempt, "", last_error, approve_blocked
    finally:
        _log_context.prefix = previous_log_context


def run_single_link_parallel_mode(
    access_token: str,
    session_token: str,
    checkout_proxies: list[str],
    promotion_proxies: list[str],
    provider_proxies: list[str],
) -> int:
    checkout_retry = env_int("PIX_CHECKOUT_RETRY_MAX", 5)
    provider_retry = env_int("PIX_PROVIDER_RETRY_MAX", 3)
    pix_retry = env_int("PIX_MAX_RETRY", 5)
    requested_workers = env_int("PIX_WORKERS", 1)
    worker_limit = env_int("PIX_WORKERS_MAX", requested_workers)
    workers = min(max(1, requested_workers), max(1, worker_limit), pix_retry)
    checkout_country = normalize_country(os.environ.get("PIX_CHECKOUT_COUNTRY", PIX_BOOTSTRAP_COUNTRY))
    checkout_currency = currency_for_country(checkout_country)
    configured_pm_country = normalize_country(
        os.environ.get("PIX_BILLING_COUNTRY", PIX_PROVIDER_COUNTRY)
    )
    max_blocked = env_int("PIX_MAX_APPROVE_BLOCKED", pix_retry)
    approve_blocked_count = 0
    last_error = ""
    stop_event = Event()

    if requested_workers > workers:
        log(f"PIX并发从 {requested_workers} 限制为 {workers}", "[WARN] ")
    log(
        "开始执行 PIX 链提取流程："
        f"checkout={checkout_country}/{checkout_currency}，PM={configured_pm_country}，locale={payment_browser_locale()}，"
        f"Checkout重试={checkout_retry}，Provider重试={provider_retry}，PIX总重试={pix_retry}，PIX并发={workers}。"
    )

    executor = ThreadPoolExecutor(max_workers=workers)
    futures: dict[Any, int] = {}
    try:
        for attempt in range(1, pix_retry + 1):
            futures[
                executor.submit(
                    run_single_link_attempt,
                    access_token,
                    session_token,
                    checkout_proxies,
                    promotion_proxies,
                    provider_proxies,
                    attempt,
                    pix_retry,
                    checkout_retry,
                    provider_retry,
                    checkout_country,
                    checkout_currency,
                    stop_event,
                )
            ] = attempt

        for future in as_completed(futures):
            try:
                attempt, redirect_url, error, approve_blocked = future.result()
            except Exception as exc:
                attempt = futures.get(future, 0)
                redirect_url = ""
                error = str(exc)
                approve_blocked = False
                log(f"第 {attempt}/{pix_retry} 次提链异常: {error[:300]}", "[WARN] ")
            if redirect_url:
                stop_event.set()
                for pending in futures:
                    pending.cancel()
                print("\n===== 结果 =====")
                print(f"PIX 最终支付 URL:\n{redirect_url}")
                return 0
            last_error = error or last_error
            if is_user_already_paid_error(error):
                log("检测到 User is already paid：用户已支付，任务正常结束")
                stop_event.set()
                for pending in futures:
                    pending.cancel()
                return 0
            if approve_blocked:
                approve_blocked_count += 1
                log(f"approve blocked 计数: {approve_blocked_count}/{max_blocked}", "[WARN] ")
            if approve_blocked_count >= max_blocked:
                log("达到当前账号 approve blocked 上限，停止继续提交新提链", "[WARN] ")
                stop_event.set()
                for pending in futures:
                    pending.cancel()
                return 1
    finally:
        executor.shutdown(wait=True, cancel_futures=stop_event.is_set())

    log(f"全部失败: {last_error}", "[ERROR] ")
    return 1


def run_single_link_mode(
    access_token: str,
    session_token: str,
    proxy_seeds: list[str],
) -> int:
    pix_workers = env_int("PIX_WORKERS", 1)
    if pix_workers > 1:
        log(f"PIX 链路固定并发=1，忽略 PIX_WORKERS={pix_workers}", "[WARN] ")

    checkout_retry = env_int("PIX_CHECKOUT_RETRY_MAX", 5)
    provider_retry = env_int("PIX_PROVIDER_RETRY_MAX", 3)
    pix_retry = env_int("PIX_MAX_RETRY", 5)
    checkout_country = normalize_country(os.environ.get("PIX_CHECKOUT_COUNTRY", PIX_BOOTSTRAP_COUNTRY))
    checkout_currency = currency_for_country(checkout_country)
    configured_pm_country = normalize_country(
        os.environ.get("PIX_BILLING_COUNTRY", PIX_PROVIDER_COUNTRY)
    )
    max_blocked = env_int("PIX_MAX_APPROVE_BLOCKED", pix_retry)
    approve_blocked_count = 0
    last_error = ""
    stop_event = Event()
    attempted_seed_keys: set[str] = set()

    log(
        "开始执行 PIX 链提取流程："
        f"checkout={checkout_country}/{checkout_currency}，PM={configured_pm_country}，locale={payment_browser_locale()}，"
        f"Checkout重试={checkout_retry}，Provider重试={provider_retry}，PIX总重试={pix_retry}。"
    )

    for attempt in range(1, pix_retry + 1):
        billing = pix_billing_profile()
        pm_country = billing["country"]
        device_id = str(uuid.uuid4())
        available_seeds = [
            proxy_seed
            for proxy_seed in proxy_seeds
            if proxy_chain_key(proxy_seed) not in attempted_seed_keys
        ]
        checkout_candidates = pick_random_proxies(available_seeds, checkout_retry, "seed")
        if not checkout_candidates:
            last_error = last_error or "本次任务的代理 Seed 已全部尝试"
            log("本次任务的代理 Seed 已全部尝试，不再重复失败节点", "[WARN] ")
            break
        checkout: dict[str, str] | None = None
        checkout_proxy_used = ""
        promotion_proxy = ""
        provider_proxy = ""

        log(f"开始第 {attempt}/{pix_retry} 次提链")
        log(
            f"Step 1: 创建 ChatGPT checkout... checkout账单={checkout_country}/{checkout_currency}，"
            f"第 {attempt}/{pix_retry} 次，每次随机抽取最多 {checkout_retry} 个节点"
        )
        log(f"  首次 PM 国家: {pm_country}")

        for checkout_index, proxy_seed in enumerate(checkout_candidates, start=1):
            previous_log_context = getattr(_log_context, "prefix", "")
            _log_context.prefix = f"  [PM={pm_country}] "
            checkout_proxy = ""
            promotion_proxy = ""
            provider_proxy = ""
            chain_key = proxy_chain_key(proxy_seed)
            attempted_seed_keys.add(chain_key)
            try:
                checkout_proxy, promotion_proxy, provider_proxy = pix_proxy_chain(proxy_seed)
                log_pix_proxy_chain(proxy_seed, checkout_proxy, promotion_proxy, provider_proxy)
                log(
                    f"Checkout {checkout_index}/{len(checkout_candidates)}: "
                    f"{checkout_country}/{checkout_currency}, proxy={proxy_label(checkout_proxy)}，"
                    f"本次已尝试 Seed={len(attempted_seed_keys)}"
                )
                zero_status, zero_amount, _zero_checked_at = checkout_zero_cache_status(checkout_proxy, checkout_country)
                if zero_status == "ok":
                    log(f"checkout 目标金额缓存命中: amount={zero_amount}")
                elif zero_status == "bad":
                    log(f"checkout 目标金额失败缓存命中: 上次 amount={zero_amount}，本轮继续验证", "[WARN] ")
                chatgpt = build_chatgpt_session(access_token, device_id, checkout_proxy, session_token)
                checkout = create_checkout(chatgpt, checkout_country)
                checkout_proxy_used = checkout_proxy
                break
            except Exception as exc:
                error = str(exc)
                last_error = error
                if is_user_already_paid_error(error):
                    log("检测到 User is already paid：用户已支付，任务正常结束")
                    return 0
                if not is_checkout_not_active_error(error):
                    record_failure_by_stage(
                        f"checkout 阶段失败: {error}",
                        checkout_proxy or proxy_seed,
                        "",
                    )
                log(f"Checkout {checkout_index}/{len(checkout_candidates)} 失败: {error[:220]}", "[WARN] ")
            finally:
                _log_context.prefix = previous_log_context

        if not checkout or not checkout_proxy_used:
            log(f"第 {attempt}/{pix_retry} 次提链 checkout 阶段失败，换下一次提链", "[WARN] ")
            continue

        stripe_pk = checkout.get("stripe_pk") or DEFAULT_STRIPE_PK
        log(f"Stripe PK: {stripe_pk[:18]}...")
        log(f"Step 2: 首次尝试 PM={pm_country}...")

        previous_log_context = getattr(_log_context, "prefix", "")
        _log_context.prefix = f"  [PM={pm_country}] "
        try:
            outcome = run_provider_flow(
                access_token,
                session_token,
                checkout_proxy_used,
                promotion_proxy,
                provider_proxy,
                [provider_proxy],
                device_id,
                checkout,
                billing,
                stop_event,
                chatgpt_session=chatgpt,
            )
            redirect_url = outcome.redirect_url
            if redirect_url:
                record_proxy_result("seed", checkout_proxy_used, True, "success")
                print("\n===== 结果 =====")
                print(f"PIX 最终支付 URL:\n{redirect_url}")
                return 0
            last_error = "no_redirect_url"
            record_proxy_result("seed", provider_proxy, False, last_error)
        except Exception as exc:
            error = str(exc)
            last_error = error
            if is_checkout_not_active_error(error):
                log("Session 已失效；当前 checkout 不再继续换 provider", "[WARN] ")
            elif is_pix_unavailable_error(error):
                log("当前 checkout 未提供 PIX，换下一次提链", "[WARN] ")
            else:
                record_failure_by_stage(error, checkout_proxy_used, provider_proxy, promotion_proxy)
                log(f"Provider 失败: {error[:220]}", "[WARN] ")
                if is_approve_failure_error(error) and "approve blocked" in error:
                    approve_blocked_count += 1
                    log(f"approve blocked 计数: {approve_blocked_count}/{max_blocked}", "[WARN] ")
        finally:
            _log_context.prefix = previous_log_context

        if approve_blocked_count >= max_blocked:
            log("达到当前账号 approve blocked 上限，停止继续提交新提链", "[WARN] ")
            return 1
        log(f"第 {attempt}/{pix_retry} 次提链结束，未拿到最终 URL", "[WARN] ")

    log(f"全部失败: {last_error}", "[ERROR] ")
    return 1


def reference_to_openai_pay_url(stripe_hosted_url: str) -> str:
    """Mirror the reference extractor's checkout.stripe.com -> pay.openai.com mapping."""
    value = str(stripe_hosted_url or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.hostname == "checkout.stripe.com":
        return urlunsplit((parsed.scheme or "https", "pay.openai.com", parsed.path, parsed.query, parsed.fragment))
    return value


def reference_stripe_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": REFERENCE_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }


def reference_stripe_init(stripe: requests.Session, cs_id: str, stripe_pk: str) -> dict[str, Any]:
    """Step 3 of the supplied reference project, without extra browser calls."""
    body = {
        "browser_locale": "pt-BR",
        "browser_timezone": "America/Sao_Paulo",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
        "elements_session_client[locale]": "pt-BR",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}/init"
    response = stripe.post(
        url,
        data=body,
        headers=reference_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    dump_http(response, "reference_stripe_init", body, "POST", url, force=response.status_code >= 400)
    if response.status_code >= 400:
        raise RuntimeError(f"参考方案 Stripe init 失败 HTTP {response.status_code}: {response.text[:500]}")
    payload = response.json() or {}
    payload["client_stripe_js_id"] = body["elements_session_client[stripe_js_id]"]
    payload["_client_context"] = {"stripe_js_id": body["elements_session_client[stripe_js_id]"]}
    return payload


def reference_build_ctx(init_payload: dict[str, Any]) -> dict[str, Any]:
    """Use the synthetic Elements context emitted by the original six-step extractor."""
    return {
        "stripe_js_id": str(init_payload.get("client_stripe_js_id") or uuid.uuid4()),
        "elements_session_id": f"elements_session_{uuid.uuid4().hex[:11]}",
        "elements_session_config_id": str(init_payload.get("config_id") or uuid.uuid4()),
        "config_id": str(init_payload.get("config_id") or ""),
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "checkout_amount": amount_from_payload(init_payload),
        "currency": str(init_payload.get("currency") or "brl").lower(),
        "locale": "pt-BR",
        "runtime_version": REFERENCE_STRIPE_RUNTIME_VERSION,
        "stripe_version": STRIPE_VERSION_FULL,
        "guid": uuid.uuid4().hex,
        "muid": uuid.uuid4().hex,
        "sid": uuid.uuid4().hex,
    }


def reference_stripe_confirm(
    stripe: requests.Session,
    cs_id: str,
    pm_id: str,
    stripe_pk: str,
    init_payload: dict[str, Any],
    ctx: dict[str, Any],
    stripe_hosted_url: str,
) -> dict[str, Any]:
    """Step 5 of the reference project: confirm by an independently-created Pix PM."""
    body = {
        "guid": str(ctx["guid"]),
        "muid": str(ctx["muid"]),
        "sid": str(ctx["sid"]),
        "payment_method": pm_id,
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "version": str(ctx["runtime_version"]),
        "expected_amount": str(ctx["checkout_amount"]),
        "expected_payment_method_type": "pix",
        "return_url": reference_to_openai_pay_url(stripe_hosted_url) or stripe_hosted_url,
        "elements_session_client[session_id]": str(ctx["elements_session_id"]),
        "elements_session_client[locale]": "pt-BR",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[stripe_js_id]": str(ctx["stripe_js_id"]),
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "client_attribution_metadata[client_session_id]": str(ctx["stripe_js_id"]),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[checkout_config_id]": str(ctx.get("config_id") or ""),
        "client_attribution_metadata[elements_session_id]": str(ctx["elements_session_id"]),
        "client_attribution_metadata[elements_session_config_id]": str(ctx["elements_session_config_id"]),
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "consent[terms_of_service]": "accepted",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm"
    response = stripe.post(url, data=body, headers=reference_stripe_headers(), timeout=DEFAULT_TIMEOUT)
    dump_http(response, "reference_pix_confirm", body, "POST", url, force=True)
    if response.status_code >= 400:
        raise RuntimeError(f"参考方案 PIX confirm 失败 HTTP {response.status_code}: {response.text[:500]}")
    return response.json() or {}


def reference_pix_details(payload: Any) -> dict[str, Any]:
    """Extract the exact Pix QR shape searched by the supplied project."""
    details: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value not in (None, "", {}, []):
            details.setdefault(key, value)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            qr = value.get("pix_display_qr_code") or value.get("display_pix_qr_code")
            if isinstance(qr, dict):
                visit(qr)
            if (
                str(value.get("type") or "").lower() in {"pix_display_qr_code", "display_pix_qr_code"}
                or any(key in value for key in ("hosted_instructions_url", "image_url_png", "image_url_svg"))
            ):
                put("hosted_instructions_url", value.get("hosted_instructions_url"))
                put("image_url_png", value.get("image_url_png"))
                put("image_url_svg", value.get("image_url_svg"))
                put("expires_at", value.get("expires_at"))
                put("copy_paste", value.get("data"))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return details


class ReferenceStripeRequiresApproval(RuntimeError):
    pass


def reference_poll_pix_result(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    ctx: dict[str, Any],
    progress: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reference step 6: poll only payment_pages after approval."""
    deadline = time.time() + env_int("PIX_POLL_TIMEOUT", 45)
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(ctx["elements_session_id"]),
        "elements_session_client[stripe_js_id]": str(ctx["stripe_js_id"]),
        "elements_session_client[locale]": "pt-BR",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}"
    last_error = ""
    poll_attempt = 0
    while time.time() < deadline:
        poll_attempt += 1
        if poll_attempt == 1 or poll_attempt % 5 == 0:
            emit_progress(progress, "poll", f"方案2 6/6：轮询 Pix instructions，第 {poll_attempt} 次")
        response = stripe.get(url, params=params, headers=reference_stripe_headers(), timeout=DEFAULT_TIMEOUT)
        if response.status_code == 200:
            payload = response.json() or {}
            details = reference_pix_details(payload)
            if str(details.get("hosted_instructions_url") or "").strip():
                dump_http(response, "reference_pix_poll_success", params, "GET", url, force=True)
                return details, payload
            submission = find_submission_attempt(payload)
            state = str(submission.get("state") or "")
            if state == "requires_approval":
                raise ReferenceStripeRequiresApproval("payment page requires ChatGPT approval")
            if state == "failed":
                raise RuntimeError(f"参考方案 Stripe submission failed: {submission}")
            last_error = f"state={state or '未知'}"
        else:
            last_error = f"HTTP {response.status_code} {response.text[:120]}"
        time.sleep(1)
    raise RuntimeError(f"参考方案 PIX 结果轮询超时: {last_error}")


def reference_chatgpt_approve(chatgpt: requests.Session, checkout: dict[str, str]) -> None:
    """Keep approval to the reference project's minimal request sequence."""
    processor = processor_entity_for_country("BR", checkout.get("processor_entity") or "")
    try:
        chatgpt.post(
            "https://chatgpt.com/backend-api/sentinel/ping",
            json={},
            headers={"Referer": "https://chatgpt.com/"},
            timeout=env_int("PIX_APPROVE_REQUEST_TIMEOUT", 30),
        )
    except Exception:
        pass
    url = "https://chatgpt.com/backend-api/payments/checkout/approve"
    body = {"checkout_session_id": checkout["cs_id"], "processor_entity": processor}
    response = chatgpt.post(
        url,
        json=body,
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
            "x-openai-target-path": "/backend-api/payments/checkout/approve",
            "x-openai-target-route": "/backend-api/payments/checkout/approve",
        },
        timeout=env_int("PIX_APPROVE_REQUEST_TIMEOUT", 30),
    )
    dump_http(response, "reference_approve", body, "POST", url, force=True)
    if response.status_code >= 400:
        raise RuntimeError(f"参考方案 ChatGPT approve 失败 HTTP {response.status_code}: {response.text[:300]}")
    result = str((response.json() or {}).get("result") or "").strip().lower()
    if result != "approved":
        raise RuntimeError(f"参考方案 ChatGPT approve 未通过: {result or response.text[:200]}")


def reference_approve_with_retry(
    chatgpt: requests.Session,
    checkout: dict[str, str],
    progress: Any | None = None,
) -> None:
    last_error = ""
    attempt_count = max(1, env_int("PIX_REFERENCE_APPROVE_RETRY_MAX", 5))
    for attempt in range(1, attempt_count + 1):
        emit_progress(progress, "confirm", f"方案2 6/6：ChatGPT approve {attempt}/{attempt_count}")
        try:
            reference_chatgpt_approve(chatgpt, checkout)
            return
        except Exception as exc:
            last_error = str(exc)
            if attempt < attempt_count:
                delay = min(2 + (attempt - 1) * 2, 8)
                emit_progress(
                    progress,
                    "confirm",
                    f"方案2：approve {attempt}/{attempt_count} 未通过，{delay} 秒后重试当前 sticky 出口",
                )
                time.sleep(delay)
    raise RuntimeError(f"参考方案 ChatGPT approve 连续失败: {last_error}")


def run_reference_provider_flow(
    access_token: str,
    session_token: str,
    checkout_proxy: str,
    promotion_proxy: str,
    provider_proxy: str,
    device_id: str,
    checkout: dict[str, str],
    billing: dict[str, str],
    *,
    chatgpt_session: requests.Session | None = None,
    cookie_header: str = "",
    fingerprint: Mapping[str, Any] | None = None,
    progress: Any | None = None,
) -> PixFlowResult:
    """Run the supplied project's six-step Pix flow, without enhanced-flow side calls."""
    def step(stage: str, number: int, message: str) -> None:
        detail = f"方案2 {number}/6：{message}"
        log(f"[参考方案] {detail}")
        emit_progress(progress, stage, detail)

    step("promotion", 2, "checkout/update 应用免费优惠")
    promotion_chatgpt = chatgpt_session_for_proxy(
        chatgpt_session,
        checkout_proxy,
        promotion_proxy,
        access_token,
        device_id,
        session_token,
        cookie_header,
        fingerprint,
    )
    update_checkout_promotion(promotion_chatgpt, checkout, "BR")

    step("stripe_init", 3, "初始化 Stripe Checkout")
    stripe = new_session(provider_proxy)
    stripe.headers.update({"User-Agent": REFERENCE_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    stripe_pk = str(checkout.get("stripe_pk") or "").strip()
    if not stripe_pk:
        raise RuntimeError("参考方案 checkout 未返回 Stripe publishable key")
    init_payload = reference_stripe_init(stripe, checkout["cs_id"], stripe_pk)
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError("参考方案 Stripe init 未返回 stripe_hosted_url")
    ctx = reference_build_ctx(init_payload)
    amount = int(ctx["checkout_amount"])
    if amount != 0:
        raise RuntimeError(f"PIX 金额策略未命中：仅接受 0，当前金额小单位={amount}，已停止生成支付链")

    step("payment_method", 4, "创建独立 Pix PaymentMethod")
    pm_id = stripe_create_pix_payment_method(
        stripe,
        checkout["cs_id"],
        stripe_pk,
        billing,
        ctx,
        reference_compat=True,
    )
    step("confirm", 5, "提交 Stripe confirm")
    confirm_payload = reference_stripe_confirm(
        stripe, checkout["cs_id"], pm_id, stripe_pk, init_payload, ctx, stripe_hosted_url
    )
    details = reference_pix_details(confirm_payload)
    resolved_payload = confirm_payload
    if not str(details.get("hosted_instructions_url") or "").strip():
        step("confirm", 6, "approve 后轮询 Pix instructions")
        approve_chatgpt = chatgpt_session_for_proxy(
            chatgpt_session,
            checkout_proxy,
            provider_proxy,
            access_token,
            device_id,
            session_token,
            cookie_header,
            fingerprint,
        )
        try:
            reference_approve_with_retry(approve_chatgpt, checkout, progress=progress)
        except Exception as approve_error:
            log(f"[参考方案] approve 未确认成功，按参考流程继续轮询: {str(approve_error)[:160]}", "[WARN] ")
        try:
            details, resolved_payload = reference_poll_pix_result(
                stripe, checkout["cs_id"], stripe_pk, ctx, progress=progress
            )
        except ReferenceStripeRequiresApproval:
            emit_progress(progress, "confirm", "方案2：Stripe 仍要求 approve，再确认一次")
            reference_approve_with_retry(approve_chatgpt, checkout, progress=progress)
            details, resolved_payload = reference_poll_pix_result(
                stripe, checkout["cs_id"], stripe_pk, ctx, progress=progress
            )

    redirect_url = str(details.get("hosted_instructions_url") or "").strip()
    if not is_pix_instructions_url(redirect_url):
        raise RuntimeError("参考方案未返回 Stripe Pix instructions 最终支付链接")
    emit_progress(progress, "extract", "方案2 6/6：已取得 Pix instructions 最终支付链接")
    intent_kind, intent_status = pix_intent_details(resolved_payload)
    expires_at = details.get("expires_at")
    try:
        expires_at = int(expires_at) if expires_at is not None else None
    except (TypeError, ValueError):
        expires_at = None
    return PixFlowResult(
        redirect_url=redirect_url,
        qr_urls=[item for item in (str(details.get("image_url_png") or ""), str(details.get("image_url_svg") or "")) if item],
        amount_minor=amount,
        intent_kind=intent_kind,
        intent_status=intent_status,
        qr_png=str(details.get("image_url_png") or ""),
        qr_svg=str(details.get("image_url_svg") or ""),
        expires_at=expires_at,
    )


def generate_standalone_pix_link(
    access_token: str,
    provider_proxy: str,
    promotion_proxy: str,
    *,
    progress: Any | None = None,
) -> dict[str, Any]:
    """Run the vendored scheme-3 project and adapt its result envelope."""

    def report(message: str) -> None:
        if progress is not None:
            progress(str(message))

    try:
        raw = pix_scheme3.generate_opll_pix_long_link(
            access_token,
            country="BR",
            currency="BRL",
            proxy_url=provider_proxy,
            promotion_proxy_url=promotion_proxy,
            log_cb=report,
        )
        if not isinstance(raw, dict):
            raise RuntimeError("方案 3 未返回有效结果")
        amount_text = str(raw.get("stripe_amount") or raw.get("confirm_amount") or "0").strip()
        try:
            amount_minor = int(amount_text)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"方案 3 返回无效金额: {amount_text!r}") from exc
        long_url = str(
            raw.get("pix_hosted_instructions_url")
            or raw.get("provider_redirect_url")
            or raw.get("long_url")
            or ""
        ).strip()
        cs_id = str(raw.get("cs_id") or "").strip()
        processor_entity = str(raw.get("processor_entity") or "").strip()
        return {
            "ok": True,
            "provider": "pix_br_vn_standalone",
            "payment_method": "pix",
            "currency": "brl",
            "billing_country": "BR",
            "long_url": long_url,
            "pix_url": long_url,
            "hosted_instructions_url": str(raw.get("pix_hosted_instructions_url") or long_url).strip(),
            "provider_redirect_url": str(raw.get("provider_redirect_url") or "").strip(),
            "pix_qr_code": str(raw.get("pix_qr_code") or "").strip(),
            "qr_image_url_png": str(raw.get("pix_qr_image_url_png") or "").strip(),
            "qr_image_url_svg": str(raw.get("pix_qr_image_url_svg") or "").strip(),
            "qr_expires_at": int(raw.get("pix_expires_at") or 0) or None,
            "checkout_session_id": cs_id,
            "processor_entity": processor_entity,
            "chatgpt_checkout_url": (
                f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}"
                if cs_id and processor_entity
                else ""
            ),
            "pay_amount_minor": amount_minor,
            "pay_amount": amount_minor / 100,
            "is_free_trial": amount_minor == 0,
            "pix_flow": "standalone",
            "debug": "core=standalone",
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": "pix_br_vn_standalone",
            "error": str(exc),
            "error_code": "pix_scheme3_failed",
            "debug": "core=standalone",
        }


def generate_pix_link_explicit(
    access_token: str,
    checkout_proxy: str,
    promotion_proxy: str,
    provider_proxy: str,
    *,
    account: str = "未知",
    session_token: str = "",
    cookie_header: str = "",
    fingerprint: Mapping[str, Any] | None = None,
    approve_proxies: tuple[str, ...] = (),
    protocol_mode: str = "enhanced",
    progress: Any | None = None,
) -> dict[str, Any]:
    """Run the upstream PIX protocol with host-selected regional proxies."""
    access_token, parsed_session_token = normalize_token(access_token)
    session_token = session_token or parsed_session_token
    checkout_proxy = normalize_proxy_url(checkout_proxy)
    promotion_proxy = normalize_proxy_url(promotion_proxy)
    provider_proxy = normalize_proxy_url(provider_proxy)
    protocol = pix_protocol_mode(protocol_mode)
    result: dict[str, Any] = {
        "ok": False, "provider": "pix_br", "account": account or "未知",
        "payment_method": "pix", "currency": "brl", "billing_country": "BR",
    }
    if not access_token:
        result.update(error="accessToken 为空", error_code="bad_token_format")
        return result
    if not checkout_proxy or not promotion_proxy or not provider_proxy:
        result.update(error="PIX 需要 BR 共享代理", error_code="proxy_empty")
        return result
    if len({checkout_proxy, promotion_proxy, provider_proxy}) != 1:
        result.update(
            error="PIX Checkout、Promotion、Provider 必须使用同一条 BR sticky 代理",
            error_code="proxy_mismatch",
        )
        return result
    for value in (checkout_proxy, promotion_proxy, provider_proxy):
        register_proxy_for_redaction(value)
    saved_cookies = cookie_header_values(cookie_header)
    device_id = saved_cookies.get("oai-did") or str(uuid.uuid4())
    billing = pix_billing_profile()
    if protocol == "enhanced" and "@" in str(account or ""):
        billing["email"] = str(account).strip()
    try:
        plan_number = 2 if protocol == "reference" else 1
        step_count = 6 if protocol == "reference" else 8
        emit_progress(
            progress,
            "checkout",
            f"方案{plan_number} 1/{step_count}：创建 BR Checkout",
        )
        chatgpt = build_chatgpt_session(
            access_token,
            device_id,
            checkout_proxy,
            session_token,
            cookie_header,
            fingerprint,
        )
        checkout = create_checkout(chatgpt, PIX_BOOTSTRAP_COUNTRY)
        result.update(
            checkout_session_id=checkout.get("cs_id"),
            processor_entity=checkout.get("processor_entity"),
            chatgpt_checkout_url=checkout_page_url(checkout),
        )
        if protocol == "reference":
            outcome = run_reference_provider_flow(
                access_token,
                session_token,
                checkout_proxy,
                promotion_proxy,
                provider_proxy,
                device_id,
                checkout,
                billing,
                chatgpt_session=chatgpt,
                cookie_header=cookie_header,
                fingerprint=fingerprint,
                progress=progress,
            )
        else:
            outcome = run_provider_flow(
                access_token, session_token, checkout_proxy, promotion_proxy, provider_proxy,
                [provider_proxy],
                device_id,
                checkout,
                billing,
                chatgpt_session=chatgpt,
                cookie_header=cookie_header,
                fingerprint=fingerprint,
                progress=progress,
            )
        redirect_url = outcome.redirect_url
        qr_urls = outcome.qr_urls
        if not redirect_url:
            raise RuntimeError("未提取到最终 PIX 支付链接")
        amount_minor = outcome.amount_minor
        is_zero_setup_intent = amount_minor == 0 and (
            outcome.intent_kind == "setup_intent" or protocol == "reference"
        )
        result.update(
            ok=True,
            long_url=redirect_url,
            pix_url=redirect_url,
            hosted_instructions_url=redirect_url,
            qr_urls=qr_urls,
            qr_image_url_png=outcome.qr_png,
            qr_image_url_svg=outcome.qr_svg,
            qr_expires_at=outcome.expires_at,
            pay_amount_minor=amount_minor, pay_amount=amount_minor / 100,
            is_free_trial=is_zero_setup_intent,
            trial_status=("PIX 0元 SetupIntent 授权" if is_zero_setup_intent else f"PIX BRL {amount_minor / 100:.2f}"),
            pix_flow=outcome.intent_kind,
            pix_setup_intent=is_zero_setup_intent,
            setup_intent_status=outcome.intent_status if outcome.intent_kind == "setup_intent" else "",
            debug=f"core={protocol}",
            **pix_billing_snapshot(billing),
        )
        return result
    except Exception as exc:
        error = str(exc)
        low = error.lower()
        amount_match = re.search(r"当前金额小单位=(\d+)", error)
        amount_minor = int(amount_match.group(1)) if amount_match else None
        if "token_invalidated" in low or "token has been invalidated" in low:
            code = "token_revoked"
        elif is_checkout_not_active_error(error):
            code = "checkout_not_active"
        elif is_pix_unavailable_error(error):
            code = "pix_unavailable"
        elif "cloudflare_challenge" in low:
            code = "cloudflare_challenge"
        elif "PIX 目标金额未命中" in error or "PIX 金额策略未命中" in error:
            code = "pix_amount_mismatch"
        elif "0元流程未返回 SetupIntent" in error:
            code = "pix_setup_intent_missing"
        elif amount_minor is not None:
            code = "non_zero_amount"
        elif is_proxy_health_failure(error):
            code = "proxy_failed"
        elif "approve blocked" in low:
            code = "approve_blocked"
        else:
            code = "pix_no_link"
        result.update(error=error, error_code=code, debug="core=open_source")
        if amount_minor is not None:
            is_zero = amount_minor == 0
            result.update(
                pay_amount_minor=amount_minor,
                pay_amount=amount_minor / 100,
                is_free_trial=is_zero,
                trial_status=(
                    "PIX 0元资格，但未生成 SetupIntent 链接"
                    if is_zero
                    else f"PIX 金额 BRL {amount_minor / 100:.2f}"
                ),
            )
        return result


def is_pix_instructions_url(value: object) -> bool:
    """Accept only Stripe-hosted Pix instruction pages."""
    url = str(value or "").strip()
    if not url or len(url) > 4096 or any(char.isspace() for char in url):
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    path = parsed.path or ""
    lower = path.lower()
    valid_path = any(
        lower.startswith(prefix) and bool(path[len(prefix) :].strip("/"))
        for prefix in ("/pix/instructions/", "/qr/instructions/")
    )
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname == "payments.stripe.com"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and valid_path
        and not parsed.fragment
    )


def _safe_pix_error(value: object, secrets: tuple[str, ...]) -> str:
    message = redact_log_text(str(value or "PIX 最终支付链接提取失败"))
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    return message[:500]


def generate_pix_final_link(
    account: Mapping[str, Any] | None,
    access_token: str,
    *,
    checkout_proxy: str,
    promotion_proxy: str = "",
    provider_proxy: str = "",
    approve_proxies: tuple[str, ...] = (),
    protocol_mode: str = "enhanced",
    checkout_channel: str = "pix",
    progress: Any | None = None,
) -> dict[str, Any]:
    """Run the independent Brazil Pix flow and return a safe result envelope."""
    token = str(access_token or "").strip()
    channel = str(checkout_channel or "pix").strip().lower()
    if channel != "pix":
        return {"ok": False, "error": "Pix 协议仅接受 pix 渠道", "error_code": "unsupported_channel"}
    if not token:
        return {"ok": False, "error": "access_token 不能为空", "error_code": "bad_token_format"}
    try:
        protocol = pix_protocol_mode(protocol_mode)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc), "error_code": "bad_protocol_mode"}

    account_data = account if isinstance(account, Mapping) else {}
    session_token = str(
        account_data.get("session_token")
        or account_data.get("sessionToken")
        or ""
    ).strip()
    cookie_header = str(account_data.get("cookie_header") or "").strip()
    fingerprint = account_data.get("fp") if isinstance(account_data.get("fp"), Mapping) else {}
    email = str(account_data.get("email") or "未知").strip() or "未知"
    checkout = normalize_proxy_url(checkout_proxy)
    promotion = normalize_proxy_url(promotion_proxy) or checkout
    provider = normalize_proxy_url(provider_proxy) or checkout
    if not checkout or not promotion or not provider:
        return {"ok": False, "error": "Pix 需要 BR 代理", "error_code": "proxy_empty"}

    if progress is not None:
        label = {
            "enhanced": "方案 1（当前流程）",
            "reference": "方案 2（参考项目）",
            "standalone": "方案 3（BR/VN 独立流程）",
        }[protocol]
        progress(f"启动巴西 Pix 最终支付链接协议：{label}")
    if protocol == "standalone":
        upstream = generate_standalone_pix_link(
            token,
            provider,
            promotion,
            progress=progress,
        )
    else:
        upstream = generate_pix_link_explicit(
            token,
            checkout,
            promotion,
            provider,
            account=email,
            session_token=session_token,
            cookie_header=cookie_header,
            fingerprint=fingerprint,
            approve_proxies=approve_proxies,
            protocol_mode=protocol,
            progress=progress,
        )
    secrets = (token, session_token, checkout, promotion, provider)
    if not isinstance(upstream, Mapping) or not upstream.get("ok"):
        payload = upstream if isinstance(upstream, Mapping) else {}
        return {
            "ok": False,
            "error": _safe_pix_error(payload.get("error"), secrets),
            "error_code": str(payload.get("error_code") or "pix_final_link_failed"),
        }

    final_url = str(
        upstream.get("hosted_instructions_url")
        or upstream.get("pix_url")
        or upstream.get("long_url")
        or ""
    ).strip()
    if not is_pix_instructions_url(final_url):
        return {
            "ok": False,
            "error": "Pix 协议未返回 Stripe Pix instructions 最终支付链接",
            "error_code": "pix_final_url_missing",
        }

    return {
        "ok": True,
        "provider": str(upstream.get("provider") or "pix_br"),
        "payment_method": "pix",
        "currency": "BRL",
        "billing_country": "BR",
        "checkout_final_kind": "stripe_pix_instructions",
        "checkout_final_url": final_url,
        "hosted_instructions_url": final_url,
        "pix_url": final_url,
        "long_url": final_url,
        "checkout_session_id": upstream.get("checkout_session_id"),
        "processor_entity": upstream.get("processor_entity"),
        "chatgpt_checkout_url": upstream.get("chatgpt_checkout_url"),
        "pay_amount_minor": int(upstream.get("pay_amount_minor") or 0),
        "pay_amount": upstream.get("pay_amount"),
        "is_free_trial": bool(upstream.get("is_free_trial")),
        "qr_image_url_png": upstream.get("qr_image_url_png") or None,
        "qr_image_url_svg": upstream.get("qr_image_url_svg") or None,
        "qr_expires_at": upstream.get("qr_expires_at"),
        "pix_qr_code": upstream.get("pix_qr_code") or None,
        "checkout_billing_name": upstream.get("checkout_billing_name"),
        "checkout_billing_email": upstream.get("checkout_billing_email"),
        "checkout_billing_address": upstream.get("checkout_billing_address"),
        "checkout_billing_tax_id": upstream.get("checkout_billing_tax_id"),
    }


def main() -> int:
    access_token, session_token = load_token()
    if not access_token:
        log("access_token 为空", "[ERROR] ")
        return 1

    proxy_seeds = load_proxy_seeds()
    flow_mode = os.environ.get("PIX_FLOW_MODE", "single").strip().lower() or "single"
    if flow_mode != "single":
        log(f"PIX_FLOW_MODE={flow_mode} 已收敛为 strict single seed 链路", "[WARN] ")
    return run_single_link_mode(access_token, session_token, proxy_seeds)


if __name__ == "__main__":
    sys.exit(main())
