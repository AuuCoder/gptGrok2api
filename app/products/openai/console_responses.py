"""Console Responses API handler — /v1/responses for console.x.ai models.

将 console.x.ai 上游的 Responses API SSE 事件流转换为 OpenAI Responses API 格式输出。
由于上游本身就是 Responses API 格式，这里主要做：
1. 账号选择 + 重试
2. 过滤/转换 SSE 事件（去掉 encrypted reasoning，保留文本 delta）
3. 包装成标准 Responses API 输出
"""

import asyncio
from typing import Any, AsyncGenerator

import orjson

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.platform.errors import RateLimitError, UpstreamError
from app.platform.runtime.clock import now_s
from app.platform.tokens import estimate_prompt_tokens, estimate_tokens
from app.control.account.enums import FeedbackKind
from app.control.account.invalid_credentials import feedback_kind_for_error
from app.control.account.runtime import get_refresh_service
from app.control.model.registry import resolve as resolve_model
from app.dataplane.account.selector import current_strategy
from app.dataplane.reverse.protocol.xai_console_chat import (
    build_console_payload,
    ConsoleStreamAdapter,
    stream_console_chat,
    validate_console_request,
)
from app.products._account_selection import reserve_account, selection_max_retries
from app.products.openai.chat import _configured_retry_codes, _should_retry_upstream
from ._format import (
    make_resp_id,
    make_resp_object,
    build_resp_usage,
    format_sse,
)


def _log_task_exception(task: "asyncio.Task") -> None:
    exc = task.exception() if not task.cancelled() else None
    if exc:
        logger.warning("background task failed: task={} error={}", task.get_name(), exc)


async def _quota_sync(token: str, mode_id: int) -> None:
    """Fire-and-forget: 成功调用后持久化配额扣减和 usage_use_count。

    Console 配额(mode_id=5)为本地管理，不依赖上游 API，
    无论 random/quota 策略都需要执行扣减和窗口重置。
    """
    try:
        if current_strategy() != "quota" and mode_id != 5:
            return
        svc = get_refresh_service()
        if svc:
            await svc.refresh_call_async(token, mode_id)
    except Exception as exc:
        logger.warning(
            "console responses quota sync failed: token={}... mode_id={} error={}",
            token[:10],
            mode_id,
            exc,
        )


async def _fail_sync(token: str, mode_id: int, exc: BaseException | None = None) -> None:
    """Fire-and-forget: 失败后持久化失败计数。"""
    try:
        svc = get_refresh_service()
        if svc:
            await svc.record_failure_async(token, mode_id, exc)
    except Exception as e:
        logger.warning(
            "console responses fail sync error: token={}... mode_id={} error={}",
            token[:10],
            mode_id,
            e,
        )


