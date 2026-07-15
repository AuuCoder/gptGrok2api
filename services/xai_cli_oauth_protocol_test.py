from __future__ import annotations

import base64
import json
import unittest

from services.xai_cli_oauth_protocol import (
    anthropic_messages_to_response_input,
    chat_messages_to_response_input,
    normalize_model_ids,
    response_to_anthropic_message,
    response_to_chat_completion,
    response_usage,
    token_email,
    token_expiry_epoch,
)


def _jwt(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{encoded}.signature"


class XaiCliOAuthProtocolTest(unittest.TestCase):
    def test_jwt_hints_are_optional_and_do_not_require_signature_verification(self) -> None:
        token = _jwt({"email": "person@example.com", "exp": 2_000_000_000})
        self.assertEqual(token_email(token), "person@example.com")
        self.assertEqual(token_expiry_epoch(token, now=1), 2_000_000_000)
        self.assertEqual(token_email("not-a-jwt"), "")

    def test_model_and_usage_extraction(self) -> None:
        payload = {
            "data": [{"id": "grok-4.5"}, {"id": "grok-4.5"}, {"id": "grok-4.5-fast"}],
        }
        self.assertEqual(normalize_model_ids(payload), ["grok-4.5", "grok-4.5-fast"])

        response = {
            "id": "resp_123",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}],
            "usage": {"input_tokens": 3, "output_tokens": 5},
        }
        self.assertEqual(response_usage(response), {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8})
        self.assertEqual(response_to_chat_completion(response, model="grok-4.5")["choices"][0]["message"]["content"], "hello")
        self.assertEqual(response_to_anthropic_message(response, model="grok-4.5")["content"][0]["text"], "hello")

    def test_chat_and_anthropic_input_adapters_preserve_text_and_images(self) -> None:
        chat = chat_messages_to_response_input(
            [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]},
            ]
        )
        self.assertEqual(chat[0]["role"], "system")
        self.assertEqual(chat[1]["content"][1], {"type": "input_image", "image_url": "https://example.test/a.png"})

        anthropic = anthropic_messages_to_response_input(
            [{"role": "user", "content": "hello"}],
            [{"type": "text", "text": "be concise"}],
        )
        self.assertEqual(anthropic[0]["role"], "system")
        self.assertEqual(anthropic[1]["content"][0]["text"], "hello")


if __name__ == "__main__":
    unittest.main()
