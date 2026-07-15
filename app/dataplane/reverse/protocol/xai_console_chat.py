"""XAI console.x.ai chat protocol — payload builder and SSE stream adapter.

端点: POST https://console.x.ai/v1/responses
认证: Authorization: Bearer anonymous  +  Cookie: sso=<token>; sso-rw=<token>

请求格式 (OpenAI Responses API):
{
    "model": "grok-4.3",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "..."}]}],
    "max_output_tokens": 1000000,
    "temperature": 0.7,
    "top_p": 0.95,
    "reasoning": {"effort": "low"},
    "store": false,
    "include": ["reasoning.encrypted_content"],
    "stream": true
}

响应 SSE 事件类型:
- response.created / response.in_progress  — 忽略
- response.output_item.added               — 忽略
- response.output_item.done                — reasoning item，含 encrypted_content（不可读）
- response.content_part.added             — 忽略
- response.output_text.delta              — 文本 token，delta 字段
- response.output_text.done              — 忽略
- response.content_part.done             — 忽略
- response.output_item.done (message)    — 忽略
- response.completed                      — 含 usage 统计
"""

from typing import Any, AsyncGenerator

import orjson

from app.platform.errors import UpstreamError, ValidationError
from app.platform.logging.logger import logger


# ---------------------------------------------------------------------------
# 支持的模型名 → console.x.ai 实际 model 字段映射
# ---------------------------------------------------------------------------

# console.x.ai 上可用的模型（通过 grok.com SSO 免费访问）
# key = grok2api 对外暴露的模型名，value = console.x.ai 实际 model 字段
CONSOLE_MODELS: dict[str, str] = {
    "grok-4.3-console":                     "grok-4.3",
    "grok-4.3-low":                         "grok-4.3",
    "grok-4.3-medium":                      "grok-4.3",
    "grok-4.3-high":                        "grok-4.3",
    "grok-4.20-0309-reasoning-console":     "grok-4.20-0309-reasoning",
    "grok-4.20-0309-console":               "grok-4.20-0309",
    "grok-4.20-0309-non-reasoning-console": "grok-4.20-0309-non-reasoning",
    "grok-4.20-multi-agent-console":        "grok-4.20-multi-agent-0309",
    "grok-4.20-multi-agent-low":            "grok-4.20-multi-agent-0309",
    "grok-4.20-multi-agent-medium":         "grok-4.20-multi-agent-0309",
    "grok-4.20-multi-agent-high":           "grok-4.20-multi-agent-0309",
    "grok-4.20-multi-agent-xhigh":          "grok-4.20-multi-agent-0309",
    "grok-build-console":                   "grok-build-0.1",
}

# 需要附带 reasoning 字段的模型（grok-4.3 系列需要，grok-4.20 系列不需要）
_MODELS_WITH_REASONING_FIELD: frozenset[str] = frozenset({
    "grok-4.3",
    "grok-4.20-multi-agent-0309",
})

# 模型名后缀 → 固定 effort 值（优先级高于用户传入的 reasoning_effort）
_MODEL_FIXED_EFFORT: dict[str, str] = {
    "grok-4.3-low":    "low",
    "grok-4.3-medium": "medium",
    "grok-4.3-high":   "high",
    "grok-4.20-multi-agent-low":    "low",
    "grok-4.20-multi-agent-medium": "medium",
    "grok-4.20-multi-agent-high":   "high",
    "grok-4.20-multi-agent-xhigh":  "xhigh",
}

# 特殊 max_output_tokens（默认 1_000_000）
_MODEL_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "grok-4.20-multi-agent-0309": 2_000_000,
    "grok-build-0.1": 256_000,
}

# 支持 web_search / x_search 工具的模型
_MODELS_WITH_SEARCH_TOOLS: frozenset[str] = frozenset({
    "grok-4.20-multi-agent-0309",
    "grok-4.20-0309",
    "grok-4.20-0309-reasoning",
    "grok-4.20-0309-non-reasoning",
    "grok-4.3",
    "grok-build-0.1",
})

# reasoning effort 映射：OpenAI reasoning_effort → console API effort
_EFFORT_MAP: dict[str, str] = {
    "none":    "none",
    "minimal": "low",
    "low":     "low",
    "medium":  "medium",
    "high":    "high",
    "xhigh":   "xhigh",
}


# Console currently supports text and image URLs only.  Do not silently turn
# binary/document inputs into a string: that looks like a successful request
# but produces an unrelated prompt upstream.
_UNSUPPORTED_CONTENT_TYPES: frozenset[str] = frozenset({
    "file",
    "input_file",
    "input_audio",
    "audio",
    "document",
})


