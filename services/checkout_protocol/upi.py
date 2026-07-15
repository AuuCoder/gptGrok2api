"""
\tUPI 支付指令链接提取脚本。

说明：
- UPI 成功后会返回 Stripe 的支付指令链接。
- 本脚本只输出 `payments.stripe.com/upi/instructions/` 最终链接或其上游跳转链接。
- 默认只读取当前项目的 proxy_seeds.txt。

用法：
  1) 环境变量方式：
     UPI_TOKEN="..." python upi_extract.py

  2) 文件方式：
     把 token 放到当前目录 token.txt
     python upi_extract.py

常用环境变量：
  UPI_CONFIRM_INLINE_PM=1   # 默认按源项目 custom-checkout 协议内联 UPI QR
  UPI_CONFIRM_VARIANTS=qr_code,empty,flow_qr,intent
  UPI_UPDATE_TAX_REGION=1   # 默认同步 Stripe 印度税务地区
  UPI_CHECKOUT_SNAPSHOT=1   # 默认同步 ChatGPT 结算页账单快照
  UPI_UPDATE_CHECKOUT_TAXES=0 # 默认不调用浏览器未使用的 checkout/taxes
  UPI_BOOTSTRAP_COUNTRY=IN  # Checkout / 首次 Stripe init 地区
  UPI_PROMOTION_COUNTRY=VN  # checkout/update 地区
  UPI_PROVIDER_COUNTRY=IN   # Stripe refresh / 税务 / PM / approve 地区
  UPI_MAX_RETRY=5
  UPI_PROVIDER_PER_CHECKOUT=1
  UPI_MAX_APPROVE_BLOCKED=5
  UPI_WORKERS=1
  UPI_WORKERS_MAX=1
  UPI_APPROVE_RETRY_MAX=10 # approve 阶段复用当前 provider 代理重试
  UPI_APPROVE_STICKY=1     # approve 优先历史/当前出口，失败后切下一个 provider 出口
  UPI_FOLLOW_REDIRECT=1
  UPI_REQUIRE_ZERO=1        # 默认强制 0 元，不生成非 0 元链
  UPI_DUMP_LIMIT=6000       # 抓包响应保存长度
  UPI_PROXY_SKIP_FAILED=1   # 普通流程失败下次软跳过
  UPI_PROXY_REMOVE_FAILED=1 # 明确的代理失败会从 proxy_seeds.txt 移除
  UPI_PROXY_DEFAULT_SCHEME=http # 裸代理默认协议；Mars SOCKS5 可设 socks5h
  UPI_PROXY_FAIL_COOLDOWN=180 # 失败代理冷却秒数，0 表示按旧逻辑一直跳过
  UPI_PROXY_REMOVE_AFTER_FAILS=3 # 已复用代理健康类失败累计 3 次移除；普通代理失败 1 次移除
  UPI_ZERO_CACHE=1          # 记录 checkout 的 0 元观察结果，供日志和排查使用
  UPI_ZERO_CACHE_SCHEDULING=0 # 显式设为 1 才按 0 元观察结果筛选/优先调度
  PP_PROMO_MODE=campaign      # 默认直接走 promo_campaign，避免 coupon 再 fallback 多耗时
  PP_TRIAL_DAYS=30            # 仅 PP_PROMO_MODE=trial/free_trial 时使用
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
from contextvars import ContextVar
from pathlib import Path
from threading import Event, RLock, local
from typing import Any, Mapping
from urllib.parse import quote, urlencode, unquote, urljoin, urlparse, urlsplit, urlunsplit

import requests

from . import stripe_token

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
UPI_UNAVAILABLE_ERROR = "当前账号支付方式不支持 UPI"
STRIPE_VERSION_FULL = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
DEFAULT_STRIPE_RUNTIME_VERSION = "299e1ea907"
CHATGPT_CLIENT_VERSION = "prod-db390ebea64862bf1899c420a4c736e0cf639747"
CHATGPT_CLIENT_BUILD_NUMBER = "7904904"
DEFAULT_STRIPE_PK = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
_ACTIVE_PLAN_NAME: ContextVar[str] = ContextVar("upi_active_plan_name", default="chatgptplusplan")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
def configured_country(name: str, default: str) -> str:
    value = str(os.environ.get(name, default) or default).strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", value):
        raise RuntimeError(f"{name} 必须是两位国家代码")
    return value


def configured_countries(name: str, default: str) -> list[str]:
    value = str(os.environ.get(name, default) or default)
    countries = [part.strip().upper() for part in value.split(",")]
    if not countries or any(not re.fullmatch(r"[A-Z]{2}", country) for country in countries):
        raise RuntimeError(f"{name} 必须是以逗号分隔的两位国家代码")
    return countries


UPI_BOOTSTRAP_COUNTRY = configured_country(
    "UPI_BOOTSTRAP_COUNTRY", os.environ.get("UPI_CHECKOUT_COUNTRY", "IN")
)
UPI_PROMOTION_COUNTRIES = configured_countries("UPI_PROMOTION_COUNTRY", "VN")
UPI_PROMOTION_COUNTRY = UPI_PROMOTION_COUNTRIES[0]
UPI_PROVIDER_COUNTRY = configured_country(
    "UPI_PROVIDER_COUNTRY", os.environ.get("UPI_BILLING_COUNTRY", "IN")
)

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

DEFAULT_UPI_BILLING = {
    "email": "redacted@example.invalid",
    "name": "Aarav Sharma",
    "country": "IN",
    "line1": "12 Mahatma Gandhi Road",
    "line2": "Sindhu Wadi, Ghatkopar East",
    "city": "Mumbai",
    "postal_code": "400077",
    "state": "Maharashtra",
}

IN_BILLING_NAMES = [
    ("Aisha", "Sharma"),
    ("Arjun", "Mehta"),
    ("Kavya", "Gupta"),
    ("Rohan", "Kapoor"),
    ("Priya", "Nair"),
]

IN_BILLING_ADDRESSES = [
    (
        "12 Mahatma Gandhi Road",
        "Sindhu Wadi, Ghatkopar East",
        "Mumbai",
        "400077",
        "Maharashtra",
    ),
    ("24 Park Street", "Taltala", "Kolkata", "700016", "West Bengal"),
    ("14 MG Road", "Ashok Nagar", "Bengaluru", "560001", "Karnataka"),
    ("18 Marine Drive", "Churchgate", "Mumbai", "400020", "Maharashtra"),
    ("32 Connaught Place", "Rajiv Chowk", "New Delhi", "110001", "Delhi"),
]

EMAIL_DOMAINS = ("gmail.com", "outlook.com", "icloud.com", "hotmail.com")

_log_file = LOG_DIR / f"upi_{time.strftime('%Y%m%d-%H%M%S')}.log"
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
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._=-]+", r"\1***", text, flags=re.IGNORECASE)
    text = re.sub(r"(__Secure-next-auth\.session-token=)[^;\s]+", r"\1***", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)(accessToken|access_token|sessionToken|session_token|client_secret)(['\"]?\s*[:=]\s*['\"])[^'\"&\s]+",
        r"\1\2***",
        text,
    )
    text = re.sub(r"(?i)(client_secret=)[^&\s'\"]+", r"\1***", text)
    text = re.sub(r"(?i)(['\"](?:cookie|set-cookie)['\"]\s*:\s*['\"])[^'\"]+", r"\1***", text)
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


def report_progress(progress: Any | None, stage: str, detail: str) -> None:
    """Emit one credential-free protocol milestone to the host task table."""
    if progress is None:
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


def is_checkout_not_active_error(value: Any) -> bool:
    return "checkout_not_active_session" in str(value)


def is_user_already_paid_error(value: Any) -> bool:
    return "user is already paid" in str(value or "").lower()


def is_upi_unavailable_error(value: Any) -> bool:
    text = str(value or "")
    return UPI_UNAVAILABLE_ERROR in text or "当前 checkout 不支持 UPI" in text


def random_user_agent() -> str:
    return DEFAULT_USER_AGENT


def active_plan_name() -> str:
    """Return the per-run plan without relying on process-global mutation."""
    return str(_ACTIVE_PLAN_NAME.get() or "chatgptplusplan").strip() or "chatgptplusplan"


def stripe_browser_id() -> str:
    return f"{uuid.uuid4()}{uuid.uuid4().hex[:8]}"


def build_email(first_name: str, last_name: str) -> str:
    first = re.sub(r"[^a-z]", "", first_name.lower())
    last = re.sub(r"[^a-z]", "", last_name.lower())
    suffix = random.randint(10000, 999999)
    domain = random.choice(EMAIL_DOMAINS)
    if random.random() < 0.5:
        local = f"{first}.{last}{suffix}"
    else:
        local = f"{first}{last}{suffix}"
    return f"{local}@{domain}"


def normalize_country(country: str) -> str:
    value = str(country or "").strip().upper()
    return value if value in COUNTRY_CURRENCY else "IN"


def currency_for_country(country: str) -> str:
    return COUNTRY_CURRENCY.get(normalize_country(country), "INR")


def payment_browser_locale() -> str:
    return os.environ.get("UPI_BROWSER_LOCALE", "en-IN").strip() or "en-IN"


def payment_elements_locale() -> str:
    return os.environ.get("UPI_ELEMENTS_LOCALE", "en").strip() or "en"


def payment_browser_timezone() -> str:
    return os.environ.get("UPI_BROWSER_TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"


def saved_payment_value() -> str:
    return os.environ.get("UPI_SAVED_PAYMENT_VALUE", "never").strip() or "never"


def payment_accept_language() -> str:
    locale = payment_browser_locale()
    if locale.lower().startswith("en"):
        return f"{locale},en;q=0.9"
    return f"{locale},{locale.split('-', 1)[0]};q=0.9,en;q=0.8"


def normalize_proxy_url(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = f"{default_proxy_scheme()}://{proxy}"

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
    raw = os.environ.get("UPI_PROXY_DEFAULT_SCHEME", "http").strip().lower()
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
_KOOKEEY_COUNTRY_SELECTOR_RE = re.compile(
    r"(?i)^(?P<prefix>.+-)(?P<value>global|[a-z]{2})"
    r"(?P<suffix>(?:-session-[a-z0-9]+(?:-life-\d+[a-z]?)?|-[a-z0-9]+))$"
)
_KOOKEEY_PROXY_HOSTS = {"gate.kookeey.info", "mobile.kookeey.info"}


def replace_proxy_country_selectors(
    value: str,
    replacement: str,
    include_kookeey: bool = False,
) -> tuple[str, int]:
    replacements = 0

    def replacement_value(current: str) -> str:
        if replacement == "*":
            return replacement
        return replacement.upper() if current.isupper() else replacement.lower()

    def replace_named(match: re.Match[str]) -> str:
        nonlocal replacements
        replacements += 1
        return (
            f"{match.group('name')}{match.group('separator')}"
            f"{replacement_value(match.group('value'))}"
        )

    value = _PROXY_COUNTRY_SELECTOR_RE.sub(replace_named, value)

    if include_kookeey:
        def replace_kookeey(match: re.Match[str]) -> str:
            nonlocal replacements
            replacements += 1
            country = replacement if replacement == "*" else replacement.upper()
            return f"{match.group('prefix')}{country}{match.group('suffix')}"

        value = _KOOKEEY_COUNTRY_SELECTOR_RE.sub(replace_kookeey, value)

    return value, replacements


def proxy_chain_key(proxy: str) -> str:
    """Return a redacted identity that stays stable across country rewrites."""
    proxy = normalize_proxy_url(proxy)
    if not proxy:
        return ""
    parsed = urlsplit(proxy)
    hostname = parsed.hostname or ""
    include_kookeey = hostname.lower() in _KOOKEEY_PROXY_HOSTS
    username, _ = replace_proxy_country_selectors(unquote(parsed.username or ""), "*")
    password, _ = replace_proxy_country_selectors(
        unquote(parsed.password or ""),
        "*",
        include_kookeey=include_kookeey,
    )
    if parsed.username is None and parsed.password is None:
        normalized = proxy
    else:
        host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        auth = quote(username, safe="-._~")
        if parsed.password is not None:
            auth = f"{auth}:{quote(password, safe='-._~')}"
        normalized = urlunsplit(
            (parsed.scheme, f"{auth}@{host}", parsed.path, parsed.query, parsed.fragment)
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
    include_kookeey = (parsed.hostname or "").lower() in _KOOKEEY_PROXY_HOSTS
    username, username_replacements = replace_proxy_country_selectors(username, target_country)
    password, password_replacements = replace_proxy_country_selectors(
        password,
        target_country,
        include_kookeey=include_kookeey,
    )
    replacements = username_replacements + password_replacements
    if not replacements:
        raise RuntimeError(
            "代理未包含可改写的 country/region 选择器或 Kookeey 地区段: "
            f"{proxy_label(proxy)}"
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


def upi_proxy_chain(proxy_seed: str) -> tuple[str, str, str]:
    """Keep one sticky seed across configured checkout, promotion, and provider stages."""
    checkout_proxy = proxy_for_country(proxy_seed, UPI_BOOTSTRAP_COUNTRY)
    promotion_proxies = [
        proxy_for_country(proxy_seed, country) for country in UPI_PROMOTION_COUNTRIES
    ]
    provider_proxy = proxy_for_country(proxy_seed, UPI_PROVIDER_COUNTRY)
    chain_key = proxy_chain_key(proxy_seed)
    if not chain_key or any(
        proxy_chain_key(proxy) != chain_key
        for proxy in (checkout_proxy, *promotion_proxies, provider_proxy)
    ):
        raise RuntimeError("代理地区改写改变了 sticky seed，已拒绝混用代理链")
    return checkout_proxy, promotion_proxies[0], provider_proxy


def log_upi_proxy_chain(proxy_seed: str, checkout_proxy: str, promotion_proxy: str, provider_proxy: str) -> None:
    promotion_steps = " -> ".join(
        f"{country} promotion={proxy_label(proxy_for_country(promotion_proxy, country))}"
        for country in UPI_PROMOTION_COUNTRIES
    )
    log(
        "派生代理链: "
        f"chain={proxy_chain_key(proxy_seed)}; seed={proxy_label(proxy_seed)}; "
        f"{UPI_BOOTSTRAP_COUNTRY} checkout={proxy_label(checkout_proxy)}; "
        f"{promotion_steps}; "
        f"{UPI_PROVIDER_COUNTRY} provider/approve={proxy_label(provider_proxy)}"
    )


def normalize_pre_proxy_url(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = f"socks5h://{proxy}"
    return normalize_proxy_url(proxy)


def proxy_state_path() -> Path:
    raw = os.environ.get("UPI_PROXY_STATE_FILE", "").strip()
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
    if not proxy or not env_bool("UPI_PROXY_SCORE", True):
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
    return env_int("UPI_PROXY_REMOVE_AFTER_FAILS", 3)


def is_reused_proxy_record(group: str, record: dict[str, Any]) -> bool:
    return int(record.get("success") or 0) > 0


def record_proxy_health_failure(group: str, proxy: str, reason: str) -> None:
    record = record_proxy_result(group, proxy, False, reason)
    fail_count = int(record.get("fail") or 0)
    remove_after = proxy_remove_after_fails() if is_reused_proxy_record(group, record) else 1
    if fail_count >= remove_after:
        remove_failed_proxy(group, proxy, reason)


def checkout_zero_cache_ttl() -> int:
    return env_int("UPI_ZERO_CACHE_TTL", 86400, minimum=0)


def zero_cache_scheduling_enabled() -> bool:
    return env_bool("UPI_ZERO_CACHE_SCHEDULING", False)


def checkout_zero_cache_status(proxy: str, country: str) -> tuple[str, int, int]:
    if not proxy or not env_bool("UPI_ZERO_CACHE", True):
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
    if record.get("zero_ok") is True:
        return "ok", amount, checked_at
    if record.get("zero_ok") is False:
        return "bad", amount, checked_at
    return "", amount, checked_at


def record_checkout_zero_result(proxy: str, country: str, amount: int) -> None:
    if not proxy or not env_bool("UPI_ZERO_CACHE", True):
        return
    record = proxy_record("seed", proxy)
    if not record:
        return
    amount = int(amount or 0)
    record["zero_ok"] = amount == 0
    record["zero_amount"] = amount
    record["zero_country"] = normalize_country(country)
    record["zero_checked_at"] = int(time.time())
    if amount == 0:
        record["zero_success"] = int(record.get("zero_success") or 0) + 1
    save_proxy_state()


def record_proxy_pair_result(checkout_proxy: str, provider_proxy: str, success: bool, reason: str = "") -> None:
    record_proxy_result("checkout", checkout_proxy, success, reason)
    record_proxy_result("provider", provider_proxy, success, reason)
    if not checkout_proxy or not provider_proxy or not env_bool("UPI_PROXY_SCORE", True):
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
    if not checkout_proxy or not provider_proxy or not approve_proxy or not env_bool("UPI_PROXY_SCORE", True):
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
    if not env_bool("UPI_PROXY_SCORE", True):
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
    if is_upi_unavailable_error(reason):
        return
    if "0 元优惠未生效" in reason:
        return
    if "approve blocked" in reason:
        return
    if "promotion 阶段失败" in reason or "checkout/update" in reason:
        record_seed_failure(promotion_proxy)
        return
    record_seed_failure(provider_proxy)


def order_proxy_group(group: str, proxies: list[str]) -> list[str]:
    if not env_bool("UPI_PROXY_SCORE", True):
        return proxies
    state = load_proxy_state().get(group, {})
    skip_failed = env_bool("UPI_PROXY_SKIP_FAILED", True)
    fail_threshold = env_int("UPI_PROXY_FAIL_SKIP_AFTER", 1)
    fail_cooldown = env_int("UPI_PROXY_FAIL_COOLDOWN", 180, minimum=0)
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
            and env_bool("UPI_ZERO_CACHE_SKIP_BAD", True)
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
            f"{group} 代理状态: 成功优先={success_seen}，0元命中={zero_seen}，"
            f"冷却跳过={cooldown_skipped}，0元失败跳过={zero_skipped}"
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
    for name in ("UPI_PRE_PROXY", "PP_PRE_PROXY", "PP_LOCAL_PROXY"):
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
        os.environ.get("UPI_PROXY_SEED_FILE", "").strip()
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
    if not failures or not env_bool("UPI_PROXY_REMOVE_FAILED", True):
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
        f"{UPI_BOOTSTRAP_COUNTRY} Checkout → {' → '.join(UPI_PROMOTION_COUNTRIES)} checkout/update → "
        f"{UPI_PROVIDER_COUNTRY} Stripe/UPI/approve"
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
    text = re.sub(r"(?im)^(cookie:\s*).*?$", r"\1***", text)
    text = re.sub(r"(?im)^(set-cookie:\s*).*?$", r"\1***", text)
    text = re.sub(r"(?i)(['\"](?:cookie|set-cookie)['\"]\s*:\s*['\"])[^'\"]+", r"\1***", text)
    text = re.sub(
        r"(accessToken|access_token|sessionToken|session_token|token|client_secret)(['\"]?\s*[:=]\s*['\"])[^'\"&\s]+",
        r"\1\2***",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?i)(client_secret=)[^&\s'\"]+", r"\1***", text)
    text = redact_log_text(text)
    if limit is None:
        limit = env_int("UPI_DUMP_LIMIT", 6000, minimum=500)
    return text[:limit]


def dump_http(
    response: requests.Response | None,
    stage: str,
    request_body: Any = None,
    request_method: str = "",
    request_url: str = "",
    force: bool = False,
) -> None:
    # The embedded service must not retain raw Stripe/OpenAI payloads by
    # default.  ``force`` selects useful diagnostic stages only after an
    # operator has explicitly enabled UPI_DUMP=1.
    if not env_bool("UPI_DUMP", False):
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
    for env_name in ("PP_TOKEN", "UPI_TOKEN"):
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


def build_chatgpt_session(access_token: str, device_id: str, proxy: str, session_token: str = "") -> requests.Session:
    session = new_session(proxy)
    cookie = f"oai-did={device_id}"
    if session_token:
        cookie += f"; __Secure-next-auth.session-token={session_token}"
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": payment_accept_language(),
            "Authorization": f"Bearer {access_token}",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "Content-Type": "application/json",
            "oai-device-id": device_id,
            "oai-language": payment_browser_locale(),
            "oai-session-id": device_id,
            "oai-client-version": CHATGPT_CLIENT_VERSION,
            "oai-client-build-number": CHATGPT_CLIENT_BUILD_NUMBER,
            "sec-ch-ua": '"Safari";v="17", "Not.A/Brand";v="8"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Cookie": cookie,
        }
    )
    return session


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
    promo_mode = os.environ.get("PP_PROMO_MODE", "campaign").strip().lower() or "campaign"
    promo_id = os.environ.get("PP_PROMO_ID", "plus-1-month-free").strip()
    body: dict[str, Any] = {
        "entry_point": os.environ.get("PP_ENTRY_POINT", "all_plans_pricing_modal"),
        "plan_name": active_plan_name(),
        "billing_details": {"country": country, "currency": currency_for_country(country)},
        "checkout_ui_mode": "custom",
    }
    if promo_mode in ("trial", "free_trial"):
        trial_days = env_int("PP_TRIAL_DAYS", 30)
        body["subscription_data"] = {"trial_period_days": trial_days}
    elif promo_mode in ("campaign", "query"):
        body["promo_campaign"] = {
            "promo_campaign_id": promo_id,
            "is_coupon_from_query_param": promo_mode == "query",
        }
    elif promo_mode == "coupon":
        body["coupon"] = promo_id
    elif promo_mode == "code":
        body["promotion_code"] = promo_id
    elif promo_mode != "off":
        log(f"未知 PP_PROMO_MODE={promo_mode!r}，已忽略", "[WARN] ")
    log(f"Checkout promo: mode={promo_mode}, id={promo_id}")

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
    if resp.status_code >= 400:
        if is_user_already_paid_error(resp.text):
            raise RuntimeError("用户已支付: User is already paid")
        raise RuntimeError(f"checkout 创建失败 HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json() or {}
    if (
        promo_mode == "coupon"
        and promo_id == "plus-1-month-free"
        and not checkout_response_has_promo(data)
        and env_bool("UPI_COUPON_FALLBACK_PROMO_CAMPAIGN", True)
    ):
        log("coupon 响应未显示优惠，按 promo_campaign 字符串重试", "[PROMO] ")
        fallback_body = dict(body)
        fallback_body.pop("coupon", None)
        fallback_body["promo_campaign"] = promo_id
        resp = chatgpt.post(
            "https://chatgpt.com/backend-api/payments/checkout",
            json=fallback_body,
            headers=headers,
            timeout=CHATGPT_TIMEOUT,
        )
        dump_http(
            resp,
            "checkout_promo_campaign",
            fallback_body,
            "POST",
            "https://chatgpt.com/backend-api/payments/checkout",
            force=True,
        )
        if resp.status_code >= 400:
            if is_user_already_paid_error(resp.text):
                raise RuntimeError("用户已支付: User is already paid")
            raise RuntimeError(f"checkout promo_campaign 重试失败 HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json() or {}
        log(f"promo_campaign 重试后 promo={checkout_response_has_promo(data)}", "[PROMO] ")

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
        f"mode={promo_mode} / promo={checkout_response_has_promo(data)} / "
        f"trial={checkout_response_has_trial(data)}"
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
        UPI_BOOTSTRAP_COUNTRY,
        checkout.get("processor_entity") or "",
    )
    return f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}"


def update_checkout_promotion(
    chatgpt: requests.Session,
    checkout: dict[str, str],
    promotion_country: str,
) -> None:
    mode = os.environ.get("PP_PROMO_MODE", "campaign").strip().lower() or "campaign"
    promo_id = os.environ.get("PP_PROMO_ID", "plus-1-month-free").strip() or "plus-1-month-free"
    body: dict[str, Any] = {
        "checkout_session_id": checkout["cs_id"],
        "processor_entity": processor_entity_for_country(
            UPI_BOOTSTRAP_COUNTRY,
            checkout.get("processor_entity") or "",
        ),
        "plan_name": active_plan_name(),
        "price_interval": "month",
        "seat_quantity": 1,
    }
    if mode in {"campaign", "query", "coupon"}:
        body["promo_campaign"] = {
            "promo_campaign_id": promo_id,
            "is_coupon_from_query_param": mode == "query",
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
    log(f"{promotion_country} checkout/update 成功: promo={promo_id if 'promo_campaign' in body else 'off'}")


def update_upi_checkout_taxes(
    chatgpt: requests.Session,
    checkout: dict[str, str],
    billing: dict[str, str],
) -> None:
    url = "https://chatgpt.com/backend-api/payments/checkout/taxes"
    body = {
        "checkout_session_id": checkout["cs_id"],
        "checkout_email": billing["email"],
        "billing_country": UPI_PROVIDER_COUNTRY,
        "billing_name": billing["name"],
        "currency": currency_for_country(UPI_PROVIDER_COUNTRY),
        "tax_id": None,
        "processor_entity": processor_entity_for_country(
            UPI_BOOTSTRAP_COUNTRY,
            checkout.get("processor_entity") or "",
        ),
        "billing_address": {
            "line1": billing["line1"],
            "line2": billing.get("line2", ""),
            "city": billing["city"],
            "country": UPI_PROVIDER_COUNTRY,
            "postal_code": billing["postal_code"],
        },
    }
    if billing.get("state"):
        body["billing_address"]["state"] = billing["state"]
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
    if resp.status_code >= 400:
        if is_checkout_not_active_error(resp.text):
            raise RuntimeError("checkout_not_active_session")
        raise RuntimeError(f"checkout/taxes 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    log(f"{UPI_PROVIDER_COUNTRY} checkout/taxes 同步成功")


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
    stripe.headers.update(
        {"User-Agent": random_user_agent(), "Accept-Language": payment_accept_language()}
    )
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
    client_context = init_payload.get("_client_context") if isinstance(init_payload.get("_client_context"), dict) else {}
    return {
        "stripe_js_id": str(client_context.get("stripe_js_id") or init_payload.get("client_stripe_js_id") or uuid.uuid4()),
        "client_session_id": str(uuid.uuid4()),
        "guid": stripe_browser_id(),
        "muid": stripe_browser_id(),
        "sid": stripe_browser_id(),
        "elements_session_id": "",
        "elements_session_config_id": "",
        "payment_element_config_id": str(uuid.uuid4()),
        "config_id": init_payload.get("config_id") or "",
        "init_checksum": init_payload.get("init_checksum") or "",
        "checkout_amount": amount_from_payload(init_payload),
        "locale": payment_elements_locale(),
        "currency": str(init_payload.get("currency") or checkout.get("currency") or "eur").lower(),
        "runtime_version": DEFAULT_STRIPE_RUNTIME_VERSION,
        "stripe_version": STRIPE_VERSION_FULL,
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
    currency = str(ctx.get("currency") or "inr").lower()
    params = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount),
        "deferred_intent[currency]": currency,
        "deferred_intent[setup_future_usage]": "off_session",
        "deferred_intent[payment_method_types][0]": "card",
        "deferred_intent[payment_method_types][1]": "upi",
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
    if not session_id:
        raise RuntimeError(
            f"Stripe elements/sessions 缺少 session_id: keys={list(payload)[:20]}"
        )
    ctx["elements_session_id"] = session_id
    ctx["elements_session_config_id"] = str(payload.get("config_id") or "")
    return payload


def upi_billing_profile() -> dict[str, str]:
    first_name, last_name = random.choice(IN_BILLING_NAMES)
    line1, line2, city, postal_code, state = random.choice(IN_BILLING_ADDRESSES)
    profile = {
        "email": build_email(first_name, last_name),
        "name": f"{first_name} {last_name}",
        "country": "IN",
        "line1": line1,
        "line2": line2,
        "city": city,
        "postal_code": postal_code,
        "state": state,
    }
    if env_bool("UPI_USE_FIXED_BILLING", False):
        profile = dict(DEFAULT_UPI_BILLING)
    env_map = {
        "email": "UPI_EMAIL",
        "name": "UPI_NAME",
        "country": "UPI_BILLING_COUNTRY",
        "line1": "UPI_LINE1",
        "line2": "UPI_LINE2",
        "city": "UPI_CITY",
        "postal_code": "UPI_POSTAL_CODE",
        "state": "UPI_STATE",
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            profile[key] = value
    profile["country"] = normalize_country(profile.get("country", "IN"))
    return profile


def upi_billing_snapshot(billing: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact billing values submitted during a successful UPI flow."""
    return {
        "checkout_billing_name": str(billing.get("name") or ""),
        "checkout_billing_email": str(billing.get("email") or ""),
        "checkout_billing_address": {
            "country": normalize_country(str(billing.get("country") or "IN")),
            "line1": str(billing.get("line1") or ""),
            "line2": str(billing.get("line2") or ""),
            "city": str(billing.get("city") or ""),
            "state": str(billing.get("state") or ""),
            "postal_code": str(billing.get("postal_code") or ""),
        },
    }