async def create(
    *,
    model: str,
    messages: list[dict],
    stream: bool,
    emit_think: bool,
    temperature: float,
    top_p: float,
    response_id: str,
    reasoning_id: str,
    message_id: str,
    tools: list[dict] | None = None,
    tool_choice: Any = None,
    max_output_tokens: int | None = None,
) -> dict | AsyncGenerator[str, None]:
    """Console models /v1/responses handler."""

    validate_console_request(
        messages,
        tools=tools,
        tool_choice=tool_choice,
        max_output_tokens=max_output_tokens,
    )

    cfg = get_config()
    spec = resolve_model(model)
    timeout_s = cfg.get_float("chat.timeout", 120.0)
    max_retries = selection_max_retries()
    retry_codes = _configured_retry_codes(cfg)

    # reasoning effort 映射
    effort = "low" if emit_think else "none"

    from app.dataplane.account import _directory as _acct_dir
    if _acct_dir is None:
        raise RateLimitError("Account directory not initialised")
    directory = _acct_dir

    # ── Streaming ─────────────────────────────────────────────────────────────
    if stream:
        async def _run_stream() -> AsyncGenerator[str, None]:
            excluded: list[str] = []
            for attempt in range(max_retries + 1):
                acct, selected_mode_id = await reserve_account(
                    directory, spec, now_s_override=now_s(),
                    exclude_tokens=excluded or None,
                )
                if acct is None:
                    raise RateLimitError("No available accounts for this model tier")

                token = acct.token
                success = False
                fail_exc: BaseException | None = None
                _retry = False
                adapter = ConsoleStreamAdapter()
                text_buf: list[str] = []

                try:
                    payload = build_console_payload(
                        messages=messages,
                        model=model,
                        temperature=temperature,
                        top_p=top_p,
                        reasoning_effort=effort,
                        max_output_tokens=max_output_tokens,
                        stream=True,
                    )

                    try:
                        # response.created
                        yield format_sse("response.created", {
                            "type": "response.created",
                            "response": make_resp_object(response_id, model, "in_progress", []),
                        })

                        # response.in_progress
                        yield format_sse("response.in_progress", {
                            "type": "response.in_progress",
                            "response": make_resp_object(response_id, model, "in_progress", []),
                        })

                        # output_item.added (message)
                        yield format_sse("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {
                                "id": message_id,
                                "type": "message",
                                "role": "assistant",
                                "status": "in_progress",
                                "content": [],
                            },
                        })

                        # content_part.added
                        yield format_sse("response.content_part.added", {
                            "type": "response.content_part.added",
                            "item_id": message_id,
                            "output_index": 0,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        })

                        event_count = 0
                        yield ": heartbeat\n\n"
                        async for event_type, data in stream_console_chat(
                            token, payload, timeout_s=timeout_s
                        ):
                            event_count += 1
                            tokens = adapter.feed(event_type, data)
                            for tok in tokens:
                                text_buf.append(tok)
                                yield format_sse("response.output_text.delta", {
                                    "type": "response.output_text.delta",
                                    "item_id": message_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "delta": tok,
                                })

                        logger.info(
                            "console responses stream raw: events={} text_tokens={} adapter_text_len={}",
                            event_count, len(text_buf), len(adapter.full_text),
                        )

                        # 流结束
                        full_text = "".join(text_buf)

                        # output_text.done
                        yield format_sse("response.output_text.done", {
                            "type": "response.output_text.done",
                            "item_id": message_id,
                            "output_index": 0,
                            "content_index": 0,
                            "text": full_text,
                        })

                        # content_part.done
                        yield format_sse("response.content_part.done", {
                            "type": "response.content_part.done",
                            "item_id": message_id,
                            "output_index": 0,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": full_text, "annotations": []},
                        })

                        # output_item.done
                        yield format_sse("response.output_item.done", {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": {
                                "id": message_id,
                                "type": "message",
                                "role": "assistant",
                                "status": "completed",
                                "content": [{"type": "output_text", "text": full_text}],
                            },
                        })

                        # usage
                        usage_data = adapter.usage
                        input_tokens = (
                            usage_data.get("input_tokens", 0) if usage_data
                            else estimate_prompt_tokens(messages)
                        )
                        output_tokens = (
                            usage_data.get("output_tokens", 0) if usage_data
                            else estimate_tokens(full_text)
                        )

                        # response.completed
                        output_items = [{
                            "id": message_id,
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": full_text}],
                        }]
                        yield format_sse("response.completed", {
                            "type": "response.completed",
                            "response": make_resp_object(
                                response_id, model, "completed", output_items,
                                usage=build_resp_usage(input_tokens, output_tokens),
                            ),
                        })
                        yield "data: [DONE]\n\n"
                        success = True
                        logger.info(
                            "console responses stream completed: model={} text_len={} attempt={}/{}",
                            model, len(full_text), attempt + 1, max_retries + 1,
                        )

                    except UpstreamError as exc:
                        fail_exc = exc
                        if _should_retry_upstream(exc, retry_codes) and attempt < max_retries:
                            _retry = True
                            logger.warning(
                                "console responses retry: attempt={}/{} status={}",
                                attempt + 1, max_retries, exc.status,
                            )
                        else:
                            raise

                finally:
                    await directory.release(acct)
                    kind = (
                        FeedbackKind.SUCCESS if success
                        else feedback_kind_for_error(fail_exc) if fail_exc
                        else FeedbackKind.SERVER_ERROR
                    )
                    await directory.feedback(token, kind, selected_mode_id, now_s_val=now_s())
                    if success:
                        asyncio.create_task(
                            _quota_sync(token, selected_mode_id)
                        ).add_done_callback(_log_task_exception)
                    else:
                        asyncio.create_task(
                            _fail_sync(token, selected_mode_id, fail_exc)
                        ).add_done_callback(_log_task_exception)

                if success or not _retry:
                    return
                excluded.append(token)

        return _run_stream()

    # ── Non-streaming ─────────────────────────────────────────────────────────
    excluded: list[str] = []
    for attempt in range(max_retries + 1):
        acct, selected_mode_id = await reserve_account(
            directory, spec, now_s_override=now_s(),
            exclude_tokens=excluded or None,
        )
        if acct is None:
            raise RateLimitError("No available accounts for this model tier")

        token = acct.token
        success = False
        fail_exc: BaseException | None = None
        adapter = ConsoleStreamAdapter()

        try:
            payload = build_console_payload(
                messages=messages,
                model=model,
                temperature=temperature,
                top_p=top_p,
                reasoning_effort=effort,
                max_output_tokens=max_output_tokens,
                stream=True,
            )

            try:
                async for event_type, data in stream_console_chat(
                    token, payload, timeout_s=timeout_s
                ):
                    adapter.feed(event_type, data)

                full_text = adapter.full_text
                usage_data = adapter.usage
                input_tokens = (
                    usage_data.get("input_tokens", 0) if usage_data
                    else estimate_prompt_tokens(messages)
                )
                output_tokens = (
                    usage_data.get("output_tokens", 0) if usage_data
                    else estimate_tokens(full_text)
                )

                output_items = [{
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": full_text}],
                }]
                result = make_resp_object(
                    response_id, model, "completed", output_items,
                    usage=build_resp_usage(input_tokens, output_tokens),
                )
                success = True
                logger.info(
                    "console responses non-stream completed: model={} text_len={}",
                    model, len(full_text),
                )
                return result

            except UpstreamError as exc:
                fail_exc = exc
                if _should_retry_upstream(exc, retry_codes) and attempt < max_retries:
                    logger.warning(
                        "console responses non-stream retry: attempt={}/{} status={}",
                        attempt + 1, max_retries, exc.status,
                    )
                    excluded.append(token)
                    continue
                raise

        finally:
            await directory.release(acct)
            kind = (
                FeedbackKind.SUCCESS if success
                else feedback_kind_for_error(fail_exc) if fail_exc
                else FeedbackKind.SERVER_ERROR
            )
            await directory.feedback(token, kind, selected_mode_id, now_s_val=now_s())
            if success:
                asyncio.create_task(
                    _quota_sync(token, selected_mode_id)
                ).add_done_callback(_log_task_exception)
            else:
                asyncio.create_task(
                    _fail_sync(token, selected_mode_id, fail_exc)
                ).add_done_callback(_log_task_exception)

    raise RateLimitError("No available accounts after retries")


__all__ = ["create"]
