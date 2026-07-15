"""Console chat completion service — routes to console.x.ai/v1/responses.

通过 console.x.ai 端点访问 grok-4.3 / grok-4 等模型，
使用 grok.com SSO token 认证，免费账号可用。

与 chat.py 的区别：
- 不走 grok.com 的 app-chat SSE 端点
- 不消耗 grok.com 配额窗口
- 响应格式是标准 OpenAI Responses API SSE 事件流
- thinking 内容以 encrypted_content 形式返回（不可读，不透传）
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
    make_response_id,
    make_stream_chunk,
    make_thinking_chunk,
    make_chat_response,
    build_usage,
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
            "console quota sync failed: token={}... mode_id={} error={}",
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
            "console fail sync error: token={}... mode_id={} error={}",
            token[:10],
            mode_id,
            e,
        )


def _reasoning_effort_from_emit_think(emit_think: bool | None) -> str:
    """将 emit_think 标志映射到 console API 的 reasoning effort。"""
    if emit_think is False:
        return "none"
    return "low"  # 默认 low，节省 token


async def completions(
    *,
    model: str,
    messages: list[dict],
    stream: bool = True,
    emit_think: bool | None = None,
    tools: list[dict] | None = None,
    tool_choice: Any = None,
    max_output_tokens: int | None = None,
    temperature: float = 0.7,
    top_p: float = 0.95,
) -> dict | AsyncGenerator[str, None]:
    """Entry point for console.x.ai chat completions.

    Returns an async generator for streaming, or a dict for non-streaming.
    """
    validate_console_request(
        messages,
        tools=tools,
        tool_choice=tool_choice,
        max_output_tokens=max_output_tokens,
    )

    cfg = get_config()
    spec = resolve_model(model)
    effort = _reasoning_effort_from_emit_think(emit_think)
    timeout_s = cfg.get_float("chat.timeout", 120.0)
    max_retries = selection_max_retries()
    retry_codes = _configured_retry_codes(cfg)
    response_id = make_response_id()

    logger.info(
        "console chat request: model={} stream={} messages={}",
        model, stream, len(messages),
    )

    from app.dataplane.account import _directory as _acct_dir
    if _acct_dir is None:
        raise RateLimitError("Account directory not initialised")
    directory = _acct_dir

    # ── Streaming path ────────────────────────────────────────────────────────
    if stream:
        async def _run_stream() -> AsyncGenerator[str, None]:
            excluded: list[str] = []
            for attempt in range(max_retries + 1):
                acct, selected_mode_id = await reserve_account(
                    directory,
                    spec,
                    now_s_override=now_s(),
                    exclude_tokens=excluded or None,
                )
                if acct is None:
                    raise RateLimitError("No available accounts for this model tier")

                token = acct.token
                success = False
                fail_exc: BaseException | None = None
                _retry = False
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
                        yield ": heartbeat\n\n"
                        async for event_type, data in stream_console_chat(
                            token, payload, timeout_s=timeout_s
                        ):
                            tokens = adapter.feed(event_type, data)
                            for tok in tokens:
                                chunk = make_stream_chunk(response_id, model, tok)
                                yield f"data: {orjson.dumps(chunk).decode()}\n\n"

                        # 流结束，发送 final chunk
                        usage_data = adapter.usage
                        prompt_tokens = (
                            usage_data.get("input_tokens", 0) if usage_data else
                            estimate_prompt_tokens(messages)
                        )
                        completion_tokens = (
                            usage_data.get("output_tokens", 0) if usage_data else
                            estimate_tokens(adapter.full_text)
                        )
                        usage = build_usage(prompt_tokens, completion_tokens)
                        final = make_stream_chunk(
                            response_id, model, "", is_final=True
                        )
                        final["usage"] = usage
                        yield f"data: {orjson.dumps(final).decode()}\n\n"
                        yield "data: [DONE]\n\n"
                        success = True
                        logger.info(
                            "console chat stream completed: attempt={}/{} model={} tokens={}",
                            attempt + 1, max_retries + 1, model,
                            (usage_data or {}).get("total_tokens", "?"),
                        )

                    except UpstreamError as exc:
                        fail_exc = exc
                        if _should_retry_upstream(exc, retry_codes) and attempt < max_retries:
                            _retry = True
                            logger.warning(
                                "console chat retry: attempt={}/{} status={} token={}...",
                                attempt + 1, max_retries, exc.status, token[:8],
                            )
                        else:
                            logger.warning(
                                "console chat upstream failed: model={} status={} attempt={}/{}",
                                model, exc.status, attempt + 1, max_retries + 1,
                            )
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

    # ── Non-streaming path ────────────────────────────────────────────────────
    excluded: list[str] = []
    for attempt in range(max_retries + 1):
        acct, selected_mode_id = await reserve_account(
            directory,
            spec,
            now_s_override=now_s(),
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
                stream=True,  # 始终用流式，非流式在本地聚合
            )

            try:
                async for event_type, data in stream_console_chat(
                    token, payload, timeout_s=timeout_s
                ):
                    adapter.feed(event_type, data)

                usage_data = adapter.usage
                prompt_tokens = (
                    usage_data.get("input_tokens", 0) if usage_data else
                    estimate_prompt_tokens(messages)
                )
                completion_tokens = (
                    usage_data.get("output_tokens", 0) if usage_data else
                    estimate_tokens(adapter.full_text)
                )
                usage = build_usage(prompt_tokens, completion_tokens)
                result = make_chat_response(
                    model, adapter.full_text, response_id=response_id, usage=usage
                )
                success = True
                logger.info(
                    "console chat non-stream completed: model={} tokens={}",
                    model, (usage_data or {}).get("total_tokens", "?"),
                )
                return result

            except UpstreamError as exc:
                fail_exc = exc
                if _should_retry_upstream(exc, retry_codes) and attempt < max_retries:
                    logger.warning(
                        "console chat non-stream retry: attempt={}/{} status={}",
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


__all__ = ["completions"]
