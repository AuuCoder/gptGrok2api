"""Unified token estimation utilities.

All API surfaces share the same tokenizer-backed approximation so usage
reporting stays consistent across OpenAI-compatible and Anthropic-compatible
responses.
"""

from functools import lru_cache
from typing import Any

import orjson
import tiktoken

PROMPT_OVERHEAD = 4
_ENCODING_NAME = "o200k_base"


@lru_cache(maxsize=1)
def _get_encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(_ENCODING_NAME)


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return orjson.dumps(value).decode()
    except (TypeError, ValueError):
        return str(value)


def estimate_tokens(value: Any) -> int:
    text = _coerce_text(value).strip()
    if not text:
        return 0
    return len(_get_encoding().encode(text, disallowed_special=()))


def estimate_prompt_tokens(value: Any, *, overhead: int = PROMPT_OVERHEAD) -> int:
    base = estimate_tokens(value)
    if base <= 0:
        return 0
    return base + max(0, overhead)


def estimate_tool_call_tokens(tool_calls: list[Any]) -> int:
    normalized: list[Any] = []
    for call in tool_calls:
        if isinstance(call, dict):
            normalized.append(call)
            continue
        name = getattr(call, "name", None)
        arguments = getattr(call, "arguments", None)
        call_id = getattr(call, "call_id", None)
        if name is not None and arguments is not None:
            normalized.append({
                "id": call_id or "",
                "name": name,
                "arguments": arguments,
            })
            continue
        normalized.append(call)
    return estimate_tokens(normalized)


__all__ = [
    "PROMPT_OVERHEAD",
    "estimate_tokens",
    "estimate_prompt_tokens",
    "estimate_tool_call_tokens",
]
