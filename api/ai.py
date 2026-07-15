from __future__ import annotations

import time
from typing import Annotated, Literal

import orjson
from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api import grok
from api.image_inputs import parse_image_edit_request, read_image_sources
from api.support import require_identity, resolve_image_base_url
from app.platform.request_context import reset_grok_log_trace, set_grok_log_trace
from services.content_filter import check_request, request_shape, request_text
from services.editable_file_task_service import editable_file_task_service
from services.log_service import LoggedCall
from services.realtime_monitor_service import realtime_monitor_service
from services.register.grok_account_store import grok_account_store
from services.protocol import (
    anthropic_v1_messages,
    openai_v1_chat_complete,
    openai_v1_image_edit,
    openai_v1_image_generations,
    openai_v1_models,
    openai_v1_response,
    openai_search,
)
from utils.helper import is_image_chat_request


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: int = Field(default=1, ge=1, le=10)
    size: str | None = None
    quality: str = "auto"
    response_format: str = "url"
    history_disabled: bool = True
    stream: bool | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    prompt: str | None = None
    n: int | None = None
    stream: bool | None = None
    modalities: list[str] | None = None
    messages: list[dict[str, object]] | None = None


class ResponseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    input: object | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: object | None = None
    stream: bool | None = None


class AnthropicMessageRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    messages: list[dict[str, object]] | None = None
    system: object | None = None
    stream: bool | None = None


class SearchRequest(BaseModel):
    prompt: str = Field(..., min_length=1)


class EditableFileTaskRequest(BaseModel):
    prompt: str = ""
    kind: str = "ppt"
    base64_images: list[str] = Field(default_factory=list)
    client_task_id: str | None = None


TRACE_REQUEST_HEADERS = {
    "x-request-id": "x_request_id",
    "x-newapi-request-id": "x_newapi_request_id",
    "x-oneapi-request-id": "x_oneapi_request_id",
    "x-channel-id": "x_channel_id",
    "x-channel-name": "x_channel_name",
}


def attach_trace_headers(call: LoggedCall, request: Request) -> None:
    if not call._trace_image_perf():
        return
    headers: dict[str, str] = {}
    for header, field in TRACE_REQUEST_HEADERS.items():
        value = str(request.headers.get(header) or "").strip()
        if value:
            headers[field] = value[:160]
    if headers:
        existing = call.trace_metadata.get("request_headers")
        if isinstance(existing, dict):
            existing.update(headers)
        else:
            call.trace_metadata["request_headers"] = headers


async def filter_or_log(call: LoggedCall, text: str) -> None:
    try:
        await run_in_threadpool(check_request, text)
    except HTTPException as exc:
        call.log("调用失败", status="failed", error=str(exc.detail))
        raise


def _grok_stream_chunk_text(chunk: object) -> str:
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    return str(chunk or "")


def _grok_stream_error_event(text: str) -> str:
    return "Grok 上游返回流式错误事件" if any(
        line.strip().lower() == "event: error"
        for line in text.splitlines()
    ) else ""


def _grok_account_log_kwargs(account: dict[str, str]) -> dict[str, object]:
    email = str(account.get("account_email") or "").strip()
    account_id = str(account.get("account_id") or "").strip()
    if not email and not account_id:
        return {}
    extra: dict[str, object] = {"provider": "xai_cli_oauth"}
    if account_id:
        extra["provider_account_id"] = account_id
    return {"account_email": email, "extra": extra}


def _grok_response_payload(response: object) -> object | None:
    if not isinstance(response, JSONResponse):
        return None
    body = getattr(response, "body", b"")
    if not body:
        return None
    try:
        return orjson.loads(body)
    except orjson.JSONDecodeError:
        return None


def _grok_response_error(payload: object, status_code: int) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("detail") or "").strip()
            if message:
                return message
        if isinstance(error, str) and error.strip():
            return error.strip()
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    return f"HTTP {status_code}"


