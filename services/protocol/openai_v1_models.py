from __future__ import annotations

from typing import Any

from services.account_service import account_service
from services.model_catalog_service import get_model_catalog
from services.openai_backend_api import OpenAIBackendAPI
from utils.helper import CODEX_IMAGE_MODEL


def _model_item(model: str) -> dict[str, Any]:
    return {
        "id": model,
        "object": "model",
        "created": 0,
        "owned_by": "gptgrok2api",
        "permission": [],
        "root": model,
        "parent": None,
    }


def _append_model(data: list[Any], seen: set[str], model: object) -> None:
    model_id = str(model or "").strip()
    if not model_id or model_id in seen:
        return
    seen.add(model_id)
    data.append(_model_item(model_id))


def _append_models(data: list[Any], seen: set[str], models: object) -> None:
    if not isinstance(models, list):
        return
    for model in models:
        _append_model(data, seen, model)


def _append_upstream_models(data: list[Any], seen: set[str]) -> None:
    try:
        with OpenAIBackendAPI() as backend:
            result = backend.list_models()
    except Exception:
        return
    upstream_data = result.get("data")
    if not isinstance(upstream_data, list):
        return
    for item in upstream_data:
        if not isinstance(item, dict):
            continue
        _append_model(data, seen, item.get("id"))


def _dynamic_image_models() -> list[str]:
    models: list[str] = []
    accounts = account_service.list_accounts()
    web_image_accounts = [
        account
        for account in accounts
        if isinstance(account, dict)
           and account_service._is_image_account_available(account)
    ]
    codex_types = {
        normalized
        for account in accounts
        if isinstance(account, dict)
           and account_service._normalize_source_type(account.get("source_type")) == "codex"
           and account_service._is_image_account_available(account)
           and (normalized := account_service._normalize_account_type(account.get("type")))
    }

    if web_image_accounts:
        models.append("gpt-image-2")
    if codex_types & {"Plus", "Team", "Pro"}:
        models.append(CODEX_IMAGE_MODEL)
    for plan_type in ("Plus", "Team", "Pro"):
        if plan_type in codex_types:
            models.append(f"{plan_type.lower()}-{CODEX_IMAGE_MODEL}")

    return models


def list_models() -> dict[str, Any]:
    catalog = get_model_catalog()
    data: list[Any] = []
    seen: set[str] = set()

    grok_items = catalog.get("models") if isinstance(catalog.get("models"), list) else []
    for item in grok_items:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        data.append(dict(item))

    _append_models(data, seen, catalog.get("chat_models"))
    _append_upstream_models(data, seen)
    _append_models(data, seen, catalog.get("image_models"))
    _append_models(data, seen, _dynamic_image_models())

    return {"object": "list", "data": data}
