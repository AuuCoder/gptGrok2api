#!/usr/bin/env python3
"""Idempotently configure WARP/FlareSolverr for both embedded runtimes."""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

DEFAULT_PROXY_RUNTIME: dict[str, Any] = {
    "enabled": False,
    "egress_mode": "direct",
    "proxy_url": "",
    "resource_proxy_url": "",
    "skip_ssl_verify": False,
    "reset_session_status_codes": [403],
    "clearance": {
        "enabled": False,
        "mode": "none",
        "cf_cookies": "",
        "cf_clearance": "",
        "user_agent": DEFAULT_USER_AGENT,
        "browser": "chrome",
        "flaresolverr_url": "",
        "timeout_sec": 60,
        "refresh_interval": 3600,
        "warm_up_on_start": False,
    },
}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def _bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError, OverflowError):
        return default


def _warp_runtime_defaults() -> dict[str, Any]:
    clearance_enabled = _env_bool("CHATGPT2API_PROXY_RUNTIME_CLEARANCE_ENABLED", True)
    clearance_mode = os.getenv("CHATGPT2API_PROXY_RUNTIME_CLEARANCE_MODE", "flaresolverr").strip().lower()
    if clearance_mode not in {"none", "manual", "flaresolverr"}:
        clearance_mode = "flaresolverr" if clearance_enabled else "none"
    if not clearance_enabled:
        clearance_mode = "none"

    egress_mode = os.getenv("CHATGPT2API_PROXY_RUNTIME_EGRESS_MODE", "single_proxy").strip().lower()
    if egress_mode not in {"direct", "single_proxy"}:
        egress_mode = "single_proxy"

    runtime = copy.deepcopy(DEFAULT_PROXY_RUNTIME)
    runtime.update(
        {
            "enabled": _env_bool("CHATGPT2API_PROXY_RUNTIME_ENABLED", True),
            "egress_mode": egress_mode,
            "proxy_url": os.getenv("CHATGPT2API_PROXY_RUNTIME_PROXY_URL", "http://privoxy:8118").strip(),
            "resource_proxy_url": os.getenv("CHATGPT2API_PROXY_RUNTIME_RESOURCE_PROXY_URL", "").strip(),
            "skip_ssl_verify": _env_bool("CHATGPT2API_PROXY_RUNTIME_SKIP_SSL_VERIFY", False),
            "reset_session_status_codes": [
                int(part.strip())
                for part in os.getenv("CHATGPT2API_PROXY_RUNTIME_RESET_STATUS_CODES", "403").split(",")
                if part.strip().isdigit() and 100 <= int(part.strip()) <= 599
            ] or [403],
        }
    )
    runtime["clearance"].update(
        {
            "enabled": clearance_enabled,
            "mode": clearance_mode,
            "user_agent": os.getenv("CHATGPT2API_PROXY_RUNTIME_USER_AGENT", DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT,
            "browser": os.getenv("CHATGPT2API_PROXY_RUNTIME_BROWSER", "chrome").strip() or "chrome",
            "flaresolverr_url": os.getenv("CHATGPT2API_FLARESOLVERR_URL", "http://flaresolverr:8191").strip(),
            "timeout_sec": _env_int("CHATGPT2API_PROXY_RUNTIME_CLEARANCE_TIMEOUT_SEC", 60, 1),
            "refresh_interval": _env_int("CHATGPT2API_PROXY_RUNTIME_CLEARANCE_REFRESH_INTERVAL", 3600, 60),
            "warm_up_on_start": _env_bool("CHATGPT2API_PROXY_RUNTIME_WARM_UP_ON_START", False),
        }
    )
    return runtime


def _deep_fill_missing(target: dict[str, Any], defaults: dict[str, Any]) -> bool:
    changed = False
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
            changed = True
            continue
        if isinstance(target[key], dict) and isinstance(value, dict):
            changed = _deep_fill_missing(target[key], value) or changed
    return changed


def _looks_like_repository_default(runtime: Any) -> bool:
    if not isinstance(runtime, dict):
        return True
    candidate = copy.deepcopy(runtime)
    _deep_fill_missing(candidate, DEFAULT_PROXY_RUNTIME)
    return candidate == DEFAULT_PROXY_RUNTIME


def _looks_like_inactive_warp_scaffold(runtime: Any) -> bool:
    if not isinstance(runtime, dict):
        return False
    clearance = runtime.get("clearance") if isinstance(runtime.get("clearance"), dict) else {}
    return (
        not _bool_value(runtime.get("enabled"), False)
        and str(runtime.get("egress_mode") or "").strip().lower() == "single_proxy"
        and str(runtime.get("proxy_url") or "").strip() == "http://privoxy:8118"
        and _bool_value(clearance.get("enabled"), False)
        and str(clearance.get("mode") or "").strip().lower() == "flaresolverr"
        and str(clearance.get("flaresolverr_url") or "").strip() == "http://flaresolverr:8191"
        and not str(clearance.get("cf_cookies") or "").strip()
        and not str(clearance.get("cf_clearance") or "").strip()
    )


def _mask_url(value: str) -> str:
    return re.sub(r"(https?://)([^\s/@:]+):([^\s/@]+)@", r"\1[REDACTED]@", value or "", flags=re.I)


def _remove_toml_section(content: str, section: str) -> str:
    pattern = rf"(?ms)^\[{re.escape(section)}\][ \t]*\n.*?(?=^\[|\Z)"
    return re.sub(pattern, "", content).strip()


def _grok_proxy_toml(runtime: dict[str, Any]) -> str:
    enabled = _bool_value(runtime.get("enabled"), False)
    proxy_url = str(runtime.get("proxy_url") or "").strip()
    resource_proxy_url = str(runtime.get("resource_proxy_url") or "").strip() or proxy_url
    egress_mode = str(runtime.get("egress_mode") or "direct").strip().lower()
    if not enabled or egress_mode not in {"direct", "single_proxy"}:
        egress_mode = "direct" if not enabled else "single_proxy"

    clearance = runtime.get("clearance") if isinstance(runtime.get("clearance"), dict) else {}
    clearance_enabled = enabled and _bool_value(clearance.get("enabled"), False)
    clearance_mode = str(clearance.get("mode") or "none").strip().lower()
    if not clearance_enabled or clearance_mode not in {"none", "manual", "flaresolverr"}:
        clearance_mode = "none"

    quote_string = lambda value: json.dumps(str(value or ""), ensure_ascii=False)
    return "\n".join(
        [
            "[proxy.egress]",
            f"mode = {quote_string(egress_mode)}",
            f"proxy_url = {quote_string(proxy_url)}",
            f"resource_proxy_url = {quote_string(resource_proxy_url)}",
            "proxy_pool = []",
            "resource_proxy_pool = []",
            f"skip_ssl_verify = {'true' if _bool_value(runtime.get('skip_ssl_verify'), False) else 'false'}",
            "",
            "[proxy.clearance]",
            f"mode = {quote_string(clearance_mode)}",
            "cf_cookies = \"\"",
            "cf_clearance = \"\"",
            f"user_agent = {quote_string(clearance.get('user_agent') or DEFAULT_USER_AGENT)}",
            f"browser = {quote_string(clearance.get('browser') or 'chrome')}",
            f"flaresolverr_url = {quote_string(clearance.get('flaresolverr_url'))}",
            f"timeout_sec = {max(1, int(clearance.get('timeout_sec') or 60))}",
            f"refresh_interval = {max(60, int(clearance.get('refresh_interval') or 3600))}",
            "",
        ]
    )


def _sync_grok_proxy_config(config_path: Path, runtime: dict[str, Any]) -> None:
    content = ""
    parsed: dict[str, Any] = {}
    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
        try:
            loaded = tomllib.loads(content)
            parsed = loaded if isinstance(loaded, dict) else {}
        except tomllib.TOMLDecodeError as exc:
            print(f"Invalid TOML in {config_path}: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

    proxy = parsed.get("proxy") if isinstance(parsed.get("proxy"), dict) else {}
    egress = proxy.get("egress") if isinstance(proxy.get("egress"), dict) else {}
    current_mode = str(egress.get("mode") or "direct").strip().lower()
    current_url = str(egress.get("proxy_url") or "").strip()
    force = _env_bool("CHATGPT2API_GROK_PROXY_RUNTIME_FORCE", False) or _env_bool(
        "CHATGPT2API_PROXY_RUNTIME_FORCE", False
    )
    default_or_warp = (current_mode == "direct" and not current_url) or current_url == "http://privoxy:8118"
    if content and not force and not default_or_warp:
        print(f"Embedded Grok proxy already configured in {config_path}")
        return

    retained = _remove_toml_section(content, "proxy.egress")
    retained = _remove_toml_section(retained, "proxy.clearance")
    payload = f"{retained}\n\n{_grok_proxy_toml(runtime)}" if retained else _grok_proxy_toml(runtime)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(payload.rstrip() + "\n", encoding="utf-8")
    print(
        "Embedded Grok proxy summary: "
        f"config={config_path}, "
        f"proxy_url={_mask_url(str(runtime.get('proxy_url') or ''))}"
    )


def main() -> int:
    config_path = Path(os.getenv("CHATGPT2API_CONFIG_FILE", "/app/config.json"))
    if not config_path.exists():
        print(f"Config file not found, creating {config_path}")
        data: dict[str, Any] = {}
    else:
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"Invalid JSON in {config_path}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(data, dict):
            print(f"Config root must be an object: {config_path}", file=sys.stderr)
            return 1

    desired = _warp_runtime_defaults()
    existing = data.get("proxy_runtime")
    changed = False

    if (
        _looks_like_repository_default(existing)
        or _looks_like_inactive_warp_scaffold(existing)
        or _env_bool("CHATGPT2API_PROXY_RUNTIME_FORCE", False)
    ):
        data["proxy_runtime"] = desired
        changed = True
        print("Created proxy_runtime defaults")
    else:
        runtime = existing if isinstance(existing, dict) else {}
        changed = _deep_fill_missing(runtime, DEFAULT_PROXY_RUNTIME)
        data["proxy_runtime"] = runtime
        print("Proxy runtime already configured")

    if changed:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(config_path)
        except OSError as exc:
            # Docker bind-mounted single files can reject atomic rename with EBUSY.
            # Fall back to in-place write so the init job works with both file and
            # directory mounts.
            if getattr(exc, "errno", None) != 16:
                raise
            config_path.write_text(payload, encoding="utf-8")
            tmp_path.unlink(missing_ok=True)

    runtime = data.get("proxy_runtime") if isinstance(data.get("proxy_runtime"), dict) else {}
    _sync_grok_proxy_config(
        Path(os.getenv("GROK_CONFIG_FILE", "/app/data/grok_runtime/config.toml")),
        runtime,
    )

    clearance = runtime.get("clearance") if isinstance(runtime.get("clearance"), dict) else {}
    print(
        "Proxy runtime summary: "
        f"enabled={bool(runtime.get('enabled'))}, "
        f"egress_mode={runtime.get('egress_mode')}, "
        f"proxy_url={_mask_url(str(runtime.get('proxy_url') or ''))}, "
        f"clearance_mode={clearance.get('mode')}, "
        f"flaresolverr_url={_mask_url(str(clearance.get('flaresolverr_url') or ''))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
