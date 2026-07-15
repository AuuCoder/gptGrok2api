from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.platform.auth.middleware import verify_api_key
from app.platform.request_context import reset_media_base_url, set_media_base_url
from app.products.media import MediaResult
from app.products.media import run_media
from app.products.anthropic.messages import create as create_anthropic_message
from app.control.model.registry import resolve as resolve_model
from app.products.openai.images import _local_image_url
from app.products.openai.responses import create as create_response
from app.products.openai.video import _local_video_url


class MediaAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_shared_dispatcher_calls_the_image_backend(self) -> None:
        with patch(
            "app.products.openai.images.generate",
            new=AsyncMock(return_value={"data": [{"b64_json": "aW1hZ2U="}]}),
        ) as generate:
            result = await run_media(
                spec=resolve_model("grok-imagine-image"),
                model="grok-imagine-image",
                messages=[{"role": "user", "content": "draw a lighthouse"}],
                image_options={"n": 2, "size": "1024x1024"},
                image_response_format="b64_json",
            )

        self.assertEqual(result.kind, "image")
        self.assertEqual(result.images, [{"b64_json": "aW1hZ2U="}])
        self.assertEqual(generate.await_args.kwargs["n"], 2)
        self.assertEqual(generate.await_args.kwargs["response_format"], "b64_json")

    async def test_shared_dispatcher_calls_the_image_edit_backend(self) -> None:
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "change the sky"},
                {"type": "image_url", "image_url": {"url": "https://example.test/input.png"}},
            ],
        }]
        with patch(
            "app.products.openai.images.edit",
            new=AsyncMock(return_value={"data": [{"url": "https://example.test/output.png"}]}),
        ) as edit:
            result = await run_media(
                spec=resolve_model("grok-imagine-image-edit"),
                model="grok-imagine-image-edit",
                messages=messages,
                image_options={"n": 1},
            )

        self.assertEqual(result.kind, "image_edit")
        self.assertEqual(result.images, [{"url": "https://example.test/output.png"}])
        self.assertEqual(edit.await_args.kwargs["messages"], messages)

    async def test_shared_dispatcher_creates_a_video_job(self) -> None:
        with patch(
            "app.products.openai.video.create_video",
            new=AsyncMock(return_value={"id": "video_test", "status": "queued"}),
        ) as create_video:
            result = await run_media(
                spec=resolve_model("grok-imagine-video"),
                model="grok-imagine-video",
                messages=[{"role": "user", "content": "make a short clip"}],
                video_options={"seconds": 10},
            )

        self.assertEqual(result.kind, "video")
        self.assertEqual(result.video, {"id": "video_test", "status": "queued"})
        self.assertEqual(create_video.await_args.kwargs["seconds"], 10)

    async def test_responses_image_model_returns_image_generation_calls(self) -> None:
        media = MediaResult(
            kind="image",
            prompt="draw a lighthouse",
            images=[{"b64_json": "aW1hZ2U="}],
        )
        with patch("app.products.openai.responses.run_media", new=AsyncMock(return_value=media)) as run_media:
            response = await create_response(
                model="grok-imagine-image",
                input_val="draw a lighthouse",
                instructions=None,
                stream=False,
                emit_think=False,
                temperature=0.8,
                top_p=0.95,
                image_options={"n": 1},
            )

        self.assertEqual(response["object"], "response")
        self.assertEqual(response["output"][0]["type"], "image_generation_call")
        self.assertEqual(response["output"][0]["result"], "aW1hZ2U=")
        self.assertEqual(run_media.await_args.kwargs["image_response_format"], "b64_json")

    async def test_responses_video_model_returns_standard_message_item(self) -> None:
        media = MediaResult(
            kind="video",
            prompt="make a short clip",
            video={"id": "video_test", "status": "queued"},
        )
        with patch("app.products.openai.responses.run_media", new=AsyncMock(return_value=media)):
            response = await create_response(
                model="grok-imagine-video",
                input_val="make a short clip",
                instructions=None,
                stream=False,
                emit_think=False,
                temperature=0.8,
                top_p=0.95,
            )

        text = response["output"][0]["content"][0]["text"]
        self.assertIn("video_test", text)
        self.assertIn("/v1/videos/video_test", text)

    async def test_responses_media_stream_ends_with_completed_and_done(self) -> None:
        media = MediaResult(
            kind="image",
            prompt="draw a lighthouse",
            images=[{"b64_json": "aW1hZ2U="}],
        )
        with patch("app.products.openai.responses.run_media", new=AsyncMock(return_value=media)):
            stream = await create_response(
                model="grok-imagine-image",
                input_val="draw a lighthouse",
                instructions=None,
                stream=True,
                emit_think=False,
                temperature=0.8,
                top_p=0.95,
            )
            chunks = [chunk async for chunk in stream]

        self.assertIn("event: response.created", chunks[0])
        self.assertTrue(any("event: response.completed" in chunk for chunk in chunks))
        self.assertEqual(chunks[-1], "data: [DONE]\n\n")

    async def test_anthropic_image_model_returns_url_text_block(self) -> None:
        media = MediaResult(
            kind="image",
            prompt="draw a lighthouse",
            images=[{"url": "https://media.example/generated.png"}],
        )
        with patch("app.products.anthropic.messages.run_media", new=AsyncMock(return_value=media)) as run_media:
            response = await create_anthropic_message(
                model="grok-imagine-image",
                messages=[{"role": "user", "content": "draw a lighthouse"}],
                stream=False,
                emit_think=False,
                temperature=0.8,
                top_p=0.95,
            )

        self.assertEqual(response["type"], "message")
        self.assertEqual(response["content"][0]["type"], "text")
        self.assertIn("https://media.example/generated.png", response["content"][0]["text"])
        self.assertEqual(run_media.await_args.kwargs["image_response_format"], "url")

    async def test_anthropic_media_stream_ends_at_message_stop(self) -> None:
        media = MediaResult(
            kind="video",
            prompt="make a short clip",
            video={"id": "video_test", "status": "queued"},
        )
        with patch("app.products.anthropic.messages.run_media", new=AsyncMock(return_value=media)):
            stream = await create_anthropic_message(
                model="grok-imagine-video",
                messages=[{"role": "user", "content": "make a short clip"}],
                stream=True,
                emit_think=False,
                temperature=0.8,
                top_p=0.95,
            )
            chunks = [chunk async for chunk in stream]

        self.assertTrue(any("event: message_start" in chunk for chunk in chunks))
        self.assertIn("event: message_stop", chunks[-1])
        self.assertFalse(any("[DONE]" in chunk for chunk in chunks))


