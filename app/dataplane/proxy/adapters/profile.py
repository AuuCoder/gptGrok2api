"""Shared Cloudflare proxy profile resolution."""

from dataclasses import dataclass
from functools import lru_cache
import re
from typing import get_args

from app.control.proxy.config import resolve_clearance_config
from app.control.proxy.models import ProxyLease


@dataclass(frozen=True)
class ProxyProfile:
    cf_cookies: str = ""
    user_agent: str = ""
    cf_clearance: str = ""
    browser: str = ""


def extract_cookie_value(cookie_header: str, name: str) -> str:
    if not cookie_header:
        return ""
    match = re.search(rf"(?:^|;\s*){re.escape(name)}=([^;]*)", cookie_header)
    return match.group(1) if match else ""


@lru_cache(maxsize=1)
def _supported_browser_profiles() -> set[str]:
    try:
        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        return {str(item) for item in get_args(BrowserTypeLiteral)}
    except Exception:
        return set()


def _supported_browser(candidate: str) -> str:
    if not candidate:
        return ""
    supported = _supported_browser_profiles()
    if not supported or candidate in supported:
        return candidate

    family = re.match(r"[a-z_]+", candidate)
    if family and family.group(0) in supported:
        return family.group(0)
    return ""


def browser_from_user_agent(user_agent: str) -> str:
    ua = user_agent or ""
    lower = ua.lower()

    firefox = re.search(r"firefox/(\d+)", lower)
    if firefox:
        return _supported_browser(f"firefox{firefox.group(1)}") or _supported_browser(
            "firefox"
        )

    edge = re.search(r"edg/(\d+)", lower)
    if edge:
        return _supported_browser(f"edge{edge.group(1)}") or _supported_browser("edge")

    chrome = re.search(r"(?:chrome|chromium|crios)/(\d+)", lower)
    if chrome:
        suffix = "_android" if "android" in lower else ""
        exact = _supported_browser(f"chrome{chrome.group(1)}{suffix}")
        return exact or _supported_browser("chrome_android" if suffix else "chrome")

    safari = "safari/" in lower and "chrome/" not in lower and "chromium/" not in lower
    if safari:
        return _supported_browser(
            "safari_ios" if ("iphone" in lower or "ipad" in lower) else "safari"
        )

    return ""


def resolve_proxy_profile(lease: ProxyLease | None) -> ProxyProfile:
    """Resolve cookies, UA, clearance and curl_cffi browser from one source order.

    Flat legacy keys win over the nested v2 clearance keys because external
    refresher sidecars write the flat values at runtime.  The resolved browser
    is derived from the effective User-Agent first, keeping header UA,
    client-hints and curl_cffi impersonation aligned.
    """
    cfg = resolve_clearance_config()

    if lease is not None:
        cookies = lease.cf_cookies or cfg.cf_cookies
        user_agent = lease.user_agent or cfg.user_agent
        clearance = (
            extract_cookie_value(lease.cf_cookies, "cf_clearance") or cfg.cf_clearance
        )
    else:
        cookies = cfg.cf_cookies
        user_agent = cfg.user_agent
        clearance = cfg.cf_clearance

    browser = (
        browser_from_user_agent(user_agent)
        or _supported_browser(cfg.browser)
        or "chrome120"
    )

    return ProxyProfile(
        cf_cookies=cookies,
        user_agent=user_agent,
        cf_clearance=clearance,
        browser=browser,
    )


__all__ = [
    "ProxyProfile",
    "browser_from_user_agent",
    "extract_cookie_value",
    "resolve_proxy_profile",
]
