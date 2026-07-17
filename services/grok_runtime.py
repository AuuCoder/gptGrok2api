from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import orjson
from fastapi import FastAPI
from fastapi.responses import Response

from app.platform.runtime.batch import run_batch
from services.register.grok_account_store import grok_account_store


T = TypeVar("T")


GROK_ACCOUNT_CHAT_TEST_MODEL = "grok-4.3-console"
GROK_ACCOUNT_CHAT_TEST_MAX_OUTPUT_TOKENS = 64
GROK_ACCOUNT_CHAT_TEST_TIMEOUT_S = 45.0
GROK_ACCOUNT_REFRESH_MAX_CONCURRENCY = 15


def _response_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, Response):
        try:
            payload = orjson.loads(bytes(value.body or b"{}"))
        except Exception as exc:
            raise RuntimeError("内置 Grok 运行时返回了无效响应") from exc
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("内置 Grok 运行时返回了未知响应")


class EmbeddedGrokRuntime:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._host_app: FastAPI | None = None
        self._ready = threading.Event()

    @property
    def available(self) -> bool:
        return self._ready.is_set() and self._loop is not None and self._host_app is not None

    def _state(self) -> tuple[Any, Any]:
        if not self.available or self._host_app is None:
            raise RuntimeError("内置 Grok 运行时尚未启动")
        repo = getattr(self._host_app.state, "repository", None)
        refresh_service = getattr(self._host_app.state, "refresh_service", None)
        if repo is None or refresh_service is None:
            raise RuntimeError("内置 Grok 账号服务尚未就绪")
        return repo, refresh_service

    @asynccontextmanager
    async def lifespan(self, host_app: FastAPI):
        from app.main import lifespan as grok_lifespan

        self._loop = asyncio.get_running_loop()
        self._host_app = host_app
        async with grok_lifespan(host_app):
            await self.import_registered_accounts()
            self._ready.set()
            try:
                await self.sync_host_accounts_from_runtime()
                yield
            finally:
                self._ready.clear()
        self._host_app = None
        self._loop = None

    def run_sync(
        self,
        factory: Callable[[], Awaitable[T]],
        *,
        timeout: float = 90.0,
    ) -> T:
        if not self.available or self._loop is None:
            raise RuntimeError("内置 Grok 运行时尚未启动")
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self._loop:
            raise RuntimeError("内置 Grok 同步操作必须在线程池中执行")

        future = asyncio.run_coroutine_threadsafe(factory(), self._loop)
        try:
            return future.result(timeout=max(1.0, float(timeout)))
        except FutureTimeoutError as exc:
            future.cancel()
            raise RuntimeError("内置 Grok 运行时操作超时") from exc

    async def import_registered_accounts(self) -> dict[str, int]:
        if self._host_app is None:
            return {"added": 0, "skipped": 0}

        repo = getattr(self._host_app.state, "repository", None)
        directory = getattr(self._host_app.state, "directory", None)
        if repo is None:
            return {"added": 0, "skipped": 0}

        items = await asyncio.to_thread(grok_account_store.list_accounts, redacted=False)
        tokens = list(
            dict.fromkeys(
                str(item.get("sso") or "").strip()
                for item in items
                if str(item.get("status") or "active").strip().lower() == "active"
                and not (
                    isinstance(item.get("runtime"), dict)
                    and item["runtime"].get("present") is False
                )
                and str(item.get("sso") or "").strip()
            )
        )
        if not tokens:
            return {"added": 0, "skipped": 0}

        from app.control.account.commands import AccountUpsert

        existing = {
            record.token
            for record in await repo.get_accounts(tokens)
            if not record.is_deleted()
        }
        new_tokens = [token for token in tokens if token not in existing]
        if new_tokens:
            await repo.upsert_accounts(
                [AccountUpsert(token=token, pool="basic", tags=["registered"]) for token in new_tokens]
            )
            if directory is not None:
                await directory.sync_if_changed()
        return {"added": len(new_tokens), "skipped": len(existing)}

    async def sync_host_accounts_from_runtime(self) -> dict[str, int]:
        """Persist a credential-free view of the runtime pool in the host archive."""
        payload = await self.list_accounts()
        tokens = payload.get("tokens") if isinstance(payload, dict) else []
        runtime_items = [dict(item) for item in tokens if isinstance(item, dict)] if isinstance(tokens, list) else []
        return await asyncio.to_thread(grok_account_store.reconcile_runtime_accounts, runtime_items)

    async def list_accounts(self) -> dict[str, Any]:
        from app.products.web.admin.tokens import list_tokens

        repo, _ = self._state()
        return _response_payload(await list_tokens(repo=repo))

    async def account_stats(self) -> dict[str, Any]:
        """Return host-compatible account and image-quota statistics."""
        from app.control.account.enums import AccountStatus
        from app.control.account.quota_defaults import supports_mode
        from app.control.account.state_machine import derive_status
        from app.control.model import registry as model_registry

        repo, _ = self._state()
        snapshot = await repo.runtime_snapshot()
        records = [record for record in snapshot.items if not record.is_deleted()]
        pool_names = {0: "basic", 1: "super", 2: "heavy"}
        image_modes_by_pool: dict[str, set[int]] = {pool: set() for pool in pool_names.values()}
        for spec in model_registry.list_enabled():
            if not (spec.is_image() or spec.is_image_edit()):
                continue
            mode_id = int(spec.mode_id)
            for pool_id in spec.pool_candidates():
                pool = pool_names[pool_id]
                if supports_mode(pool, mode_id):
                    image_modes_by_pool[pool].add(mode_id)

        counts = {
            AccountStatus.ACTIVE: 0,
            AccountStatus.COOLING: 0,
            AccountStatus.EXPIRED: 0,
            AccountStatus.DISABLED: 0,
        }
        by_type: dict[str, int] = {}
        quota_by_mode: dict[str, int] = {}
        total_quota = 0
        unknown_quota_count = 0

        for record in records:
            status = derive_status(record)
            counts[status] = counts.get(status, 0) + 1
            by_type[record.pool] = by_type.get(record.pool, 0) + 1
            if status != AccountStatus.ACTIVE:
                continue

            quota_set = record.quota_set()
            modes = image_modes_by_pool.get(record.pool, set())
            found_window = False
            for mode_id in modes:
                window = quota_set.get(mode_id)
                if window is None:
                    continue
                found_window = True
                remaining = max(0, int(window.remaining or 0))
                total_quota += remaining
                mode_key = str(mode_id)
                quota_by_mode[mode_key] = quota_by_mode.get(mode_key, 0) + remaining
            if modes and not found_window:
                unknown_quota_count += 1

        active = counts[AccountStatus.ACTIVE]
        return {
            "total": len(records),
            "cumulative_total": len(records),
            "active": active,
            "limited": counts[AccountStatus.COOLING],
            "abnormal": counts[AccountStatus.EXPIRED],
            "disabled": counts[AccountStatus.DISABLED],
            "total_quota": total_quota,
            "unlimited_quota_count": 0,
            "unknown_quota_count": unknown_quota_count,
            "total_success": sum(max(0, int(record.usage_use_count or 0)) for record in records),
            "total_fail": sum(max(0, int(record.usage_fail_count or 0)) for record in records),
            "by_type": by_type,
            "quota_by_mode": quota_by_mode,
            "healthy": active > 0,
        }

    async def model_catalog(self) -> dict[str, Any]:
        from app.control.account.quota_defaults import supports_mode
        from app.control.account.state_machine import is_manageable
        from app.control.model import registry as model_registry

        repo, _ = self._state()
        snapshot = await repo.runtime_snapshot()
        pools = {record.pool for record in snapshot.items if is_manageable(record)}
        items: list[dict[str, Any]] = []
        chat_models: list[str] = []
        image_models: list[str] = []
        image_edit_models: list[str] = []
        video_models: list[str] = []

        for spec in model_registry.list_enabled():
            available = any(
                (pool := {0: "basic", 1: "super", 2: "heavy"}[pool_id]) in pools
                and supports_mode(pool, int(spec.mode_id))
                for pool_id in spec.pool_candidates()
            )
            if not available:
                continue

            capabilities: list[str] = []
            if spec.is_chat() or spec.is_console_chat():
                capabilities.append("chat")
                chat_models.append(spec.model_name)
            if spec.is_image():
                capabilities.append("image")
                image_models.append(spec.model_name)
            if spec.is_image_edit():
                capabilities.append("image_edit")
                image_edit_models.append(spec.model_name)
            if spec.is_video():
                capabilities.append("video")
                video_models.append(spec.model_name)

            items.append(
                {
                    "id": spec.model_name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "xai",
                    "name": spec.public_name,
                    "provider": "grok",
                    "capabilities": capabilities,
                }
            )

        return {
            "items": items,
            "chat_models": chat_models,
            "image_models": image_models,
            "image_edit_models": image_edit_models,
            "video_models": video_models,
        }

    async def generate_image(
        self,
        payload: dict[str, Any],
        *,
        trace: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from api import grok
        from app.platform.request_context import (
            reset_grok_log_trace,
            reset_media_base_url,
            set_grok_log_trace,
            set_media_base_url,
        )

        body = dict(payload)
        body["stream"] = False
        media_token = set_media_base_url(str(body.get("base_url") or ""))
        trace_token = set_grok_log_trace(trace) if trace is not None else None
        try:
            return _response_payload(await grok.dispatch_image_generation(body))
        finally:
            if trace_token is not None:
                reset_grok_log_trace(trace_token)
            reset_media_base_url(media_token)

    async def edit_image(
        self,
        payload: dict[str, Any],
        *,
        trace: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from api import grok
        from app.platform.request_context import (
            reset_grok_log_trace,
            reset_media_base_url,
            set_grok_log_trace,
            set_media_base_url,
        )

        body = dict(payload)
        body["stream"] = False
        media_token = set_media_base_url(str(body.get("base_url") or ""))
        trace_token = set_grok_log_trace(trace) if trace is not None else None
        try:
            return _response_payload(await grok.dispatch_image_edit(body))
        finally:
            if trace_token is not None:
                reset_grok_log_trace(trace_token)
            reset_media_base_url(media_token)

    async def add_accounts(
        self,
        tokens: list[str],
        *,
        pool: str,
        tags: list[str],
        auto_nsfw: bool,
    ) -> dict[str, Any]:
        from app.products.web.admin.tokens import AddTokensRequest, add_tokens

        repo, refresh_service = self._state()
        response = await add_tokens(
            AddTokensRequest(tokens=tokens, pool=pool, tags=tags),
            auto_nsfw=auto_nsfw,
            repo=repo,
            refresh_svc=refresh_service,
        )
        payload = _response_payload(response)
        await self.sync_host_accounts_from_runtime()
        return payload

    async def refresh_accounts(self, tokens: list[str]) -> dict[str, Any]:
        from app.products.web.admin.batch import BatchRequest, batch_refresh
        from app.platform.config.snapshot import get_config

        repo, refresh_service = self._state()
        try:
            configured_concurrency = int(get_config("account.refresh.usage_concurrency", 15) or 15)
        except (TypeError, ValueError):
            configured_concurrency = 15
        configured_concurrency = max(1, configured_concurrency)
        response = await batch_refresh(
            BatchRequest(tokens=tokens),
            async_mode=False,
            all_manageable=False,
            concurrency=min(GROK_ACCOUNT_REFRESH_MAX_CONCURRENCY, configured_concurrency),
            repo=repo,
            refresh_svc=refresh_service,
        )
        payload = _response_payload(response)
        await self.sync_host_accounts_from_runtime()
        return payload

    async def verify_accounts(self, tokens: list[str]) -> dict[str, Any]:
        """Run one ``fast`` rate-limits probe for each supplied SSO token.

        The result is intentionally an internal runtime payload: callers that
        expose it outside the admin boundary must translate ``token`` back to
        their stable account ID and remove the token first.
        """
        _, refresh_service = self._state()
        unique_tokens = list(
            dict.fromkeys(str(token or "").strip() for token in tokens if str(token or "").strip())
        )

        async def verify_one(token: str) -> dict[str, Any]:
            try:
                result = await refresh_service.verify_fast_token(token)
            except Exception:
                # The registration endpoint must not expose upstream exception
                # text because it may include a raw SSO token.
                result = {
                    "status": "unknown",
                    "error": "fast 配额探针失败，未确认登录态",
                }

            status = str(result.get("status") or "unknown").lower()
            if status not in {"valid", "invalid", "unknown"}:
                status = "unknown"
            item: dict[str, Any] = {"token": token, "status": status}
            quota = result.get("quota")
            if status == "valid" and isinstance(quota, dict):
                item["quota"] = {
                    "remaining": max(0, int(quota.get("remaining", 0) or 0)),
                    "total": max(0, int(quota.get("total", 0) or 0)),
                }
            error = str(result.get("error") or "").strip()
            if error:
                item["error"] = error[:300]
            return item

        results = await run_batch(
            unique_tokens,
            verify_one,
            concurrency=min(8, max(1, len(unique_tokens))),
        )
        return {"results": results}

    async def chat_test(
        self,
        token: str,
        *,
        prompt: str,
        model: str = GROK_ACCOUNT_CHAT_TEST_MODEL,
        timeout_s: float = GROK_ACCOUNT_CHAT_TEST_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Send one minimal Console request through one explicitly supplied SSO.

        This deliberately bypasses account selection.  The token may belong to
        the registration archive without being in the runtime pool; that still
        permits an operator to test it, but only a pre-existing runtime record
        receives usage/health feedback.
        """
        from app.control.account.enums import FeedbackKind
        from app.dataplane.reverse.protocol.xai_console_chat import (
            CONSOLE_MODELS,
            ConsoleStreamAdapter,
            build_console_payload,
            stream_console_chat,
        )
        from app.platform.errors import UpstreamError
        from app.platform.logging.logger import logger
        from app.platform.runtime.clock import now_s

        selected_model = str(model or GROK_ACCOUNT_CHAT_TEST_MODEL).strip()
        if selected_model not in CONSOLE_MODELS:
            raise ValueError("仅支持 Console Grok 模型进行对话测试")
        text = str(prompt or "").strip()
        if not text:
            raise ValueError("测试消息不能为空")
        if len(text) > 1_200:
            raise ValueError("测试消息不能超过 1200 个字符")

        repo, refresh_service = self._state()
        records = await repo.get_accounts([token])
        runtime_record = next(
            (record for record in records if not record.is_deleted()),
            None,
        )
        directory = getattr(getattr(self._host_app, "state", None), "directory", None)
        adapter = ConsoleStreamAdapter()
        started_at = time.monotonic()
        success = False
        failure: BaseException | None = None

        try:
            payload = build_console_payload(
                messages=[{"role": "user", "content": text}],
                model=selected_model,
                temperature=0.0,
                top_p=1.0,
                max_output_tokens=GROK_ACCOUNT_CHAT_TEST_MAX_OUTPUT_TOKENS,
                stream=True,
            )
            # The regular Console bridge enables x/web search for supported
            # models.  An account health test must remain a minimal text-only
            # request with no tools or image/search side effects.
            payload.pop("tools", None)
            payload.pop("tool_choice", None)

            async for event_type, data in stream_console_chat(
                token,
                payload,
                timeout_s=max(5.0, min(float(timeout_s), GROK_ACCOUNT_CHAT_TEST_TIMEOUT_S)),
            ):
                adapter.feed(event_type, data)

            if not adapter._done:
                raise UpstreamError("Console 对话未正常完成", status=502)
            content = adapter.full_text.strip()
            if not content:
                raise UpstreamError("Console 未返回文本回复", status=502)

            success = True
            return {
                "model": selected_model,
                "content": content,
                "elapsed_ms": max(0, int((time.monotonic() - started_at) * 1000)),
            }
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if runtime_record is not None:
                try:
                    if success:
                        if directory is not None:
                            await directory.feedback(
                                token,
                                FeedbackKind.SUCCESS,
                                5,
                                now_s_val=now_s(),
                            )
                        await refresh_service.refresh_call_async(token, 5)
                    else:
                        from app.dataplane.reverse.protocol.xai_usage import (
                            is_invalid_credentials_error,
                        )

                        status = getattr(failure, "status", None)
                        # A generic Console 401/403 is an endpoint-permission
                        # failure, not proof that the grok.com SSO expired.
                        kind = (
                            FeedbackKind.UNAUTHORIZED
                            if failure is not None and is_invalid_credentials_error(failure)
                            else FeedbackKind.RATE_LIMITED
                            if status == 429
                            else FeedbackKind.FORBIDDEN
                            if status in {401, 403}
                            else FeedbackKind.SERVER_ERROR
                        )
                        if directory is not None:
                            await directory.feedback(token, kind, 5, now_s_val=now_s())
                        await refresh_service.record_failure_async(token, 5, failure)
                except Exception as accounting_error:
                    logger.warning(
                        "grok account chat-test accounting failed: token={}... error={}",
                        token[:10],
                        accounting_error,
                    )
                try:
                    # Keep the registration archive's non-secret runtime
                    # mirror current so the account list immediately shows
                    # this test's use/fail count and Console quota.
                    await self.sync_host_accounts_from_runtime()
                except Exception as sync_error:
                    logger.warning(
                        "grok account chat-test runtime mirror failed: token={}... error={}",
                        token[:10],
                        sync_error,
                    )

    async def set_accounts_disabled(self, tokens: list[str], disabled: bool) -> dict[str, Any]:
        from app.products.web.admin.tokens import ToggleTokensDisabledRequest, toggle_tokens_disabled

        repo, _ = self._state()
        response = await toggle_tokens_disabled(
            ToggleTokensDisabledRequest(tokens=tokens, disabled=disabled),
            repo=repo,
        )
        payload = _response_payload(response)
        await self.sync_host_accounts_from_runtime()
        return payload

    async def delete_accounts(self, tokens: list[str]) -> dict[str, Any]:
        from app.products.web.admin.tokens import delete_tokens

        repo, _ = self._state()
        payload = _response_payload(await delete_tokens(tokens=tokens, repo=repo))
        await self.sync_host_accounts_from_runtime()
        return payload


grok_runtime = EmbeddedGrokRuntime()


__all__ = ["EmbeddedGrokRuntime", "grok_runtime"]