def stripe_update_customer_data(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    ctx: dict[str, Any],
    billing: dict[str, str],
) -> bool:
    body: dict[str, Any] = {
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
        "expected_amount": str(ctx.get("checkout_amount") or 0),
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[locale]": ctx["locale"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "customer_data[email]": billing["email"],
        "customer_data[name]": billing["name"],
        "customer_data[address][country]": billing["country"],
        "customer_data[address][line1]": billing["line1"],
        "customer_data[address][city]": billing["city"],
        "customer_data[address][postal_code]": billing["postal_code"],
    }
    if billing.get("line2"):
        body["customer_data[address][line2]"] = billing["line2"]
    if billing.get("state"):
        body["customer_data[address][state]"] = billing["state"]

    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}"
    try:
        resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
        dump_http(resp, "customer_data_update_br", body, "POST", url, force=resp.status_code >= 400)
        if resp.status_code < 400:
            log(
                f"印度 customer_data 已提交: {billing['name']} / {billing['city']} / {billing['postal_code']}"
            )
            return True
        if is_checkout_not_active_error(resp.text):
            raise RuntimeError("checkout_not_active_session")
        log(f"印度 customer_data 提交失败 HTTP {resp.status_code}: {resp.text[:180]}", "[WARN] ")
    except Exception as exc:
        if is_checkout_not_active_error(exc):
            raise
        log(f"印度 customer_data 提交异常: {exc}", "[WARN] ")
    return False


