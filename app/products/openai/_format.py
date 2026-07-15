"""Response formatting utilities — pure functions, no async, no IO.

Two sections:
  - Chat Completions format  (make_response_id, make_stream_chunk, …)
  - Responses API format     (make_resp_id, make_resp_object, …)
"""

import os
import time
from typing import Any

import orjson
from app.platform.tokens import estimate_prompt_tokens, estimate_tokens, estimate_tool_call_tokens


# ---------------------------------------------------------------------------
# Chat Completions format
# ---------------------------------------------------------------------------

def make_response_id() -> str:
    return f"chatcmpl-{int(time.time() * 1000)}{os.urandom(4).hex()}"


def build_usage(prompt_tokens: int, completion_tokens: int, *, reasoning_tokens: int = 0) -> dict:
    pt = max(0, prompt_tokens)
    ct = max(0, completion_tokens)
    rt = max(0, reasoning_tokens)
    return {
        "prompt_tokens":     pt,
        "completion_tokens": ct,
        "total_tokens":      pt + ct,
        "prompt_tokens_details": {
            "cached_tokens": 0, "text_tokens": pt,
            "audio_tokens":  0, "image_tokens": 0,
        },
        "completion_tokens_details": {
            "text_tokens": ct - rt, "audio_tokens": 0, "reasoning_tokens": rt,
        },
    }


