from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from api import ai
from api.errors import install_exception_handlers
from app.platform.request_context import (
    record_grok_account_selection,
    record_grok_proxy_lease,
)


class RootGrokCompatibilityRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        install_exception_handlers(self.app)
        self.app.include_router(ai.create_router())
        self.client = TestClient(self.app)
        self.headers = {"Authorization": "Bearer host-key"}

    def test_grok_image_generation_accepts_ten_and_defaults_to_url(self) -> None:
        with (
            patch("api.ai.require_identity", return_value={"id": "user"}),
            patch("api.ai.filter_or_log", new_callable=AsyncMock),
            patch("api.ai.grok.is_grok_model", return_value=True),
            patch.object(ai.LoggedCall, "log") as log_call,
            patch(
                "api.ai.grok.dispatch_image_generation",
                new_callable=AsyncMock,
                return_value=JSONResponse({"created": 1, "data": []}),
            ) as dispatch,
        ):
            response = self.client.post(
                "/v1/images/generations",
                headers=self.headers,
                json={"model": "grok-imagine-image", "prompt": "a test image", "n": 10},
            )

        self.assertEqual(response.status_code, 200)
        payload = dispatch.await_args.args[0]
        self.assertEqual(payload["n"], 10)
        self.assertEqual(payload["response_format"], "url")
        log_call.assert_called_once_with("调用完成", {"created": 1, "data": []})

    def test_grok_image_generation_logs_gpt_compatible_detail_fields(self) -> None:
        async def dispatch(_payload):
            record_grok_account_selection(
                "secret-sso-token",
                pool_id=1,
                mode_id=-1,
                account_wait_ms=17,
            )
            record_grok_proxy_lease(
                "http://proxy-user:proxy-password@127.0.0.1:8080",
                egress_mode="single_proxy",
                scope="app",
                kind="websocket",
                egress_acquire_ms=23,
            )
            return JSONResponse(
                {
                    "created": 1,
                    "data": [{"url": "https://pro.muyuai.top/v1/files/image?id=image-1"}],
                }
            )

        with (
            patch("api.ai.require_identity", return_value={"id": "user", "name": "host-key", "role": "admin"}),
            patch("api.ai.filter_or_log", new_callable=AsyncMock),
            patch("api.ai.grok.is_grok_model", return_value=True),
            patch("api.ai.grok.dispatch_image_generation", new=dispatch),
            patch(
                "api.ai.grok_account_store.runtime_identity_for_token",
                return_value={
                    "account_id": "grok-account-1",
                    "account_email": "us***r@example.com",
                },
            ) as resolve_identity,
            patch("services.log_service.log_service.add") as add_log,
        ):
            response = self.client.post(
                "/v1/images/generations",
                headers=self.headers,
                json={"model": "grok-imagine-image", "prompt": "a test image"},
            )

        self.assertEqual(response.status_code, 200)
        resolve_identity.assert_called_once_with("secret-sso-token")
        _, summary, detail = add_log.call_args.args
        self.assertEqual(summary, "Grok 文生图调用完成")
        self.assertEqual(detail["account_email"], "us***r@example.com")
        self.assertEqual(detail["provider"], "grok_sso")
        self.assertEqual(detail["provider_account_id"], "grok-account-1")
        self.assertEqual(detail["proxy_source"], "grok_runtime")
        self.assertTrue(detail["has_proxy"])
        self.assertEqual(detail["egress_mode"], "single_proxy")
        self.assertEqual(detail["perf"]["account_wait_ms"], 17)
        self.assertEqual(detail["perf"]["egress_acquire_ms"], 23)
        self.assertGreater(detail["perf"]["conversation_stream_ms"], 0)
        self.assertEqual(
            detail["urls"],
            ["https://pro.muyuai.top/v1/files/image?id=image-1"],
        )
        self.assertEqual(detail["monitor"]["metrics"]["account_wait_ms"], 17)
        self.assertEqual(detail["monitor"]["metrics"]["egress_acquire_ms"], 23)
        serialized_detail = repr(detail)
        self.assertNotIn("secret-sso-token", serialized_detail)
        self.assertNotIn("proxy-user", serialized_detail)
        self.assertNotIn("proxy-password", serialized_detail)

    def test_grok_image_generation_rejects_more_than_ten(self) -> None:
        response = self.client.post(
            "/v1/images/generations",
            headers=self.headers,
            json={"model": "grok-imagine-image", "prompt": "a test image", "n": 11},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["type"], "invalid_request_error")

    def test_root_image_edit_rejects_non_grok_edit_model_before_reading_files(self) -> None:
        with (
            patch("api.ai.require_identity", return_value={"id": "user"}),
            patch(
                "api.ai.parse_image_edit_request",
                new_callable=AsyncMock,
                return_value=({"prompt": "edit", "model": "grok-imagine-image", "n": 1}, [], []),
            ),
            patch("api.ai.read_image_sources", new_callable=AsyncMock) as read_sources,
            patch("api.ai.grok.dispatch_image_edit", new_callable=AsyncMock) as dispatch,
        ):
            response = self.client.post("/v1/images/edits", headers=self.headers, json={})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["param"], "model")
        read_sources.assert_not_awaited()
        dispatch.assert_not_awaited()

    def test_root_image_edit_routes_gpt_model_to_gpt_handler(self) -> None:
        with (
            patch("api.ai.require_identity", return_value={"id": "user"}),
            patch("api.ai.filter_or_log", new_callable=AsyncMock),
            patch(
                "api.ai.parse_image_edit_request",
                new_callable=AsyncMock,
                return_value=(
                    {"prompt": "edit", "model": "gpt-image-2", "n": 1},
                    [object()],
                    [object()],
                ),
            ),
            patch(
                "api.ai.read_image_sources",
                new_callable=AsyncMock,
                side_effect=[[(b"image", "input.png", "image/png")], [(b"mask", "mask.png", "image/png")]],
            ),
            patch.object(ai.LoggedCall, "run", new_callable=AsyncMock, return_value={"created": 1, "data": []}) as run,
            patch("api.ai.grok.dispatch_image_edit", new_callable=AsyncMock) as grok_dispatch,
        ):
            response = self.client.post("/v1/images/edits", headers=self.headers, json={})

        self.assertEqual(response.status_code, 200)
        self.assertIs(run.await_args.args[0], ai.openai_v1_image_edit.handle)
        self.assertEqual(run.await_args.args[1]["model"], "gpt-image-2")
        self.assertEqual(run.await_args.args[1]["mask"][0][0], b"mask")
        grok_dispatch.assert_not_awaited()

    def test_root_image_edit_rejects_mask_before_reading_files(self) -> None:
        with (
            patch("api.ai.require_identity", return_value={"id": "user"}),
            patch(
                "api.ai.parse_image_edit_request",
                new_callable=AsyncMock,
                return_value=(
                    {"prompt": "edit", "model": "grok-imagine-image-edit", "n": 1},
                    [],
                    [object()],
                ),
            ),
            patch("api.ai.read_image_sources", new_callable=AsyncMock) as read_sources,
            patch("api.ai.grok.dispatch_image_edit", new_callable=AsyncMock) as dispatch,
        ):
            response = self.client.post("/v1/images/edits", headers=self.headers, json={})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["param"], "mask")
        read_sources.assert_not_awaited()
        dispatch.assert_not_awaited()

    def test_grok_model_detail_uses_embedded_handler(self) -> None:
        with (
            patch("api.ai.require_identity", return_value={"id": "user"}) as authenticate,
            patch("api.ai.grok.is_grok_text_model", return_value=True),
            patch(
                "api.ai.grok.dispatch_model_detail",
                new_callable=AsyncMock,
                return_value=JSONResponse({"id": "grok-4.20-fast"}),
            ) as dispatch,
        ):
            response = self.client.get("/v1/models/grok-4.20-fast", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "grok-4.20-fast")
        authenticate.assert_called_once_with("Bearer host-key")
        dispatch.assert_awaited_once()
        self.assertEqual(dispatch.await_args.args[0], "grok-4.20-fast")

    def test_non_grok_model_detail_uses_unified_catalog(self) -> None:
        with (
            patch("api.ai.require_identity", return_value={"id": "user"}),
            patch("api.ai.grok.is_grok_text_model", return_value=False),
            patch("api.ai.grok.dispatch_model_detail", new_callable=AsyncMock) as dispatch,
            patch(
                "api.ai.openai_v1_models.list_models",
                return_value={
                    "object": "list",
                    "data": [
                        {
                            "id": "gpt-5-6",
                            "object": "model",
                            "owned_by": "gptgrok2api",
                        }
                    ],
                },
            ),
        ):
            response = self.client.get("/v1/models/gpt-5-6", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "gpt-5-6")
        dispatch.assert_not_awaited()

    def test_non_grok_model_detail_returns_404_when_absent_from_catalog(self) -> None:
        with (
            patch("api.ai.require_identity", return_value={"id": "user"}),
            patch("api.ai.grok.is_grok_text_model", return_value=False),
            patch("api.ai.openai_v1_models.list_models", return_value={"object": "list", "data": []}),
        ):
            response = self.client.get("/v1/models/not-a-model", headers=self.headers)

        self.assertEqual(response.status_code, 404)

    def test_root_chat_routes_grok_45_to_the_oauth_provider_dispatcher(self) -> None:
        with (
            patch("api.ai.require_identity", return_value={"id": "user"}),
            patch("api.ai.filter_or_log", new_callable=AsyncMock),
            patch("api.ai.grok.is_grok_text_model", return_value=True),
            patch.object(ai.LoggedCall, "log") as log_call,
            patch(
                "api.ai.grok.dispatch_chat_completion",
                new_callable=AsyncMock,
                return_value=JSONResponse({"id": "chatcmpl_45", "model": "grok-4.5", "choices": []}),
            ) as dispatch,
        ):
            response = self.client.post(
                "/v1/chat/completions",
                headers=self.headers,
                json={"model": "grok-4.5", "messages": [{"role": "user", "content": "hello"}]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "grok-4.5")
        self.assertEqual(dispatch.await_args.args[0]["model"], "grok-4.5")
        log_call.assert_called_once_with(
            "调用完成",
            {"id": "chatcmpl_45", "model": "grok-4.5", "choices": []},
        )

    def test_root_cli_oauth_call_logs_selected_account(self) -> None:
        async def dispatch(_payload, *, on_account_selected=None):
            self.assertIsNotNone(on_account_selected)
            on_account_selected({"account_id": "xai-cli-oauth-test", "account_email": "pe***n@example.com"})
            return JSONResponse({"id": "chatcmpl_45", "model": "grok-4.5", "choices": []})

        with (
            patch("api.ai.require_identity", return_value={"id": "user"}),
            patch("api.ai.filter_or_log", new_callable=AsyncMock),
            patch("api.ai.grok.is_grok_text_model", return_value=True),
            patch.object(ai.LoggedCall, "log") as log_call,
            patch("api.ai.grok.dispatch_chat_completion", new=dispatch),
        ):
            response = self.client.post(
                "/v1/chat/completions",
                headers=self.headers,
                json={"model": "grok-4.5", "messages": [{"role": "user", "content": "hello"}]},
            )

        self.assertEqual(response.status_code, 200)
        log_call.assert_called_once_with(
            "调用完成",
            {"id": "chatcmpl_45", "model": "grok-4.5", "choices": []},
            account_email="pe***n@example.com",
            extra={"provider": "xai_cli_oauth", "provider_account_id": "xai-cli-oauth-test"},
        )

    def test_root_streaming_cli_oauth_call_logs_selected_account(self) -> None:
        async def body():
            yield b'data: {"choices": []}\n\n'
            yield b"data: [DONE]\n\n"

        async def dispatch(_payload, *, on_account_selected=None):
            self.assertIsNotNone(on_account_selected)
            on_account_selected({"account_id": "xai-cli-oauth-test", "account_email": "pe***n@example.com"})
            return StreamingResponse(body(), media_type="text/event-stream")

        with (
            patch("api.ai.require_identity", return_value={"id": "user"}),
            patch("api.ai.filter_or_log", new_callable=AsyncMock),
            patch("api.ai.grok.is_grok_text_model", return_value=True),
            patch.object(ai.LoggedCall, "log") as log_call,
            patch("api.ai.grok.dispatch_chat_completion", new=dispatch),
        ):
            response = self.client.post(
                "/v1/chat/completions",
                headers=self.headers,
                json={"model": "grok-4.5", "stream": True, "messages": [{"role": "user", "content": "hello"}]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("[DONE]", response.text)
        log_call.assert_called_once_with(
            "流式调用结束",
            account_email="pe***n@example.com",
            extra={"provider": "xai_cli_oauth", "provider_account_id": "xai-cli-oauth-test"},
        )

    def test_root_streaming_grok_call_logs_when_stream_finishes(self) -> None:
        async def body():
            yield b'data: {"choices": []}\n\n'
            yield b"data: [DONE]\n\n"

        with (
            patch("api.ai.require_identity", return_value={"id": "user"}),
            patch("api.ai.filter_or_log", new_callable=AsyncMock),
            patch("api.ai.grok.is_grok_text_model", return_value=True),
            patch.object(ai.LoggedCall, "log") as log_call,
            patch(
                "api.ai.grok.dispatch_chat_completion",
                new_callable=AsyncMock,
                return_value=StreamingResponse(body(), media_type="text/event-stream"),
            ),
        ):
            response = self.client.post(
                "/v1/chat/completions",
                headers=self.headers,
                json={"model": "grok-4.5", "stream": True, "messages": [{"role": "user", "content": "hello"}]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("[DONE]", response.text)
        log_call.assert_called_once_with("流式调用结束")

    def test_root_streaming_grok_error_event_is_logged_as_failure(self) -> None:
        async def body():
            yield b"event: "
            yield b'error\ndata: {"error":{"message":"upstream failed"}}\n\n'

        with (
            patch("api.ai.require_identity", return_value={"id": "user"}),
            patch("api.ai.filter_or_log", new_callable=AsyncMock),
            patch("api.ai.grok.is_grok_text_model", return_value=True),
            patch.object(ai.LoggedCall, "log") as log_call,
            patch(
                "api.ai.grok.dispatch_chat_completion",
                new_callable=AsyncMock,
                return_value=StreamingResponse(body(), media_type="text/event-stream"),
            ),
        ):
            response = self.client.post(
                "/v1/chat/completions",
                headers=self.headers,
                json={"model": "grok-4.5", "stream": True, "messages": [{"role": "user", "content": "hello"}]},
            )

        self.assertEqual(response.status_code, 200)
        log_call.assert_called_once_with("流式调用失败", status="failed", error="Grok 上游返回流式错误事件")

    def test_root_streaming_grok_content_containing_event_error_is_not_marked_failed(self) -> None:
        async def body():
            yield b'data: {"choices":[{"delta":{"content":"event: error"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        with (
            patch("api.ai.require_identity", return_value={"id": "user"}),
            patch("api.ai.filter_or_log", new_callable=AsyncMock),
            patch("api.ai.grok.is_grok_text_model", return_value=True),
            patch.object(ai.LoggedCall, "log") as log_call,
            patch(
                "api.ai.grok.dispatch_chat_completion",
                new_callable=AsyncMock,
                return_value=StreamingResponse(body(), media_type="text/event-stream"),
            ),
        ):
            response = self.client.post(
                "/v1/chat/completions",
                headers=self.headers,
                json={"model": "grok-4.5", "stream": True, "messages": [{"role": "user", "content": "hello"}]},
            )

        self.assertEqual(response.status_code, 200)
        log_call.assert_called_once_with("流式调用结束")

    def test_video_and_local_media_routes_are_authenticated_root_routes(self) -> None:
        with (
            patch("api.ai.require_identity", return_value={"id": "user"}) as authenticate,
            patch(
                "api.ai.grok.dispatch_video_create",
                new_callable=AsyncMock,
                return_value=JSONResponse({"id": "video_123", "status": "queued"}),
            ) as create_video,
            patch(
                "api.ai.grok.dispatch_video_retrieve",
                new_callable=AsyncMock,
                return_value=JSONResponse({"id": "video_123", "status": "completed"}),
            ) as retrieve_video,
            patch(
                "api.ai.grok.dispatch_video_content",
                new_callable=AsyncMock,
                return_value=Response(b"video", media_type="video/mp4"),
            ) as video_content,
            patch(
                "api.ai.grok.dispatch_image_file",
                new_callable=AsyncMock,
                return_value=Response(b"image", media_type="image/png"),
            ) as image_file,
            patch(
                "api.ai.grok.dispatch_video_file",
                new_callable=AsyncMock,
                return_value=Response(b"video", media_type="video/mp4"),
            ) as video_file,
        ):
            create_response = self.client.post(
                "/v1/videos",
                headers=self.headers,
                data={"model": "grok-imagine-video", "prompt": "a short test"},
                files={"input_reference[]": ("reference.png", b"png", "image/png")},
            )
            retrieve_response = self.client.get("/v1/videos/video_123", headers=self.headers)
            content_response = self.client.get("/v1/videos/video_123/content", headers=self.headers)
            image_response = self.client.get("/v1/files/image?id=0123456789abcdef")
            video_response = self.client.get("/v1/files/video?id=0123456789abcdef")

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(retrieve_response.status_code, 200)
        self.assertEqual(content_response.status_code, 200)
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(video_response.status_code, 200)
        self.assertEqual(authenticate.call_count, 3)
        create_video.assert_awaited_once()
        self.assertEqual(create_video.await_args.kwargs["model"], "grok-imagine-video")
        self.assertEqual(create_video.await_args.kwargs["prompt"], "a short test")
        self.assertEqual(len(create_video.await_args.kwargs["input_reference"] or []), 1)
        retrieve_video.assert_awaited_once_with("video_123")
        video_content.assert_awaited_once_with("video_123")
        image_file.assert_awaited_once_with("0123456789abcdef")
        video_file.assert_awaited_once_with("0123456789abcdef")


if __name__ == "__main__":
    unittest.main()