class MediaBaseUrlContextTest(unittest.TestCase):
    def test_local_media_urls_prefer_the_request_public_base_url(self) -> None:
        token = set_media_base_url("https://api.example.test/")
        try:
            self.assertEqual(
                _local_image_url("0123456789abcdef"),
                "https://api.example.test/v1/files/image?id=0123456789abcdef",
            )
            self.assertEqual(
                _local_video_url("0123456789abcdef"),
                "https://api.example.test/v1/files/video?id=0123456789abcdef",
            )
        finally:
            reset_media_base_url(token)


class MediaHttpRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        from app.products.anthropic.router import router as anthropic_router
        from app.products.openai.router import router as openai_router

        app = FastAPI()

        async def allow_api_key() -> None:
            return None

        app.dependency_overrides[verify_api_key] = allow_api_key
        app.include_router(openai_router)
        app.include_router(anthropic_router)
        self.client = TestClient(app)

    def test_responses_route_keeps_media_configuration(self) -> None:
        media = MediaResult(
            kind="image",
            prompt="draw a lighthouse",
            images=[{"b64_json": "aW1hZ2U="}],
        )
        with patch("app.products.openai.responses.run_media", new=AsyncMock(return_value=media)) as run_media:
            response = self.client.post(
                "/v1/responses",
                json={
                    "model": "grok-imagine-image",
                    "input": "draw a lighthouse",
                    "stream": False,
                    "image_config": {"n": 2, "size": "1024x1024"},
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output"][0]["type"], "image_generation_call")
        self.assertEqual(run_media.await_args.kwargs["image_options"]["n"], 2)

    def test_chat_route_accepts_top_level_image_configuration(self) -> None:
        with patch(
            "app.products.openai.images.generate",
            new=AsyncMock(return_value={"id": "chatcmpl_test", "choices": []}),
        ) as generate:
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "grok-imagine-image",
                    "messages": [{"role": "user", "content": "draw a lighthouse"}],
                    "stream": False,
                    "n": 2,
                    "size": "1792x1024",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(generate.await_args.kwargs["n"], 2)
        self.assertEqual(generate.await_args.kwargs["size"], "1792x1024")

    def test_chat_route_accepts_top_level_video_configuration(self) -> None:
        with patch(
            "app.products.openai.video.completions",
            new=AsyncMock(return_value={"id": "chatcmpl_test", "choices": []}),
        ) as completions:
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "grok-imagine-video",
                    "messages": [{"role": "user", "content": "make a short clip"}],
                    "stream": False,
                    "seconds": 10,
                    "size": "1280x720",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(completions.await_args.kwargs["seconds"], 10)
        self.assertEqual(completions.await_args.kwargs["size"], "1280x720")

    def test_anthropic_route_keeps_media_configuration(self) -> None:
        media = MediaResult(
            kind="image",
            prompt="draw a lighthouse",
            images=[{"url": "https://media.example/generated.png"}],
        )
        with patch("app.products.anthropic.messages.run_media", new=AsyncMock(return_value=media)) as run_media:
            response = self.client.post(
                "/v1/messages",
                json={
                    "model": "grok-imagine-image",
                    "max_tokens": 32,
                    "stream": False,
                    "messages": [{"role": "user", "content": "draw a lighthouse"}],
                    "image_config": {"n": 2},
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("generated.png", response.json()["content"][0]["text"])
        self.assertEqual(run_media.await_args.kwargs["image_options"]["n"], 2)


if __name__ == "__main__":
    unittest.main()
