"""Anthropic Messages API router (/v1/messages)."""

from typing import Any

import orjson
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.platform.auth.middleware import verify_api_key
from app.platform.errors import AppError, ValidationError
from app.platform.logging.logger import logger
from app.control.model import registry as model_registry


router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])
_TAG_MESSAGES = "Anthropic - Messages"

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class _ContentBlock(BaseModel):
    model_config = {"extra": "allow"}
    type: str = "text"


class _Message(BaseModel):
    model_config = {"extra": "allow"}
    role:    str
    content: Any = ""


class MessagesRequest(BaseModel):
    model_config = {"extra": "ignore"}

    model:       str
    messages:    list[_Message]
    system:      Any = None          # string or array of content blocks
    # Console models forward this to console.x.ai as max_output_tokens; the
    # grok.com text transport still has no upstream equivalent.
    max_tokens:  int | None = None
    stream:      bool | None = None
    temperature: float | None = None
    top_p:       float | None = None
    tools:       list[dict] | None = None
    tool_choice: Any = None
    thinking:    Any = None          # {type:"enabled", budget_tokens:N} — used to enable thinking output
    # Non-standard compatibility extensions for Grok Imagine models. Anthropic
    # has no native image/video generation object, so the response remains a
    # standard text block containing the generated URL or video task details.
    image_config: dict[str, Any] | None = None
    video_config: dict[str, Any] | None = None
    n: int | None = Field(default=None, ge=1, le=10)
    size: str | None = None
    response_format: str | None = None
    seconds: int | None = None
    resolution_name: str | None = None
    preset: str | None = None


# ---------------------------------------------------------------------------
# SSE error wrapper
# ---------------------------------------------------------------------------

async def _safe_sse_anthropic(stream):
    """Wrap an Anthropic SSE stream, converting exceptions to error events."""
    try:
        async for chunk in stream:
            yield chunk
    except AppError as exc:
        err = exc.to_dict()["error"]
        payload = orjson.dumps({"type": "error", "error": err}).decode()
        yield f"event: error\ndata: {payload}\n\n"
    except Exception as exc:
        payload = orjson.dumps({
            "type": "error",
            "error": {"type": "api_error", "message": str(exc)},
        }).decode()
        yield f"event: error\ndata: {payload}\n\n"


# ---------------------------------------------------------------------------
# /v1/messages
# ---------------------------------------------------------------------------

@router.post("/messages", tags=[_TAG_MESSAGES])
async def messages_endpoint(req: MessagesRequest):
    from app.platform.config.snapshot import get_config

    # Model validation
    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled:
        raise ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model", code="model_not_found",
        )

    if not req.messages:
        raise ValidationError("messages cannot be empty", param="messages")

    cfg       = get_config()
    is_stream = req.stream if req.stream is not None else cfg.get_bool("features.stream", True)

    # thinking flag: enable when request has thinking config or config default
    if req.thinking is not None and isinstance(req.thinking, dict):
        emit_think = req.thinking.get("type") != "disabled"
    else:
        emit_think = cfg.get_bool("features.thinking", True)

    # Convert Pydantic models → plain dicts
    messages = [m.model_dump() for m in req.messages]

    image_options = {
        key: value
        for key, value in {
            "n": req.n,
            "size": req.size,
            "response_format": req.response_format,
        }.items()
        if value is not None
    }
    if req.image_config:
        image_options.update({key: value for key, value in req.image_config.items() if value is not None})
    video_options = {
        key: value
        for key, value in {
            "seconds": req.seconds,
            "resolution_name": req.resolution_name,
            "preset": req.preset,
        }.items()
        if value is not None
    }
    if req.size is not None:
        video_options["size"] = req.size
    if req.video_config:
        video_options.update({key: value for key, value in req.video_config.items() if value is not None})

    from .messages import create as messages_create
    result = await messages_create(
        model        = req.model,
        messages     = messages,
        system       = req.system,
        stream       = is_stream,
        emit_think   = emit_think,
        temperature  = req.temperature if req.temperature is not None else 0.8,
        top_p        = req.top_p if req.top_p is not None else 0.95,
        tools        = req.tools or None,
        tool_choice  = req.tool_choice,
        max_tokens   = req.max_tokens,
        image_options = image_options,
        video_options = video_options,
    )

    if isinstance(result, dict):
        return JSONResponse(result)
    return StreamingResponse(
        _safe_sse_anthropic(result),
        media_type = "text/event-stream",
        headers    = _SSE_HEADERS,
    )


__all__ = ["router"]