def _grok_start_monitor(call: LoggedCall) -> None:
    if not call._trace_image_perf():
        return
    realtime_monitor_service.start(
        call.call_id,
        endpoint=call.endpoint,
        model=call.model,
        summary=call.summary,
        role=str(call.identity.get("role") or ""),
        key_name=str(call.identity.get("name") or ""),
    )
    realtime_monitor_service.stage(
        call.call_id,
        "handler_started",
        endpoint=call.endpoint,
        model=call.model,
    )


def _grok_apply_trace(call: LoggedCall, trace: dict[str, object]) -> None:
    metrics = trace.get("metrics")
    if isinstance(metrics, dict):
        for key, value in metrics.items():
            if not str(key).endswith("_ms"):
                continue
            try:
                value_ms = max(0, int(value or 0))
            except (TypeError, ValueError):
                continue
            if value_ms:
                call.perf_timings[str(key)] = max(
                    int(call.perf_timings.get(str(key)) or 0),
                    value_ms,
                )

    if not call._trace_image_perf():
        return
    events = trace.get("events")
    if not isinstance(events, list):
        return
    flushed = int(trace.get("_flushed_events") or 0)
    for item in events[flushed:]:
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "").strip()
        if not event:
            continue
        realtime_monitor_service.stage(
            call.call_id,
            event,
            **{
                str(key): value
                for key, value in item.items()
                if key != "event" and value not in (None, "")
            },
        )
    trace["_flushed_events"] = len(events)


def _grok_dispatch_log_kwargs(
    call: LoggedCall,
    selected_account: dict[str, str],
    trace: dict[str, object],
) -> dict[str, object]:
    _grok_apply_trace(call, trace)
    if selected_account:
        kwargs = _grok_account_log_kwargs(selected_account)
    else:
        identity = grok_account_store.runtime_identity_for_token(
            str(trace.get("account_token") or "")
        )
        kwargs = {}
        email = str(identity.get("account_email") or "").strip()
        account_id = str(identity.get("account_id") or "").strip()
        if email:
            kwargs["account_email"] = email
        if email or account_id:
            kwargs["extra"] = {"provider": "grok_sso"}
            if account_id:
                kwargs["extra"]["provider_account_id"] = account_id

    extra = dict(kwargs.get("extra") or {})
    proxy = trace.get("proxy")
    if isinstance(proxy, dict):
        for key in (
            "proxy_source",
            "proxy_hash",
            "has_proxy",
            "egress_mode",
            "egress_key",
            "egress_label",
            "proxy_scope",
            "proxy_kind",
        ):
            if key in proxy and proxy[key] not in (None, ""):
                extra[key] = proxy[key]
    if extra:
        kwargs["extra"] = extra
    return kwargs


def _grok_record_elapsed(call: LoggedCall, key: str, started: float) -> None:
    call.perf_timings[key] = max(
        int(call.perf_timings.get(key) or 0),
        max(1, int((time.perf_counter() - started) * 1000)),
    )