def stripe_update_tax_region(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    ctx: dict[str, Any],
    billing: dict[str, str],
) -> bool:
    body: dict[str, Any] = {
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[locale]": ctx["locale"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": saved_payment_value(),
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_payment_value(),
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
        "tax_region[country]": billing["country"],
        "tax_region[postal_code]": billing["postal_code"],
        "tax_region[line1]": billing["line1"],
        "tax_region[city]": billing["city"],
    }
    if billing.get("state"):
        body["tax_region[state]"] = billing["state"]
    if billing.get("line2"):
        body["tax_region[line2]"] = billing["line2"]

    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}"
    try:
        resp = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
        dump_http(resp, "tax_region_update", body, "POST", url, force=resp.status_code >= 400)
        if resp.status_code < 400:
            log(f"tax_region 已提交: {billing['country']} / {billing['city']} {billing['postal_code']}")
            return True
        if is_checkout_not_active_error(resp.text):
            raise RuntimeError("checkout_not_active_session")
        log(f"tax_region 提交失败 HTTP {resp.status_code}: {resp.text[:180]}", "[WARN] ")
    except Exception as exc:
        if is_checkout_not_active_error(exc):
            raise
        log(f"tax_region 提交异常: {exc}", "[WARN] ")
    return False


def checkout_snapshot(chatgpt: requests.Session, checkout: dict[str, str], billing: dict[str, str]) -> None:
    cs_id = checkout["cs_id"]
    processor = processor_entity_for_country(checkout.get("billing_country", "IN"), checkout.get("processor_entity") or "")
    checkout_page_url = f"https://chatgpt.com/checkout/{processor}/{cs_id}"
    body = {
        "snapshot": {
            "billing_address": {
                "name": billing["name"],
                "address": {
                    "line1": billing["line1"],
                    "line2": billing.get("line2", ""),
                    "city": billing["city"],
                    "country": billing["country"],
                    "postal_code": billing["postal_code"],
                    "state": billing.get("state", ""),
                },
            }
        }
    }
    try:
        resp = chatgpt.post(
            "https://chatgpt.com/backend-api/payments/checkout/snapshot",
            json=body,
            headers={
                "Referer": checkout_page_url,
                "x-openai-target-path": "/backend-api/payments/checkout/snapshot",
                "x-openai-target-route": "/backend-api/payments/checkout/snapshot",
            },
            timeout=CHATGPT_TIMEOUT,
        )
        dump_http(resp, "checkout_snapshot", body, "POST", "https://chatgpt.com/backend-api/payments/checkout/snapshot", force=env_bool("UPI_DUMP_WARMUP", False) or resp.status_code >= 400)
        if resp.status_code >= 400:
            if is_checkout_not_active_error(resp.text):
                raise RuntimeError("checkout_not_active_session")
            log(f"checkout snapshot 失败 HTTP {resp.status_code}: {resp.text[:180]}", "[WARN] ")
        else:
            log("checkout snapshot 已提交")
    except Exception as exc:
        if is_checkout_not_active_error(exc):
            raise
        log(f"checkout snapshot 异常: {exc}", "[WARN] ")


