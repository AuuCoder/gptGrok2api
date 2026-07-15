from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

import tiktoken

from services.account_service import ImageAccountSelectionError, account_service
from services.config import config
from services.image_storage_service import image_storage_service
from services.openai_backend_api import ImageContentPolicyError, ImagePollTimeoutError, ImageTextReplyError, OpenAIBackendAPI
from services.proxy_service import proxy_settings
from services.realtime_monitor_service import realtime_monitor_service
from services.request_cancel_service import RequestCancelledError, request_cancel_service
from utils.helper import (
    IMAGE_MODELS,
    extract_image_from_message_content,
    is_codex_image_model,
    is_supported_image_model,
    split_image_model,
)
from utils.image_tokens import count_image_content_tokens, image_size_from_bytes
from utils.log import logger
from utils.diagnostics import diagnostic_excerpt


class ImageGenerationError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 502,
        error_type: str = "server_error",
        code: str | None = "upstream_error",
        param: str | None = None,
        account_email: str = "",
        conversation_id: str = "",
        raw_error: str = "",
        upstream_error: str = "",
        raw_upstream_message: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.param = param
        self.account_email = account_email
        self.conversation_id = conversation_id
        self.raw_error = raw_error
        self.upstream_error = upstream_error
        self.raw_upstream_message = raw_upstream_message

    def to_openai_error(self) -> dict[str, Any]:
        error_dict = {
            "error": {
                "message": public_image_error_message(str(self), code=self.code),
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }
        if self.account_email:
            error_dict["error"]["account_email"] = self.account_email
        return error_dict


def public_image_error_message(message: str, code: str | None = None) -> str:
    text = str(message or "").strip()
    lower = text.lower()
    if not config.image_error_friendly_enabled:
        return _legacy_public_image_error_message(text)
    if code in {"upstream_text_reply", "no_image_generated"}:
        return _public_text_reply_message(text)
    if not text:
        return _friendly_image_error_message("fallback")
    selection_key = _image_account_selection_error_key(lower)
    if selection_key:
        return _friendly_image_error_message(selection_key)
    if _is_image_quota_error(lower):
        return _friendly_image_error_message("quota")
    if _is_local_image_busy_error(lower):
        return _friendly_image_error_message("local_busy")
    if "unsupported image model" in lower:
        return _friendly_image_error_message("unsupported_model")
    if _is_image_poll_timeout_error(lower):
        return _friendly_image_error_message("poll_timeout")
    if is_stream_transport_error(text):
        return _friendly_image_error_message("stream_interrupted")
    if is_connection_timeout_error(text):
        return _friendly_image_error_message("connection_timeout")
    if is_tls_connection_error(text):
        return _friendly_image_error_message("connection_failed")
    if _is_upstream_text_reply_error(text):
        return _public_text_reply_message(text)
    if any(item in lower for item in ("backend-api/", "status=", "body=", "chatgpt.com", "upstreamhttperror")):
        return _friendly_image_error_message("fallback")
    return text or _friendly_image_error_message("fallback")


def _legacy_public_image_error_message(message: str) -> str:
    text = str(message or "").strip()
    lower = text.lower()
    fallback = "The image generation request failed. Please try again later."
    if any(item in lower for item in ("backend-api/", "status=", "body=", "chatgpt.com", "upstreamhttperror")):
        return fallback
    return text or fallback


def _friendly_image_error_message(key: str, text: str = "") -> str:
    messages = config.get_image_error_messages()
    template = str(messages.get(key) or messages.get("fallback") or "图片生成请求失败，请稍后重试。").strip()
    if text:
        if "{text}" in template:
            return template.replace("{text}", text)
        return f"{template}\n文本如下：{text}"
    return template.replace("{text}", "").strip()


def _public_text_excerpt(message: str, limit: int = 260) -> str:
    text = str(message or "").strip()
    text = re.sub(r"(?i)^status(?:_code)?\s*=\s*\d+\s*[,，:：]?\s*", "", text)
    text = re.sub(r"(?i)^error\s*[:：]\s*", "", text)
    text = " ".join(text.split())
    lower = text.lower()
    if any(item in lower for item in ("backend-api/", "authorization", "access_token", "refresh_token", "cookie", "bearer ")):
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _public_text_reply_message(message: str) -> str:
    excerpt = _public_text_excerpt(message)
    return _friendly_image_error_message("text_reply", excerpt) if excerpt else _friendly_image_error_message("text_reply")


def _is_image_quota_error(lower: str) -> bool:
    return (
        "image_account_selection:quota_exhausted" in lower
        or "insufficient_quota" in lower
    )


def _image_account_selection_error_key(lower: str) -> str:
    if "image_account_selection:quota_exhausted" in lower:
        return "quota"
    if "image_account_selection:unavailable" in lower:
        return "no_account"
    return ""


def _is_local_image_busy_error(lower: str) -> bool:
    return (
        "no account in the pool" in lower
        or "no available image quota" in lower
        or "account concurrency" in lower
        or "server busy" in lower
        or "local busy" in lower
        or "rate-limit status" in lower
    )


def _is_image_poll_timeout_error(lower: str) -> bool:
    return (
        "生图超时" in lower
        or "imagepolltimeouterror" in lower
        or "image_poll_timeout" in lower
        or "poll timeout" in lower
        or "image_poll_timeout_secs" in lower
    )


def _is_upstream_text_reply_error(message: str) -> bool:
    lower = str(message or "").lower()
    return (
        is_model_text_reply_instead_of_image(message)
        or "upstream completed without generating images" in lower
        or "no image result found" in lower
        or "returned a text description" in lower
        or "content_policy_violation" in lower
        or "防护限制" in message
        or "违反" in message
        or "裸露" in message
        or "色情" in message
        or "情色" in message
    )


def _is_content_policy_image_message(message: str) -> bool:
    text = str(message or "")
    lower = text.lower()
    return (
        "content policy" in lower
        or "policy violation" in lower
        or "content_policy_violation" in lower
        or "safety" in lower and "policy" in lower
        or "防护限制" in text
        or "可能违反" in text
        or "违反" in text
        or "裸露" in text
        or "色情" in text
        or "情色" in text
    )


def _image_message_error_metadata(message: str) -> tuple[int, str, str]:
    if _is_content_policy_image_message(message):
        return 400, "invalid_request_error", "content_policy_violation"
    if is_model_text_reply_instead_of_image(message):
        return 400, "invalid_request_error", "upstream_text_reply"
    return 400, "invalid_request_error", "no_image_generated"


def _monitor_image_stage(request: "ConversationRequest", event: str, **data: Any) -> None:
    if request.trace_image_perf and request.call_id:
        realtime_monitor_service.stage(request.call_id, event, model=request.model, **data)


def _raise_if_request_cancelled(request: "ConversationRequest") -> None:
    if request.call_id:
        request_cancel_service.raise_if_cancelled(request.call_id)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _proxy_hash(proxy_url: object) -> str:
    value = str(proxy_url or "").strip()
    if not value:
        return "direct"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def _backend_egress_data(backend: OpenAIBackendAPI) -> dict[str, Any]:
    profile = getattr(backend, "proxy_profile", None)
    proxy_url = getattr(profile, "proxy_url", "") if profile else ""
    return {
        "proxy_source": str(getattr(profile, "proxy_source", "") or "direct"),
        "proxy_hash": _proxy_hash(proxy_url),
        "egress_key": str(getattr(profile, "egress_key", "") or "direct"),
        "egress_label": str(getattr(profile, "egress_label", "") or ""),
        "proxy_group_id": str(getattr(profile, "proxy_group_id", "") or ""),
        "proxy_node_id": str(getattr(profile, "proxy_node_id", "") or ""),
        "proxy_node_name": str(getattr(profile, "proxy_node_name", "") or ""),
        "image_egress_limit": int(getattr(profile, "image_concurrency_limit", 0) or 0),
        "has_proxy": bool(proxy_url),
        "egress_mode": str(getattr(profile, "egress_mode", "") or "direct"),
    }


def _backend_http_timing_data(backend: OpenAIBackendAPI | None, name: str = "image_generation_stream") -> dict[str, Any]:
    if backend is None or not hasattr(backend, "pop_http_timing"):
        return {}
    try:
        return backend.pop_http_timing(name)
    except Exception:
        return {}


_IMAGE_PROGRESS_STAGE_EVENTS = {
    "uploading": "image_uploading",
    "bootstrapping": "image_bootstrapping",
    "getting_token": "image_getting_token",
    "preparing_conversation": "image_preparing_conversation",
    "starting_generation": "image_starting_generation",
    "generating": "image_generating",
}

_IMAGE_PROGRESS_DURATION_KEYS = {
    "uploading": "upload_ms",
    "bootstrapping": "bootstrap_ms",
    "getting_token": "requirements_ms",
    "preparing_conversation": "prepare_conversation_ms",
    "starting_generation": "generation_start_ms",
}


def _image_progress_callback_with_monitor(
        request: "ConversationRequest",
        index: int,
        total: int,
        account_email_getter: Callable[[], str],
) -> Callable[[str], None]:
    original_callback = request.progress_callback
    last_step = ""
    last_step_started = time.perf_counter()

    def report(step: str) -> None:
        nonlocal last_step, last_step_started
        now = time.perf_counter()
        step_name = str(step or "").strip()
        data: dict[str, Any] = {
            "index": index,
            "total": total,
        }
        account_email = account_email_getter()
        if account_email:
            data["account_email"] = account_email
        duration_key = _IMAGE_PROGRESS_DURATION_KEYS.get(last_step)
        if duration_key:
            data[duration_key] = int((now - last_step_started) * 1000)
        stage_event = _IMAGE_PROGRESS_STAGE_EVENTS.get(step_name)
        if stage_event:
            _monitor_image_stage(request, stage_event, **data)
        last_step = step_name
        last_step_started = now
        if original_callback:
            original_callback(step_name)

    return report


def _resolve_image_urls_with_monitor(
        backend: OpenAIBackendAPI,
        request: "ConversationRequest",
        conversation_id: str,
        file_ids: list[str],
        sediment_ids: list[str],
        index: int,
        total: int,
        path: str = "",
        **kwargs: Any,
) -> list[str]:
    resolve_started = time.perf_counter()
    try:
        image_urls = backend.resolve_conversation_image_urls(
            conversation_id,
            file_ids,
            sediment_ids,
            **kwargs,
        )
    except Exception as exc:
        text_reply = isinstance(exc, ImageTextReplyError)
        if request.trace_image_perf:
            resolve_ms = _elapsed_ms(resolve_started)
            _monitor_image_stage(
                request,
                "image_text_reply" if text_reply else "image_resolve_failed",
                conversation_id=conversation_id,
                resolve_ms=resolve_ms,
                index=index,
                total=total,
                status="failed",
                upstream_error=diagnostic_excerpt(repr(exc), 1000),
            )
            log_payload: dict[str, Any] = {
                "event": "image_text_reply" if text_reply else "image_resolve_failed",
                "call_id": request.call_id,
                "conversation_id": conversation_id,
                "resolve_ms": resolve_ms,
                "error": repr(exc)[:300],
            }
            if path:
                log_payload["path"] = path
            if text_reply:
                logger.info(log_payload)
            else:
                logger.warning(log_payload)
        raise
    if request.trace_image_perf:
        resolve_ms = _elapsed_ms(resolve_started)
        _monitor_image_stage(
            request,
            "image_resolve_done",
            conversation_id=conversation_id,
            resolve_ms=resolve_ms,
            url_count=len(image_urls),
            index=index,
            total=total,
        )
        log_payload = {
            "event": "image_resolve_done",
            "call_id": request.call_id,
            "conversation_id": conversation_id,
            "resolve_ms": resolve_ms,
            "url_count": len(image_urls),
        }
        if path:
            log_payload["path"] = path
        logger.info(log_payload)
    return image_urls


def _download_image_bytes_with_monitor(
        backend: OpenAIBackendAPI,
        request: "ConversationRequest",
        conversation_id: str,
        image_urls: list[str],
        index: int,
        total: int,
        path: str = "",
) -> list[bytes]:
    download_started = time.perf_counter()
    try:
        downloaded_images = backend.download_image_bytes(image_urls)
    except Exception as exc:
        if request.trace_image_perf:
            download_ms = _elapsed_ms(download_started)
            _monitor_image_stage(
                request,
                "image_download_failed",
                conversation_id=conversation_id,
                download_ms=download_ms,
                url_count=len(image_urls),
                index=index,
                total=total,
                status="failed",
                upstream_error=diagnostic_excerpt(repr(exc), 1000),
            )
            log_payload: dict[str, Any] = {
                "event": "image_download_failed",
                "call_id": request.call_id,
                "conversation_id": conversation_id,
                "download_ms": download_ms,
                "url_count": len(image_urls),
                "error": repr(exc)[:300],
            }
            if path:
                log_payload["path"] = path
            logger.warning(log_payload)
        raise
    if request.trace_image_perf:
        download_ms = _elapsed_ms(download_started)
        download_bytes = sum(len(image) for image in downloaded_images)
        download_kbps = int((download_bytes / 1024) / (download_ms / 1000)) if download_ms > 0 else 0
        _monitor_image_stage(
            request,
            "image_download_done",
            conversation_id=conversation_id,
            download_ms=download_ms,
            download_bytes=download_bytes,
            download_kbps=download_kbps,
            url_count=len(image_urls),
            image_count=len(downloaded_images),
            index=index,
            total=total,
        )
        log_payload = {
            "event": "image_download_done",
            "call_id": request.call_id,
            "conversation_id": conversation_id,
            "download_ms": download_ms,
            "download_bytes": download_bytes,
            "download_kbps": download_kbps,
            "url_count": len(image_urls),
            "image_count": len(downloaded_images),
        }
        if path:
            log_payload["path"] = path
        logger.info(log_payload)
    return downloaded_images


def _retry_sleep_with_monitor(
        request: "ConversationRequest",
        seconds: float,
        *,
        conversation_id: str = "",
        account_email: str = "",
        index: int = 1,
        total: int = 1,
) -> None:
    wait_secs = max(0.0, float(seconds or 0))
    _monitor_image_stage(
        request,
        "image_retry_wait",
        conversation_id=conversation_id,
        account_email=account_email,
        retry_wait_ms=int(wait_secs * 1000),
        index=index,
        total=total,
        status="waiting",
    )
    if wait_secs > 0:
        time.sleep(wait_secs)


def is_token_invalid_error(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "token_invalidated" in text
        or "token_revoked" in text
        or "authentication token has been invalidated" in text
        or "invalidated oauth token" in text
    )


def should_refresh_image_account(message: object) -> bool:
    text = str(message or "")
    lower = text.lower()
    return (
        is_token_invalid_error(text)
        or _is_image_quota_error(lower)
        or any(
            marker in lower
            for marker in (
                "status=429",
                "status_code=429",
                "http 429",
                "rate limit exceeded",
                "rate_limit_exceeded",
                "rate_limited",
                "quota exceeded",
                "quota_exceeded",
                "image quota",
                "too many requests",
            )
        )
    )


def is_tls_connection_error(message: str) -> bool:
    """检测 TLS/SSL 连接错误，这类错误通常可以通过重试解决。"""
    text = str(message or "").lower()
    return (
        "curl: (35)" in text
        or "tls connect error" in text
        or "openssl_internal" in text
        or "ssl: wrong_version_number" in text
        or "ssl: certificate_verify_failed" in text
        or "connection aborted" in text
        or "remote disconnected" in text
        or "connection reset by peer" in text
        or "upstream image connection failed" in text
    )


def is_connection_timeout_error(message: str) -> bool:
    """检测连接超时错误（如 curl 28），这类错误可通过同账号短等待重试解决。"""
    text = str(message or "").lower()
    return (
        "curl: (28)" in text
        or "operation timed out" in text
        or "connection timed out" in text
        or "read timed out" in text
        or "connect timeout" in text
        or "upstream connection timed out" in text
    )


def is_stream_transport_error(message: str) -> bool:
    """检测上游流式响应传输错误，这类错误通常来自 HTTP/2/SSE/代理长连接。"""
    text = str(message or "").lower()
    return (
        "curl: (92)" in text
        or "http/2 stream" in text
        or "internal_error" in text
        or "stream was not closed cleanly" in text
        or "stream reset" in text
        or "response ended prematurely" in text
        or "sse stream exceeded" in text
        or "upstream image stream interrupted" in text
    )


def image_stream_error_message(message: str) -> str:
    text = str(message or "")
    if not config.image_error_friendly_enabled:
        return _legacy_image_stream_error_message(text)
    if is_token_invalid_error(text):
        return _friendly_image_error_message("token_invalid")
    if is_stream_transport_error(text):
        return _friendly_image_error_message("stream_interrupted")
    if is_tls_connection_error(text):
        return _friendly_image_error_message("connection_failed")
    if is_connection_timeout_error(text):
        return _friendly_image_error_message("connection_timeout")
    return text or _friendly_image_error_message("fallback")


def _legacy_image_stream_error_message(message: str) -> str:
    text = str(message or "")
    if is_token_invalid_error(text):
        return "image generation failed"
    if is_stream_transport_error(text):
        return "upstream image stream interrupted, please retry later"
    if is_tls_connection_error(text):
        return "upstream image connection failed, please retry later"
    if is_connection_timeout_error(text):
        return "upstream connection timed out, please retry later"
    return text or "image generation failed"


REFERENCED_IMAGE_IDS_RE = re.compile(r'"referenced_image_ids"\s*:\s*\[([^\]]+)\]')
# 检测模型返回的部分工具调用 JSON（如 {"size":"1920x1088","n":1}）
# 这些 JSON 包含图片生成工具的参数，但没有实际生成图片
TOOL_PARAMS_JSON_RE = re.compile(
    r'\{\s*"size"\s*:\s*"\d+x\d+"\s*,\s*"n"\s*:\s*\d+\s*\}'
)


def is_model_text_reply_instead_of_image(message: str) -> bool:
    """检测模型是否返回了文本回复（包含工具调用 JSON）而非实际生成图片。

    当上游 ChatGPT 未能触发图片生成工具时，会返回一段描述性文本，
    其中可能包含 JSON 参数（如 prompt、referenced_image_ids、size/n 等）。
    这种情况应被视为「上游未生成图片」而非「内容策略违规」。

    检测两种模式：
    1. 完整的工具调用 JSON（含 referenced_image_ids）
    2. 部分的工具参数 JSON（如 {"size":"1920x1088","n":1}）
    """
    if not message:
        return False
    if REFERENCED_IMAGE_IDS_RE.search(message):
        return True
    # 检测部分工具参数 JSON（模型返回了工具参数但未触发工具）
    if TOOL_PARAMS_JSON_RE.search(message):
        return True
    return False


def encode_images(images: Iterable[tuple[bytes, str, str]]) -> list[str]:
    return [base64.b64encode(data).decode("ascii") for data, _, _ in images if data]


def save_image_bytes(image_data: bytes, base_url: str | None = None) -> str:
    return image_storage_service.save(image_data, base_url).url


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and str(item.get("type") or "") in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


def normalize_messages(messages: object, system: Any = None) -> list[dict[str, Any]]:
    normalized = []
    if config.global_system_prompt:
        normalized.append({"role": "system", "content": config.global_system_prompt})
    system_text = message_text(system)
    if system_text:
        normalized.append({"role": "system", "content": system_text})
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "user")
            content = message.get("content", "")
            text = message_text(content)
            images: list[tuple[bytes, str]] = []
            if role == "user":
                images.extend(extract_image_from_message_content(content))
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict) or part.get("type") != "image":
                            continue
                        data = part.get("data")
                        if isinstance(data, (bytes, bytearray)) and all(existing[0] != bytes(data) for existing in images):
                            images.append((bytes(data), str(part.get("mime") or "image/png")))
            if images:
                parts: list[Any] = []
                if text:
                    parts.append({"type": "text", "text": text})
                for data, mime in images:
                    parts.append({"type": "image", "data": data, "mime": mime})
                normalized.append({"role": role, "content": parts})
            else:
                normalized.append({"role": role, "content": text})
    return normalized


