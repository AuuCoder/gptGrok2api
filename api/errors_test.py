from __future__ import annotations

import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from api import grok
from api.errors import install_exception_handlers
from app.platform.errors import ValidationError


class _RequiredBody(BaseModel):
    value: str


class EmbeddedGrokErrorCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        install_exception_handlers(app)
        grok.install_exception_handlers(app)

        @app.get("/grok/v1/chat/completions")
        async def grok_openai_http_error():
            raise HTTPException(status_code=401, detail="missing key")

        @app.post("/grok/v1/images/generations")
        async def grok_validation_error(_: _RequiredBody):
            return {"ok": True}

        @app.post("/grok/v1/messages")
        async def grok_anthropic_app_error(_: _RequiredBody):
            raise ValidationError("invalid message", param="messages")

        @app.get("/grok/v1/responses")
        async def grok_openai_app_error():
            raise ValidationError("invalid response", param="input")

        self.client = TestClient(app)

    def test_grok_openai_http_errors_use_openai_shape(self) -> None:
        response = self.client.get("/grok/v1/chat/completions")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["type"], "authentication_error")

    def test_grok_validation_errors_use_openai_shape(self) -> None:
        response = self.client.post("/grok/v1/images/generations", json={})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["type"], "invalid_request_error")

    def test_grok_messages_app_errors_use_anthropic_shape(self) -> None:
        response = self.client.post("/grok/v1/messages", json={"value": "ok"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["type"], "error")
        self.assertEqual(response.json()["error"]["type"], "invalid_request_error")
        self.assertEqual(response.json()["error"]["message"], "invalid message")

    def test_grok_messages_validation_errors_use_anthropic_shape(self) -> None:
        response = self.client.post("/grok/v1/messages", json={})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["type"], "error")
        self.assertEqual(response.json()["error"]["type"], "invalid_request_error")

    def test_grok_non_messages_app_errors_use_openai_shape(self) -> None:
        response = self.client.get("/grok/v1/responses")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["type"], "invalid_request_error")
        self.assertEqual(response.json()["error"]["param"], "input")


if __name__ == "__main__":
    unittest.main()