def stripe_create_upi_pm(stripe: requests.Session, cs_id: str, stripe_pk: str, billing: dict[str, str], ctx: dict[str, Any]) -> str:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body: dict[str, Any] = {
        "type": "upi",
        "billing_details[name]": billing.get("name") or "Aisha Sharma",
        "billing_details[email]": billing.get("email") or "redacted@example.invalid",
        "billing_details[address][country]": billing.get("country") or "IN",
        "billing_details[address][line1]": billing.get("line1") or "24 Park Street",
        "billing_details[address][city]": billing.get("city") or "Kolkata",
        "billing_details[address][postal_code]": billing.get("postal_code") or "700016",
        "guid": str(ctx.get("guid") or stripe_browser_id()),
        "muid": str(ctx.get("muid") or stripe_browser_id()),
        "sid": str(ctx.get("sid") or stripe_browser_id()),
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
        "key": stripe_pk,
        "payment_user_agent": (
            f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; checkout"
        ),
        "client_attribution_metadata[client_session_id]": str(
            ctx.get("stripe_js_id") or ctx.get("client_session_id") or ""
        ),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[checkout_config_id]": str(ctx.get("config_id") or ""),
    }
    if billing.get("state"):
        body["billing_details[address][state]"] = billing["state"]

    resp = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=DEFAULT_TIMEOUT)
    dump_http(resp, "upi_pm", body, "POST", "https://api.stripe.com/v1/payment_methods", force=resp.status_code >= 400)
    if resp.status_code >= 400:
        raise RuntimeError(f"创建 UPI PM 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    pm_id = str((resp.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"创建 UPI PM 响应异常: {resp.text[:300]}")
    return pm_id


def add_inline_upi_payment_method_data(
    body: dict[str, Any],
    cs_id: str,
    billing: dict[str, str],
    ctx: dict[str, Any],
    variant: str = "qr_code",
) -> None:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body.update(
        {
            "payment_method_data[type]": "upi",
            "payment_method_data[billing_details][name]": billing["name"],
            "payment_method_data[billing_details][email]": billing["email"],
            "payment_method_data[billing_details][address][country]": billing["country"],
            "payment_method_data[billing_details][address][line1]": billing["line1"],
            "payment_method_data[billing_details][address][city]": billing["city"],
            "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
            "payment_method_data[payment_user_agent]": (
                f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; "
                "payment-element; deferred-intent"
            ),
            "payment_method_data[referrer]": "https://chatgpt.com",
            "payment_method_data[time_on_page]": str(random.randint(18000, 55000)),
            "payment_method_data[client_attribution_metadata][checkout_session_id]": cs_id,
            "payment_method_data[client_attribution_metadata][client_session_id]": ctx["stripe_js_id"],
            "payment_method_data[client_attribution_metadata][checkout_config_id]": (
                ctx.get("payment_element_config_id") or str(uuid.uuid4())
            ),
            "payment_method_data[client_attribution_metadata][elements_session_id]": ctx["elements_session_id"],
            "payment_method_data[client_attribution_metadata][elements_session_config_id]": ctx["elements_session_config_id"],
            "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
            "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
            "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
            "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
            "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "automatic",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]": "expressCheckout",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][1]": "payment",
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][2]": "address",
        }
    )
    if variant == "flow_qr":
        body["payment_method_data[upi][flow]"] = "qr_code"
    elif variant == "intent":
        body["payment_method_data[upi][intent]"] = "qr_code"
    elif variant not in ("qr_code", "empty"):
        raise RuntimeError(f"未知 UPI confirm variant: {variant}")
    if billing.get("state"):
        body["payment_method_data[billing_details][address][state]"] = billing["state"]
    if billing.get("line2"):
        body["payment_method_data[billing_details][address][line2]"] = billing["line2"]


