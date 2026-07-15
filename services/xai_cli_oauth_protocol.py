"""Pure protocol helpers for xAI Grok CLI OAuth.

The CLI promotion endpoint is separate from the grok.com SSO and
console.x.ai transports used by the embedded Grok runtime.  Keep the
wire-format and response adaptation here so it can be tested without a
credential or network access.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any, Iterable


XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
XAI_CLI_BASE_URL = "https://cli-chat-proxy.grok.com/v1"

XAI_CLI_HEADERS = {
    "x-grok-client-version": "0.2.93",
    "x-xai-token-auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-shell",
    "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
}

GROK_45_MODEL_ID = "grok-4.5"
GROK_45_MODEL_ITEM = {
    "id": GROK_45_MODEL_ID,
    "object": "model",
    "created": 0,
    "owned_by": "xai",
    "name": "Grok 4.5",
    "provider": "grok",
    "capabilities": ["chat"],
}


def jwt_claims(token: object) -> dict[str, Any]:
    """Decode an unsigned JWT payload for expiry/profile hints only.

    The token is verified by xAI on use; this helper never treats a decoded
    payload as proof of identity.
    """
    raw = str(token or "").strip()
    parts = raw.split(".")
    if len(parts) < 2:
        return {}
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def token_expiry_epoch(token: object, *, fallback_seconds: int = 21_600, now: float | None = None) -> int:
    claims = jwt_claims(token)
    try:
        exp = int(claims.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0
    if exp > 0:
        return exp
    return int((time.time() if now is None else now) + max(60, int(fallback_seconds or 0)))


def token_email(token: object) -> str:
    claims = jwt_claims(token)
    for key in ("email", "preferred_username", "upn"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value
    return ""


def normalize_model_ids(data: object) -> list[str]:
    """Extract unique model ids from an OpenAI-style ``/models`` response."""
    if not isinstance(data, dict):
        return []
    items = data.get("data")
    if not isinstance(items, list):
        return []
    ids: list[str] = []
    for item in items:
        model_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
        if model_id and model_id not in ids:
            ids.append(model_id)
    return ids


def response_text(response: object) -> str:
    """Collect visible text from an OpenAI Responses API object."""
    if not isinstance(response, dict):
        return ""
    direct = response.get("output_text")
    if isinstance(direct, str) and direct:
        return direct

    parts: list[str] = []
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "") in {"output_text", "text"}:
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    return "".join(parts)


def response_usage(response: object) -> dict[str, int]:
    source = response.get("usage") if isinstance(response, dict) else None
    source = source if isinstance(source, dict) else {}

    def as_int(*keys: str) -> int:
        for key in keys:
            try:
                value = int(source.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value >= 0:
                return value
        return 0

    prompt = as_int("input_tokens", "prompt_tokens")
    completion = as_int("output_tokens", "completion_tokens")
    total = as_int("total_tokens") or prompt + completion
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def response_to_chat_completion(response: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Adapt a non-streaming Responses object to Chat Completions."""
    response_id = str(response.get("id") or f"chatcmpl_{uuid.uuid4().hex}")
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response_text(response)},
                "finish_reason": "stop",
            }
        ],
        "usage": response_usage(response),
    }


def response_to_anthropic_message(response: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Adapt a non-streaming Responses object to Anthropic Messages."""
    usage = response_usage(response)
    return {
        "id": str(response.get("id") or f"msg_{uuid.uuid4().hex}"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": response_text(response)}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage["prompt_tokens"],
            "output_tokens": usage["completion_tokens"],
        },
    }


def _normalize_content(content: object, *, role: str) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "input_text", "text": str(content or "")}]

    blocks: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            blocks.append({"type": "input_text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "text")
        if kind in {"text", "input_text", "output_text"}:
            text = str(block.get("text") or "")
            if text:
                blocks.append({"type": "input_text", "text": text})
        elif kind in {"image_url", "input_image"}:
            image_url = block.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if image_url:
                blocks.append({"type": "input_image", "image_url": str(image_url)})
        elif kind == "file":
            value = block.get("file")
            if isinstance(value, dict):
                value = value.get("data") or value.get("file_url")
            if value:
                blocks.append({"type": "input_file", "file_data": str(value)})
    if not blocks:
        blocks.append({"type": "input_text", "text": ""})
    return blocks


def chat_messages_to_response_input(messages: Iterable[object]) -> list[dict[str, Any]]:
    """Map common Chat Completions messages to Responses API input items."""
    result: list[dict[str, Any]] = []
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "user").strip().lower() or "user"
        # The CLI proxy follows Responses role names. Tool transcripts are
        # represented as ordinary text context when they cannot be losslessly
        # expressed in the public Responses input schema.
        if role == "tool":
            role = "user"
        if role not in {"developer", "system", "user", "assistant"}:
            role = "user"
        result.append({"role": role, "content": _normalize_content(raw.get("content"), role=role)})
    return result


def anthropic_messages_to_response_input(messages: Iterable[object], system: object = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(system, str) and system.strip():
        result.append({"role": "system", "content": [{"type": "input_text", "text": system}]})
    elif isinstance(system, list):
        text = "\n".join(
            str(item.get("text") or "")
            for item in system
            if isinstance(item, dict) and str(item.get("type") or "") == "text"
        ).strip()
        if text:
            result.append({"role": "system", "content": [{"type": "input_text", "text": text}]})
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "user").strip().lower()
        result.append({
            "role": "assistant" if role == "assistant" else "user",
            "content": _normalize_content(raw.get("content"), role=role),
        })
    return result


__all__ = [
    "GROK_45_MODEL_ID",
    "GROK_45_MODEL_ITEM",
    "XAI_CLI_BASE_URL",
    "XAI_CLI_HEADERS",
    "XAI_DEVICE_CODE_URL",
    "XAI_OAUTH_CLIENT_ID",
    "XAI_OAUTH_SCOPE",
    "XAI_TOKEN_URL",
    "anthropic_messages_to_response_input",
    "chat_messages_to_response_input",
    "jwt_claims",
    "normalize_model_ids",
    "response_text",
    "response_to_anthropic_message",
    "response_to_chat_completion",
    "response_usage",
    "token_email",
    "token_expiry_epoch",
]