def prompt_with_global_system(prompt: str) -> str:
    return f"{config.global_system_prompt}\n\n{prompt}" if config.global_system_prompt else prompt


def assistant_history_text(messages: list[dict[str, Any]]) -> str:
    return "".join(str(item.get("content") or "") for item in messages if item.get("role") == "assistant")


def assistant_history_messages(messages: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("content") or "") for item in messages if item.get("role") == "assistant" and item.get("content")]


def build_image_prompt(prompt: str, size: str | None, quality: str = "auto") -> str:
    hints = []
    if size:
        hints.append(f"输出图片尺寸为 {size}。")
    if quality:
        hints.append(f"输出图片质量为 {quality}。")
    return f"{prompt.strip()}\n\n{''.join(hints)}" if hints else prompt


def encoding_for_model(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        try:
            return tiktoken.get_encoding("o200k_base")
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")


def count_message_image_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return sum(count_image_content_tokens(message.get("content"), model) for message in messages)


def count_message_text_tokens(messages: list[dict[str, Any]], model: str) -> int:
    encoding = encoding_for_model(model)
    total = 0
    for message in messages:
        total += 3
        for key, value in message.items():
            if key == "content" and isinstance(value, list):
                total += len(encoding.encode(message_text(value)))
            elif isinstance(value, str):
                total += len(encoding.encode(value))
            else:
                continue
            if key == "name":
                total += 1
    return total + 3


def count_message_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return count_message_text_tokens(messages, model) + count_message_image_tokens(messages, model)


def count_text_tokens(text: str, model: str) -> int:
    return len(encoding_for_model(model).encode(text))


def format_image_result(
    items: list[dict[str, Any]],
    prompt: str,
    response_format: str,
    base_url: str | None = None,
    created: int | None = None,
    message: str = "",
) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    for item in items:
        b64_json = str(item.get("b64_json") or "").strip()
        if not b64_json:
            continue
        revised_prompt = str(item.get("revised_prompt") or prompt).strip() or prompt
        image_bytes = base64.b64decode(b64_json)
        stored_url = save_image_bytes(image_bytes, base_url)
        asset: dict[str, Any] = {"revised_prompt": revised_prompt}
        if response_format == "b64_json":
            asset["b64_json"] = b64_json
        else:
            asset["url"] = stored_url
        dimensions = image_size_from_bytes(image_bytes)
        if dimensions:
            asset["width"], asset["height"] = dimensions
        data.append(asset)
    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if message and not data:
        result["message"] = message
    return result


@dataclass
class ConversationRequest:
    model: str = "auto"
    prompt: str = ""
    messages: list[dict[str, Any]] | None = None
    thinking_effort: str = ""
    images: list[str] | None = None
    n: int = 1
    size: str | None = None
    quality: str = "auto"
    response_format: str = "b64_json"
    base_url: str | None = None
    message_as_error: bool = False
    progress_callback: Any = None  # Callable[[str], None] | None
    call_id: str = ""
    trace_image_perf: bool = False


@dataclass
class ConversationState:
    text: str = ""
    raw_text: str = ""
    conversation_id: str = ""
    file_ids: list[str] = field(default_factory=list)
    sediment_ids: list[str] = field(default_factory=list)
    blocked: bool = False
    tool_invoked: bool | None = None
    turn_use_case: str = ""


@dataclass
class ImageOutput:
    kind: str
    model: str
    index: int
    total: int
    created: int = field(default_factory=lambda: int(time.time()))
    text: str = ""
    upstream_event_type: str = ""
    data: list[dict[str, Any]] = field(default_factory=list)
    account_email: str = ""
    conversation_id: str = ""

    def to_chunk(self) -> dict[str, Any]:
        chunk: dict[str, Any] = {
            "object": "image.generation.chunk",
            "created": self.created,
            "model": self.model,
            "index": self.index,
            "total": self.total,
            "progress_text": self.text,
            "upstream_event_type": self.upstream_event_type,
            "data": [],
        }
        if self.account_email:
            chunk["_account_email"] = self.account_email
        if self.conversation_id:
            chunk["_conversation_id"] = self.conversation_id
        if self.kind == "message":
            chunk.update({
                "object": "image.generation.message",
                "message": self.text,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        elif self.kind == "result":
            chunk.update({
                "object": "image.generation.result",
                "data": self.data,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        return chunk


def assistant_message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") or []
    if isinstance(parts, list) and parts:
        text = "".join(part for part in parts if isinstance(part, str))
        if text:
            return text
    # Fallback: content_type "code" stores text in the "text" field instead of "parts"
    text_field = str(content.get("text") or "")
    if text_field:
        return text_field
    return ""


def strip_history(text: str, history_text: str = "") -> str:
    text = str(text or "")
    history_text = str(history_text or "")
    while history_text and text.startswith(history_text):
        text = text[len(history_text):]
    return text


def sanitize_output_text(text: str) -> str:
    text = str(text or "")

    def is_internal_annotation_part(part: str) -> bool:
        value = part.strip()
        if not value:
            return True
        lower = value.lower()
        return bool(
            re.fullmatch(r"turn\d+[a-z]*\d*", lower)
            or re.fullmatch(r"turn\d+\w*", lower)
            or lower.startswith(("turn", "source", "sources"))
        )

    def readable_annotation_part(parts: list[str]) -> str:
        for part in parts:
            value = part.strip()
            if value and not is_internal_annotation_part(value):
                return value
        return ""

    def replace_annotation(match: re.Match[str]) -> str:
        payload = match.group(1)
        parts = [part.strip() for part in payload.split("\ue202")]
        kind = (parts[0] if parts else "").lower()
        data = parts[1:]
        if kind == "url":
            label = data[0] if data else ""
            url = data[1] if len(data) > 1 else ""
            if label and url.startswith(("http://", "https://")):
                return f"{label} ({url})"
            return label or url
        if kind == "cite":
            return readable_annotation_part(data)
        return readable_annotation_part(data)

    # ChatGPT web sometimes returns rich annotation markers using private-use
    # characters. API clients cannot render those. Preserve readable labels
    # from entity/link annotations, while removing internal citation pointers.
    text = re.sub(r"\ue200([^\ue201]*)\ue201", replace_annotation, text)
    text = re.sub(r"\ue200[^\ue201]*$", "", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    return text


def assistant_raw_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if not isinstance(message, dict):
            continue
        role = str((message.get("author") or {}).get("role") or "").strip().lower()
        if role != "assistant":
            continue
        text = assistant_message_text(message)
        if text:
            return strip_history(text, history_text)
    return apply_text_patch(event, current_text, history_text)


def assistant_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    return sanitize_output_text(assistant_raw_text(event, current_text, history_text))


def event_assistant_text(event: dict[str, Any], history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if isinstance(message, dict) and (message.get("author") or {}).get("role") == "assistant":
            return strip_history(assistant_message_text(message), history_text)
    return ""


def apply_text_patch(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    if event.get("p") == "/message/content/parts/0":
        return apply_patch_op(event, current_text, history_text)

    operations = event.get("v")
    if isinstance(operations, str) and current_text and not event.get("p") and not event.get("o"):
        return current_text + operations

    if event.get("o") == "patch" and isinstance(operations, list):
        text = current_text
        for item in operations:
            if isinstance(item, dict):
                text = apply_text_patch(item, text, history_text)
        return text

    if not isinstance(operations, list):
        return current_text

    text = current_text
    for item in operations:
        if isinstance(item, dict):
            text = apply_text_patch(item, text, history_text)
    return text


def apply_patch_op(operation: dict[str, Any], current_text: str, history_text: str = "") -> str:
    op = operation.get("o")
    value = str(operation.get("v") or "")
    if op == "append":
        return current_text + value
    if op == "replace":
        return strip_history(value, history_text)
    return current_text


def add_unique(values: list[str], candidates: list[str]) -> None:
    for candidate in candidates:
        if candidate and candidate not in values:
            values.append(candidate)


FILE_SERVICE_ID_RE = re.compile(r"file-service://([A-Za-z0-9_-]+)")
FILE_ID_RE = re.compile(r"\b(file[-_](?!service\b)[A-Za-z0-9_-]+)\b")
# 真正的图片文件 ID 格式：file_00000000 + 24位十六进制字符（共32字符）
# 用于过滤非图片文件 ID（如 file_upload_business_upsell）
REAL_IMAGE_FILE_ID_RE = re.compile(r"\bfile_00000000[a-f0-9]{24}\b")
SEDIMENT_ID_RE = re.compile(r"sediment://([A-Za-z0-9_-]+)")


def extract_conversation_ids(payload: str) -> tuple[str, list[str], list[str]]:
    conversation_match = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
    conversation_id = conversation_match.group(1) if conversation_match else ""
    file_ids: list[str] = []
    # Negative lookahead excludes "file-service" (URI prefix, not a real id).
    add_unique(file_ids, FILE_SERVICE_ID_RE.findall(payload))
    # 只提取真正的图片文件 ID（file_00000000... 格式），过滤非图片文件 ID（如 file_upload_business_upsell）
    add_unique(file_ids, REAL_IMAGE_FILE_ID_RE.findall(payload))
    sediment_ids = SEDIMENT_ID_RE.findall(payload)
    return conversation_id, file_ids, sediment_ids


def is_image_tool_event(event: dict[str, Any]) -> bool:
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if not isinstance(message, dict):
        return False
    metadata = message.get("metadata") or {}
    author = message.get("author") or {}
    content = message.get("content") or {}
    if author.get("role") != "tool":
        return False
    if metadata.get("async_task_type") == "image_gen":
        return True
    if content.get("content_type") != "multimodal_text":
        return False
    return any(
        isinstance(part, dict) and (
                part.get("content_type") == "image_asset_pointer"
                or str(part.get("asset_pointer") or "").startswith(("file-service://", "sediment://"))
        )
        for part in content.get("parts") or []
    )


def _is_user_message_event(event: dict[str, Any]) -> bool:
    """检查事件是否来自 user 角色消息。"""
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if isinstance(message, dict):
        author = message.get("author") or {}
        if str(author.get("role") or "").strip().lower() == "user":
            return True
    return False


def update_conversation_state(state: ConversationState, payload: str, event: dict[str, Any] | None = None) -> None:
    conversation_id, file_ids, sediment_ids = extract_conversation_ids(payload)
    if conversation_id and not state.conversation_id:
        state.conversation_id = conversation_id
    # Accept file_id / sediment_id when any of:
    #   1) event is a complete image_gen tool message
    #   2) prior server_ste_metadata already flipped tool_invoked True (in an image_gen turn),
    #      BUT only for non-user messages — user messages contain the uploaded input image
    #      which must NOT be treated as a generated output.
    #   3) patch event whose payload references asset_pointer / file-service://,
    #      BUT only when the event is not a user message.
    is_patch_event = isinstance(event, dict) and event.get("o") == "patch"
    is_user_msg = isinstance(event, dict) and _is_user_message_event(event)
    image_context = (
        (isinstance(event, dict) and is_image_tool_event(event))
        or (state.tool_invoked is True and not is_user_msg)
        or (is_patch_event and not is_user_msg and ("asset_pointer" in payload or "file-service://" in payload))
    )
    if image_context:
        add_unique(state.file_ids, file_ids)
        add_unique(state.sediment_ids, sediment_ids)
    if not isinstance(event, dict):
        return
    state.conversation_id = str(event.get("conversation_id") or state.conversation_id)
    value = event.get("v")
    if isinstance(value, dict):
        state.conversation_id = str(value.get("conversation_id") or state.conversation_id)
    if event.get("type") == "moderation":
        moderation = event.get("moderation_response")
        if isinstance(moderation, dict) and moderation.get("blocked") is True:
            state.blocked = True
    if event.get("type") == "server_ste_metadata":
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            if isinstance(metadata.get("tool_invoked"), bool):
                state.tool_invoked = metadata["tool_invoked"]
            state.turn_use_case = str(metadata.get("turn_use_case") or state.turn_use_case)


def conversation_base_event(event_type: str, state: ConversationState, **extra: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "text": state.text,
        "conversation_id": state.conversation_id,
        "file_ids": list(state.file_ids),
        "sediment_ids": list(state.sediment_ids),
        "blocked": state.blocked,
        "tool_invoked": state.tool_invoked,
        "turn_use_case": state.turn_use_case,
        **extra,
    }


def iter_conversation_payloads(payloads: Iterator[str], history_text: str = "",
                               history_messages: list[str] | None = None) -> Iterator[dict[str, Any]]:
    state = ConversationState()
    history_messages = history_messages or []
    history_index = 0
    for payload in payloads:
        # print(f"[upstream_sse] {payload}", flush=True)
        if not payload:
            continue
        if payload == "[DONE]":
            yield conversation_base_event("conversation.done", state, done=True)
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            update_conversation_state(state, payload)
            yield conversation_base_event("conversation.raw", state, payload=payload)
            continue
        if not isinstance(event, dict):
            yield conversation_base_event("conversation.event", state, raw=event)
            continue
        update_conversation_state(state, payload, event)
        if history_index < len(history_messages) and event_assistant_text(event, history_text) == history_messages[history_index]:
            history_index += 1
            state.raw_text = ""
            state.text = ""
            continue
        next_raw_text = assistant_raw_text(event, state.raw_text, history_text)
        next_text = sanitize_output_text(next_raw_text)
        state.raw_text = next_raw_text
        if next_text != state.text:
            delta = next_text[len(state.text):] if next_text.startswith(state.text) else next_text
            state.text = next_text
            yield conversation_base_event("conversation.delta", state, raw=event, delta=delta)
            continue
        yield conversation_base_event("conversation.event", state, raw=event)


def conversation_events(
    backend: OpenAIBackendAPI,
    messages: list[dict[str, Any]] | None = None,
    model: str = "auto",
    prompt: str = "",
    images: list[str] | None = None,
    size: str | None = None,
    quality: str = "auto",
    thinking_effort: str = "",
) -> Iterator[dict[str, Any]]:
    normalized = normalize_messages(messages or ([{"role": "user", "content": prompt}] if prompt else []))
    image_model = is_supported_image_model(model)
    history_text = "" if image_model else assistant_history_text(normalized)
    history_messages = [] if image_model else assistant_history_messages(normalized)
    final_prompt = prompt_with_global_system(build_image_prompt(prompt, size, quality)) if image_model else prompt
    payloads = backend.stream_conversation(
        messages=normalized,
        model=model,
        prompt=final_prompt,
        images=images if image_model else None,
        system_hints=["picture_v2"] if image_model else None,
        thinking_effort=thinking_effort if not image_model else "",
    )
    yield from iter_conversation_payloads(payloads, history_text, history_messages)


def _text_account_email(access_token: str) -> str:
    account = account_service.get_account(access_token)
    if not account:
        return ""
    return str(account.get("email") or "").strip()


def _remember_text_account(backend: OpenAIBackendAPI, access_token: str) -> str:
    email = _text_account_email(access_token)
    setattr(backend, "account_email", email)
    return email


def text_backend() -> OpenAIBackendAPI:
    access_token = account_service.get_text_access_token()
    backend = OpenAIBackendAPI(access_token=access_token)
    _remember_text_account(backend, access_token)
    return backend


def stream_text_deltas(backend: OpenAIBackendAPI, request: ConversationRequest) -> Iterator[str]:
    attempted_tokens: set[str] = set()
    token = getattr(backend, "access_token", "")
    emitted = False
    while True:
        if token and token in attempted_tokens:
            raise RuntimeError("no available text account")
        if token:
            attempted_tokens.add(token)
        active_backend: OpenAIBackendAPI | None = None
        try:
            _remember_text_account(backend, token)
            active_backend = OpenAIBackendAPI(access_token=token)
            _remember_text_account(active_backend, token)
            for event in conversation_events(
                active_backend,
                messages=request.messages,
                model=request.model,
                prompt=request.prompt,
                thinking_effort=request.thinking_effort,
            ):
                if event.get("type") != "conversation.delta":
                    continue
                delta = str(event.get("delta") or "")
                if delta:
                    emitted = True
                    yield delta
            account_service.mark_text_used(token)
            return
        except Exception as exc:
            error_message = str(exc)
            if token and not emitted and is_token_invalid_error(error_message):
                refreshed_token = account_service.refresh_access_token(token, force=True, event="text_stream")
                if refreshed_token and refreshed_token != token and refreshed_token not in attempted_tokens:
                    token = refreshed_token
                else:
                    account_service.handle_invalid_token(token, "text_stream", error=error_message)
                    token = account_service.get_text_access_token(attempted_tokens)
                if token:
                    continue
            if token and not getattr(exc, "account_email", ""):
                setattr(exc, "account_email", _text_account_email(token))
            raise
        finally:
            if active_backend is not None:
                active_backend.close()


def collect_text(backend: OpenAIBackendAPI, request: ConversationRequest) -> str:
    return "".join(stream_text_deltas(backend, request))


def _get_detailed_error_from_tasks(
    backend: OpenAIBackendAPI,
    conversation_id: str,
    timeout_secs: float = 10.0,
    wait_secs: float = 2.0,
) -> str:
    """从 /backend-api/tasks/ 接口获取结构化错误信息。

    当 SSE 流检测到 moderation 拦截时，轮询 tasks 接口获取详细错误文本。
    使用结构化字段（metadata.is_error, author.role, content.content_type）判断，
    而非依赖易变的文本匹配。

    参数：
    - `backend`：OpenAIBackendAPI 实例。
    - `conversation_id`：会话 ID。
    - `timeout_secs`：请求超时秒数。
    - `wait_secs`：等待任务创建的秒数。设为 0 可跳过等待。

    返回：
    - 详细错误信息文本，如果未找到则返回空字符串。
    """
    import time as _time
    try:
        if wait_secs > 0:
            _time.sleep(wait_secs)
        tasks = backend._query_backend_tasks(conversation_id=conversation_id, timeout_secs=timeout_secs)
        if not tasks:
            return ""

        for task in tasks:
            is_error, error_msg, metadata = backend.check_task_error(task)
            if is_error and error_msg:
                logger.info({
                    "event": "image_task_structured_error",
                    "conversation_id": conversation_id,
                    "error_msg": error_msg,
                    "metadata": metadata,
                })
                return error_msg
        return ""
    except Exception as exc:
        logger.warning({
            "event": "image_task_error_query_failed",
            "conversation_id": conversation_id,
            "error": diagnostic_excerpt(exc, 300),
        })
        return ""


def _recover_image_conversation_id(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        *,
        reason: str,
        message: str = "",
        started_at: float | None = None,
) -> str:
    """从最近对话中补救 conversation_id；只做一次，失败不影响主流程。"""
    if not request.prompt:
        return ""
    try:
        recovered_id = backend.find_conversation_by_prompt(
            request.prompt,
            started_at or time.time(),
            timeout_secs=5.0,
        )
        if recovered_id:
            logger.info({
                "event": "image_conversation_id_recovered",
                "reason": reason,
                "conversation_id": recovered_id,
                "message_preview": message[:200],
            })
            return recovered_id
    except Exception as exc:
        logger.warning({
            "event": "image_conversation_id_recovery_failed",
            "reason": reason,
            "error": repr(exc)[:300],
        })
    return ""


def _image_stream_timeout_task_diagnostics(
        backend: OpenAIBackendAPI,
        conversation_id: str,
) -> tuple[str, list[dict[str, Any]], str]:
    """Collect a small task summary after an upstream SSE timeout."""
    try:
        tasks = backend._query_backend_tasks(conversation_id=conversation_id, timeout_secs=5.0)
    except Exception as exc:
        return "", [], diagnostic_excerpt(repr(exc), 1000)

    summaries: list[dict[str, Any]] = []
    first_error = ""
    for task in tasks[:5]:
        if not isinstance(task, dict):
            continue
        is_error, error_msg, metadata = backend.check_task_error(task)
        metadata = metadata if isinstance(metadata, dict) else {}
        if error_msg and not first_error:
            first_error = error_msg
        summary: dict[str, Any] = {
            "id": str(task.get("id") or task.get("task_id") or "")[:80],
            "status": str(task.get("status") or metadata.get("status") or "")[:80],
            "type": str(task.get("type") or metadata.get("async_task_type") or "")[:80],
            "is_error": bool(is_error or metadata.get("is_error")),
        }
        for key in ("conversation_id", "original_conversation_id"):
            value = task.get(key)
            if value not in (None, ""):
                summary[key] = str(value)[:80]
        if error_msg:
            summary["error_preview"] = diagnostic_excerpt(error_msg, 500)
        summaries.append({key: value for key, value in summary.items() if value not in (None, "")})
    return first_error, summaries, ""


def _image_stream_timeout_error(
        raw_error: str,
        conversation_id: str,
        upstream_error: str,
        raw_upstream_message: str,
        followup: dict[str, Any],
        conversation_snapshot: dict[str, Any] | None = None,
) -> ImageGenerationError:
    exc = ImageGenerationError(
        image_stream_error_message(raw_error),
        status_code=502,
        error_type="server_error",
        code="image_stream_timeout",
        conversation_id=conversation_id,
        raw_error=raw_error,
        upstream_error=diagnostic_excerpt(upstream_error or raw_error, 4000),
        raw_upstream_message=diagnostic_excerpt(raw_upstream_message, 4000),
    )
    setattr(exc, "stream_timeout_secs", config.image_stream_timeout_secs)
    setattr(exc, "stream_timeout_followup", followup)
    if followup.get("task_error"):
        setattr(exc, "last_task_error", followup["task_error"])
    if conversation_snapshot:
        setattr(exc, "last_conversation_snapshot", conversation_snapshot)
    if raw_upstream_message:
        setattr(exc, "upstream_message_preview", diagnostic_excerpt(raw_upstream_message, 1000))
    return exc


def _recover_after_image_stream_timeout(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        last: dict[str, Any],
        timeout_error: Exception,
        index: int,
        total: int,
        stream_started_at: float,
) -> ImageOutput:
    raw_error = str(timeout_error) or f"SSE stream exceeded {config.image_stream_timeout_secs}s"
    conversation_id = str(last.get("conversation_id") or "")
    file_ids = [str(item) for item in last.get("file_ids") or []]
    sediment_ids = [str(item) for item in last.get("sediment_ids") or []]
    message = str(last.get("text") or "").strip()

    if not conversation_id:
        conversation_id = _recover_image_conversation_id(
            backend,
            request,
            reason="stream_timeout",
            message=message or raw_error,
            started_at=stream_started_at,
        )

    followup: dict[str, Any] = {
        "reason": "sse_timeout",
        "timeout_secs": config.image_stream_timeout_secs,
        "conversation_id": conversation_id,
        "stream_error": raw_error,
        "last_stream_state": {
            "conversation_id": str(last.get("conversation_id") or ""),
            "file_ids": file_ids,
            "sediment_ids": sediment_ids,
            "blocked": bool(last.get("blocked")),
            "tool_invoked": last.get("tool_invoked"),
            "turn_use_case": str(last.get("turn_use_case") or ""),
            "text_preview": diagnostic_excerpt(message, 1000),
        },
    }
    task_error = ""
    task_summaries: list[dict[str, Any]] = []
    task_probe_error = ""
    conversation_snapshot: dict[str, Any] = {}
    latest_assistant_text = ""
    policy_message = ""
    conversation_probe_error = ""

    if conversation_id:
        task_error, task_summaries, task_probe_error = _image_stream_timeout_task_diagnostics(backend, conversation_id)
        try:
            conversation = backend._get_conversation(conversation_id, timeout_secs=10)
            conversation_snapshot, latest_assistant_text = backend._conversation_poll_snapshot(conversation)
            for record in backend._extract_image_tool_records(conversation):
                add_unique(file_ids, [str(item) for item in record.get("file_ids") or []])
                add_unique(sediment_ids, [str(item) for item in record.get("sediment_ids") or []])
            policy_message = backend._find_content_policy_error_in_conversation(conversation)
        except Exception as exc:
            conversation_probe_error = diagnostic_excerpt(repr(exc), 1000)

    followup.update({
        "task_error": diagnostic_excerpt(task_error, 2000),
        "task_count": len(task_summaries),
        "tasks": task_summaries,
        "task_probe_error": task_probe_error,
        "conversation_probe_error": conversation_probe_error,
        "conversation_message_count": len(conversation_snapshot.get("messages") or []) if conversation_snapshot else 0,
        "latest_assistant_text": diagnostic_excerpt(latest_assistant_text, 2000),
        "policy_message": diagnostic_excerpt(policy_message, 2000),
        "recovered_file_ids": file_ids,
        "recovered_sediment_ids": sediment_ids,
    })

    policy_error = policy_message or (task_error if task_error and _is_content_policy_image_message(task_error) else "")
    if policy_error:
        policy_exc = ImageGenerationError(
            policy_error,
            status_code=400,
            error_type="invalid_request_error",
            code="content_policy_violation",
            conversation_id=conversation_id,
            raw_error=raw_error,
            upstream_error=policy_error,
            raw_upstream_message=policy_error,
        )
        setattr(policy_exc, "stream_timeout_secs", config.image_stream_timeout_secs)
        setattr(policy_exc, "stream_timeout_followup", followup)
        if conversation_snapshot:
            setattr(policy_exc, "last_conversation_snapshot", conversation_snapshot)
        raise policy_exc

    if file_ids or sediment_ids:
        try:
            image_urls = _resolve_image_urls_with_monitor(
                backend,
                request,
                conversation_id,
                file_ids,
                sediment_ids,
                index=index,
                total=total,
                path="stream_timeout_followup",
                poll=False,
            )
            result_output = _image_result_output_from_urls(
                backend,
                request,
                conversation_id,
                image_urls,
                index,
                total,
                path="stream_timeout_followup",
            )
            if result_output:
                logger.info({
                    "event": "image_stream_timeout_recovered_result",
                    "call_id": request.call_id,
                    "conversation_id": conversation_id,
                    "file_ids": file_ids,
                    "sediment_ids": sediment_ids,
                    "url_count": len(image_urls),
                })
                return result_output
        except Exception as exc:
            followup["result_recovery_error"] = diagnostic_excerpt(repr(exc), 1000)

    upstream_error = task_error or policy_message
    if not upstream_error:
        upstream_error = json.dumps(
            {
                key: value
                for key, value in followup.items()
                if key not in {"conversation_snapshot"}
            },
            ensure_ascii=False,
            default=str,
        )
    logger.warning({
        "event": "image_stream_timeout_followup",
        "call_id": request.call_id,
        "conversation_id": conversation_id,
        "task_error": diagnostic_excerpt(task_error, 500),
        "task_count": len(task_summaries),
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "conversation_probe_error": conversation_probe_error,
        "task_probe_error": task_probe_error,
    })
    raise _image_stream_timeout_error(
        raw_error,
        conversation_id,
        upstream_error,
        latest_assistant_text or message,
        followup,
        conversation_snapshot,
    )


def _cleanup_image_conversations_after_success(backend: OpenAIBackendAPI, outputs: Iterable[ImageOutput]) -> None:
    if not config.image_remove_conversation_after_result:
        return
    conversation_ids: list[str] = []
    seen: set[str] = set()
    for output in outputs:
        conversation_id = str(getattr(output, "conversation_id", "") or "").strip()
        if output.kind != "result" or not conversation_id or conversation_id in seen:
            continue
        seen.add(conversation_id)
        conversation_ids.append(conversation_id)
    for conversation_id in conversation_ids:
        try:
            backend.delete_conversation(conversation_id)
            logger.info({"event": "image_conversation_removed", "conversation_id": conversation_id})
        except Exception as exc:
            logger.warning({
                "event": "image_conversation_remove_failed",
                "conversation_id": conversation_id,
                "error": diagnostic_excerpt(exc, 500),
            })


def _image_result_output_from_urls(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        conversation_id: str,
        image_urls: list[str],
        index: int,
        total: int,
        *,
        path: str = "",
) -> ImageOutput | None:
    if not image_urls:
        return None
    if request.progress_callback:
        request.progress_callback("receiving_image")
    downloaded_images = _download_image_bytes_with_monitor(
        backend,
        request,
        conversation_id,
        image_urls,
        index,
        total,
        path=path,
    )
    image_items = [
        {"b64_json": base64.b64encode(image_data).decode("ascii")}
        for image_data in downloaded_images
    ]
    data = format_image_result(
        image_items,
        request.prompt,
        request.response_format,
        request.base_url,
        int(time.time()),
    )["data"]
    if not data:
        return None
    return ImageOutput(
        kind="result",
        model=request.model,
        index=index,
        total=total,
        data=data,
        conversation_id=conversation_id,
    )


def stream_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    """执行一张 ChatGPT 图片任务。

    统一原则：上游 SSE 只负责启动/生成阶段；SSE 结束后只进入一次结果解析/轮询。
    不再在文本回复、空结果、轮询超时后叠加多轮长重试，避免一个配置的 300 秒被隐式放大到十几分钟。
    """
    last: dict[str, Any] = {}
    conversation_stream_started = time.perf_counter()
    conversation_wall_started = time.time()
    try:
        for event in conversation_events(
                backend,
                prompt=request.prompt,
                model=request.model,
                images=request.images or [],
                size=request.size,
                quality=request.quality,
        ):
            last = event
            if event.get("type") == "conversation.delta":
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    text=str(event.get("delta") or ""),
                    upstream_event_type="conversation.delta",
                )
                continue
            if event.get("type") == "conversation.event":
                raw = event.get("raw")
                raw_type = str(raw.get("type") or "") if isinstance(raw, dict) else ""
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    upstream_event_type=raw_type,
                )
    except TimeoutError as exc:
        yield _recover_after_image_stream_timeout(
            backend,
            request,
            last,
            exc,
            index,
            total,
            conversation_wall_started,
        )
        return

    conversation_id = str(last.get("conversation_id") or "")
    file_ids = [str(item) for item in last.get("file_ids") or []]
    sediment_ids = [str(item) for item in last.get("sediment_ids") or []]
    message = str(last.get("text") or "").strip()
    should_poll_for_image = bool(request.images) or last.get("turn_use_case") == "image gen"
    # Image-generation SSE can finish with a human-readable queue/status
    # placeholder while the actual file IDs are still committed via the
    # conversation document.  When the turn structurally says an image should be
    # polled, do not treat stream text as terminal; let the poll path decide
    # from conversation/tool structure.
    is_text_reply = bool(
        message
        and not should_poll_for_image
        and backend._is_human_facing_image_text_reply_payload(message, last)
    )
    conversation_stream_ms = int((time.perf_counter() - conversation_stream_started) * 1000)
    http_timing = _backend_http_timing_data(backend)
    _monitor_image_stage(
        request,
        "image_stream_resolve_start",
        conversation_id=conversation_id,
        conversation_stream_ms=conversation_stream_ms,
        index=index,
        total=total,
        **http_timing,
    )
    logger.info({
        "event": "image_stream_resolve_start",
        "call_id": request.call_id,
        "conversation_id": conversation_id,
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "tool_invoked": last.get("tool_invoked"),
        "turn_use_case": last.get("turn_use_case"),
        "is_text_reply": is_text_reply,
        "should_poll_for_image": should_poll_for_image,
        "conversation_stream_ms": conversation_stream_ms,
        **http_timing,
    })
    if request.progress_callback:
        request.progress_callback("image_stream_resolve_start")

    if is_text_reply:
        logger.info({
            "event": "image_stream_text_reply_detected",
            "conversation_id": conversation_id,
            "message_preview": message[:200],
        })

    if not conversation_id and (should_poll_for_image or is_text_reply):
        conversation_id = _recover_image_conversation_id(
            backend,
            request,
            reason="stream_result",
            message=message,
        )

    if message and not file_ids and not sediment_ids and last.get("blocked"):
        detailed_error = _get_detailed_error_from_tasks(backend, conversation_id)
        error_text = detailed_error or message or "Image generation was rejected by upstream policy."
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=error_text, conversation_id=conversation_id)
        return

    if is_text_reply and message and not file_ids and not sediment_ids:
        text_exc = ImageTextReplyError(message)
        setattr(text_exc, "conversation_id", conversation_id or "")
        setattr(text_exc, "upstream_error", message)
        setattr(text_exc, "raw_upstream_message", message)
        setattr(text_exc, "last_assistant_text", message)
        raise text_exc

    detailed_error = ""
    if not file_ids and not sediment_ids and conversation_id:
        detailed_error = _get_detailed_error_from_tasks(backend, conversation_id, timeout_secs=5.0, wait_secs=1.0)
        if detailed_error and not should_poll_for_image and not is_text_reply:
            logger.info({
                "event": "image_task_error_before_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })
            yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=detailed_error, conversation_id=conversation_id)
            return
        if detailed_error and _is_content_policy_image_message(detailed_error):
            logger.info({
                "event": "image_task_policy_error_before_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })
            yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=detailed_error, conversation_id=conversation_id)
            return
        if detailed_error:
            logger.info({
                "event": "image_task_error_observed_before_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })

    if message and not file_ids and not sediment_ids and not should_poll_for_image and not is_text_reply:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
        return

    image_urls = _resolve_image_urls_with_monitor(
        backend,
        request,
        conversation_id,
        file_ids,
        sediment_ids,
        poll_timeout_secs=config.image_poll_timeout_secs,
        index=index,
        total=total,
    )
    result_output = _image_result_output_from_urls(
        backend,
        request,
        conversation_id,
        image_urls,
        index,
        total,
    )
    if result_output:
        yield result_output
        return

    if message and not should_poll_for_image:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
        return

    if detailed_error:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=detailed_error, conversation_id=conversation_id)
        return

    if conversation_id:
        yield ImageOutput(
            kind="message",
            model=request.model,
            index=index,
            total=total,
            text="Image generation completed upstream but the result could not be retrieved. Please try again in a moment.",
            conversation_id=conversation_id,
        )
        return

    yield ImageOutput(
        kind="message",
        model=request.model,
        index=index,
        total=total,
        text="Image generation started upstream but the response was incomplete. Please try again.",
        conversation_id=conversation_id,
    )

def _codex_response_images(value: Any) -> list[str]:
    if isinstance(value, dict):
        if value.get("type") == "image_generation_call" and isinstance(value.get("result"), str):
            result = value["result"].strip()
            if result:
                return [result.split(",", 1)[1] if result.startswith("data:image/") else result]
        images: list[str] = []
        for item in value.values():
            images.extend(_codex_response_images(item))
        return images
    if isinstance(value, list):
        images: list[str] = []
        for item in value:
            images.extend(_codex_response_images(item))
        return images
    return []


def stream_codex_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    codex_started = time.perf_counter()
    images = _codex_response_images(list(backend.iter_codex_image_response_events(
        prompt=request.prompt,
        images=request.images or [],
        size=request.size,
        quality=request.quality,
    )))
    if request.trace_image_perf:
        _monitor_image_stage(
            request,
            "image_codex_response_done",
            response_ms=int((time.perf_counter() - codex_started) * 1000),
            image_count=len(images),
            index=index,
            total=total,
        )
        logger.info({
            "event": "image_codex_response_done",
            "call_id": request.call_id,
            "model": request.model,
            "index": index,
            "total": total,
            "response_ms": int((time.perf_counter() - codex_started) * 1000),
            "image_count": len(images),
        })
    if not images:
        raise ImageGenerationError("No image result found in response")
    data = format_image_result(
        [{"b64_json": item, "revised_prompt": request.prompt} for item in images],
        request.prompt,
        request.response_format,
        request.base_url,
        int(time.time()),
    )["data"]
    if data:
        yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data)
        return
    raise ImageGenerationError("No image result found in response")


def _generate_single_image(
        request: ConversationRequest,
        index: int,
        total: int,
) -> list[ImageOutput]:
    """为单张图片执行生成逻辑（含重试），返回结果列表。

    该函数在独立线程中运行，每个线程使用不同的账号，
    实现并行生图，避免串行超时阻塞。
    """
    account_email = ""
    retry_token = ""
    fallback_retry_pending = False
    fallback_retry_used = False
    fallback_from_egress: dict[str, Any] = {}
    single_started = time.perf_counter()

    while True:
        account_wait_started = time.perf_counter()
        stream_started = 0.0
        try:
            _raise_if_request_cancelled(request)
            if retry_token:
                token = retry_token
                retry_token = ""
            else:
                if request.progress_callback:
                    request.progress_callback("getting_account")
                _monitor_image_stage(request, "image_getting_account", index=index, total=total)
                plan_type, _ = split_image_model(request.model)
                codex_model = is_codex_image_model(request.model)
                token = account_service.get_available_access_token(
                    plan_type=plan_type,
                    source_type="codex" if codex_model else None,
                    plan_types=("plus", "team", "pro") if codex_model and not plan_type else None,
                )
        except ImageAccountSelectionError as exc:
            _monitor_image_stage(
                request,
                "image_local_rejected",
                local_reason="account_pool",
                status="failed",
                index=index,
                total=total,
            )
            raise ImageGenerationError(
                str(exc) or "image generation failed",
                status_code=exc.status_code,
                error_type=exc.error_type,
                code=exc.code,
                account_email=account_email,
            ) from exc
        except RuntimeError as exc:
            _monitor_image_stage(
                request,
                "image_local_rejected",
                local_reason="account_pool",
                status="failed",
                index=index,
                total=total,
            )
            raise ImageGenerationError(str(exc) or "image generation failed", account_email=account_email) from exc

        emitted_for_token = False
        returned_message = False
        returned_result = False
        image_slot_finalized = False

        def finalize_image_slot(success: bool, *, refresh_account: bool = False) -> None:
            nonlocal image_slot_finalized
            if image_slot_finalized:
                return
            image_slot_finalized = True
            account_service.mark_image_result(
                token,
                success,
                refresh_account=refresh_account,
            )

        account_wait_ms = int((time.perf_counter() - account_wait_started) * 1000)
        account = account_service.get_account(token) or {}
        account_email = str(account.get("email") or "").strip()
        _monitor_image_stage(
            request,
            "image_account_lookup",
            account_wait_ms=account_wait_ms,
            account_email=account_email,
            account_found=bool(account),
            index=index,
            total=total,
        )
        if account_wait_ms >= 5000:
            logger.warning({
                "event": "image_account_wait_slow",
                "call_id": request.call_id,
                "account_wait_ms": account_wait_ms,
                "account_email": account_email,
                "index": index,
            })
        logger.debug({
            "event": "image_account_lookup",
            "call_id": request.call_id,
            "token_prefix": token[:12] + "..." if len(token) > 12 else token,
            "account_email": account_email,
            "account_found": bool(account),
            "account_wait_ms": account_wait_ms,
            "index": index,
        })
        backend: OpenAIBackendAPI | None = None
        egress_acquired = False
        try:
            egress_started = time.perf_counter()
            fallback_profile = None
            using_fallback_profile = fallback_retry_pending
            fallback_retry_pending = False
            if using_fallback_profile:
                fallback_profile = proxy_settings.get_fallback_profile(
                    upstream=True,
                    reserve_image_egress=True,
                )
                if fallback_profile is None:
                    raise ImageGenerationError(
                        "fallback proxy is not configured",
                        status_code=502,
                        error_type="server_error",
                        code="connection_failed",
                        account_email=account_email,
                    )
            backend = OpenAIBackendAPI(
                access_token=token,
                proxy_profile=fallback_profile,
                reserve_image_egress=fallback_profile is None,
            )
            backend.cancel_checker = lambda: _raise_if_request_cancelled(request)
            if request.trace_image_perf:
                egress_data = _backend_egress_data(backend)
                if using_fallback_profile:
                    egress_data.update({
                        "fallback_retry": True,
                        "fallback_from_egress_key": fallback_from_egress.get("egress_key", ""),
                        "fallback_from_egress_label": fallback_from_egress.get("egress_label", ""),
                    })
                _monitor_image_stage(
                    request,
                    "image_egress_waiting",
                    account_email=account_email,
                    index=index,
                    total=total,
                    **egress_data,
                )
            egress_acquire_ms = proxy_settings.acquire_image_egress(backend.proxy_profile)
            egress_acquired = int(getattr(backend.proxy_profile, "image_concurrency_limit", 0) or 0) > 0
            egress_wait_ms = int((time.perf_counter() - egress_started) * 1000)
            if request.trace_image_perf:
                egress_data = _backend_egress_data(backend)
                if using_fallback_profile:
                    egress_data.update({
                        "fallback_retry": True,
                        "fallback_from_egress_key": fallback_from_egress.get("egress_key", ""),
                        "fallback_from_egress_label": fallback_from_egress.get("egress_label", ""),
                    })
                _monitor_image_stage(
                    request,
                    "image_egress_ready",
                    egress_wait_ms=egress_wait_ms,
                    egress_acquire_ms=egress_acquire_ms,
                    account_email=account_email,
                    index=index,
                    total=total,
                    **egress_data,
                )
                logger.debug({
                    "event": "image_egress_ready",
                    "call_id": request.call_id,
                    "model": request.model,
                    "index": index,
                    "total": total,
                    "account_email": account_email,
                    "egress_wait_ms": egress_wait_ms,
                    "egress_acquire_ms": egress_acquire_ms,
                    **egress_data,
                })
            if request.progress_callback or request.trace_image_perf:
                backend.progress_callback = _image_progress_callback_with_monitor(
                    request,
                    index,
                    total,
                    lambda: account_email,
                )
            stream_fn = stream_codex_image_outputs if is_codex_image_model(request.model) else stream_image_outputs
            outputs: list[ImageOutput] = []
            stream_started = time.perf_counter()
            for output in stream_fn(backend, request, index, total):
                _raise_if_request_cancelled(request)
                if account_email and not output.account_email:
                    output.account_email = account_email
                if output.kind == "message" and request.message_as_error:
                    status_code, error_type, code = _image_message_error_metadata(output.text)
                    raise ImageGenerationError(
                        output.text or "Image generation was rejected by upstream policy.",
                        status_code=status_code,
                        error_type=error_type,
                        code=code,
                        account_email=account_email,
                        conversation_id=output.conversation_id,
                        upstream_error=output.text or "",
                        raw_upstream_message=output.text or "",
                    )
                emitted_for_token = True
                returned_message = output.kind == "message"
                returned_result = returned_result or output.kind == "result"
                outputs.append(output)
            stream_ms = int((time.perf_counter() - stream_started) * 1000)
            if request.trace_image_perf:
                _monitor_image_stage(
                    request,
                    "image_single_stream_done",
                    stream_ms=stream_ms,
                    returned_message=returned_message,
                    returned_result=returned_result,
                    account_email=account_email,
                    index=index,
                    total=total,
                )
                logger.info({
                    "event": "image_single_stream_done",
                    "call_id": request.call_id,
                    "model": request.model,
                    "index": index,
                    "total": total,
                    "stream_ms": stream_ms,
                    "returned_message": returned_message,
                    "returned_result": returned_result,
                    "account_email": account_email,
                })
            if returned_message:
                message_text = outputs[-1].text if outputs else ""
                finalize_image_slot(
                    False,
                    refresh_account=should_refresh_image_account(message_text),
                )
                if request.trace_image_perf:
                    _monitor_image_stage(
                        request,
                        "image_single_done",
                        total_ms=int((time.perf_counter() - single_started) * 1000),
                        status="message",
                        account_email=account_email,
                        index=index,
                        total=total,
                    )
                    logger.info({
                        "event": "image_single_done",
                        "call_id": request.call_id,
                        "model": request.model,
                        "index": index,
                        "total": total,
                        "total_ms": int((time.perf_counter() - single_started) * 1000),
                        "status": "message",
                        "account_email": account_email,
                    })
                return outputs
            if not returned_result:
                finalize_image_slot(False)
                if emitted_for_token:
                    conv_id = outputs[-1].conversation_id if outputs else ""
                    raise ImageGenerationError(
                        "upstream completed without generating images",
                        status_code=502,
                        error_type="server_error",
                        code="no_image_generated",
                        account_email=account_email,
                        conversation_id=conv_id,
                    )
                return outputs
            _cleanup_image_conversations_after_success(backend, outputs)
            finalize_image_slot(True)
            if request.trace_image_perf:
                _monitor_image_stage(
                    request,
                    "image_single_done",
                    total_ms=int((time.perf_counter() - single_started) * 1000),
                    status="success",
                    account_email=account_email,
                    index=index,
                    total=total,
                )
                logger.info({
                    "event": "image_single_done",
                    "call_id": request.call_id,
                    "model": request.model,
                    "index": index,
                    "total": total,
                    "total_ms": int((time.perf_counter() - single_started) * 1000),
                    "status": "success",
                    "account_email": account_email,
                })
            return outputs
        except ImagePollTimeoutError as exc:
            if account_email:
                setattr(exc, "account_email", account_email)
            raw_error = str(exc)
            upstream_error = getattr(exc, "upstream_error", "") or getattr(exc, "last_task_error", "") or raw_error
            finalize_image_slot(
                False,
                refresh_account=should_refresh_image_account(upstream_error),
            )
            logger.warning({
                "event": "image_poll_timeout",
                "request_token": token,
                "account_email": account_email,
                "index": index,
                "error": str(exc)[:200],
                "upstream_error": str(upstream_error)[:1000] if upstream_error else "",
                "last_conversation_snapshot": getattr(exc, "last_conversation_snapshot", None),
            })
            image_error = ImageGenerationError(
                raw_error,
                status_code=502,
                error_type="server_error",
                code="image_poll_timeout",
                account_email=account_email,
                conversation_id=getattr(exc, "conversation_id", ""),
                raw_error=raw_error,
                upstream_error=str(upstream_error or ""),
                raw_upstream_message=str(getattr(exc, "last_assistant_text", "") or ""),
            )
            for attr in (
                "poll_attempts",
                "poll_timeout_secs",
                "last_task_error",
                "last_conversation_snapshot",
                "last_assistant_text",
            ):
                if hasattr(exc, attr):
                    setattr(image_error, attr, getattr(exc, attr))
            raise image_error from exc
        except RequestCancelledError as exc:
            finalize_image_slot(False)
            if request.trace_image_perf:
                _monitor_image_stage(
                    request,
                    "image_cancelled",
                    status="cancelled",
                    account_email=account_email,
                    index=index,
                    total=total,
                )
            raise ImageGenerationError(
                str(exc) or "request cancelled by administrator",
                status_code=499,
                error_type="server_error",
                code="request_cancelled",
                account_email=account_email,
            ) from exc
        except ImageContentPolicyError as exc:
            finalize_image_slot(False)
            if request.trace_image_perf:
                _monitor_image_stage(
                    request,
                    "image_stream_failed",
                    stream_error_ms=int((time.perf_counter() - stream_started) * 1000) if stream_started > 0 else 0,
                    account_email=account_email,
                    index=index,
                    total=total,
                    status="failed",
                    upstream_error=str(exc),
                )
            logger.warning({
                "event": "image_stream_content_policy_error",
                "request_token": token,
                "account_email": account_email,
                "error": diagnostic_excerpt(exc, 1000),
                "index": index,
            })
            raise ImageGenerationError(
                str(exc) or "Image generation was rejected by upstream policy.",
                status_code=400,
                error_type="invalid_request_error",
                code="content_policy_violation",
                account_email=account_email,
                conversation_id=getattr(exc, "conversation_id", ""),
                upstream_error=str(exc),
                raw_upstream_message=str(exc),
            ) from exc
        except ImageTextReplyError as exc:
            finalize_image_slot(False)
            text_reply = str(exc) or "上游返回了文本回复而不是图片。"
            if request.trace_image_perf:
                _monitor_image_stage(
                    request,
                    "image_text_reply",
                    stream_error_ms=int((time.perf_counter() - stream_started) * 1000) if stream_started > 0 else 0,
                    account_email=account_email,
                    index=index,
                    total=total,
                    status="failed",
                    upstream_error=text_reply,
                )
            logger.info({
                "event": "image_stream_text_reply",
                "request_token": token,
                "account_email": account_email,
                "conversation_id": getattr(exc, "conversation_id", ""),
                "message_preview": diagnostic_excerpt(text_reply, 1000),
                "index": index,
            })
            image_error = ImageGenerationError(
                text_reply,
                status_code=400,
                error_type="invalid_request_error",
                code="upstream_text_reply",
                account_email=account_email,
                conversation_id=getattr(exc, "conversation_id", ""),
                raw_error=text_reply,
                upstream_error=str(getattr(exc, "upstream_error", "") or text_reply),
                raw_upstream_message=str(getattr(exc, "raw_upstream_message", "") or text_reply),
            )
            for attr in (
                "upstream_message_preview",
                "last_conversation_snapshot",
                "last_assistant_text",
            ):
                if hasattr(exc, attr):
                    setattr(image_error, attr, getattr(exc, attr))
            raise image_error from exc
        except ImageGenerationError as exc:
            if account_email and not getattr(exc, "account_email", ""):
                exc.account_email = account_email
            error_text = str(exc)
            finalize_image_slot(
                False,
                refresh_account=should_refresh_image_account(error_text),
            )
            if request.trace_image_perf:
                _monitor_image_stage(
                    request,
                    "image_stream_failed",
                    stream_error_ms=int((time.perf_counter() - stream_started) * 1000) if stream_started > 0 else 0,
                    account_email=account_email,
                    index=index,
                    total=total,
                    status="failed",
                    upstream_error=error_text,
                )
            logger.warning({
                "event": "image_stream_generation_error",
                "request_token": token,
                "account_email": account_email,
                "error": diagnostic_excerpt(error_text, 1000),
                "index": index,
            })
            raise
        except Exception as exc:
            last_error = str(exc)
            stream_error_ms = int((time.perf_counter() - stream_started) * 1000) if stream_started > 0 else 0
            http_timing = _backend_http_timing_data(backend)
            if request.trace_image_perf and stream_error_ms > 0:
                _monitor_image_stage(
                    request,
                    "image_stream_failed",
                    stream_error_ms=stream_error_ms,
                    stream_ms=stream_error_ms,
                    account_email=account_email,
                    index=index,
                    total=total,
                    status="failed",
                    upstream_error=last_error,
                    **http_timing,
                )
            logger.warning({
                "event": "image_stream_fail",
                "request_token": token,
                "account_email": account_email,
                "error": diagnostic_excerpt(last_error, 1000),
                "stream_error_ms": stream_error_ms,
                "index": index,
                **http_timing,
            })
            if not emitted_for_token and is_token_invalid_error(last_error):
                finalize_image_slot(False)
                refreshed_token = account_service.refresh_access_token(token, force=True, event="image_stream")
                if refreshed_token and refreshed_token != token:
                    token = refreshed_token
                    continue
                account_service.handle_invalid_token(token, "image_stream", error=last_error)
                continue
            quick_timeout_retry_ms = min(30000, max(5000, int(config.image_stream_timeout_secs * 1000 * 0.2)))
            early_connection_failure = (
                not emitted_for_token
                and (stream_error_ms == 0 or stream_error_ms <= quick_timeout_retry_ms)
                and (is_tls_connection_error(last_error) or is_connection_timeout_error(last_error))
            )
            fallback_reference = proxy_settings.get_fallback_proxy_reference()
            if early_connection_failure and fallback_reference and not fallback_retry_used:
                fallback_retry_used = True
                fallback_retry_pending = True
                retry_token = token
                fallback_from_egress = _backend_egress_data(backend) if backend is not None else {}
                logger.warning({
                    "event": "image_stream_fallback_retry",
                    "request_token": token,
                    "account_email": account_email,
                    "index": index,
                    "fallback_proxy_configured": True,
                    "fallback_from_egress_key": fallback_from_egress.get("egress_key", ""),
                    "fallback_from_egress_label": fallback_from_egress.get("egress_label", ""),
                    "stream_error_ms": stream_error_ms,
                    "error": last_error[:200],
                })
                if request.trace_image_perf:
                    _monitor_image_stage(
                        request,
                        "image_egress_fallback_retry",
                        account_email=account_email,
                        index=index,
                        total=total,
                        status="retrying",
                        fallback_from_egress_key=fallback_from_egress.get("egress_key", ""),
                        fallback_from_egress_label=fallback_from_egress.get("egress_label", ""),
                    )
                continue
            finalize_image_slot(
                False,
                refresh_account=should_refresh_image_account(last_error),
            )
            raise ImageGenerationError(
                image_stream_error_message(last_error),
                account_email=account_email,
                conversation_id="",
                raw_error=last_error,
                upstream_error=last_error,
            ) from exc
        finally:
            if egress_acquired and backend is not None:
                proxy_settings.release_image_egress(backend.proxy_profile)
            if backend is not None:
                backend.close()


def stream_image_outputs_with_pool(request: ConversationRequest) -> Iterator[ImageOutput]:
    """并行生成多张图片，每张图片使用独立线程和账号，互不阻塞。"""
    if not is_supported_image_model(request.model):
        _monitor_image_stage(
            request,
            "image_local_rejected",
            local_reason="unsupported_model",
            status="failed",
        )
        raise ImageGenerationError("unsupported image model,supported models: " + ", ".join(sorted(IMAGE_MODELS)))

    if request.n <= 1:
        # 单张图片，直接执行（无需线程池开销）
        outputs = _generate_single_image(request, 1, 1)
        for output in outputs:
            yield output
        return

    # 多张图片：根据配置选择并行或串行执行
    if not config.image_parallel_generation:
        logger.info({
            "event": "image_serial_generation_start",
            "n": request.n,
            "model": request.model,
        })
        for index in range(1, request.n + 1):
            outputs = _generate_single_image(request, index, request.n)
            for output in outputs:
                yield output
        return

    logger.info({
        "event": "image_parallel_generation_start",
        "n": request.n,
        "model": request.model,
    })
    # 每张图片一个线程，同时启动
    futures = {}
    results: dict[int, list[ImageOutput]] = {}
    errors: dict[int, Exception] = {}
    with ThreadPoolExecutor(max_workers=request.n) as executor:
        for index in range(1, request.n + 1):
            future = executor.submit(_generate_single_image, request, index, request.n)
            futures[future] = index

        # yield 结果：按完成顺序立即输出，不再等所有图片都结束后才返回成功结果。
        emitted = False
        last_error = ""

        for future in as_completed(futures):
            index = futures[future]
            try:
                outputs = future.result()
                results[index] = outputs
                for output in outputs:
                    emitted = True
                    yield output
            except Exception as exc:
                errors[index] = exc
                last_error = str(exc)
                logger.warning({
                    "event": "image_parallel_generation_error",
                    "index": index,
                    "error": last_error[:300],
                })
                if not emitted:
                    logger.warning({
                        "event": "image_parallel_failure_before_success",
                        "failed_index": index,
                        "error": last_error[:200],
                    })

    # 如果有失败但也有成功，记录警告
    if emitted:
        for index in range(1, request.n + 1):
            if index in errors:
                logger.warning({
                    "event": "image_parallel_partial_failure",
                    "failed_index": index,
                    "error": str(errors[index])[:200],
                })

    if not emitted:
        if not last_error:
            last_error = "no account in the pool could generate images — check account quota and rate-limit status"
        raise ImageGenerationError(
            image_stream_error_message(last_error),
            conversation_id="",
            raw_error=last_error,
            upstream_error=last_error,
        )


def _image_stream_payload(output: ImageOutput, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    item = {"type": event_type, **payload}
    if output.account_email:
        item["_account_email"] = output.account_email
    if output.conversation_id:
        item["_conversation_id"] = output.conversation_id
    return item


def stream_image_chunks(
    outputs: Iterable[ImageOutput],
    event_prefix: str = "image_generation",
    usage_builder: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    partial_images: object = 0,
) -> Iterator[dict[str, Any]]:
    prefix = str(event_prefix or "image_generation").strip() or "image_generation"
    # ChatGPT Web only gives us final image bytes here. Emitting those bytes as a
    # synthetic partial_image makes some clients display the same image twice.
    for output in outputs:
        if output.kind == "result":
            for item_index, item in enumerate(output.data):
                if not isinstance(item, dict):
                    continue
                b64_json = str(item.get("b64_json") or "").strip()
                if not b64_json:
                    continue
                completed: dict[str, Any] = {"b64_json": b64_json}
                if usage_builder:
                    usage = usage_builder([item])
                    if usage:
                        completed["usage"] = usage
                completed_payload = _image_stream_payload(output, f"{prefix}.completed", completed)
                image_url = str(item.get("url") or "").strip()
                if image_url:
                    completed_payload["_image_urls"] = [image_url]
                yield completed_payload
        elif output.kind == "message" and output.text:
            yield _image_stream_payload(
                output,
                f"{prefix}.failed",
                {"error": {"message": output.text, "type": "image_generation_error"}},
            )


def collect_image_outputs(outputs: Iterable[ImageOutput]) -> dict[str, Any]:
    created = None
    data: list[dict[str, Any]] = []
    message = ""
    progress_parts: list[str] = []
    account_email = ""
    for output in outputs:
        created = created or output.created
        if output.account_email and not account_email:
            account_email = output.account_email
        if output.kind == "progress" and output.text:
            progress_parts.append(output.text)
        elif output.kind == "message":
            message = output.text
        elif output.kind == "result":
            data.extend(output.data)

    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if not data:
        text = message or "".join(progress_parts).strip()
        if text:
            result["message"] = text
    if account_email:
        result["_account_email"] = account_email
    return result
