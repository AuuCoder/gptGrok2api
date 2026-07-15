from __future__ import annotations

import base64
from typing import Any, Callable, TypeVar

import orjson
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError as PydanticValidationError


ModelT = TypeVar("ModelT", bound=BaseModel)
AccountSelectedCallback = Callable[[dict[str, str]], None]
_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive"}


def _validate_payload(schema: type[ModelT], payload: dict[str, Any]) -> ModelT:
    try:
        return schema.model_validate(payload)
    except PydanticValidationError as exc:
        raise RequestValidationError(exc.errors(), body=payload) from exc


def create_router() -> APIRouter:
    from app.products.anthropic.router import router as anthropic_router
    from app.products.openai.router import router as openai_router

    router = APIRouter(prefix="/grok")
    router.include_router(openai_router)
    router.include_router(anthropic_router)

    @router.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "runtime": "embedded"}

    return router


def model_spec(model: object):
    from app.control.model import registry as model_registry

    return model_registry.get(str(model or "").strip())


def is_grok_model(model: object) -> bool:
    """Whether a model belongs to the embedded grok.com SSO runtime."""
    return model_spec(model) is not None


def is_xai_cli_oauth_model(model: object) -> bool:
    """Whether an id is reserved for the separate xAI CLI OAuth provider.

    This ownership check is deliberately independent of account availability:
    callers must return a useful "no OAuth account" error rather than falling
    through to the ChatGPT backend when a user explicitly selected Grok 4.5.
    """
    from services.xai_cli_oauth_protocol import GROK_45_MODEL_ID

    return str(model or "").strip() == GROK_45_MODEL_ID


def is_grok_text_model(model: object) -> bool:
    return is_grok_model(model) or is_xai_cli_oauth_model(model)


async def _safe_cli_stream(stream):
    """Keep OAuth provider exceptions inside an already-open SSE response."""
    try:
        async for chunk in stream:
            yield chunk
    except Exception as exc:
        payload = {"error": {"message": str(exc), "type": "server_error"}}
        yield f"event: error\ndata: {orjson.dumps(payload).decode()}\n\n"
        yield "data: [DONE]\n\n"


