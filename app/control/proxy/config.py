"""Proxy clearance config helpers shared by control and dataplane code."""

from dataclasses import dataclass
from typing import Any

from app.platform.config.snapshot import get_config


@dataclass(frozen=True)
class ClearanceConfig:
    cf_cookies: str = ""
    user_agent: str = ""
    cf_clearance: str = ""
    browser: str = ""


def _cfg_str(cfg: Any, key: str) -> str:
    value = cfg.get_str(key, "")
    return value if value.strip() else ""


def first_config_str(cfg: Any, *keys: str) -> str:
    for key in keys:
        value = _cfg_str(cfg, key)
        if value:
            return value
    return ""


def resolve_clearance_config(cfg: Any | None = None) -> ClearanceConfig:
    cfg = cfg or get_config()
    return ClearanceConfig(
        cf_cookies=first_config_str(
            cfg,
            "proxy.cf_cookies",
            "proxy.clearance.cf_cookies",
        ),
        user_agent=first_config_str(
            cfg,
            "proxy.user_agent",
            "proxy.clearance.user_agent",
        ),
        cf_clearance=first_config_str(
            cfg,
            "proxy.cf_clearance",
            "proxy.clearance.cf_clearance",
        ),
        browser=first_config_str(
            cfg,
            "proxy.browser",
            "proxy.clearance.browser",
        ),
    )


__all__ = ["ClearanceConfig", "first_config_str", "resolve_clearance_config"]
