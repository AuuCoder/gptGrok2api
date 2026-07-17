from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import orjson
from fastapi.responses import JSONResponse

from api.grok import is_grok_model
from app.control.account.enums import AccountStatus, FeedbackKind
from app.control.account.models import AccountRecord, RuntimeSnapshot
from app.control.account.quota_defaults import default_quota_set
from app.platform.errors import UpstreamError
from app.platform.request_context import get_grok_log_trace, get_media_base_url
import services.grok_runtime as grok_runtime_module
from services.grok_runtime import EmbeddedGrokRuntime
from services.register.grok_account_store import GrokAccountStore


class _Repository:
    async def runtime_snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            revision=1,
            items=[
                AccountRecord(
                    token="test-token",
                    pool="basic",
                    quota=default_quota_set("basic").to_dict(),
                )
            ],
        )


class _StatsRepository:
    async def runtime_snapshot(self) -> RuntimeSnapshot:
        basic = AccountRecord(
            token="basic-token",
            pool="basic",
            quota=default_quota_set("basic").to_dict(),
            usage_use_count=3,
            usage_fail_count=1,
        )
        super_account = AccountRecord(
            token="super-token",
            pool="super",
            quota=default_quota_set("super").to_dict(),
            usage_use_count=5,
            usage_fail_count=2,
        )
        cooling = AccountRecord(
            token="cooling-token",
            pool="basic",
            status=AccountStatus.COOLING,
            quota=default_quota_set("basic").to_dict(),
        )
        return RuntimeSnapshot(revision=2, items=[basic, super_account, cooling])


class _ImportRepository:
    def __init__(self) -> None:
        self.upserts = []

    async def get_accounts(self, _tokens):
        return []

    async def upsert_accounts(self, items):
        self.upserts.extend(items)


class _ChatRepository:
    def __init__(self, token: str, *, present: bool = True) -> None:
        self.record = (
            AccountRecord(
                token=token,
                pool="basic",
                quota=default_quota_set("basic").to_dict(),
            )
            if present
            else None
        )

    async def get_accounts(self, tokens):
        return [self.record] if self.record is not None and self.record.token in tokens else []


class _ChatDirectory:
    def __init__(self) -> None:
        self.feedback = AsyncMock()


class _ChatRefreshService:
    def __init__(self) -> None:
        self.refresh_call_async = AsyncMock()
        self.record_failure_async = AsyncMock()


class EmbeddedGrokRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_basic_pool_catalog_only_exposes_supported_models(self) -> None:
        runtime = EmbeddedGrokRuntime()
        with patch.object(runtime, "_state", return_value=(_Repository(), object())):
            catalog = await runtime.model_catalog()

        self.assertIn("grok-4.20-0309-non-reasoning", catalog["chat_models"])
        self.assertIn("grok-4.3-console", catalog["chat_models"])
        self.assertIn("grok-imagine-image-lite", catalog["image_models"])
        self.assertNotIn("grok-4.20-0309", catalog["chat_models"])
        self.assertEqual(catalog["video_models"], [])

    async def test_account_stats_include_image_quota_for_each_supported_pool(self) -> None:
        runtime = EmbeddedGrokRuntime()
        with patch.object(runtime, "_state", return_value=(_StatsRepository(), object())):
            stats = await runtime.account_stats()

        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["active"], 2)
        self.assertEqual(stats["limited"], 1)
        self.assertEqual(stats["total_quota"], 220)
        self.assertEqual(stats["quota_by_mode"], {"1": 170, "0": 50})
        self.assertEqual(stats["total_success"], 8)
        self.assertEqual(stats["total_fail"], 3)

    async def test_model_detection_only_matches_registered_grok_models(self) -> None:
        self.assertTrue(is_grok_model("grok-4.20-0309-non-reasoning"))
        self.assertFalse(is_grok_model("gpt-5"))

    async def test_image_task_dispatch_binds_public_url_and_trace_context(self) -> None:
        runtime = EmbeddedGrokRuntime()
        trace: dict[str, object] = {}
        received: list[dict] = []

        async def dispatch(payload: dict):
            received.append(payload)
            self.assertIs(get_grok_log_trace(), trace)
            self.assertEqual(get_media_base_url(), "https://pro.muyuai.top")
            return JSONResponse(
                {
                    "created": 1,
                    "data": [{"url": "https://pro.muyuai.top/v1/files/image?id=test"}],
                }
            )

        with patch("api.grok.dispatch_image_generation", new=dispatch):
            result = await runtime.generate_image(
                {
                    "model": "grok-imagine-image-lite",
                    "prompt": "test",
                    "base_url": "https://pro.muyuai.top/",
                    "stream": True,
                },
                trace=trace,
            )

        self.assertEqual(result["data"][0]["url"], "https://pro.muyuai.top/v1/files/image?id=test")
        self.assertFalse(received[0]["stream"])
        self.assertIsNone(get_grok_log_trace())
        self.assertEqual(get_media_base_url(), "")

    async def test_runtime_snapshot_is_mirrored_to_host_archive(self) -> None:
        runtime = EmbeddedGrokRuntime()
        payload = {
            "tokens": [
                {
                    "token": "runtime-token",
                    "pool": "heavy",
                    "status": "disabled",
                    "quota": {"heavy": {"remaining": 1, "total": 2}},
                    "use_count": 4,
                    "fail_count": 2,
                    "tags": ["imported"],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            with patch.object(runtime, "list_accounts", AsyncMock(return_value=payload)), patch.object(
                grok_runtime_module, "grok_account_store", store
            ):
                result = await runtime.sync_host_accounts_from_runtime()

            self.assertEqual(result, {"added": 1, "updated": 0, "missing": 0, "count": 1})
            account = store.list_accounts(redacted=False)[0]
            self.assertEqual(account["source_type"], "runtime")
            self.assertEqual(account["runtime"]["pool"], "heavy")
            self.assertEqual(account["runtime"]["status"], "disabled")

    async def test_removed_archive_entries_are_not_reimported_on_startup(self) -> None:
        runtime = EmbeddedGrokRuntime()
        repo = _ImportRepository()
        host = type("Host", (), {"state": type("State", (), {"repository": repo, "directory": None})()})()
        runtime._host_app = host
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            store.upsert({"email": "keep@example.com", "sso": "keep-token", "source_type": "protocol"})
            store.upsert({"email": "removed@example.com", "sso": "removed-token", "source_type": "protocol"})
            store.reconcile_runtime_accounts([{"token": "keep-token", "status": "active"}])
            store.reconcile_runtime_accounts(
                [
                    {"token": "keep-token", "status": "active"},
                    {"token": "runtime-only-token", "status": "active"},
                ]
            )

            with patch.object(grok_runtime_module, "grok_account_store", store):
                result = await runtime.import_registered_accounts()

        self.assertEqual(result, {"added": 2, "skipped": 0})
        self.assertEqual([item.token for item in repo.upserts], ["keep-token", "runtime-only-token"])

    async def test_manual_refresh_caps_single_egress_concurrency(self) -> None:
        runtime = EmbeddedGrokRuntime()
        response = JSONResponse(
            {
                "summary": {"total": 2, "ok": 2, "fail": 0},
                "results": {
                    "token-one": {"refreshed": 1},
                    "token-two": {"refreshed": 1},
                },
            }
        )
        with patch.object(runtime, "_state", return_value=(object(), object())), patch(
            "app.products.web.admin.batch.batch_refresh",
            new=AsyncMock(return_value=response),
        ) as refresh, patch(
            "app.platform.config.snapshot.get_config",
            return_value=15,
        ), patch.object(runtime, "sync_host_accounts_from_runtime", AsyncMock()):
            result = await runtime.refresh_accounts(["token-one", "token-two"])

        self.assertEqual(result["summary"], {"total": 2, "ok": 2, "fail": 0})
        self.assertEqual(refresh.await_args.kwargs["concurrency"], 15)

    async def test_chat_test_uses_explicit_token_without_tools_and_records_synced_account(self) -> None:
        token = "explicit-sso-token"
        runtime = EmbeddedGrokRuntime()
        repo = _ChatRepository(token)
        refresh_service = _ChatRefreshService()
        directory = _ChatDirectory()
        runtime._host_app = type("Host", (), {"state": type("State", (), {"directory": directory})()})()
        received: list[tuple[str, dict]] = []

        async def stream(upstream_token: str, payload: dict, **_kwargs):
            received.append((upstream_token, payload))
            yield "response.output_text.delta", orjson.dumps({"delta": "pong"}).decode()
            yield "response.completed", orjson.dumps({"response": {"usage": {}}}).decode()

        with patch.object(runtime, "_state", return_value=(repo, refresh_service)), patch(
            "app.dataplane.reverse.protocol.xai_console_chat.stream_console_chat",
            stream,
        ), patch.object(runtime, "sync_host_accounts_from_runtime", AsyncMock()) as sync:
            result = await runtime.chat_test(token, prompt="ping")

        self.assertEqual(result["model"], "grok-4.3-console")
        self.assertEqual(result["content"], "pong")
        self.assertGreaterEqual(result["elapsed_ms"], 0)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], token)
        self.assertEqual(received[0][1]["max_output_tokens"], 64)
        self.assertEqual(received[0][1]["temperature"], 0.0)
        self.assertTrue(received[0][1]["stream"])
        self.assertNotIn("tools", received[0][1])
        self.assertNotIn("tool_choice", received[0][1])
        directory.feedback.assert_awaited_once()
        self.assertEqual(directory.feedback.await_args.args[:3], (token, FeedbackKind.SUCCESS, 5))
        refresh_service.refresh_call_async.assert_awaited_once_with(token, 5)
        refresh_service.record_failure_async.assert_not_awaited()
        sync.assert_awaited_once()

    async def test_chat_test_console_403_is_not_marked_as_invalid_credentials(self) -> None:
        token = "console-permission-token"
        runtime = EmbeddedGrokRuntime()
        repo = _ChatRepository(token)
        refresh_service = _ChatRefreshService()
        directory = _ChatDirectory()
        runtime._host_app = type("Host", (), {"state": type("State", (), {"directory": directory})()})()

        async def stream(*_args, **_kwargs):
            raise UpstreamError("Console API returned 403", status=403, body="permission-denied")
            yield  # pragma: no cover

        with patch.object(runtime, "_state", return_value=(repo, refresh_service)), patch(
            "app.dataplane.reverse.protocol.xai_console_chat.stream_console_chat",
            stream,
        ), patch.object(runtime, "sync_host_accounts_from_runtime", AsyncMock()) as sync:
            with self.assertRaisesRegex(UpstreamError, "403"):
                await runtime.chat_test(token, prompt="ping")

        directory.feedback.assert_awaited_once()
        self.assertEqual(directory.feedback.await_args.args[:3], (token, FeedbackKind.FORBIDDEN, 5))
        refresh_service.record_failure_async.assert_awaited_once()
        refresh_service.refresh_call_async.assert_not_awaited()
        sync.assert_awaited_once()

    async def test_chat_test_does_not_create_or_update_an_unsynced_account(self) -> None:
        token = "archive-only-token"
        runtime = EmbeddedGrokRuntime()
        repo = _ChatRepository(token, present=False)
        refresh_service = _ChatRefreshService()
        directory = _ChatDirectory()
        runtime._host_app = type("Host", (), {"state": type("State", (), {"directory": directory})()})()

        async def stream(*_args, **_kwargs):
            yield "response.output_text.delta", orjson.dumps({"delta": "pong"}).decode()
            yield "response.completed", orjson.dumps({"response": {"usage": {}}}).decode()

        with patch.object(runtime, "_state", return_value=(repo, refresh_service)), patch(
            "app.dataplane.reverse.protocol.xai_console_chat.stream_console_chat",
            stream,
        ), patch.object(runtime, "sync_host_accounts_from_runtime", AsyncMock()) as sync:
            result = await runtime.chat_test(token, prompt="ping")

        self.assertEqual(result["content"], "pong")
        directory.feedback.assert_not_awaited()
        refresh_service.refresh_call_async.assert_not_awaited()
        refresh_service.record_failure_async.assert_not_awaited()
        sync.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