def _cli_result_response(result):
    if isinstance(result, dict):
        return JSONResponse(result)
    return StreamingResponse(
        _safe_cli_stream(result),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


async def dispatch_chat_completion(
    payload: dict[str, Any],
    *,
    on_account_selected: AccountSelectedCallback | None = None,
):
    if is_xai_cli_oauth_model(payload.get("model")):
        from services.xai_cli_oauth_service import xai_cli_oauth_service

        result = await xai_cli_oauth_service.create_chat_completion(
            model=str(payload.get("model") or ""),
            messages=list(payload.get("messages") or []),
            stream=bool(payload.get("stream")),
            temperature=payload.get("temperature"),
            top_p=payload.get("top_p"),
            max_tokens=payload.get("max_tokens"),
            tools=payload.get("tools") if isinstance(payload.get("tools"), list) else None,
            tool_choice=payload.get("tool_choice"),
            on_account_selected=on_account_selected,
        )
        return _cli_result_response(result)
    from app.products.openai.router import chat_completions_endpoint
    from app.products.openai.schemas import ChatCompletionRequest

    body = dict(payload)
    if not body.get("messages") and body.get("prompt"):
        body["messages"] = [{"role": "user", "content": str(body["prompt"])}]
    return await chat_completions_endpoint(_validate_payload(ChatCompletionRequest, body))


async def dispatch_response(
    payload: dict[str, Any],
    *,
    on_account_selected: AccountSelectedCallback | None = None,
):
    if is_xai_cli_oauth_model(payload.get("model")):
        from services.xai_cli_oauth_service import xai_cli_oauth_service

        request = dict(payload)
        request["stream"] = bool(payload.get("stream"))
        return _cli_result_response(
            await xai_cli_oauth_service.create_response(request, on_account_selected=on_account_selected)
        )
    from app.products.openai.router import responses_endpoint
    from app.products.openai.schemas import ResponsesCreateRequest

    return await responses_endpoint(_validate_payload(ResponsesCreateRequest, payload))


async def dispatch_image_generation(payload: dict[str, Any]):
    from app.products.openai.router import image_generations
    from app.products.openai.schemas import ImageGenerationRequest

    return await image_generations(_validate_payload(ImageGenerationRequest, payload))


async def dispatch_anthropic_message(
    payload: dict[str, Any],
    *,
    on_account_selected: AccountSelectedCallback | None = None,
):
    if is_xai_cli_oauth_model(payload.get("model")):
        from services.xai_cli_oauth_service import xai_cli_oauth_service

        result = await xai_cli_oauth_service.create_anthropic_message(
            model=str(payload.get("model") or ""),
            messages=list(payload.get("messages") or []),
            system=payload.get("system"),
            stream=bool(payload.get("stream")),
            temperature=payload.get("temperature"),
            top_p=payload.get("top_p"),
            max_tokens=payload.get("max_tokens"),
            tools=payload.get("tools") if isinstance(payload.get("tools"), list) else None,
            tool_choice=payload.get("tool_choice"),
            on_account_selected=on_account_selected,
        )
        return _cli_result_response(result)
    from app.products.anthropic.router import MessagesRequest, messages_endpoint

    return await messages_endpoint(_validate_payload(MessagesRequest, payload))


async def dispatch_image_edit(payload: dict[str, Any]):
    from app.products.openai.images import edit
    from app.products.openai.router import _SSE_HEADERS, _safe_sse

    prompt = str(payload.get("prompt") or "").strip()
    images = payload.get("images") if isinstance(payload.get("images"), list) else []
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for item in images:
        if not isinstance(item, tuple) or len(item) < 3:
            continue
        raw, _, mime = item
        data_url = f"data:{mime or 'image/png'};base64,{base64.b64encode(raw).decode('ascii')}"
        content.append({"type": "image_url", "image_url": {"url": data_url}})

    result = await edit(
        model=str(payload.get("model") or "grok-imagine-image-edit"),
        messages=[{"role": "user", "content": content}],
        n=int(payload.get("n") or 1),
        size=str(payload.get("size") or "1024x1024"),
        response_format=str(payload.get("response_format") or "url"),
        stream=bool(payload.get("stream")),
        chat_format=False,
    )
    if isinstance(result, dict):
        return JSONResponse(result)
    return StreamingResponse(
        _safe_sse(result),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


async def dispatch_model_detail(model_id: str, request: Request):
    """Serve one registered Grok model through the host ``/v1`` surface."""
    if is_xai_cli_oauth_model(model_id):
        from services.xai_cli_oauth_service import xai_cli_oauth_service

        for item in xai_cli_oauth_service.model_items():
            if item.get("id") == model_id:
                return JSONResponse(item)
        return JSONResponse(
            {
                "error": {
                    "message": f"Model {model_id!r} is unavailable because no verified xAI CLI OAuth account exists.",
                    "type": "invalid_request_error",
                    "code": "model_not_found",
                    "param": "model",
                }
            },
            status_code=404,
        )
    from app.products.openai.router import get_model_endpoint

    return await get_model_endpoint(model_id, request)


async def dispatch_video_create(
    *,
    model: str,
    prompt: str,
    seconds: int = 6,
    size: str = "720x1280",
    resolution_name: str | None = None,
    preset: str | None = None,
    input_reference: list[Any] | None = None,
):
    """Delegate the root multipart video endpoint to Grok2API's handler."""
    from app.products.openai.router import videos_create

    return await videos_create(
        model=model,
        prompt=prompt,
        seconds=seconds,
        size=size,
        resolution_name=resolution_name,
        preset=preset,
        input_reference=input_reference,
    )


async def dispatch_video_retrieve(video_id: str):
    from app.products.openai.router import videos_retrieve

    return await videos_retrieve(video_id)


async def dispatch_video_content(video_id: str):
    from app.products.openai.router import videos_content

    return await videos_content(video_id)


async def dispatch_video_file(file_id: str):
    from app.products.openai.router import serve_video

    return await serve_video(id=file_id)


async def dispatch_image_file(file_id: str):
    from app.products.openai.router import serve_image

    return await serve_image(id=file_id)


def install_exception_handlers(app: FastAPI) -> None:
    from app.platform.errors import AppError
    from api.errors import _compatible_error_response

    @app.exception_handler(AppError)
    async def grok_app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return _compatible_error_response(request, exc.to_dict(), exc.status)


__all__ = [
    "create_router",
    "dispatch_anthropic_message",
    "dispatch_chat_completion",
    "dispatch_image_file",
    "dispatch_image_edit",
    "dispatch_image_generation",
    "dispatch_model_detail",
    "dispatch_response",
    "dispatch_video_content",
    "dispatch_video_create",
    "dispatch_video_file",
    "dispatch_video_retrieve",
    "install_exception_handlers",
    "is_grok_model",
    "is_grok_text_model",
    "is_xai_cli_oauth_model",
    "model_spec",
]