def make_stream_chunk(
    response_id: str,
    model:       str,
    content:     str,
    *,
    index:         int       = 0,
    role:          str       = "assistant",
    is_final:      bool      = False,
    finish_reason: str | None = None,
    usage:         dict | None = None,
    annotations:   list[dict] | None = None,
) -> dict:
    choice: dict = {
        "index": index,
        "delta": {"role": role, "content": content},
    }
    if is_final:
        choice["finish_reason"] = finish_reason or "stop"
        # annotations 仅在 final chunk 的 delta 中发送（Vercel AI SDK 读 delta.annotations）
        if annotations:
            choice["delta"]["annotations"] = annotations

    chunk: dict = {
        "id":      response_id,
        "object":  "chat.completion.chunk",
        "created": int(time.time()),
        "model":   model,
        "choices": [choice],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def make_thinking_chunk(
    response_id: str,
    model:       str,
    content:     str,
    *,
    index: int = 0,
    role:  str = "assistant",
) -> dict:
    """Stream chunk carrying reasoning_content (DeepSeek-R1 style thinking delta)."""
    return {
        "id":      response_id,
        "object":  "chat.completion.chunk",
        "created": int(time.time()),
        "model":   model,
        "choices": [{
            "index": index,
            "delta": {"role": role, "reasoning_content": content},
        }],
    }


def make_chat_response(
    model:   str,
    content: str,
    *,
    prompt_content:     Any | None  = None,
    response_id:       str | None  = None,
    usage:             dict | None = None,
    reasoning_content: str | None  = None,
    search_sources:    list[dict] | None = None,
    annotations:       list[dict] | None = None,
) -> dict:
    rid = response_id or make_response_id()
    pt  = estimate_prompt_tokens(prompt_content)
    ct  = estimate_tokens(content)
    rt  = estimate_tokens(reasoning_content) if reasoning_content else 0
    ct += rt

    msg: dict = {"role": "assistant", "content": content}
    if reasoning_content:
        msg["reasoning_content"] = reasoning_content
    if annotations:
        msg["annotations"] = annotations
    resp = {
        "id":      rid,
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model,
        "choices": [{
            "index":         0,
            "message":       msg,
            "finish_reason": "stop",
        }],
        "usage": usage or build_usage(pt, ct, reasoning_tokens=rt),
    }
    # search_sources 放在响应根对象（避免 Vercel AI SDK 的 message strict schema 拒绝未知字段）
    if search_sources:
        resp["search_sources"] = search_sources
    return resp


# ---------------------------------------------------------------------------
# Responses API format
# ---------------------------------------------------------------------------

def make_resp_id(prefix: str) -> str:
    """Generate a Responses API item ID, e.g. resp_xxx / rs_xxx / msg_xxx / fc_xxx."""
    return f"{prefix}_{int(time.time() * 1000)}{os.urandom(4).hex()}"


def build_resp_usage(input_tokens: int, output_tokens: int, reasoning_tokens: int = 0) -> dict:
    return {
        "input_tokens":  max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "total_tokens":  max(0, input_tokens + output_tokens),
        "output_tokens_details": {"reasoning_tokens": max(0, reasoning_tokens)},
    }


def make_resp_object(
    response_id: str,
    model:       str,
    status:      str,
    output:      list[dict],
    usage:       dict | None = None,
) -> dict:
    obj: dict = {
        "id":         response_id,
        "object":     "response",
        "created_at": int(time.time()),
        "status":     status,
        "model":      model,
        "output":     output,
    }
    if usage is not None:
        obj["usage"] = usage
    return obj


def format_sse(event: str, data: dict) -> str:
    """Encode a single Responses API SSE event frame."""
    return f"event: {event}\ndata: {orjson.dumps(data).decode()}\n\n"


# ---------------------------------------------------------------------------
# Tool call format (Chat Completions)
# ---------------------------------------------------------------------------

def make_tool_call_chunk(
    response_id: str,
    model:       str,
    index:       int,
    call_id:     str,
    name:        str,
    arguments:   str,
    *,
    is_first: bool = False,
) -> dict:
    """A streaming delta chunk carrying a tool_calls item.

    On the first chunk for a given call index set *is_first=True* — this
    emits the id/type/name fields.  Subsequent chunks carry only the
    arguments delta.
    """
    if is_first:
        tool_call_delta = {
            "index": index,
            "id":    call_id,
            "type":  "function",
            "function": {"name": name, "arguments": arguments},
        }
    else:
        tool_call_delta = {
            "index": index,
            "function": {"arguments": arguments},
        }
    return {
        "id":      response_id,
        "object":  "chat.completion.chunk",
        "created": int(time.time()),
        "model":   model,
        "choices": [{
            "index": 0,
            "delta": {
                "role":       "assistant",
                "content":    None,
                "tool_calls": [tool_call_delta],
            },
        }],
    }


def make_tool_call_done_chunk(
    response_id: str,
    model:       str,
    *,
    usage: dict | None = None,
) -> dict:
    """Final streaming chunk with finish_reason='tool_calls'."""
    chunk: dict = {
        "id":      response_id,
        "object":  "chat.completion.chunk",
        "created": int(time.time()),
        "model":   model,
        "choices": [{
            "index":         0,
            "delta":         {},
            "finish_reason": "tool_calls",
        }],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def make_tool_call_response(
    model:      str,
    tool_calls: list,
    *,
    prompt_content: Any | None = None,
    response_id: str | None = None,
    usage:       dict | None = None,
) -> dict:
    """Non-streaming chat completion response carrying tool_calls."""
    from app.dataplane.reverse.protocol.tool_parser import ParsedToolCall
    rid = response_id or make_response_id()
    tc_list = [
        {
            "id":   tc.call_id,
            "type": "function",
            "function": {
                "name":      tc.name,
                "arguments": tc.arguments,
            },
        }
        for tc in tool_calls
        if isinstance(tc, ParsedToolCall)
    ]
    ct = estimate_tool_call_tokens(tool_calls)
    pt = estimate_prompt_tokens(prompt_content)
    return {
        "id":      rid,
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model,
        "choices": [{
            "index": 0,
            "message": {
                "role":       "assistant",
                "content":    None,
                "tool_calls": tc_list,
            },
            "finish_reason": "tool_calls",
        }],
        "usage": usage or build_usage(pt, ct),
    }


__all__ = [
    # chat completions
    "make_response_id", "build_usage",
    "make_stream_chunk", "make_thinking_chunk", "make_chat_response",
    # tool calls
    "make_tool_call_chunk", "make_tool_call_done_chunk", "make_tool_call_response",
    # responses api
    "make_resp_id", "build_resp_usage", "make_resp_object", "format_sse",
]