def stripe_confirm_return_url(
    cs_id: str,
    checkout: Mapping[str, Any],
    stripe_hosted_url: str,
) -> str:
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "IN"),
        str(checkout.get("processor_entity") or ""),
    )
    verify_url = "https://chatgpt.com/checkout/verify?" + urlencode(
        {
            "stripe_session_id": cs_id,
            "processor_entity": processor,
            "plan_type": "plus",
        }
    )
    hosted_url = stripe_hosted_url or f"https://checkout.stripe.com/c/pay/{cs_id}"
    parts = urlsplit(hosted_url)
    query = urlencode(
        {
            "returned_from_redirect": "true",
            "ui_mode": "custom",
            "return_url": verify_url,
        }
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def processor_entity_for_country(country: str, processor_entity: str = "") -> str:
    if processor_entity:
        return processor_entity
    return "openai_llc" if normalize_country(country) == "US" else "openai_ie"


def stripe_confirm_upi(
    stripe: requests.Session,
    cs_id: str,
    pm_id: str,
    stripe_pk: str,
    init_payload: dict[str, Any],
    ctx: dict[str, Any],
    checkout: dict[str, str],
    stripe_hosted_url: str,
    billing: dict[str, str],
    *,
    token_config: stripe_token.StripeTokenConfig | None = None,
    variant: str = "qr_code",
) -> dict[str, Any]:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    inline_payment_method = env_bool("UPI_CONFIRM_INLINE_PM", True)
    body = {
        "expected_amount": os.environ.get("PP_EXPECTED_AMOUNT", "").strip() or str(ctx.get("checkout_amount") or amount_from_payload(init_payload)),
        "expected_payment_method_type": "upi",
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
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": str(ctx.get("elements_session_id") or ""),
        "client_attribution_metadata[elements_session_config_id]": str(
            ctx.get("elements_session_config_id") or ""
        ),
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
    }
    body.update(stripe_elements_session_params(ctx))
    if inline_payment_method:
        if token_config is None:
            raise RuntimeError("Stripe 动态 token 配置缺失，停止发送不完整 confirm")
        body.update(
            stripe_token.build_token_fields(
                ppage_id=str(init_payload.get("id") or ""),
                config=token_config,
            )
        )
        add_inline_upi_payment_method_data(body, cs_id, billing, ctx, variant)
    else:
        body["payment_method"] = pm_id
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm"
    resp = stripe.post(
        url,
        data=body,
        headers=stripe_api_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    dump_http(resp, "upi_confirm", body, "POST", url, force=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"UPI confirm 失败 HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json() or {}


def configured_confirm_variants() -> list[str]:
    raw = os.environ.get("UPI_CONFIRM_VARIANTS", "qr_code,empty,flow_qr,intent")
    variants = list(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    allowed = {"qr_code", "empty", "flow_qr", "intent"}
    if not variants or any(variant not in allowed for variant in variants):
        raise RuntimeError(
            "UPI_CONFIRM_VARIANTS 只允许 qr_code,empty,flow_qr,intent"
        )
    return variants


def stripe_confirm_upi_variants(
    stripe: requests.Session,
    cs_id: str,
    pm_id: str,
    stripe_pk: str,
    init_payload: dict[str, Any],
    ctx: dict[str, Any],
    checkout: dict[str, str],
    stripe_hosted_url: str,
    billing: dict[str, str],
    token_config: stripe_token.StripeTokenConfig,
) -> tuple[dict[str, Any], str]:
    variants = configured_confirm_variants() if env_bool("UPI_CONFIRM_INLINE_PM", True) else ["empty"]
    last_error = ""
    for index, variant in enumerate(variants, start=1):
        try:
            payload = stripe_confirm_upi(
                stripe,
                cs_id,
                pm_id,
                stripe_pk,
                init_payload,
                ctx,
                checkout,
                stripe_hosted_url,
                billing,
                token_config=token_config,
                variant=variant,
            )
            log(f"Stripe confirm variant={variant} 成功 ({index}/{len(variants)})")
            return payload, variant
        except Exception as exc:
            if is_checkout_not_active_error(exc):
                raise
            last_error = str(exc)
            log(
                f"Stripe confirm variant={variant} 失败 ({index}/{len(variants)}): "
                f"{last_error[:180]}",
                "[WARN] ",
            )
    raise RuntimeError(f"所有 UPI confirm variant 均失败: {last_error}")


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


def is_upi_instructions_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
        port = parsed.port
    except ValueError:
        return False
    prefix = "/upi/instructions/"
    path = parsed.path or ""
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname == "payments.stripe.com"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and path.startswith(prefix)
        and bool(path[len(prefix) :].strip("/"))
        and not parsed.fragment
    )


def is_redirect_like_url(url: str, from_action_field: bool = False) -> bool:
    if not isinstance(url, str):
        return False
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False
    if is_resource_url(url):
        return False
    if is_upi_instructions_url(url):
        return True
    if from_action_field:
        return True

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    text = f"{host}{path}?{query}"
    if host in {"hooks.stripe.com", "payments.stripe.com"}:
        return True
    return any(part in text for part in ("upi", "/redirect/", "redirect_to_url", "authenticate"))


def is_qr_candidate(url: str) -> bool:
    lower = url.lower()
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


def qr_candidate_summary(url: str) -> dict[str, Any]:
    value = str(url or "").strip()
    if value.lower().startswith("data:image/"):
        media_type = value.split(";", 1)[0].split(":", 1)[-1]
        return {"kind": "data_image", "media_type": media_type, "length": len(value)}
    try:
        parsed = urlparse(value)
    except ValueError:
        return {"kind": "invalid", "length": len(value)}
    return {
        "kind": "url",
        "scheme": parsed.scheme.lower(),
        "host": (parsed.hostname or "").lower(),
        "path_has_qr": "qr" in (parsed.path or "").lower(),
        "query_has_qr": "qr" in (parsed.query or "").lower(),
        "resource": is_resource_url(value),
        "upi_instructions": is_upi_instructions_url(value),
        "length": len(value),
    }


def log_qr_candidate_summaries(stage: str, urls: list[str]) -> None:
    if not urls:
        return
    summaries = [qr_candidate_summary(url) for url in urls]
    log(f"{stage} QR 候选摘要: {json.dumps(summaries, ensure_ascii=False)}")


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
            hosted_instructions_url = str(next_action.get("hosted_instructions_url") or "").strip()
            if is_upi_instructions_url(hosted_instructions_url):
                return hosted_instructions_url
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
            "hosted_instructions_url",
            "redirect_url",
            "redirect_to_url",
            "authorization_url",
            "authentication_url",
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


def setup_intent_error_summary(payload: Any, current_pm_id: str = "") -> dict[str, Any]:
    if isinstance(payload, dict):
        payload_id = str(payload.get("id") or "").strip()
        is_setup_intent = payload.get("object") == "setup_intent" or payload_id.startswith("seti_")
        candidates: list[dict[str, Any]] = [payload] if is_setup_intent else []
        nested_intent = payload.get("setup_intent")
        if isinstance(nested_intent, dict) and all(nested_intent is not item for item in candidates):
            candidates.append(nested_intent)
        for setup_intent in candidates:
            last_error = setup_intent.get("last_setup_error")
            if not isinstance(last_error, dict):
                continue
            error_pm = last_error.get("payment_method")
            error_pm_id = ""
            error_pm_type = ""
            if isinstance(error_pm, dict):
                error_pm_id = str(error_pm.get("id") or "").strip()
                error_pm_type = str(error_pm.get("type") or "").strip()
            elif isinstance(error_pm, str):
                error_pm_id = error_pm.strip()
            if current_pm_id and error_pm_id and error_pm_id != current_pm_id:
                continue
            summary = {
                "setup_intent_status": setup_intent.get("status"),
                "error_type": last_error.get("type"),
                "error_code": last_error.get("code"),
                "decline_code": last_error.get("decline_code"),
                "payment_method_type": error_pm_type,
                "payment_method_matches_current": bool(
                    current_pm_id and error_pm_id and error_pm_id == current_pm_id
                ),
            }
            return {key: value for key, value in summary.items() if value not in (None, "")}
        for value in payload.values():
            found = setup_intent_error_summary(value, current_pm_id=current_pm_id)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = setup_intent_error_summary(value, current_pm_id=current_pm_id)
            if found:
                return found
    return {}


def raise_if_setup_intent_blocked(payload: Any, context: str, current_pm_id: str = "") -> None:
    last_error = setup_intent_last_error(payload, current_pm_id=current_pm_id)
    if not last_error:
        return
    summary = setup_intent_error_summary(payload, current_pm_id=current_pm_id)
    if summary:
        log(f"{context} SetupIntent 拒绝摘要: {format_payment_summary(summary)}", "[WARN] ")
    if "generic_decline" in last_error.lower():
        raise RuntimeError(f"Stripe 风控拒绝（generic_decline）：{context} SetupIntent 创建失败，未生成 redirect_url")
    raise RuntimeError(f"{context}: setup_intent.last_setup_error: {last_error}")


def should_retry_second_confirm_after_approve(error: Any) -> bool:
    text = str(error or "").lower()
    return (
        "checkout_upcoming_invoice_mismatch" in text
        or "redirect url resolution timeout" in text
        or "missing_redirect" in text
        or "generic_decline" in text
    )


def stripe_intent_redirect_url(
    stripe: requests.Session,
    intent_payload: Any,
    stripe_pk: str,
    current_pm_id: str = "",
) -> str:
    if not isinstance(intent_payload, dict):
        return ""
    intent_id = str(intent_payload.get("id") or "").strip()
    client_secret = str(intent_payload.get("client_secret") or "").strip()
    if not intent_id or not client_secret:
        return ""
    intent_object = str(intent_payload.get("object") or "").strip()
    intent_path = "setup_intents" if intent_object == "setup_intent" or intent_id.startswith("seti_") else "payment_intents"
    params = {"key": stripe_pk, "client_secret": client_secret}
    url = f"https://api.stripe.com/v1/{intent_path}/{intent_id}"
    resp = stripe.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    dump_http(resp, "stripe_intent_get", params, "GET", url, force=resp.status_code >= 400)
    if resp.status_code != 200:
        return ""
    try:
        payload = resp.json() or {}
    except Exception:
        payload = {"_raw_text": resp.text}
    raise_if_setup_intent_blocked(payload, "stripe intent", current_pm_id=current_pm_id)
    redirect_url = extract_redirect_url(payload)
    if redirect_url:
        dump_http(resp, "stripe_intent_redirect", params, "GET", url, force=True)
        log(f"读取 Stripe intent 拿到 redirect_url: {redirect_url[:180]}")
    return redirect_url


def stripe_payload_intent_redirect_url(
    stripe: requests.Session,
    payload: Any,
    stripe_pk: str,
    current_pm_id: str = "",
) -> str:
    if not isinstance(payload, dict):
        return ""
    for intent_key in ("setup_intent", "payment_intent"):
        candidates: list[Any] = []
        direct = payload.get(intent_key)
        if isinstance(direct, dict):
            candidates.append(direct)
        nested = first_value_by_key(payload, intent_key)
        if isinstance(nested, dict) and all(nested is not item for item in candidates):
            candidates.append(nested)
        for intent_payload in candidates:
            redirect_url = stripe_intent_redirect_url(stripe, intent_payload, stripe_pk, current_pm_id=current_pm_id)
            if redirect_url:
                return redirect_url
    return ""


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


def warmup_approve_context(chatgpt: requests.Session, checkout_page_url: str) -> None:
    try:
        resp = chatgpt.post(
            "https://chatgpt.com/backend-api/sentinel/ping",
            json={},
            headers={
                "Referer": "https://chatgpt.com/",
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=CHATGPT_TIMEOUT,
        )
        dump_http(resp, "sentinel_ping", {}, "POST", "https://chatgpt.com/backend-api/sentinel/ping", force=env_bool("UPI_DUMP_WARMUP", False))
    except Exception as exc:
        log(f"approve sentinel 请求异常: {exc}", "[WARN] ")


def chatgpt_approve(chatgpt: requests.Session, checkout: dict[str, str]) -> None:
    cs_id = checkout["cs_id"]
    processor = processor_entity_for_country(checkout.get("billing_country", "IN"), checkout.get("processor_entity", ""))
    checkout_page_url = f"https://chatgpt.com/checkout/{processor}/{cs_id}"
    if env_bool("UPI_APPROVE_WARMUP", True):
        warmup_approve_context(chatgpt, checkout_page_url)
        time.sleep(random.uniform(0.8, 1.6))

    body = {"checkout_session_id": cs_id, "processor_entity": processor}
    headers = {
        "Referer": checkout_page_url,
        "x-openai-target-path": "/backend-api/payments/checkout/approve",
        "x-openai-target-route": "/backend-api/payments/checkout/approve",
    }
    resp = chatgpt.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json=body,
        headers=headers,
        timeout=CHATGPT_TIMEOUT,
    )
    dump_http(resp, "approve", body, "POST", "https://chatgpt.com/backend-api/payments/checkout/approve", force=True)
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
) -> None:
    log(f"approve 第 {index}/{attempt_count} 次 / proxy={proxy_label(proxy)}")
    chatgpt = build_chatgpt_session(access_token, device_id, proxy, session_token)
    chatgpt_approve(chatgpt, checkout)


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
) -> str:
    max_retry = env_int("UPI_APPROVE_RETRY_MAX", 10)
    parallel = env_int("UPI_APPROVE_PARALLEL", 1)
    last_error = ""
    if max_retry <= 0:
        raise RuntimeError("approve 重试次数必须大于 0")
    proxies = [proxy for proxy in dict.fromkeys(proxies) if proxy]
    if not proxies:
        raise RuntimeError("approve 代理为空")
    sticky = env_bool("UPI_APPROVE_STICKY", True)
    if sticky and parallel > 1:
        log("approve 失败切换代理，按候选顺序串行执行")
        parallel = 1
    if sticky:
        selected_proxies = proxies[:max_retry]
    else:
        attempt_count = min(max_retry, len(proxies))
        fixed_proxies = proxies[: min(2, attempt_count)]
        selected_proxies = fixed_proxies[:]
        remain_count = attempt_count - len(selected_proxies)
        if remain_count > 0:
            selected_proxies.extend(random.sample(proxies[2:], min(remain_count, len(proxies) - 2)))
    attempt_count = len(selected_proxies)
    log(f"approve 代理策略: {'sticky' if sticky else 'rotate'}")
    if parallel > 1:
        workers = min(parallel, attempt_count)
        log(f"approve 并发: workers={workers}, attempts={attempt_count}")
        blocked_count = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    approve_attempt,
                    access_token,
                    device_id,
                    checkout,
                    session_token,
                    proxy,
                    index,
                    attempt_count,
                ): proxy
                for index, proxy in enumerate(selected_proxies, start=1)
            }
            for future in as_completed(futures):
                try:
                    future.result()
                    log("approve 成功")
                    for pending in futures:
                        pending.cancel()
                    return futures[future]
                except Exception as exc:
                    last_error = str(exc)
                    if is_checkout_not_active_error(last_error):
                        raise RuntimeError("checkout_not_active_session")
                    if log_approve_failure(last_error):
                        blocked_count += 1
        if blocked_count and blocked_count == attempt_count:
            raise RuntimeError("approve blocked")
        raise RuntimeError(f"approve 重试失败: {last_error}")

    blocked_count = 0
    for index, proxy in enumerate(selected_proxies, start=1):
        try:
            approve_attempt(access_token, device_id, checkout, session_token, proxy, index, attempt_count)
            log("approve 成功")
            return proxy
        except Exception as exc:
            last_error = str(exc)
            if is_checkout_not_active_error(last_error):
                raise RuntimeError("checkout_not_active_session")
            if log_approve_failure(last_error):
                blocked_count += 1
            if index < attempt_count:
                time.sleep(random.uniform(1, 2))
    if blocked_count and blocked_count == attempt_count:
        raise RuntimeError("approve blocked")
    raise RuntimeError(f"approve 重试失败: {last_error}")


def poll_payment_page(
    stripe: requests.Session,
    checkout: dict[str, str],
    stripe_pk: str,
    ctx: dict[str, Any],
    current_pm_id: str = "",
) -> tuple[str, list[str]]:
    cs_id = checkout["cs_id"]
    deadline = time.time() + env_int("UPI_POLL_TIMEOUT", 45)
    params = {
        **stripe_elements_session_params(ctx),
        "key": stripe_pk,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
    }
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}"
    last_error = ""
    last_payload: dict[str, Any] = {}
    last_summary = ""
    while time.time() < deadline:
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
            return redirect_url, qr_urls
        intent_redirect = stripe_payload_intent_redirect_url(stripe, payload, stripe_pk, current_pm_id=current_pm_id)
        if intent_redirect:
            dump_http(resp, "poll_success", params, "GET", url, force=True)
            return intent_redirect, qr_urls
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
    log(f"poll 结束，未发现真实 UPI redirect/QR: {last_error}", "[WARN] ")
    raise RuntimeError(f"redirect url resolution timeout: {last_error}")


def fetch_redirect_page(stripe: requests.Session, start_url: str) -> list[str]:
    if not start_url or not env_bool("UPI_FOLLOW_REDIRECT", True):
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
    if not start_url or not env_bool("UPI_FOLLOW_REDIRECT", True):
        return start_url
    current = start_url
    for hop in range(1, 6):
        if is_upi_instructions_url(current):
            return current
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


def approve_proxy_candidates(checkout_proxy: str, provider_proxy: str, approve_pool: list[str]) -> list[str]:
    approve_preferences = successful_approve_preferences(checkout_proxy, provider_proxy, [provider_proxy] + approve_pool)
    if approve_preferences:
        log(f"命中成功 approve 代理优先: {proxy_label(approve_preferences[0])}")
    return list(dict.fromkeys(approve_preferences + [provider_proxy] + approve_pool))


