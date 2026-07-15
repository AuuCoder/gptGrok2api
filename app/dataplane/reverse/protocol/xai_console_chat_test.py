"""Regression tests for Console request validation and terminal SSE errors."""

import asyncio
import importlib
import unittest
from unittest.mock import AsyncMock, patch

import orjson

from app.dataplane.reverse.protocol.xai_console_chat import (
    ConsoleStreamAdapter,
    build_console_payload,
    validate_console_request,
)
from app.platform.errors import UpstreamError, ValidationError
from app.products.anthropic.console_messages import create as console_messages_create
from app.products.anthropic import console_messages as console_messages_module
from app.products.anthropic.messages import create as anthropic_messages_create
from app.products.openai.console_chat import completions as console_chat_completions
from app.products.openai.console_responses import create as console_responses_create
from app.products.openai.chat import completions as openai_chat_completions
from app.products.openai.responses import create as openai_responses_create
from app.products.openai.schemas import ChatCompletionRequest, ResponsesCreateRequest
from app.products.anthropic.router import MessagesRequest


openai_router_module = importlib.import_module("app.products.openai.router")
anthropic_router_module = importlib.import_module("app.products.anthropic.router")


class ConsoleRequestValidationTest(unittest.TestCase):
    def test_text_and_image_urls_are_preserved(self) -> None:
        payload = build_console_payload(
            model="grok-4.3-console",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image"},
                    {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}},
                    {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
                ],
            }],
        )

        self.assertEqual(payload["input"], [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe this image"},
                {"type": "input_image", "image_url": "https://example.test/image.png"},
                {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
            ],
        }])

    def test_file_and_audio_blocks_are_rejected(self) -> None:
        for content_type in ("file", "input_file", "input_audio", "audio", "document"):
            with self.subTest(content_type=content_type):
                with self.assertRaisesRegex(ValidationError, "do not support") as raised:
                    validate_console_request([{
                        "role": "user",
                        "content": [{"type": content_type}],
                    }])
                self.assertEqual(raised.exception.status, 400)
                self.assertEqual(raised.exception.code, "unsupported_value")

        with self.assertRaisesRegex(ValidationError, "only support image data URIs"):
            validate_console_request([{
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": "data:application/pdf;base64,AA=="},
                }],
            }])

    def test_tools_and_tool_choice_are_rejected(self) -> None:
        messages = [{"role": "user", "content": "hello"}]
        with self.assertRaisesRegex(ValidationError, "function tools") as tools_error:
            validate_console_request(messages, tools=[{
                "type": "function",
                "function": {"name": "weather"},
            }])
        self.assertEqual(tools_error.exception.status, 400)
        self.assertEqual(tools_error.exception.param, "tools")

        with self.assertRaisesRegex(ValidationError, "tool_choice") as choice_error:
            validate_console_request(messages, tool_choice="auto")
        self.assertEqual(choice_error.exception.status, 400)
        self.assertEqual(choice_error.exception.param, "tool_choice")

    def test_tool_history_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "function tool messages"):
            validate_console_request([{
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "weather is sunny",
            }])

    def test_max_output_tokens_overrides_default_and_requires_positive_int(self) -> None:
        messages = [{"role": "user", "content": "hello"}]
        self.assertEqual(
            build_console_payload(
                model="grok-build-console",
                messages=messages,
            )["max_output_tokens"],
            256_000,
        )
        self.assertEqual(
            build_console_payload(
                model="grok-build-console",
                messages=messages,
                max_output_tokens=321,
            )["max_output_tokens"],
            321,
        )
        for value in (0, -1, True, "321"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValidationError, "positive integer") as raised:
                    build_console_payload(
                        model="grok-4.3-console",
                        messages=messages,
                        max_output_tokens=value,  # type: ignore[arg-type]
                    )
                self.assertEqual(raised.exception.status, 400)
                self.assertEqual(raised.exception.param, "max_output_tokens")


class ConsoleStreamAdapterTest(unittest.TestCase):
    def test_failed_and_incomplete_events_raise_upstream_error(self) -> None:
        payload = orjson.dumps({
            "response": {"error": {"message": "upstream rejected request"}},
        }).decode()
        for event_type in ("response.failed", "response.incomplete"):
            with self.subTest(event_type=event_type):
                adapter = ConsoleStreamAdapter()
                with self.assertRaisesRegex(UpstreamError, "upstream rejected request") as raised:
                    adapter.feed(event_type, payload)
                self.assertEqual(raised.exception.status, 502)
                self.assertTrue(adapter._done)


