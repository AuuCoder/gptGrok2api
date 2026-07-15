from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from api import grok
from api.errors import install_exception_handlers


class GrokDispatchValidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_embedded_payloads_raise_request_validation_error(self) -> None:
        cases = [
            (grok.dispatch_chat_completion, {"model": "grok-4.20-fast"}),
            (grok.dispatch_response, {"model": "grok-4.20-fast"}),
            (grok.dispatch_anthropic_message, {"model": "grok-4.20-fast"}),
        ]

        for dispatch, payload in cases:
            with self.subTest(dispatch=dispatch.__name__), self.assertRaises(RequestValidationError):
                await dispatch(payload)


class GrokDispatchErrorResponseTest(unittest.TestCase):
    def test_invalid_root_grok_payload_returns_openai_422(self) -> None:
        app = FastAPI()
        install_exception_handlers(app)

        @app.post("/v1/chat/completions")
        async def route():
            return await grok.dispatch_chat_completion({"model": "grok-4.20-fast"})

        response = TestClient(app).post("/v1/chat/completions")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["type"], "invalid_request_error")


class GrokCliOAuthDispatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_cli_response_stream_is_wrapped_as_sse_http_response(self) -> None:
        async def stream():
            yield 'event: response.output_text.delta\ndata: {"delta":"hello"}\n\n'

        from services.xai_cli_oauth_service import xai_cli_oauth_service

        with patch.object(xai_cli_oauth_service, "create_response", new=AsyncMock(return_value=stream())):
            response = await grok.dispatch_response({"model": "grok-4.5", "input": "hello", "stream": True})

        self.assertIsInstance(response, StreamingResponse)
        self.assertEqual(response.media_type, "text/event-stream")
        chunks = [chunk async for chunk in response.body_iterator]
        self.assertIn("hello", "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