async def _run_logged_grok_dispatch(call: LoggedCall, dispatch):
    """Log Grok dispatches without changing their OpenAI/Anthropic response body."""
    selected_account: dict[str, str] = {}
    trace: dict[str, object] = {}
    trace_token = set_grok_log_trace(trace)
    dispatch_started = time.perf_counter()
    _grok_start_monitor(call)

    def on_account_selected(account: dict[str, str]) -> None:
        selected_account.clear()
        selected_account.update(
            {
                key: str(account.get(key) or "").strip()
                for key in ("account_id", "account_email")
                if str(account.get(key) or "").strip()
            }
        )

    def log(suffix: str, result: object = None, *, status: str = "success", error: str = "") -> None:
        kwargs = _grok_dispatch_log_kwargs(call, selected_account, trace)
        if status != "success":
            kwargs["status"] = status
        if error:
            kwargs["error"] = error
        if result is None:
            call.log(suffix, **kwargs)
        else:
            call.log(suffix, result, **kwargs)

    try:
        response = await dispatch(on_account_selected)
    except HTTPException as exc:
        _grok_record_elapsed(call, "conversation_stream_ms", dispatch_started)
        try:
            log("调用失败", status="failed", error=str(exc.detail))
        finally:
            reset_grok_log_trace(trace_token)
        raise
    except Exception as exc:
        _grok_record_elapsed(call, "conversation_stream_ms", dispatch_started)
        try:
            log("调用失败", status="failed", error=str(exc))
        finally:
            reset_grok_log_trace(trace_token)
        raise

    if isinstance(response, dict):
        _grok_record_elapsed(call, "conversation_stream_ms", dispatch_started)
        try:
            log("调用完成", response)
        finally:
            reset_grok_log_trace(trace_token)
        return response

    status_code = int(getattr(response, "status_code", 200) or 200)
    response_payload = _grok_response_payload(response)
    if status_code >= 400:
        _grok_record_elapsed(call, "conversation_stream_ms", dispatch_started)
        try:
            log(
                "调用失败",
                response_payload,
                status="failed",
                error=_grok_response_error(response_payload, status_code),
            )
        finally:
            reset_grok_log_trace(trace_token)
        return response

    if not isinstance(response, StreamingResponse):
        _grok_record_elapsed(call, "conversation_stream_ms", dispatch_started)
        try:
            log("调用完成", response_payload)
        finally:
            reset_grok_log_trace(trace_token)
        return response

    original_iterator = response.body_iterator
    _grok_record_elapsed(call, "generation_start_ms", dispatch_started)
    reset_grok_log_trace(trace_token)

    async def logged_stream():
        stream_error = ""
        raised_error = ""
        stream_probe = ""
        stream_started = time.perf_counter()
        stream_trace_token = set_grok_log_trace(trace)
        try:
            async for chunk in original_iterator:
                if not stream_error:
                    stream_probe = (stream_probe + _grok_stream_chunk_text(chunk))[-512:]
                    stream_error = _grok_stream_error_event(stream_probe)
                yield chunk
        except Exception as exc:
            raised_error = str(exc) or type(exc).__name__
            raise
        finally:
            _grok_record_elapsed(call, "conversation_stream_ms", stream_started)
            try:
                if raised_error or stream_error:
                    log("流式调用失败", status="failed", error=raised_error or stream_error)
                else:
                    log("流式调用结束")
            finally:
                reset_grok_log_trace(stream_trace_token)

    response.body_iterator = logged_stream()
    return response


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_models(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        try:
            return await run_in_threadpool(openai_v1_models.list_models)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    @router.get("/v1/models/{model_id}")
    async def get_model(
            model_id: str,
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        require_identity(authorization)
        if grok.is_grok_text_model(model_id):
            return await grok.dispatch_model_detail(model_id, request)

        # Keep model detail discovery consistent with the root /v1/models
        # aggregate.  This covers locally advertised GPT fallback models as
        # well as models discovered from the authenticated upstream catalog.
        try:
            catalog = await run_in_threadpool(openai_v1_models.list_models)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

        items = catalog.get("data") if isinstance(catalog, dict) else None
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and str(item.get("id") or "") == model_id:
                    return item
        raise HTTPException(status_code=404, detail="Not Found")

    @router.post("/v1/videos")
    async def create_video(
            model: Annotated[str, Form(...)],
            prompt: Annotated[str, Form(...)],
            seconds: Annotated[int, Form()] = 6,
            size: Annotated[
                Literal["720x1280", "1280x720", "1024x1024", "1024x1792", "1792x1024"], Form()
            ] = "720x1280",
            resolution_name: Annotated[Literal["480p", "720p"] | None, Form()] = None,
            preset: Annotated[Literal["fun", "normal", "spicy", "custom"] | None, Form()] = None,
            input_reference: Annotated[list[UploadFile] | None, File(alias="input_reference[]")] = None,
            authorization: str | None = Header(default=None),
    ):
        require_identity(authorization)
        return await grok.dispatch_video_create(
            model=model,
            prompt=prompt,
            seconds=seconds,
            size=size,
            resolution_name=resolution_name,
            preset=preset,
            input_reference=input_reference,
        )

    @router.get("/v1/videos/{video_id}/content")
    async def get_video_content(
            video_id: str,
            authorization: str | None = Header(default=None),
    ):
        require_identity(authorization)
        return await grok.dispatch_video_content(video_id)

    @router.get("/v1/videos/{video_id}")
    async def get_video(
            video_id: str,
            authorization: str | None = Header(default=None),
    ):
        require_identity(authorization)
        return await grok.dispatch_video_retrieve(video_id)

    @router.get("/v1/files/image")
    async def get_grok_image(id: str):
        # Match Grok2API's local-media URLs: browsers fetch generated results
        # directly and therefore cannot attach the caller's API key.
        return await grok.dispatch_image_file(id)

    @router.get("/v1/files/video")
    async def get_grok_video(id: str):
        # See /v1/files/image above. File IDs are random local cache IDs.
        return await grok.dispatch_video_file(id)

    @router.post("/v1/images/generations")
    async def generate_images(
            body: ImageGenerationRequest,
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        payload["base_url"] = resolve_image_base_url(request)
        call = LoggedCall(identity, "/v1/images/generations", body.model, "文生图", request_text=body.prompt)
        attach_trace_headers(call, request)
        call.attach_trace_metadata(payload)
        await filter_or_log(call, body.prompt)
        if grok.is_grok_model(body.model):
            call.summary = f"Grok {call.summary}"
            return await _run_logged_grok_dispatch(call, lambda _on_account_selected: grok.dispatch_image_generation(payload))
        return await call.run(openai_v1_image_generations.handle, payload)

    @router.post("/v1/images/edits")
    async def edit_images(
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload, image_sources, mask_sources = await parse_image_edit_request(request)
        prompt = str(payload["prompt"])
        model = str(payload["model"])
        model_spec = grok.model_spec(model)
        grok_edit = bool(model_spec and model_spec.is_image_edit())
        if model_spec is not None and not grok_edit:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": "Grok image edits require model 'grok-imagine-image-edit'",
                        "type": "invalid_request_error",
                        "param": "model",
                        "code": "invalid_value",
                    }
                },
            )
        if grok_edit and mask_sources:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": "mask is not supported yet",
                        "type": "invalid_request_error",
                        "param": "mask",
                        "code": "invalid_value",
                    }
                },
            )
        call = LoggedCall(identity, "/v1/images/edits", model, "图生图", request_text=prompt)
        attach_trace_headers(call, request)
        call.attach_trace_metadata(payload)
        await filter_or_log(call, prompt)
        payload["images"] = await read_image_sources(image_sources)
        payload["mask"] = await read_image_sources(mask_sources)
        payload["base_url"] = resolve_image_base_url(request)
        if grok_edit:
            call.summary = f"Grok {call.summary}"
            return await _run_logged_grok_dispatch(call, lambda _on_account_selected: grok.dispatch_image_edit(payload))
        return await call.run(openai_v1_image_edit.handle, payload)

    @router.post("/v1/chat/completions")
    async def create_chat_completion(body: ChatCompletionRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        payload["base_url"] = resolve_image_base_url(request)
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("prompt"), payload.get("messages"))
        image_chat = is_image_chat_request(payload)
        call = LoggedCall(
            identity,
            "/v1/chat/completions",
            model,
            "聊天生图" if image_chat else "文本生成",
            request_text=request_preview,
            request_shape=request_shape(payload.get("messages")),
        )
        attach_trace_headers(call, request)
        call.attach_trace_metadata(payload)
        await filter_or_log(call, request_preview)
        if grok.is_grok_text_model(model):
            call.summary = f"Grok {call.summary}"
            return await _run_logged_grok_dispatch(
                call,
                lambda on_account_selected: grok.dispatch_chat_completion(
                    payload,
                    on_account_selected=on_account_selected,
                ),
            )
        return await call.run(openai_v1_chat_complete.handle, payload)

    @router.post("/v1/responses")
    async def create_response(body: ResponseCreateRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("input"), payload.get("instructions"))
        call = LoggedCall(
            identity,
            "/v1/responses",
            model,
            "Responses",
            request_text=request_preview,
            request_shape=request_shape(payload.get("input")),
        )
        call.attach_trace_metadata(payload)
        await filter_or_log(call, request_preview)
        if grok.is_grok_text_model(model):
            call.summary = f"Grok {call.summary}"
            return await _run_logged_grok_dispatch(
                call,
                lambda on_account_selected: grok.dispatch_response(
                    payload,
                    on_account_selected=on_account_selected,
                ),
            )
        return await call.run(openai_v1_response.handle, payload)

    @router.post("/v1/messages")
    async def create_message(
            body: AnthropicMessageRequest,
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="x-api-key"),
            anthropic_version: str | None = Header(default=None, alias="anthropic-version"),
    ):
        identity = require_identity(authorization or (f"Bearer {x_api_key}" if x_api_key else None))
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        request_preview = request_text(payload.get("system"), payload.get("messages"), payload.get("tools"))
        call = LoggedCall(identity, "/v1/messages", model, "Messages", request_text=request_preview)
        await filter_or_log(call, request_preview)
        if grok.is_grok_text_model(model):
            call.summary = f"Grok {call.summary}"
            return await _run_logged_grok_dispatch(
                call,
                lambda on_account_selected: grok.dispatch_anthropic_message(
                    payload,
                    on_account_selected=on_account_selected,
                ),
            )
        return await call.run(anthropic_v1_messages.handle, payload, sse="anthropic")

    @router.post("/v1/search")
    async def search(body: SearchRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        call = LoggedCall(identity, "/v1/search", openai_search.MODEL, "搜索", request_text=body.prompt)
        await filter_or_log(call, body.prompt)
        return await call.run(openai_search.handle, body.model_dump(mode="python"))

    @router.get("/v1/editable-file-tasks")
    async def list_editable_file_tasks(ids: str = "", authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        task_ids = [item.strip() for item in ids.split(",") if item.strip()]
        return await run_in_threadpool(editable_file_task_service.list_tasks, identity, task_ids)

    @router.post("/v1/editable-file-tasks")
    async def create_editable_file_task(body: EditableFileTaskRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        kind = (body.kind or "ppt").strip().lower()
        if kind not in {"ppt", "psd"}:
            raise HTTPException(status_code=400, detail={"error": "kind must be ppt or psd"})
        endpoint = f"/v1/{kind}/generations"
        await filter_or_log(
            LoggedCall(identity, endpoint, "gpt-5-5-thinking", f"{kind.upper()} generation task", request_text=body.prompt),
            body.prompt,
        )
        submit = editable_file_task_service.submit_psd if kind == "psd" else editable_file_task_service.submit_ppt
        return await run_in_threadpool(
            submit,
            identity,
            client_task_id=body.client_task_id or "",
            prompt=body.prompt,
            base64_images=body.base64_images,
            base_url=resolve_image_base_url(request),
        )

    @router.get("/files/{file_path:path}")
    async def download_editable_file(file_path: str):
        try:
            path = await run_in_threadpool(editable_file_task_service.public_file_path, file_path)
        except Exception as exc:
            raise HTTPException(status_code=404, detail={"error": "file not found"}) from exc
        return FileResponse(path, filename=path.name)

    @router.post("/v1/ppt/generations")
    async def create_ppt_task(body: EditableFileTaskRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        await filter_or_log(LoggedCall(identity, "/v1/ppt/generations", "gpt-5-5-thinking", "PPT生成任务", request_text=body.prompt), body.prompt)
        return await run_in_threadpool(
            editable_file_task_service.submit_ppt,
            identity,
            client_task_id=body.client_task_id or "",
            prompt=body.prompt,
            base64_images=body.base64_images,
            base_url=resolve_image_base_url(request),
        )

    @router.post("/v1/psd/generations")
    async def create_psd_task(body: EditableFileTaskRequest, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        await filter_or_log(LoggedCall(identity, "/v1/psd/generations", "gpt-5-5-thinking", "PSD生成任务", request_text=body.prompt), body.prompt)
        return await run_in_threadpool(
            editable_file_task_service.submit_psd,
            identity,
            client_task_id=body.client_task_id or "",
            prompt=body.prompt,
            base64_images=body.base64_images,
            base_url=resolve_image_base_url(request),
        )

    return router