def resolve_confirm_payload_upi(
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
    progress: Any | None = None,
) -> tuple[str, list[str], str]:
    raise_if_setup_intent_blocked(confirm_payload, "stripe confirm", current_pm_id=pm_id)
    redirect_url = extract_redirect_url(confirm_payload)
    if not redirect_url:
        redirect_url = stripe_payload_intent_redirect_url(stripe, confirm_payload, stripe_pk, current_pm_id=pm_id)
    qr_urls = extract_qr_candidates(confirm_payload)
    submission = find_submission_attempt(confirm_payload)

    if redirect_url:
        log(f"confirm 提取到最终支付 URL: {redirect_url[:180]}")
    if qr_urls:
        log(f"confirm 提取到 QR 候选 {len(qr_urls)} 个")
        log_qr_candidate_summaries("confirm", qr_urls)

    approve_proxy = ""
    if not redirect_url and submission.get("state") == "requires_approval":
        report_progress(progress, "approve", "等待 ChatGPT approve 授权")
        log("需要 ChatGPT approve...")
        approve_proxies = approve_proxy_candidates(checkout_proxy, provider_proxy, approve_pool)
        log("需要 approve：UPI 0 元场景，优先使用历史成功/当前 Provider 代理，失败后切换下一个 Provider 代理。")
        approve_proxy = approve_with_retry(access_token, device_id, checkout, approve_proxies, session_token, "provider")
        report_progress(progress, "poll", "Approve 完成，轮询 Stripe payment_pages")
        log("跟随跳转提取最终链...")
        redirect_url, poll_qr = poll_payment_page(stripe, checkout, stripe_pk, ctx, current_pm_id=pm_id)
        qr_urls.extend(poll_qr)
    elif not redirect_url and not qr_urls:
        report_progress(progress, "poll", "Confirm 未直接出链，轮询 Stripe payment_pages")
        log("confirm 未返回真实 UPI redirect/QR，继续 poll payment_pages 做最终确认", "[WARN] ")
        redirect_url, poll_qr = poll_payment_page(stripe, checkout, stripe_pk, ctx, current_pm_id=pm_id)
        qr_urls.extend(poll_qr)

    return redirect_url, list(dict.fromkeys(qr_urls)), approve_proxy


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
    progress: Any | None = None,
) -> tuple[str, list[str]]:
    checkout_country = normalize_country(os.environ.get("UPI_CHECKOUT_COUNTRY", UPI_BOOTSTRAP_COUNTRY))
    stripe_pk = checkout.get("stripe_pk") or DEFAULT_STRIPE_PK

    def inspect_init(
        payload: dict[str, Any],
        stage: str,
        *,
        require_upi: bool = True,
    ) -> tuple[dict[str, Any], int]:
        current_ctx = build_ctx(payload, checkout)
        current_amount = int(current_ctx.get("checkout_amount") or 0)
        amount_major = current_amount / 100
        log(f"{stage} Stripe init 成功, 金额={checkout['currency']} {amount_major:.2f}")
        payment_method_types = first_value_by_key(payload, "payment_method_types")
        if isinstance(payment_method_types, list):
            methods = [str(item).lower() for item in payment_method_types]
            log(f"Stripe 可用支付方式: {methods}")
            if require_upi and "upi" not in methods:
                raise RuntimeError(
                    f"{UPI_UNAVAILABLE_ERROR}: {stage} amount={current_amount}; "
                    f"payment_method_types={methods}"
                )
            if not require_upi and "upi" not in methods:
                log(
                    f"{stage} 暂未提供 UPI，继续执行 "
                    f"{' -> '.join(UPI_PROMOTION_COUNTRIES)} Promotion 后再由 "
                    f"{UPI_PROVIDER_COUNTRY} Provider 复核",
                    "[WARN] ",
                )
        return current_ctx, current_amount

    report_progress(progress, "stripe_bootstrap", "IN Bootstrap：检查金额和 UPI 支付方式")
    log(
        f"{UPI_BOOTSTRAP_COUNTRY} Bootstrap Stripe init "
        f"(PM={billing['country']}, proxy={proxy_label(checkout_proxy)})..."
    )
    init_payload = stripe_init(checkout["cs_id"], stripe_pk, checkout_proxy)
    if not checkout.get("processor_entity"):
        processor_entity = infer_processor_entity(init_payload)
        if processor_entity:
            checkout["processor_entity"] = processor_entity
            log(f"从 Stripe init 推断 processor_entity={processor_entity}")
    inspect_init(
        init_payload,
        f"{UPI_BOOTSTRAP_COUNTRY} Bootstrap",
        require_upi=False,
    )
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")

    stripe = new_session(provider_proxy)
    stripe.headers.update(
        {"User-Agent": random_user_agent(), "Accept-Language": payment_accept_language()}
    )
    hosted_url = ""
    ctx: dict[str, Any] = {}
    amount = 0
    for promotion_index, promotion_country in enumerate(UPI_PROMOTION_COUNTRIES, start=1):
        current_promotion_proxy = proxy_for_country(promotion_proxy, promotion_country)
        stage_label = f"{promotion_country} checkout/update {promotion_index}/{len(UPI_PROMOTION_COUNTRIES)}"
        report_progress(
            progress,
            "promotion",
            f"{promotion_country} Promotion {promotion_index}/{len(UPI_PROMOTION_COUNTRIES)}：更新 0 元优惠",
        )
        log(f"{stage_label}: proxy={proxy_label(current_promotion_proxy)}")
        try:
            promotion_chatgpt = build_chatgpt_session(
                access_token, device_id, current_promotion_proxy, session_token
            )
            update_checkout_promotion(promotion_chatgpt, checkout, promotion_country)
        except Exception as exc:
            if is_checkout_not_active_error(exc):
                raise
            raise RuntimeError(f"promotion 阶段失败: {exc}") from exc
        record_proxy_result("promotion", current_promotion_proxy, True, "promotion_update_success")

        report_progress(
            progress,
            "stripe_provider",
            f"{UPI_PROVIDER_COUNTRY} Provider：刷新 Stripe 并复核 UPI 资格",
        )
        log(
            f"{stage_label} 后通过 {UPI_PROVIDER_COUNTRY} 刷新 Stripe: "
            f"proxy={proxy_label(provider_proxy)}"
        )
        init_payload = stripe_init(
            checkout["cs_id"], stripe_pk, provider_proxy, session=stripe
        )
        hosted_url = str(init_payload.get("stripe_hosted_url") or hosted_url or "")
        ctx, amount = inspect_init(
            init_payload, f"{promotion_country} 更新后 {UPI_PROVIDER_COUNTRY}"
        )
        record_checkout_zero_result(checkout_proxy, checkout_country, amount)
        if amount == 0:
            log("Promotion 后金额为 0，继续按 0 元 UPI 流程提取最终支付 URL")
            break
        if promotion_index < len(UPI_PROMOTION_COUNTRIES):
            log(
                f"{promotion_country} 更新后金额仍非 0，继续下一段 checkout/update",
                "[WARN] ",
            )
            continue
        raise RuntimeError(f"0 元优惠未生效，当前金额小单位={amount}，已停止生成非 0 元 UPI 链")

    report_progress(progress, "stripe_elements", "创建 Stripe Elements 会话")
    elements_payload = stripe_elements_session(
        stripe, checkout["cs_id"], stripe_pk, ctx
    )
    log(
        "Stripe elements/sessions 成功: "
        f"session={str(elements_payload.get('session_id') or '')[:18]}..."
    )

    report_progress(progress, "billing", "提交印度 Billing、税区和 Checkout 快照")
    if env_bool("UPI_UPDATE_CHECKOUT_TAXES", False):
        log(f"同步 {UPI_PROVIDER_COUNTRY} checkout/taxes...")
        tax_chatgpt = build_chatgpt_session(access_token, device_id, provider_proxy, session_token)
        update_upi_checkout_taxes(tax_chatgpt, checkout, billing)

    if env_bool("UPI_CHECKOUT_SNAPSHOT", True):
        snapshot_chatgpt = build_chatgpt_session(access_token, device_id, provider_proxy, session_token)
        checkout_snapshot(snapshot_chatgpt, checkout, billing)

    if env_bool("UPI_UPDATE_TAX_REGION", True):
        log(f"同步 {UPI_PROVIDER_COUNTRY} Stripe tax_region...")
        stripe_update_tax_region(stripe, checkout["cs_id"], stripe_pk, ctx, billing)

    report_progress(progress, "stripe_token", "加载 Stripe 动态确认配置")
    token_config = stripe_token.extract_config_live(
        stripe,
        log=lambda message: log(message),
        user_agent=random_user_agent(),
    )
    ctx["runtime_version"] = token_config.runtime_version
    log(
        "Stripe 动态 token 配置成功: "
        f"runtime={token_config.runtime_version}, shift={token_config.shift}, "
        f"bundle={token_config.bundle_hash[:12]}"
    )

    report_progress(progress, "payment_method", "准备 UPI 支付方式和印度账单资料")
    pm_id = ""
    if env_bool("UPI_CONFIRM_INLINE_PM", True):
        log(
            f"UPI confirm 内联资料: {billing['name']} / "
            f"{billing['line1']} / {billing['city']} {billing['postal_code']}"
        )
    else:
        log(f"创建 PM (UPI): {billing['country']} {billing['name']} / {billing['city']}")
        pm_id = stripe_create_upi_pm(stripe, checkout["cs_id"], stripe_pk, billing, ctx)
        log(f"PM 创建成功: {pm_id}")
        log(
            f"印度资料已填入 PM billing_details: {billing['name']} / "
            f"{billing['line1']} / {billing['city']} {billing['postal_code']}"
        )

    if env_bool("UPI_UPDATE_CUSTOMER_DATA", False):
        log(
            f"提交印度资料填充: {billing['name']} / {billing['line1']} / "
            f"{billing['city']} {billing['postal_code']} / {billing['email']}"
        )
        stripe_update_customer_data(stripe, checkout["cs_id"], stripe_pk, ctx, billing)

    report_progress(progress, "confirm", "提交 Stripe UPI Confirm")
    log("Stripe confirm (expected=UPI, source protocol)...")
    confirm_payload, confirm_variant = stripe_confirm_upi_variants(
        stripe,
        checkout["cs_id"],
        pm_id,
        stripe_pk,
        init_payload,
        ctx,
        checkout,
        hosted_url,
        billing,
        token_config,
    )
    log("Stripe confirm 成功, 解析跳转...")
    log_payment_page_summary("confirm", confirm_payload)
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")

    approve_proxy = ""
    qr_urls: list[str] = []
    try:
        redirect_url, qr_urls, approve_proxy = resolve_confirm_payload_upi(
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
            progress=progress,
        )
    except Exception as exc:
        if not should_retry_second_confirm_after_approve(exc):
            raise
        report_progress(progress, "confirm_retry", "首次 Confirm 未出链，刷新 Stripe 后二次确认")
        log(f"approve/confirm 后未拿到 redirect，刷新 Stripe init 后二次 confirm: {str(exc)[:180]}", "[WARN] ")
        init_payload = stripe_init(
            checkout["cs_id"], stripe_pk, provider_proxy, session=stripe
        )
        hosted_url = str(init_payload.get("stripe_hosted_url") or hosted_url or "")
        ctx = build_ctx(init_payload, checkout)
        elements_payload = stripe_elements_session(
            stripe, checkout["cs_id"], stripe_pk, ctx
        )
        ctx["runtime_version"] = token_config.runtime_version
        if "generic_decline" in str(exc).lower() and not env_bool("UPI_CONFIRM_INLINE_PM", True):
            pm_id = stripe_create_upi_pm(stripe, checkout["cs_id"], stripe_pk, billing, ctx)
            log(f"二次 confirm 已创建新 PM: {pm_id}")
        confirm_payload, confirm_variant = stripe_confirm_upi_variants(
            stripe,
            checkout["cs_id"],
            pm_id,
            stripe_pk,
            init_payload,
            ctx,
            checkout,
            hosted_url,
            billing,
            token_config,
        )
        log("二次 Stripe confirm 成功, 解析跳转...")
        log_payment_page_summary("second_confirm", confirm_payload)
        redirect_url, qr_urls, retry_approve_proxy = resolve_confirm_payload_upi(
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
            progress=progress,
        )
        approve_proxy = retry_approve_proxy or approve_proxy

    if redirect_url and approve_proxy:
        record_proxy_pair_approve_success(checkout_proxy, provider_proxy, approve_proxy)
        log("完成 - 已记忆此 checkout/provider/approve combo")

    if redirect_url:
        report_progress(progress, "extract", "解析并校验 Stripe UPI instructions 链接")
        final_url = resolve_external_redirect(stripe, redirect_url)
        if final_url and final_url != redirect_url:
            log(f"跟随 redirect 得到最终链: {final_url[:180]}")
            redirect_url = final_url

    return redirect_url, list(dict.fromkeys(qr_urls))


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
) -> tuple[str, list[str]]:
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")
    device_id = str(uuid.uuid4())
    checkout_country = normalize_country(os.environ.get("UPI_CHECKOUT_COUNTRY", UPI_BOOTSTRAP_COUNTRY))
    billing = upi_billing_profile()
    log(f"开始 UPI 提取，第 {attempt}/{max_retry} 次")
    log(
        "组合测试: "
        f"{checkout_country} / {billing['country']} / {currency_for_country(checkout_country)} / "
        f"{payment_browser_locale()} / "
        f"{os.environ.get('UPI_PROVIDER_COUNTRY_LABEL', UPI_PROVIDER_COUNTRY).strip() or UPI_PROVIDER_COUNTRY}"
    )
    if stop_event and stop_event.is_set():
        raise RuntimeError("任务已停止，跳过本轮")

    try:
        proxy_seed = checkout_proxy
        checkout_proxy, promotion_proxy, provider_proxy = upi_proxy_chain(proxy_seed)
        log_upi_proxy_chain(proxy_seed, checkout_proxy, promotion_proxy, provider_proxy)
        log(f"本轮代理: checkout/资格={proxy_label(checkout_proxy)}；Stripe/UPI={proxy_label(provider_proxy)}")
        zero_status, zero_amount, _zero_checked_at = checkout_zero_cache_status(checkout_proxy, checkout_country)
        if zero_status == "ok":
            log(f"checkout 0元资格缓存命中: amount={zero_amount}")
        elif zero_status == "bad":
            log(f"checkout 0元失败缓存命中: 上次 amount={zero_amount}，本轮继续验证", "[WARN] ")
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
        redirect_url, qr_urls = run_once(
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
        if is_upi_unavailable_error(error):
            log(f"第 {attempt}/{max_retry} 轮 checkout 未提供 UPI，保留代理并继续后续组合", "[WARN] ")
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
    if not env_bool("UPI_PROXY_SCORE", True):
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
    per_checkout = env_int("UPI_PROVIDER_PER_CHECKOUT", 30)
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
    if not group or not env_bool("UPI_PROXY_SCORE", True):
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
    upi_retry: int,
    checkout_retry: int,
    provider_retry: int,
    checkout_country: str,
    checkout_currency: str,
    stop_event: Event,
) -> tuple[int, str, str, bool]:
    previous_log_context = getattr(_log_context, "prefix", "")
    _log_context.prefix = f"[UPI {attempt}/{upi_retry}] "
    last_error = ""
    approve_blocked = False
    checkout_proxy_used = ""
    try:
        if stop_event.is_set():
            return attempt, "", "任务已停止，跳过本轮", False
        billing = upi_billing_profile()
        pm_country = billing["country"]
        device_id = str(uuid.uuid4())
        checkout_candidates = pick_random_proxies(checkout_proxies, checkout_retry, "checkout")
        checkout: dict[str, str] | None = None
        promotion_proxy = ""
        provider_proxy = ""

        log(f"开始第 {attempt}/{upi_retry} 次提链")
        log(
            f"Step 1: 创建 ChatGPT checkout... checkout账单={checkout_country}/{checkout_currency}，"
            f"第 {attempt}/{upi_retry} 次，每次随机抽取最多 {checkout_retry} 个节点"
        )
        log(f"首次 PM 国家: {pm_country}")

        for checkout_index, proxy_seed in enumerate(checkout_candidates, start=1):
            if stop_event.is_set():
                return attempt, "", "任务已停止，跳过本轮", False
            _log_context.prefix = f"[UPI {attempt}/{upi_retry}][PM={pm_country}] "
            try:
                checkout_proxy, promotion_proxy, provider_proxy = upi_proxy_chain(proxy_seed)
                log_upi_proxy_chain(proxy_seed, checkout_proxy, promotion_proxy, provider_proxy)
                log(f"Checkout {checkout_index}/{len(checkout_candidates)}: {checkout_country}/{checkout_currency}, proxy={proxy_label(checkout_proxy)}")
                zero_status, zero_amount, _zero_checked_at = checkout_zero_cache_status(checkout_proxy, checkout_country)
                if zero_status == "ok":
                    log(f"checkout 0元资格缓存命中: amount={zero_amount}")
                elif zero_status == "bad":
                    log(f"checkout 0元失败缓存命中: 上次 amount={zero_amount}，本轮继续验证", "[WARN] ")
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

        _log_context.prefix = f"[UPI {attempt}/{upi_retry}] "
        if not checkout or not checkout_proxy_used:
            log(f"第 {attempt}/{upi_retry} 次提链 checkout 阶段失败", "[WARN] ")
            return attempt, "", last_error or "checkout_failed", False

        stripe_pk = checkout.get("stripe_pk") or DEFAULT_STRIPE_PK
        log(f"Stripe PK: {stripe_pk[:18]}...")
        log(f"Step 2: 首次尝试 PM={pm_country}...")

        if stop_event.is_set():
            return attempt, "", "任务已停止，跳过本轮", False
        _log_context.prefix = f"[UPI {attempt}/{upi_retry}][PM={pm_country}] "
        try:
            redirect_url, _qr_urls = run_provider_flow(
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
            )
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
            elif is_upi_unavailable_error(error):
                log("当前 checkout 未提供 UPI，换下一轮 checkout 组合", "[WARN] ")
                return attempt, "", error, False
            else:
                record_failure_by_stage(error, checkout_proxy_used, provider_proxy, promotion_proxy)
                log(f"Provider 失败: {error[:220]}", "[WARN] ")
                if is_approve_failure_error(error) and "approve blocked" in error:
                    approve_blocked = True

        _log_context.prefix = f"[UPI {attempt}/{upi_retry}] "
        log(f"第 {attempt}/{upi_retry} 次提链结束，未拿到最终 URL", "[WARN] ")
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
    checkout_retry = env_int("UPI_CHECKOUT_RETRY_MAX", 5)
    provider_retry = env_int("UPI_PROVIDER_RETRY_MAX", 3)
    upi_retry = env_int("UPI_MAX_RETRY", 5)
    requested_workers = env_int("UPI_WORKERS", 1)
    worker_limit = env_int("UPI_WORKERS_MAX", requested_workers)
    workers = min(max(1, requested_workers), max(1, worker_limit), upi_retry)
    checkout_country = normalize_country(os.environ.get("UPI_CHECKOUT_COUNTRY", UPI_BOOTSTRAP_COUNTRY))
    checkout_currency = currency_for_country(checkout_country)
    configured_pm_country = normalize_country(
        os.environ.get("UPI_BILLING_COUNTRY", UPI_PROVIDER_COUNTRY)
    )
    max_blocked = env_int("UPI_MAX_APPROVE_BLOCKED", upi_retry)
    approve_blocked_count = 0
    last_error = ""
    stop_event = Event()

    if requested_workers > workers:
        log(f"UPI并发从 {requested_workers} 限制为 {workers}", "[WARN] ")
    log(
        "开始执行 UPI 链提取流程："
        f"checkout={checkout_country}/{checkout_currency}，PM={configured_pm_country}，locale={payment_browser_locale()}，"
        f"Checkout重试={checkout_retry}，Provider重试={provider_retry}，UPI总重试={upi_retry}，UPI并发={workers}。"
    )

    executor = ThreadPoolExecutor(max_workers=workers)
    futures: dict[Any, int] = {}
    try:
        for attempt in range(1, upi_retry + 1):
            futures[
                executor.submit(
                    run_single_link_attempt,
                    access_token,
                    session_token,
                    checkout_proxies,
                    promotion_proxies,
                    provider_proxies,
                    attempt,
                    upi_retry,
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
                log(f"第 {attempt}/{upi_retry} 次提链异常: {error[:300]}", "[WARN] ")
            if redirect_url:
                stop_event.set()
                for pending in futures:
                    pending.cancel()
                print("\n===== 结果 =====")
                print(f"UPI 最终支付 URL:\n{redirect_url}")
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
    upi_workers = env_int("UPI_WORKERS", 1)
    if upi_workers > 1:
        log(f"UPI 链路固定并发=1，忽略 UPI_WORKERS={upi_workers}", "[WARN] ")

    checkout_retry = env_int("UPI_CHECKOUT_RETRY_MAX", 5)
    provider_retry = env_int("UPI_PROVIDER_RETRY_MAX", 3)
    upi_retry = env_int("UPI_MAX_RETRY", 5)
    checkout_country = normalize_country(os.environ.get("UPI_CHECKOUT_COUNTRY", UPI_BOOTSTRAP_COUNTRY))
    checkout_currency = currency_for_country(checkout_country)
    configured_pm_country = normalize_country(
        os.environ.get("UPI_BILLING_COUNTRY", UPI_PROVIDER_COUNTRY)
    )
    max_blocked = env_int("UPI_MAX_APPROVE_BLOCKED", upi_retry)
    approve_blocked_count = 0
    last_error = ""
    stop_event = Event()
    attempted_seed_keys: set[str] = set()

    log(
        "开始执行 UPI 链提取流程："
        f"checkout={checkout_country}/{checkout_currency}，PM={configured_pm_country}，locale={payment_browser_locale()}，"
        f"Checkout重试={checkout_retry}，Provider重试={provider_retry}，UPI总重试={upi_retry}。"
    )

    for attempt in range(1, upi_retry + 1):
        billing = upi_billing_profile()
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

        log(f"开始第 {attempt}/{upi_retry} 次提链")
        log(
            f"Step 1: 创建 ChatGPT checkout... checkout账单={checkout_country}/{checkout_currency}，"
            f"第 {attempt}/{upi_retry} 次，每次随机抽取最多 {checkout_retry} 个节点"
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
                checkout_proxy, promotion_proxy, provider_proxy = upi_proxy_chain(proxy_seed)
                log_upi_proxy_chain(proxy_seed, checkout_proxy, promotion_proxy, provider_proxy)
                log(
                    f"Checkout {checkout_index}/{len(checkout_candidates)}: "
                    f"{checkout_country}/{checkout_currency}, proxy={proxy_label(checkout_proxy)}，"
                    f"本次已尝试 Seed={len(attempted_seed_keys)}"
                )
                zero_status, zero_amount, _zero_checked_at = checkout_zero_cache_status(checkout_proxy, checkout_country)
                if zero_status == "ok":
                    log(f"checkout 0元资格缓存命中: amount={zero_amount}")
                elif zero_status == "bad":
                    log(f"checkout 0元失败缓存命中: 上次 amount={zero_amount}，本轮继续验证", "[WARN] ")
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
            log(f"第 {attempt}/{upi_retry} 次提链 checkout 阶段失败，换下一次提链", "[WARN] ")
            continue

        stripe_pk = checkout.get("stripe_pk") or DEFAULT_STRIPE_PK
        log(f"Stripe PK: {stripe_pk[:18]}...")
        log(f"Step 2: 首次尝试 PM={pm_country}...")

        previous_log_context = getattr(_log_context, "prefix", "")
        _log_context.prefix = f"  [PM={pm_country}] "
        try:
            redirect_url, _qr_urls = run_provider_flow(
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
            )
            if redirect_url:
                record_proxy_result("seed", checkout_proxy_used, True, "success")
                print("\n===== 结果 =====")
                print(f"UPI 最终支付 URL:\n{redirect_url}")
                return 0
            last_error = "no_redirect_url"
            record_proxy_result("seed", provider_proxy, False, last_error)
        except Exception as exc:
            error = str(exc)
            last_error = error
            if is_checkout_not_active_error(error):
                log("Session 已失效；当前 checkout 不再继续换 provider", "[WARN] ")
            elif is_upi_unavailable_error(error):
                log("当前 checkout 未提供 UPI，换下一次提链", "[WARN] ")
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
        log(f"第 {attempt}/{upi_retry} 次提链结束，未拿到最终 URL", "[WARN] ")

    log(f"全部失败: {last_error}", "[ERROR] ")
    return 1


def generate_upi_link_explicit(
    access_token: str,
    checkout_proxy: str,
    promotion_proxy: str,
    provider_proxy: str,
    *,
    account: str = "未知",
    session_token: str = "",
    plan: str = "chatgptplusplan",
    progress: Any | None = None,
) -> dict[str, Any]:
    """Run the upstream UPI protocol with host-selected regional proxies."""
    access_token, parsed_session_token = normalize_token(access_token)
    session_token = session_token or parsed_session_token
    plan_name = str(plan or "chatgptplusplan").strip() or "chatgptplusplan"
    checkout_proxy = normalize_proxy_url(checkout_proxy)
    promotion_proxy = normalize_proxy_url(promotion_proxy)
    provider_proxy = normalize_proxy_url(provider_proxy)
    result: dict[str, Any] = {
        "ok": False, "provider": "upi_open_source", "account": account or "未知",
        "payment_method": "upi", "currency": "inr", "billing_country": "IN", "plan": plan_name,
    }
    if not access_token:
        result.update(error="accessToken 为空", error_code="bad_token_format")
        return result
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", plan_name):
        result.update(error="plan 格式无效", error_code="bad_plan")
        return result
    if not checkout_proxy or not promotion_proxy or not provider_proxy:
        result.update(
            error="UPI 需要 IN 共享代理（Checkout / Provider / Approve）与 VN Promotion 代理",
            error_code="proxy_empty",
        )
        return result
    for value in (checkout_proxy, promotion_proxy, provider_proxy):
        register_proxy_for_redaction(value)
    plan_context = _ACTIVE_PLAN_NAME.set(plan_name)
    try:
        device_id = str(uuid.uuid4())
        billing = upi_billing_profile()
        if "@" in str(account or ""):
            billing["email"] = str(account).strip()
        report_progress(progress, "checkout", "创建 ChatGPT Checkout")
        chatgpt = build_chatgpt_session(access_token, device_id, checkout_proxy, session_token)
        checkout = create_checkout(chatgpt, UPI_BOOTSTRAP_COUNTRY)
        report_progress(progress, "checkout_created", "Checkout 已创建，准备初始化 Stripe")
        redirect_url, qr_urls = run_provider_flow(
            access_token, session_token, checkout_proxy, promotion_proxy, provider_proxy,
            [provider_proxy], device_id, checkout, billing, progress=progress,
        )
        if not redirect_url:
            raise RuntimeError("未提取到最终 UPI 支付链接")
        result.update(
            ok=True, long_url=redirect_url, upi_url=redirect_url, qr_urls=qr_urls,
            pay_amount_minor=0, pay_amount=0.0, is_free_trial=True,
            trial_status="0元资格", debug="core=open_source",
            checkout_session_id=checkout.get("cs_id"),
            chatgpt_checkout_url=checkout_page_url(checkout),
            processor_entity=checkout.get("processor_entity") or None,
            **upi_billing_snapshot(billing),
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
        elif is_upi_unavailable_error(error):
            code = "upi_unavailable"
        elif amount_minor is not None:
            code = "non_zero_amount"
        elif is_proxy_health_failure(error):
            code = "proxy_failed"
        elif "approve blocked" in low:
            code = "approve_blocked"
        else:
            code = "upi_no_link"
        result.update(error=error, error_code=code, debug="core=open_source")
        if amount_minor is not None:
            result.update(pay_amount_minor=amount_minor, pay_amount=amount_minor / 100, is_free_trial=False, trial_status="非0元资格")
        return result
    finally:
        _ACTIVE_PLAN_NAME.reset(plan_context)


_FINAL_CHECKOUT_SESSION_ID_RE = re.compile(r"^cs_[A-Za-z0-9_-]{3,200}$")


def _final_link_session_token(account: Mapping[str, Any] | None, explicit: str) -> str:
    if explicit:
        return str(explicit).strip()
    if not isinstance(account, Mapping):
        return ""
    for key in ("session_token", "next_auth_session_token", "nextAuthSessionToken"):
        value = str(account.get(key) or "").strip()
        if value:
            return value
    return ""


def _final_link_account_label(account: Mapping[str, Any] | None) -> str:
    if not isinstance(account, Mapping):
        return "local-account"
    for key in ("email", "account_id", "id"):
        value = str(account.get(key) or "").strip()
        if value:
            return value[:200]
    return "local-account"


def _final_link_error(value: object, secrets: tuple[str, ...]) -> str:
    text = str(value or "").strip()
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return _redact_text(text, limit=500) or "UPI 协议未返回可用最终支付链接"


def _safe_final_checkout_session_id(value: object) -> str | None:
    session_id = str(value or "").strip()
    return session_id if _FINAL_CHECKOUT_SESSION_ID_RE.fullmatch(session_id) else None


def _safe_final_chatgpt_checkout_url(value: object, session_id: str | None) -> str | None:
    url = str(value or "").strip()
    if not url or not session_id:
        return None
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme.lower() == "https"
        and parsed.hostname == "chatgpt.com"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and path.startswith("/checkout/")
        and path.endswith(f"/{session_id}")
    ):
        return url
    return None


def generate_upi_final_link(
    access_token: str,
    *,
    checkout_proxy: str,
    promotion_proxy: str,
    provider_proxy: str,
    account: Mapping[str, Any] | None = None,
    session_token: str = "",
    plan: str = "chatgptplusplan",
    progress: Any | None = None,
) -> dict[str, Any]:
    """Extract and strictly validate a Stripe UPI instructions link.

    This is the embedding-safe protocol boundary.  It receives transient
    credentials, the shared IN proxy, and the VN Promotion proxy, but returns only data that
    callers may retain: the final URL, checkout identity, and payment state.
    It never updates the account store and intentionally omits cookies, Stripe
    keys, client secrets, payment-method ids, QR data, and raw HTTP payloads.
    """
    token = str(access_token or "").strip()
    normalized_plan = str(plan or "chatgptplusplan").strip().lower()
    if not token:
        return {"ok": False, "error": "access_token 不能为空", "error_code": "bad_token_format"}
    if normalized_plan not in {"chatgptplusplan", "chatgpt_plus", "plus"}:
        return {"ok": False, "error": "当前 UPI 提链协议仅支持 ChatGPT Plus 计划", "error_code": "unsupported_plan"}

    checkout_proxy = normalize_proxy_url(checkout_proxy)
    promotion_proxy = normalize_proxy_url(promotion_proxy)
    provider_proxy = normalize_proxy_url(provider_proxy)
    if not checkout_proxy or not promotion_proxy or not provider_proxy:
        return {
            "ok": False,
            "error": "UPI 需要 Checkout、Promotion 和 Provider 三段代理",
            "error_code": "proxy_empty",
        }

    # The low-level implementation accepts the canonical Plus plan name.  Its
    # ContextVar keeps this value isolated when several registration workers
    # run at once.
    normalized_plan = "chatgptplusplan"
    resolved_session_token = _final_link_session_token(account, session_token)
    secrets = (token, resolved_session_token, checkout_proxy, promotion_proxy, provider_proxy)
    try:
        upstream = generate_upi_link_explicit(
            token,
            checkout_proxy,
            promotion_proxy,
            provider_proxy,
            account=_final_link_account_label(account),
            session_token=resolved_session_token,
            plan=normalized_plan,
            progress=progress,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": _final_link_error(exc, secrets),
            "error_code": "upi_final_link_failed",
        }

    if not isinstance(upstream, Mapping) or not upstream.get("ok"):
        payload = upstream if isinstance(upstream, Mapping) else {}
        return {
            "ok": False,
            "error": _final_link_error(payload.get("error"), secrets),
            "error_code": str(payload.get("error_code") or "upi_final_link_failed"),
        }

    report_progress(progress, "final_validate", "校验 /upi/instructions/ 最终支付链接")
    final_url = str(upstream.get("upi_url") or upstream.get("long_url") or "").strip()
    if not is_upi_instructions_url(final_url):
        return {
            "ok": False,
            "error": "UPI 协议未返回 Stripe UPI instructions 最终支付链接",
            "error_code": "upi_final_url_invalid",
        }

    report_progress(progress, "completed", "UPI 最终支付链接已就绪")
    session_id = _safe_final_checkout_session_id(upstream.get("checkout_session_id"))
    return {
        "ok": True,
        "provider": "upi_open_source",
        "payment_method": "upi",
        "currency": "INR",
        "billing_country": "IN",
        "checkout_plan": normalized_plan,
        "checkout_final_kind": "stripe_upi_instructions",
        "checkout_final_url": final_url,
        "upi_url": final_url,
        "long_url": final_url,
        "checkout_session_id": session_id,
        "chatgpt_checkout_url": _safe_final_chatgpt_checkout_url(
            upstream.get("chatgpt_checkout_url"), session_id
        ),
        "processor_entity": str(upstream.get("processor_entity") or "").strip() or None,
        "pay_amount_minor": int(upstream.get("pay_amount_minor") or 0),
        "pay_amount": upstream.get("pay_amount"),
        "is_free_trial": bool(upstream.get("is_free_trial")),
        "checkout_billing_name": upstream.get("checkout_billing_name"),
        "checkout_billing_email": upstream.get("checkout_billing_email"),
        "checkout_billing_address": upstream.get("checkout_billing_address"),
    }


def main() -> int:
    access_token, session_token = load_token()
    if not access_token:
        log("access_token 为空", "[ERROR] ")
        return 1

    proxy_seeds = load_proxy_seeds()
    flow_mode = os.environ.get("UPI_FLOW_MODE", "single").strip().lower() or "single"
    if flow_mode != "single":
        log(f"UPI_FLOW_MODE={flow_mode} 已收敛为 strict single seed 链路", "[WARN] ")
    return run_single_link_mode(access_token, session_token, proxy_seeds)


if __name__ == "__main__":
    sys.exit(main())
