"""Optional delivery of locally saved xAI OAuth credentials to external pools."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

from services.cpa_service import cpa_config, upload_xai_oauth_file
from services.sub2api_service import normalize_sync_config, sub2api_config, sync_xai_oauth_account


DEFAULT_XAI_OAUTH_DELIVERY_CONFIG = {
    "sub2api": {
        "enabled": False,
        "server_id": "",
        "group_mode": "existing",
        "group_id": "",
        "group_name": "",
    },
    "cpa": {
        "enabled": False,
        "pool_id": "",
    },
}


def _clean(value: object) -> str:
    return str(value or "").strip()


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = _clean(value).lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_xai_oauth_delivery_config(raw: object) -> dict[str, dict[str, Any]]:
    source = raw if isinstance(raw, dict) else {}
    sub2api = normalize_sync_config(source.get("sub2api"))
    cpa_source = source.get("cpa") if isinstance(source.get("cpa"), dict) else {}
    return {
        "sub2api": sub2api,
        "cpa": {
            "enabled": _as_bool(cpa_source.get("enabled"), False),
            "pool_id": _clean(cpa_source.get("pool_id")),
        },
    }


def _safe_error(error: BaseException, account: dict) -> str:
    message = _clean(error) or type(error).__name__
    for key in ("access_token", "refresh_token", "id_token", "email", "subject"):
        secret = _clean(account.get(key))
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:500]


def _deliver_sub2api(account: dict, settings: dict) -> dict:
    server_id = _clean(settings.get("server_id"))
    server = sub2api_config.get_server(server_id)
    if server is None:
        raise RuntimeError("选择的 Sub2API 连接不存在")
    return sync_xai_oauth_account(server, account, settings)


def _deliver_cpa(account: dict, settings: dict) -> dict:
    pool_id = _clean(settings.get("pool_id"))
    pool = cpa_config.get_pool(pool_id)
    if pool is None:
        raise RuntimeError("选择的 CPA 连接不存在")
    return upload_xai_oauth_file(pool, account)


def deliver_xai_oauth_account(account: dict, raw_config: object) -> dict[str, dict[str, Any]]:
    """Deliver enabled targets independently and return credential-free results."""
    settings = normalize_xai_oauth_delivery_config(raw_config)
    results: dict[str, dict[str, Any]] = {}
    targets: dict[str, tuple[Callable[[dict, dict], dict], dict, str]] = {
        "sub2api": (_deliver_sub2api, settings["sub2api"], _clean(settings["sub2api"].get("server_id"))),
        "cpa": (_deliver_cpa, settings["cpa"], _clean(settings["cpa"].get("pool_id"))),
    }
    enabled_targets = {
        name: target
        for name, target in targets.items()
        if bool(target[1].get("enabled"))
    }
    for name, (_, _, target_id) in targets.items():
        if name not in enabled_targets:
            results[name] = {
                "status": "skipped",
                "target_id": target_id,
                "at": _now_iso(),
            }

    if not enabled_targets:
        return results

    with ThreadPoolExecutor(max_workers=len(enabled_targets), thread_name_prefix="xai-oauth-delivery") as executor:
        pending = {
            executor.submit(handler, account, target_settings): (name, target_id)
            for name, (handler, target_settings, target_id) in enabled_targets.items()
        }
        for future in as_completed(pending):
            name, target_id = pending[future]
            try:
                remote = future.result()
                results[name] = {
                    "status": "success",
                    "target_id": target_id,
                    "at": _now_iso(),
                    "remote": remote,
                }
            except Exception as exc:
                results[name] = {
                    "status": "failed",
                    "target_id": target_id,
                    "at": _now_iso(),
                    "error": _safe_error(exc, account),
                }
    return results


__all__ = [
    "DEFAULT_XAI_OAUTH_DELIVERY_CONFIG",
    "deliver_xai_oauth_account",
    "normalize_xai_oauth_delivery_config",
]