def validate_console_request(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    max_output_tokens: int | None = None,
) -> None:
    """Reject request features that the Console bridge cannot represent.

    Console's built-in web/X search tools are configured by the bridge.  User
    supplied function tools, tool histories, and file/audio inputs have no
    compatible conversion yet, so reporting a validation error is preferable
    to silently changing the prompt or response contract.
    """
    if tools:
        raise ValidationError(
            "Console models do not support user-defined function tools yet",
            param="tools",
            code="unsupported_value",
        )
    if tool_choice is not None:
        raise ValidationError(
            "Console models do not support tool_choice yet",
            param="tool_choice",
            code="unsupported_value",
        )
    if max_output_tokens is not None and (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or max_output_tokens <= 0
    ):
        raise ValidationError(
            "max_output_tokens must be a positive integer",
            param="max_output_tokens",
            code="invalid_value",
        )

    for message_index, message in enumerate(messages):
        if message.get("tool_calls") or message.get("role") == "tool":
            raise ValidationError(
                "Console models do not support function tool messages yet",
                param=f"messages.{message_index}",
                code="unsupported_value",
            )

        content = message.get("content")
        if not isinstance(content, list):
            continue
        for content_index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            content_type = str(block.get("type") or "")
            if content_type in _UNSUPPORTED_CONTENT_TYPES:
                raise ValidationError(
                    f"Console models do not support {content_type!r} input yet",
                    param=f"messages.{message_index}.content.{content_index}",
                    code="unsupported_value",
                )
            if content_type in {"image_url", "input_image"}:
                image_url = block.get("image_url")
                url = (
                    image_url.get("url", "")
                    if isinstance(image_url, dict)
                    else image_url if isinstance(image_url, str) else ""
                )
                if isinstance(url, str) and url.lower().startswith("data:") and not url.lower().startswith("data:image/"):
                    raise ValidationError(
                        "Console models only support image data URIs, not file or audio data",
                        param=f"messages.{message_index}.content.{content_index}",
                        code="unsupported_value",
                    )


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

def build_console_payload(
    *,
    messages: list[dict[str, Any]],
    model: str,
    temperature: float = 0.7,
    top_p: float = 0.95,
    reasoning_effort: str | None = None,
    max_output_tokens: int | None = None,
    stream: bool = True,
) -> dict[str, Any]:
    """Build the JSON payload for POST console.x.ai/v1/responses.

    将 OpenAI messages 格式转换为 Responses API input 格式。
    """
    validate_console_request(messages, max_output_tokens=max_output_tokens)

    # 转换 messages → input 数组
    input_items: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # 映射 role
        if role in ("system", "developer"):
            # system 消息作为 instructions 字段处理，这里先放入 input
            api_role = "system"
        elif role == "assistant":
            api_role = "assistant"
        else:
            api_role = "user"

        # 处理 content
        if isinstance(content, str):
            content_blocks = [{"type": "input_text", "text": content}]
        elif isinstance(content, list):
            content_blocks = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    content_blocks.append({"type": "input_text", "text": block.get("text", "")})
                elif btype in {"image_url", "input_image"}:
                    image_url = block.get("image_url")
                    url = (
                        image_url.get("url", "")
                        if isinstance(image_url, dict)
                        else image_url if isinstance(image_url, str) else ""
                    )
                    if url:
                        content_blocks.append({"type": "input_image", "image_url": url})
                else:
                    # 其他类型降级为文本
                    text = block.get("text") or str(block)
                    content_blocks.append({"type": "input_text", "text": text})
        else:
            content_blocks = [{"type": "input_text", "text": str(content)}]

        if content_blocks:
            input_items.append({"role": api_role, "content": content_blocks})

    # reasoning effort：模型名固定值优先，其次用户传入，最后默认 medium
    effort = _MODEL_FIXED_EFFORT.get(model) or _EFFORT_MAP.get(reasoning_effort or "medium", "medium")

    # 获取 console 实际模型名
    console_model = CONSOLE_MODELS.get(model, model)
    resolved_max_output_tokens = (
        max_output_tokens
        if max_output_tokens is not None
        else _MODEL_MAX_OUTPUT_TOKENS.get(console_model, 1_000_000)
    )

    payload: dict[str, Any] = {
        "model": console_model,
        "input": input_items,
        "max_output_tokens": resolved_max_output_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "stream": stream,
    }

    # 只有 grok-4.3 需要附带 reasoning 字段，grok-4.20 系列不需要
    if console_model in _MODELS_WITH_REASONING_FIELD:
        payload["reasoning"] = {"effort": effort}

    # 为 multi-agent 和支持搜索的模型添加 tools
    if console_model in _MODELS_WITH_SEARCH_TOOLS:
        payload["tools"] = [
            {"type": "web_search", "enable_image_understanding": True},
            {"type": "x_search", "enable_video_understanding": True},
        ]
        payload["tool_choice"] = "auto"

    logger.debug(
        "console payload built: model={} console_model={} input_items={} has_reasoning={}",
        model, console_model, len(input_items), console_model in _MODELS_WITH_REASONING_FIELD,
    )
    return payload


# ---------------------------------------------------------------------------
# SSE stream adapter
# ---------------------------------------------------------------------------