class ConsoleHandlerValidationTest(unittest.TestCase):
    def test_all_console_handlers_reject_user_tools_before_account_selection(self) -> None:
        tools = [{"type": "function", "function": {"name": "weather"}}]
        messages = [{"role": "user", "content": "hello"}]

        async def run() -> None:
            with self.assertRaises(ValidationError):
                await console_chat_completions(
                    model="grok-4.3-console",
                    messages=messages,
                    tools=tools,
                )
            with self.assertRaises(ValidationError):
                await console_responses_create(
                    model="grok-4.3-console",
                    messages=messages,
                    stream=False,
                    emit_think=False,
                    temperature=0.8,
                    top_p=0.95,
                    response_id="resp_test",
                    reasoning_id="rs_test",
                    message_id="msg_test",
                    tools=tools,
                )
            with self.assertRaises(ValidationError):
                await console_messages_create(
                    model="grok-4.3-console",
                    messages=messages,
                    stream=False,
                    emit_think=False,
                    temperature=0.8,
                    top_p=0.95,
                    msg_id="msg_test",
                    tools=tools,
                )

        asyncio.run(run())

    def test_all_console_handlers_reject_invalid_max_output_tokens_before_account_selection(self) -> None:
        messages = [{"role": "user", "content": "hello"}]

        async def run() -> None:
            with self.assertRaises(ValidationError):
                await console_chat_completions(
                    model="grok-4.3-console",
                    messages=messages,
                    max_output_tokens=0,
                )
            with self.assertRaises(ValidationError):
                await console_responses_create(
                    model="grok-4.3-console",
                    messages=messages,
                    stream=False,
                    emit_think=False,
                    temperature=0.8,
                    top_p=0.95,
                    response_id="resp_test",
                    reasoning_id="rs_test",
                    message_id="msg_test",
                    max_output_tokens=0,
                )
            with self.assertRaises(ValidationError):
                await console_messages_create(
                    model="grok-4.3-console",
                    messages=messages,
                    stream=False,
                    emit_think=False,
                    temperature=0.8,
                    top_p=0.95,
                    msg_id="msg_test",
                    max_output_tokens=0,
                )

        asyncio.run(run())

    def test_console_anthropic_stream_ends_with_message_stop_only(self) -> None:
        class Config:
            def get_float(self, _key: str, default: float) -> float:
                return default

            def get(self, _key: str, default=None):
                return default

        class Account:
            token = "sso_test"

        class Directory:
            release = AsyncMock()
            feedback = AsyncMock()

        async def upstream_stream(*_args, **_kwargs):
            yield "response.output_text.delta", orjson.dumps({"delta": "hello"}).decode()
            yield "response.completed", orjson.dumps({
                "response": {"usage": {"input_tokens": 1, "output_tokens": 1}},
            }).decode()

        async def quota_sync(*_args, **_kwargs) -> None:
            return None

        async def run() -> list[str]:
            directory = Directory()
            with (
                patch("app.dataplane.account._directory", directory),
                patch.object(console_messages_module, "get_config", return_value=Config()),
                patch.object(console_messages_module, "selection_max_retries", return_value=0),
                patch.object(console_messages_module, "reserve_account", AsyncMock(return_value=(Account(), 5))),
                patch.object(console_messages_module, "stream_console_chat", upstream_stream),
                patch.object(console_messages_module, "_quota_sync", quota_sync),
            ):
                stream = await console_messages_create(
                    model="grok-4.3-console",
                    messages=[{"role": "user", "content": "hello"}],
                    stream=True,
                    emit_think=False,
                    temperature=0.8,
                    top_p=0.95,
                    msg_id="msg_test",
                )
                return [chunk async for chunk in stream]

        chunks = asyncio.run(run())
        self.assertTrue(chunks[-1].startswith("event: message_stop"))
        self.assertFalse(any("data: [DONE]" in chunk for chunk in chunks))

    def test_public_api_handlers_preserve_console_validation(self) -> None:
        tools = [{"type": "function", "function": {"name": "weather"}}]

        async def run() -> None:
            with self.assertRaises(ValidationError):
                await openai_chat_completions(
                    model="grok-4.3-console",
                    messages=[{"role": "user", "content": "hello"}],
                    stream=False,
                    tools=tools,
                )
            with self.assertRaises(ValidationError):
                await openai_responses_create(
                    model="grok-4.3-console",
                    input_val="hello",
                    instructions=None,
                    stream=False,
                    emit_think=False,
                    temperature=0.8,
                    top_p=0.95,
                    tools=tools,
                )
            with self.assertRaises(ValidationError):
                await anthropic_messages_create(
                    model="grok-4.3-console",
                    messages=[{"role": "user", "content": "hello"}],
                    stream=False,
                    emit_think=False,
                    temperature=0.8,
                    top_p=0.95,
                    tools=tools,
                )
            with self.assertRaises(ValidationError):
                await anthropic_messages_create(
                    model="grok-4.3-console",
                    messages=[{
                        "role": "user",
                        "content": [{"type": "audio", "source": {"type": "base64", "data": "AA=="}}],
                    }],
                    stream=False,
                    emit_think=False,
                    temperature=0.8,
                    top_p=0.95,
                )

        asyncio.run(run())

    def test_public_api_services_forward_model_output_limits(self) -> None:
        async def run() -> None:
            with patch(
                "app.products.openai.console_chat.completions",
                AsyncMock(return_value={"object": "chat.completion"}),
            ) as console_chat:
                await openai_chat_completions(
                    model="grok-4.3-console",
                    messages=[{"role": "user", "content": "hello"}],
                    stream=False,
                    max_tokens=111,
                )
                self.assertEqual(console_chat.await_args.kwargs["max_output_tokens"], 111)

            with patch(
                "app.products.openai.console_responses.create",
                AsyncMock(return_value={"object": "response"}),
            ) as console_responses:
                await openai_responses_create(
                    model="grok-4.3-console",
                    input_val="hello",
                    instructions=None,
                    stream=False,
                    emit_think=False,
                    temperature=0.8,
                    top_p=0.95,
                    max_output_tokens=222,
                )
                self.assertEqual(console_responses.await_args.kwargs["max_output_tokens"], 222)

            with patch(
                "app.products.anthropic.console_messages.create",
                AsyncMock(return_value={"type": "message"}),
            ) as console_messages:
                await anthropic_messages_create(
                    model="grok-4.3-console",
                    messages=[{"role": "user", "content": "hello"}],
                    stream=False,
                    emit_think=False,
                    temperature=0.8,
                    top_p=0.95,
                    max_tokens=333,
                )
                self.assertEqual(console_messages.await_args.kwargs["max_output_tokens"], 333)

        asyncio.run(run())

    def test_api_routers_forward_output_limits(self) -> None:
        async def run() -> None:
            with patch.object(
                openai_router_module,
                "chat_completions",
                AsyncMock(return_value={"object": "chat.completion"}),
            ) as chat_create:
                await openai_router_module.chat_completions_endpoint(
                    ChatCompletionRequest(
                        model="grok-4.3-console",
                        messages=[{"role": "user", "content": "hello"}],
                        stream=False,
                        max_tokens=444,
                    )
                )
                self.assertEqual(chat_create.await_args.kwargs["max_tokens"], 444)

            with patch(
                "app.products.openai.responses.create",
                AsyncMock(return_value={"object": "response"}),
            ) as responses_create:
                await openai_router_module.responses_endpoint(
                    ResponsesCreateRequest(
                        model="grok-4.3-console",
                        input="hello",
                        stream=False,
                        max_output_tokens=555,
                    )
                )
                self.assertEqual(responses_create.await_args.kwargs["max_output_tokens"], 555)

            with patch(
                "app.products.anthropic.messages.create",
                AsyncMock(return_value={"type": "message"}),
            ) as messages_create:
                await anthropic_router_module.messages_endpoint(
                    MessagesRequest(
                        model="grok-4.3-console",
                        messages=[{"role": "user", "content": "hello"}],
                        stream=False,
                        max_tokens=666,
                    )
                )
                self.assertEqual(messages_create.await_args.kwargs["max_tokens"], 666)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
