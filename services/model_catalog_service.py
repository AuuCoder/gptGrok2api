from __future__ import annotations

from typing import Any

from services.account_service import account_service
from services.config import config
from utils.helper import CODEX_IMAGE_MODEL


FALLBACK_CHAT_MODELS = [
    "auto",
    "gpt-5",
    "gpt-5-1",
    "gpt-5-2",
    "gpt-5-3",
    "gpt-5-3-mini",
    "gpt-5-5",
    "gpt-5-6",
    "gpt-5-6-sol",
    "gpt-5-6-terra",
    "gpt-5-6-luna",
    "gpt-5-mini",
]

FALLBACK_IMAGE_MODELS = [
    "gpt-image-2",
]


def _normalize_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def _settings_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _configured_chat_models(settings: dict[str, Any]) -> list[str]:
    catalog = _settings_dict(settings.get("model_catalog"))
    explicit = _normalize_list(catalog.get("chat_models"))
    if explicit:
        return explicit

    combined: list[str] = []
    for key in ("base_chat_models", "specialized_chat_models", "image_capable_chat_models"):
        for model in _normalize_list(catalog.get(key)):
            if model not in combined:
                combined.append(model)
    return combined


def _configured_image_models(settings: dict[str, Any]) -> list[str]:
    image_generation = _settings_dict(settings.get("image_generation"))
    catalog = _settings_dict(settings.get("model_catalog"))
    for source in (
        image_generation.get("model_options"),
        catalog.get("image_api_models"),
        image_generation.get("supported_models"),
    ):
        models = _normalize_list(source)
        if models:
            return models
    return []


def _image_models_from_accounts(accounts: list[dict[str, Any]]) -> list[str]:
    available_accounts = [
        account
        for account in accounts
        if isinstance(account, dict) and account_service._is_image_account_available(account)
    ]
    if not available_accounts:
        return []

    models: list[str] = ["gpt-image-2"]
    codex_types = {
        normalized
        for account in available_accounts
        if account_service._normalize_source_type(account.get("source_type")) == "codex"
        and (normalized := account_service._normalize_account_type(account.get("type")))
    }

    if codex_types & {"Plus", "Team", "Pro"}:
        models.append(CODEX_IMAGE_MODEL)
    for plan_type in ("Plus", "Team", "Pro"):
        if plan_type in codex_types:
            models.append(f"{plan_type.lower()}-{CODEX_IMAGE_MODEL}")
    return models


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def get_model_catalog() -> dict[str, Any]:
    settings = config.get()
    configured_chat_models = _configured_chat_models(settings)
    configured_image_models = _configured_image_models(settings)

    chat_source = "config" if configured_chat_models else "fallback"
    chat_models = configured_chat_models or list(FALLBACK_CHAT_MODELS)

    if configured_image_models:
        image_source = "config"
        image_models = configured_image_models
    else:
        account_models = _image_models_from_accounts(account_service.list_accounts())
        image_source = "accounts" if account_models else "fallback"
        image_models = account_models or list(FALLBACK_IMAGE_MODELS)

    grok_catalog: dict[str, Any] = {}
    try:
        from services.grok_runtime import grok_runtime

        if grok_runtime.available:
            grok_catalog = grok_runtime.run_sync(grok_runtime.model_catalog, timeout=10)
    except Exception:
        grok_catalog = {}

    # Grok Build/CLI OAuth is a distinct bearer-token provider.  Its cached
    # model discovery is local-only here: model listing must never make an
    # upstream call or refresh a credential on the request path.
    try:
        from services.xai_cli_oauth_service import xai_cli_oauth_service

        oauth_grok_items = xai_cli_oauth_service.model_items()
    except Exception:
        oauth_grok_items = []

    grok_chat_models = _normalize_list(grok_catalog.get("chat_models"))
    for item in oauth_grok_items:
        if isinstance(item, dict):
            model_id = str(item.get("id") or "").strip()
            if model_id and model_id not in grok_chat_models:
                grok_chat_models.append(model_id)
    grok_image_models = _normalize_list(grok_catalog.get("image_models"))
    grok_image_edit_models = _normalize_list(grok_catalog.get("image_edit_models"))
    grok_video_models = _normalize_list(grok_catalog.get("video_models"))

    chat_models = _unique([*chat_models, *grok_chat_models])
    image_edit_models = _unique([*image_models, *grok_image_edit_models])
    image_models = _unique([*image_models, *grok_image_models])
    all_models = _unique([
        *chat_models,
        *image_models,
        *image_edit_models,
        *grok_video_models,
    ])

    grok_items = grok_catalog.get("items") if isinstance(grok_catalog.get("items"), list) else []
    all_grok_items: list[dict[str, Any]] = []
    grok_item_ids: set[str] = set()
    for item in [*grok_items, *oauth_grok_items]:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id or model_id in grok_item_ids:
            continue
        grok_item_ids.add(model_id)
        all_grok_items.append(dict(item))

    if grok_catalog and oauth_grok_items:
        grok_source = "runtime+oauth"
    elif grok_catalog:
        grok_source = "runtime"
    elif oauth_grok_items:
        grok_source = "oauth"
    else:
        grok_source = "unavailable"

    return {
        "object": "model_catalog",
        "chat_models": chat_models,
        "image_models": image_models,
        "image_edit_models": image_edit_models,
        "video_models": grok_video_models,
        "models": all_grok_items,
        "all_models": all_models,
        "source": {
            "chat": chat_source,
            "image": image_source,
            "grok": grok_source,
        },
        "openai_models_endpoint": "/v1/models",
    }