class ConsoleStreamAdapter:
    """Parse console.x.ai SSE events and yield text tokens.

    只关心 response.output_text.delta 事件，其余忽略。
    response.completed 事件用于提取 usage 统计。
    """

    __slots__ = ("text_buf", "usage", "_done")

    def __init__(self) -> None:
        self.text_buf: list[str] = []
        self.usage: dict[str, Any] | None = None
        self._done = False

    def feed(self, event_type: str, data: str) -> list[str]:
        """解析一个 SSE 事件，返回文本 token 列表（通常 0 或 1 个）。"""
        if self._done:
            return []

        try:
            obj = orjson.loads(data)
        except (orjson.JSONDecodeError, ValueError):
            return []

        if event_type == "response.output_text.delta":
            delta = obj.get("delta", "")
            if delta:
                self.text_buf.append(delta)
                return [delta]

        elif event_type == "response.completed":
            resp = obj.get("response", {})
            self.usage = resp.get("usage")
            self._done = True

        elif event_type in {"error", "response.failed", "response.incomplete"}:
            response = obj.get("response")
            error = response.get("error") if isinstance(response, dict) else None
            if not isinstance(error, dict):
                error = obj.get("error")
            msg = (
                error.get("message") if isinstance(error, dict) else None
            ) or obj.get("message") or str(obj)
            self._done = True
            raise UpstreamError(f"Console API error: {msg}", status=502)

        return []

    @property
    def full_text(self) -> str:
        return "".join(self.text_buf)


def classify_console_line(line: str) -> tuple[str, str]:
    """Parse a raw SSE line into (event_type, data).

    console.x.ai 使用标准 SSE 格式:
        event: response.output_text.delta
        data: {...}
    """
    line = line.strip()
    if not line:
        return "skip", ""
    if line.startswith("event:"):
        return "event", line[6:].strip()
    if line.startswith("data:"):
        data = line[5:].strip()
        if data == "[DONE]":
            return "done", ""
        return "data", data
    return "skip", ""


async def stream_console_chat(
    token: str,
    payload: dict[str, Any],
    *,
    timeout_s: float = 120.0,
) -> AsyncGenerator[tuple[str, str], None]:
    """POST to console.x.ai/v1/responses and yield (event_type, data) pairs.

    走现有的 proxy lease + curl-cffi 体系，与 grok.com 共用 CF clearance。
    """
    from app.dataplane.proxy import get_proxy_runtime
    from app.dataplane.proxy.adapters.headers import build_console_headers
    from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs
    from app.dataplane.reverse.runtime.endpoint_table import CONSOLE_RESPONSES

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()

    headers = build_console_headers(token, lease=lease)
    payload_bytes = orjson.dumps(payload)
    session_kwargs = build_session_kwargs(lease=lease)

    async with ResettableSession(**session_kwargs) as session:
        try:
            response = await session.post(
                CONSOLE_RESPONSES,
                headers=headers,
                data=payload_bytes,
                timeout=timeout_s,
                stream=True,
            )
        except Exception as exc:
            await proxy.feedback(lease, _transport_error_feedback())
            raise UpstreamError(f"Console transport failed: {exc}", status=502) from exc

        if response.status_code != 200:
            try:
                body = response.content.decode("utf-8", "replace")[:400]
            except Exception:
                body = ""
            await proxy.feedback(lease, _status_feedback(response.status_code))
            raise UpstreamError(
                f"Console API returned {response.status_code}",
                status=response.status_code,
                body=body,
            )

        await proxy.feedback(lease, _success_feedback())

        current_event = ""
        try:
            async for raw_line in response.aiter_lines():
                # curl-cffi 的 aiter_lines 返回 bytes，先解码为 str
                if isinstance(raw_line, bytes):
                    try:
                        raw_line = raw_line.decode("utf-8")
                    except UnicodeDecodeError:
                        raw_line = raw_line.decode("utf-8", errors="replace")
                kind, value = classify_console_line(raw_line)
                if kind == "event":
                    current_event = value
                elif kind == "data":
                    yield current_event, value
                    current_event = ""
                elif kind == "done":
                    return
        except Exception as exc:
            raise UpstreamError(f"Console stream read failed: {exc}", status=502) from exc


def _success_feedback():
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    return ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200)

def _transport_error_feedback():
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    return ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR)

def _status_feedback(status: int):
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    if status == 403:
        kind = ProxyFeedbackKind.CHALLENGE
    elif status == 429:
        kind = ProxyFeedbackKind.RATE_LIMITED
    elif status >= 500:
        kind = ProxyFeedbackKind.UPSTREAM_5XX
    else:
        kind = ProxyFeedbackKind.FORBIDDEN
    return ProxyFeedback(kind=kind, status_code=status)


__all__ = [
    "CONSOLE_MODELS",
    "validate_console_request",
    "build_console_payload",
    "ConsoleStreamAdapter",
    "classify_console_line",
    "stream_console_chat",
]
