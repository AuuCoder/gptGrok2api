from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.register import grok_protocol, grok_register


class ProtobufTest(unittest.TestCase):
    def test_varint_round_trip(self) -> None:
        for value in (0, 1, 127, 128, 16384, 2**63 - 1):
            encoded = grok_protocol.encode_varint(value)
            decoded, offset = grok_protocol.decode_varint(encoded)
            self.assertEqual(decoded, value)
            self.assertEqual(offset, len(encoded))

    def test_create_email_request_uses_email_and_castle_fields(self) -> None:
        payload = grok_protocol.create_email_validation_request("user@example.com", "castle-token")
        fields = grok_protocol.parse_protobuf_fields(payload)

        self.assertEqual(fields[1], [b"user@example.com"])
        self.assertEqual(fields[3], [b"castle-token"])
        self.assertNotIn(2, fields)

    def test_create_email_request_omits_empty_castle_field(self) -> None:
        payload = grok_protocol.create_email_validation_request("user@example.com", "")

        self.assertEqual(grok_protocol.parse_protobuf_fields(payload), {1: [b"user@example.com"]})

    def test_validate_password_uses_captured_descriptor_fields(self) -> None:
        payload = grok_protocol.validate_password_request("user@example.com", "Secret123!")

        self.assertEqual(
            grok_protocol.parse_protobuf_fields(payload),
            {4: [b"user@example.com"], 5: [b"Secret123!"]},
        )

    def test_verify_email_request_only_uses_descriptor_fields(self) -> None:
        payload = grok_protocol.verify_email_validation_request("user@example.com", "ABC-123")
        fields = grok_protocol.parse_protobuf_fields(payload)

        self.assertEqual(fields, {1: [b"user@example.com"], 2: [b"ABC-123"]})


class GrpcWebTest(unittest.TestCase):
    def test_parses_empty_message_and_success_trailer(self) -> None:
        body = grok_protocol.grpc_web_envelope(b"") + grok_protocol.grpc_web_envelope(
            b"grpc-status: 0\r\ngrpc-message: \r\n",
            flags=0x80,
        )

        result = grok_protocol.decode_grpc_web_response(body)

        self.assertEqual(result.messages, (b"",))
        self.assertEqual(result.status, 0)

    def test_checks_grpc_status_from_headers_when_body_is_empty(self) -> None:
        with self.assertRaises(grok_protocol.GrpcWebError) as raised:
            grok_protocol.decode_grpc_web_response(
                b"",
                {"grpc-status": "3", "grpc-message": "invalid%20code"},
            )

        self.assertEqual(raised.exception.status, 3)
        self.assertIn("invalid code", str(raised.exception))

    def test_verify_accepts_grpc_success_without_response_message(self) -> None:
        client = grok_protocol.GrokProtocolClient()
        self.addCleanup(client.close)
        response = grok_protocol.GrpcWebResult((), {}, 0, "")
        with patch.object(client, "_grpc_post", return_value=response):
            token = client.verify_email_validation_code("user@example.com", "ABC-123")

        self.assertEqual(token, "")

    def test_protocol_session_does_not_inherit_environment_proxy(self) -> None:
        client = grok_protocol.GrokProtocolClient()
        self.addCleanup(client.close)

        self.assertFalse(client.session.trust_env)


class LandingMetadataTest(unittest.TestCase):
    @staticmethod
    def _next_script(payload: str) -> str:
        return f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script>"

    def test_extracts_constants_and_pruned_router_tree(self) -> None:
        route = [
            "",
            {
                "children": [
                    "(app)",
                    {
                        "children": [
                            "(auth)",
                            {
                                "children": [
                                    "sign-up",
                                    {"children": ['__PAGE__?{"redirect":"grok-com"}', {}]},
                                ]
                            },
                        ]
                    },
                ]
            },
            "$undefined",
            "$undefined",
            16,
        ]
        root = {"f": [[[route]]], "sitekey": "0x4AAAA-test", "castlePk": "pk_castle-test"}
        html = self._next_script("0:" + json.dumps(root, separators=(",", ":")))

        self.assertEqual(grok_protocol.extract_turnstile_sitekey(html), "0x4AAAA-test")
        self.assertEqual(grok_protocol.extract_castle_pk(html), "pk_castle-test")
        self.assertEqual(
            grok_protocol.extract_next_router_state_tree(html),
            [
                "",
                {
                    "children": [
                        "(app)",
                        {
                            "children": [
                                "(auth)",
                                {
                                    "children": [
                                        "sign-up",
                                        {"children": ["__PAGE__", {}]},
                                    ]
                                },
                            ]
                        },
                    ]
                },
            ],
        )

    def test_extracts_42_character_server_action(self) -> None:
        action_id = "7f50061dd2f5b389a530e4a048d5fdf0c48d1d9259"
        source = (
            'let tK=(0,tU.createServerReference)("'
            + action_id
            + '",tU.callServer,void 0,tU.findSourceMapURL,"default");'
            + "emailValidationCode createUserAndSessionRequest"
        )

        self.assertEqual(grok_protocol.extract_action_id(source), action_id)

    def test_extracts_castle_lazy_chunk_mapping(self) -> None:
        source = (
            "CastleProvider createRequestToken e.A(37942);"
            '37942,e=>{e.v(t=>Promise.all(["static/chunks/castle-current.js"].map(t=>e.l(t))))}'
        )

        self.assertEqual(
            grok_protocol.extract_castle_lazy_chunk(source),
            "static/chunks/castle-current.js",
        )


class FlightTest(unittest.TestCase):
    def test_resolves_root_promise_record(self) -> None:
        payload = (
            '0:{"a":"$@1","f":"unused"}\n'
            '1:"https://grok.com/?referrer=accounts"\n'
        )

        self.assertEqual(
            grok_protocol.parse_flight_result(payload),
            "https://grok.com/?referrer=accounts",
        )

    def test_returns_server_action_error(self) -> None:
        payload = (
            '0:{"a":"$@1"}\n'
            '1:{"error":"[internal] Failed to verify Cloudflare turnstile token."}\n'
        )

        self.assertEqual(
            grok_protocol.parse_flight_result(payload),
            {"error": "[internal] Failed to verify Cloudflare turnstile token."},
        )

    def test_resolves_length_delimited_text_record(self) -> None:
        redirect_url = "https://grok.com/auth/callback?exchange=" + ("a" * 1400)
        payload = (
            b'0:{"a":"$@1"}\n'
            + f"1:T{len(redirect_url.encode('utf-8')):x},".encode()
            + redirect_url.encode()
            + b'2:{"ignored":true}\n'
        )

        self.assertEqual(grok_protocol.parse_flight_result(payload), redirect_url)

    def test_resolves_nested_model_reference_to_length_delimited_url(self) -> None:
        redirect_url = "https://auth.grok.com/set-cookie?q=" + ("a" * 1200)
        payload = (
            b'0:{"a":"$@1"}\n'
            b'1:"$18"\n'
            + f"18:T{len(redirect_url.encode('utf-8')):x},".encode()
            + redirect_url.encode()
            + b'19:{"ignored":true}\n'
        )

        self.assertEqual(grok_protocol.parse_flight_result(payload), redirect_url)

    def test_extracts_action_redirect_and_navigation_mode(self) -> None:
        self.assertEqual(
            grok_protocol.extract_action_redirect(
                {"X-Action-Redirect": "https://grok.com/auth/callback?exchange=token;push"}
            ),
            "https://grok.com/auth/callback?exchange=token",
        )
        self.assertEqual(grok_protocol.extract_action_redirect({}), "")

    def test_sensitive_redirect_summary_does_not_expose_exchange_value(self) -> None:
        secret = "one-time-exchange-secret"

        summary = grok_protocol.summarize_sensitive_url(
            f"https://grok.com/auth/callback?exchange={secret}&referrer=accounts"
        )

        self.assertIn("https://grok.com/auth/callback", summary)
        self.assertIn("exchange(len=24,sha256=", summary)
        self.assertIn("referrer(len=8,sha256=", summary)
        self.assertNotIn(secret, summary)

    def test_follows_action_redirect_without_flight_content_type(self) -> None:
        signup_url = "https://accounts.x.ai/sign-up?redirect=grok-com"
        redirect_url = "https://grok.com/auth/callback?exchange=session-token"
        client = grok_protocol.GrokProtocolClient()
        self.addCleanup(client.close)
        client.metadata = grok_protocol.SignupMetadata(
            signup_url=signup_url,
            action_id="a" * 42,
            sitekey="0x-test",
            castle_pk="pk-test",
            router_state_tree=[],
            castle_sdk_url="https://accounts.x.ai/castle.js",
            castle_sdk_path="/tmp/castle.js",
        )
        response = MagicMock(
            status_code=200,
            text="",
            content=b"",
            url=signup_url,
            headers={"x-action-redirect": redirect_url + ";replace"},
        )

        exchange = grok_protocol.SessionExchangeResult(
            redirect_url,
            redirect_url,
            200,
            1,
            "sso_cookie",
            sso="sso-token",
            sso_rw="sso-rw-token",
        )

        with (
            patch.object(client, "_prewarm_grok_session"),
            patch.object(client, "create_castle_token", return_value="castle-token"),
            patch.object(client, "_server_action_request", return_value=response),
            patch.object(client, "_follow_signup_result", return_value=exchange) as follow,
        ):
            result = client.create_user_and_session(
                email="user@example.com",
                code="ABC-123",
                given_name="Test",
                family_name="User",
                password="Secret123!",
                turnstile_token="turnstile-token",
            )

        follow.assert_called_once_with(redirect_url, base_url=signup_url)
        self.assertEqual(result["sso"], "sso-token")
        self.assertEqual(result["redirect_url"], grok_protocol.summarize_sensitive_url(redirect_url))

    def test_xconsole_creation_matches_reference_payload(self) -> None:
        signup_url = "https://accounts.x.ai/sign-up?redirect=grok-com"
        redirect_url = "https://grok.com/auth/callback?exchange=session-token"
        client = grok_protocol.GrokProtocolClient({"signup_flow": "xconsole"})
        self.addCleanup(client.close)
        client.metadata = grok_protocol.SignupMetadata(
            signup_url=signup_url,
            action_id="a" * 42,
            sitekey="0x-test",
            castle_pk="pk-test",
            router_state_tree=[],
            castle_sdk_url="https://accounts.x.ai/castle.js",
            castle_sdk_path="/tmp/castle.js",
        )
        response = MagicMock(
            status_code=200,
            text="",
            content=b"",
            url=signup_url,
            headers={"x-action-redirect": redirect_url + ";replace"},
        )
        exchange = grok_protocol.SessionExchangeResult(
            redirect_url,
            redirect_url,
            200,
            1,
            "sso_cookie",
            sso="sso-token",
        )

        with (
            patch.object(client, "_prewarm_grok_session") as prewarm,
            patch.object(client, "create_castle_token") as create_castle,
            patch.object(client, "_server_action_request", return_value=response) as submit,
            patch.object(client, "_follow_signup_result", return_value=exchange),
        ):
            result = client.create_user_and_session(
                email="user@example.com",
                code="ABC-123",
                given_name="Test",
                family_name="User",
                password="Secret123!",
                turnstile_token="turnstile-token",
            )

        payload = submit.call_args.args[0]
        self.assertEqual(payload["createUserAndSessionRequest"]["tosAcceptedVersion"], "$undefined")
        self.assertEqual(payload["castleRequestToken"], "")
        self.assertTrue(payload["conversionId"])
        prewarm.assert_not_called()
        create_castle.assert_not_called()
        self.assertEqual(result["sso"], "sso-token")

    def test_xconsole_email_and_password_rpcs_match_reference_sequence(self) -> None:
        client = grok_protocol.GrokProtocolClient({"signup_flow": "xconsole"})
        self.addCleanup(client.close)
        grpc_result = grok_protocol.GrpcWebResult((), {}, 0, "")

        with (
            patch.object(client, "create_castle_token") as create_castle,
            patch.object(client, "_grpc_post", return_value=grpc_result) as grpc_post,
        ):
            client.send_email_validation_code("user@example.com")
            client.validate_password("user@example.com", "Secret123!")

        create_castle.assert_not_called()
        self.assertEqual(
            [call.args[0] for call in grpc_post.call_args_list],
            [
                "/auth_mgmt.AuthManagement/CreateEmailValidationCode",
                "/auth_mgmt.AuthManagement/ValidatePassword",
            ],
        )
        self.assertEqual(
            grok_protocol.parse_protobuf_fields(grpc_post.call_args_list[0].args[1]),
            {1: [b"user@example.com"]},
        )

    def test_reports_business_error_from_non_2xx_flight_response(self) -> None:
        signup_url = "https://accounts.x.ai/sign-up?redirect=grok-com"
        client = grok_protocol.GrokProtocolClient()
        self.addCleanup(client.close)
        client.metadata = grok_protocol.SignupMetadata(
            signup_url=signup_url,
            action_id="a" * 42,
            sitekey="0x-test",
            castle_pk="pk-test",
            router_state_tree=[],
            castle_sdk_url="https://accounts.x.ai/castle.js",
            castle_sdk_path="/tmp/castle.js",
        )
        response = SimpleNamespace(
            status_code=500,
            text="",
            content=(
                b'0:{"a":"$@1"}\n'
                b'1:{"error":"[invalid_argument] account_email_domain_rejected"}\n'
            ),
            url=signup_url,
            headers={"content-type": "text/x-component"},
        )

        with (
            patch.object(client, "_prewarm_grok_session"),
            patch.object(client, "create_castle_token", return_value="castle-token"),
            patch.object(client, "_server_action_request", return_value=response),
        ):
            with self.assertRaises(grok_protocol.GrokProtocolError) as raised:
                client.create_user_and_session(
                    email="user@example.com",
                    code="ABC-123",
                    given_name="Test",
                    family_name="User",
                    password="Secret123!",
                    turnstile_token="turnstile-token",
                )

        error = raised.exception
        self.assertIn("account_email_domain_rejected", str(error))
        self.assertTrue(error.mail_retryable)
        self.assertTrue(error.retryable)
        self.assertEqual(error.reason_code, "server_action_http_500")

    def test_opaque_server_error_reports_only_safe_response_fingerprint(self) -> None:
        signup_url = "https://accounts.x.ai/sign-up?redirect=grok-com"
        action_id = "a" * 42
        secret_body = b"internal response with user@example.com and a secret token"
        client = grok_protocol.GrokProtocolClient()
        self.addCleanup(client.close)
        client.metadata = grok_protocol.SignupMetadata(
            signup_url=signup_url,
            action_id=action_id,
            sitekey="0x-test",
            castle_pk="pk-test",
            router_state_tree=[],
            castle_sdk_url="https://accounts.x.ai/castle.js",
            castle_sdk_path="/tmp/castle.js",
        )
        response = SimpleNamespace(
            status_code=500,
            text=secret_body.decode(),
            content=secret_body,
            url=signup_url,
            headers={"content-type": "text/html; charset=utf-8", "cf-ray": "test-ray-NRT"},
        )

        with (
            patch.object(client, "_prewarm_grok_session"),
            patch.object(client, "create_castle_token", return_value="castle-token"),
            patch.object(client, "_server_action_request", return_value=response),
        ):
            with self.assertRaises(grok_protocol.GrokProtocolError) as raised:
                client.create_user_and_session(
                    email="user@example.com",
                    code="ABC-123",
                    given_name="Test",
                    family_name="User",
                    password="Secret123!",
                    turnstile_token="turnstile-token",
                )

        message = str(raised.exception)
        self.assertIn("Grok Server Action HTTP 500", message)
        self.assertIn("type=text/html", message)
        self.assertIn(f"body={len(secret_body)}B", message)
        self.assertIn("cf-ray=test-ray-NRT", message)
        self.assertIn("action-sha256=", message)
        self.assertNotIn("user@example.com", message)
        self.assertNotIn("secret token", message)
        self.assertEqual(raised.exception.reason_code, "server_action_http_500")

    def test_prewarm_obtains_device_cookie_before_submission(self) -> None:
        client = grok_protocol.GrokProtocolClient()
        self.addCleanup(client.close)
        response = SimpleNamespace(status_code=200)

        def request(*_args, **_kwargs):
            client.session.cookies.set("grok_device_id", "device-id", domain=".grok.com", path="/")
            return response

        with patch.object(client, "_request", side_effect=request) as mocked:
            client._prewarm_grok_session()

        self.assertTrue(client._grok_session_warmed)
        self.assertEqual(mocked.call_args.args[:2], ("GET", "https://grok.com/"))

    def test_prewarm_retries_transient_transport_error(self) -> None:
        client = grok_protocol.GrokProtocolClient()
        self.addCleanup(client.close)
        response = SimpleNamespace(status_code=200)
        attempts = 0

        def request(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary TLS failure")
            client.session.cookies.set("grok_device_id", "device-id", domain=".grok.com", path="/")
            return response

        with (
            patch.object(client, "_request", side_effect=request),
            patch.object(grok_protocol.time, "sleep", return_value=None),
        ):
            client._prewarm_grok_session()

        self.assertEqual(attempts, 2)
        self.assertTrue(client._grok_session_warmed)

    def test_callback_reads_sso_from_set_cookie_without_client_javascript(self) -> None:
        client = grok_protocol.GrokProtocolClient()
        self.addCleanup(client.close)
        secret = "one-time-exchange-secret"
        logs: list[str] = []
        client.log = logs.append
        response = SimpleNamespace(
            status_code=200,
            url=f"https://grok.com/auth/callback?exchange={secret}",
            headers={"set-cookie": "sso=sso-token; Path=/; Secure; HttpOnly"},
            cookies=None,
        )

        with patch.object(client, "_request", return_value=response) as mocked:
            result = client._follow_signup_result(response.url)

        self.assertEqual(result.sso, "sso-token")
        self.assertEqual(result.reason_code, "sso_cookie")
        self.assertFalse(mocked.call_args.kwargs["allow_redirects"])
        self.assertNotIn(secret, "\n".join(logs))

    def test_callback_rejects_cookie_domain_that_response_host_cannot_set(self) -> None:
        client = grok_protocol.GrokProtocolClient()
        self.addCleanup(client.close)
        response = SimpleNamespace(
            status_code=200,
            url="https://auth.grokusercontent.com/set-cookie?q=secret",
            headers={"set-cookie": "sso=fake-token; Domain=.grok.com; Path=/; Secure; HttpOnly"},
            cookies=None,
        )

        with patch.object(client, "_request", return_value=response):
            result = client._follow_signup_result(response.url)

        self.assertEqual(result.reason_code, "callback_no_sso")
        self.assertEqual(result.sso, "")

    def test_callback_follows_bounded_redirect_and_reads_sso_rw(self) -> None:
        client = grok_protocol.GrokProtocolClient()
        self.addCleanup(client.close)
        first = SimpleNamespace(
            status_code=303,
            url="https://auth.grokipedia.com/set-cookie?q=first-secret",
            headers={
                "location": "https://auth.grokusercontent.com/set-cookie?q=second-secret",
                "set-cookie": "sso=intermediate-token; Domain=.grokipedia.com; Path=/; Secure; HttpOnly",
            },
            cookies=None,
        )
        second = SimpleNamespace(
            status_code=303,
            url="https://auth.grokusercontent.com/set-cookie?q=second-secret",
            headers={
                "location": "https://auth.grok.com/set-cookie?q=third-secret",
                "set-cookie": "sso=intermediate-token; Domain=.grokusercontent.com; Path=/; Secure; HttpOnly",
            },
            cookies=None,
        )
        third = SimpleNamespace(
            status_code=303,
            url="https://auth.grok.com/set-cookie?q=third-secret",
            headers={
                "location": "https://grok.com/?referrer=accounts",
                "set-cookie": "sso-rw=rw-token; Domain=.grok.com; Path=/; Secure; HttpOnly",
            },
            cookies=None,
        )
        fourth = SimpleNamespace(
            status_code=200,
            url="https://grok.com/?referrer=accounts",
            headers={},
            cookies=None,
        )

        responses = iter([first, second, third, fourth])

        def request(*_args, **_kwargs):
            response = next(responses)
            if response is third:
                client.session.cookies.set("sso-rw", "rw-token", domain=".grok.com", path="/")
            return response

        with patch.object(client, "_request", side_effect=request) as mocked:
            result = client._follow_signup_result(first.url)

        self.assertEqual(result.sso, "rw-token")
        self.assertEqual(result.sso_rw, "rw-token")
        self.assertEqual(result.hops, 4)
        self.assertEqual(mocked.call_count, 4)
        for call in mocked.call_args_list:
            self.assertNotIn("Sec-Fetch-Site", call.kwargs["headers"])
        self.assertEqual(mocked.call_args_list[1].kwargs["headers"]["Referer"], "https://auth.grokipedia.com/")
        self.assertEqual(mocked.call_args_list[2].kwargs["headers"]["Referer"], "https://auth.grokusercontent.com/")
        self.assertEqual(mocked.call_args_list[3].kwargs["headers"]["Referer"], "https://auth.grok.com/")

    def test_missing_sso_returns_partial_account_without_raw_exchange(self) -> None:
        signup_url = "https://accounts.x.ai/sign-up?redirect=grok-com"
        secret = "one-time-exchange-secret"
        redirect_url = f"https://grok.com/auth/callback?exchange={secret}"
        client = grok_protocol.GrokProtocolClient()
        self.addCleanup(client.close)
        client.metadata = grok_protocol.SignupMetadata(
            signup_url=signup_url,
            action_id="a" * 42,
            sitekey="0x-test",
            castle_pk="pk-test",
            router_state_tree=[],
            castle_sdk_url="https://accounts.x.ai/castle.js",
            castle_sdk_path="/tmp/castle.js",
        )
        response = SimpleNamespace(
            status_code=200,
            text="",
            content=(b'0:{"a":"$@1"}\n' + f'1:"{redirect_url}"\n'.encode()),
            url=signup_url,
            headers={"content-type": "text/x-component"},
        )
        exchange = grok_protocol.SessionExchangeResult(
            redirect_url,
            redirect_url,
            200,
            1,
            "callback_no_sso",
        )

        with (
            patch.object(client, "_prewarm_grok_session"),
            patch.object(client, "create_castle_token", return_value="castle-token"),
            patch.object(client, "_server_action_request", return_value=response),
            patch.object(client, "_follow_signup_result", return_value=exchange),
            patch.object(client, "_cookie_value", return_value=""),
        ):
            with self.assertRaises(grok_protocol.GrokProtocolError) as raised:
                client.create_user_and_session(
                    email="user@example.com",
                    code="ABC-123",
                    given_name="Test",
                    family_name="User",
                    password="Secret123!",
                    turnstile_token="turnstile-token",
                )

        error = raised.exception
        self.assertTrue(error.account_created)
        self.assertEqual(error.reason_code, "callback_no_sso")
        self.assertEqual(error.partial_result["status"], "pending_sso")
        self.assertEqual(error.partial_result["password"], "Secret123!")
        self.assertNotIn(secret, repr(error.partial_result))

    def test_rejects_external_session_redirect_before_request(self) -> None:
        client = grok_protocol.GrokProtocolClient()
        self.addCleanup(client.close)
        with patch.object(client, "_request") as mocked:
            with self.assertRaises(grok_protocol.GrokProtocolError) as raised:
                client._follow_signup_result("https://example.com/callback?exchange=secret")

        self.assertEqual(raised.exception.reason_code, "redirect_not_allowed")
        mocked.assert_not_called()

    def test_detects_action_not_found_header(self) -> None:
        response = MagicMock(status_code=200, text="", headers={"x-nextjs-action-not-found": "1"})

        self.assertTrue(grok_protocol.GrokProtocolClient._invalid_action_response(response))


class TurnstileSolverTest(unittest.TestCase):
    def test_local_solver_calls_solve_endpoint_without_api_key(self) -> None:
        calls: list[tuple[str, dict, dict]] = []

        def transport(url: str, payload: dict, headers: dict) -> dict:
            calls.append((url, payload, headers))
            return {"solved": True, "token": "local-turnstile-token"}

        solver = grok_protocol.TurnstileSolver(
            {
                "provider": "local",
                "api_base": "http://127.0.0.1:8877/",
                "proxy": "http://proxy.example.test:8080",
                "captcha_timeout": 90,
            },
            transport=transport,
        )
        self.addCleanup(solver.close)

        token = solver.solve(
            website_url="https://accounts.x.ai/sign-up?redirect=grok-com",
            sitekey="0x4AAAA-test",
            action="signup",
        )

        self.assertEqual(token, "local-turnstile-token")
        self.assertEqual(calls[0][0], "http://127.0.0.1:8877/solve")
        self.assertEqual(calls[0][1]["type"], "turnstile")
        self.assertEqual(calls[0][1]["action"], "signup")
        self.assertIs(calls[0][1]["real_page"], True)
        self.assertEqual(calls[0][1]["timeout_s"], 60)
        self.assertEqual(calls[0][1]["queue_timeout_s"], 25)
        self.assertEqual(calls[0][1]["concurrency"], 2)
        self.assertEqual(calls[0][1]["proxy"], "http://proxy.example.test:8080")
        self.assertNotIn("Authorization", calls[0][2])

    def test_local_solver_retries_unsolved_browser_attempt(self) -> None:
        calls: list[dict] = []
        responses = iter([
            {"solved": False, "error": "Token not received"},
            {"solved": True, "token": "retry-token"},
        ])

        def transport(_url: str, payload: dict, _headers: dict) -> dict:
            calls.append(payload)
            return next(responses)

        solver = grok_protocol.TurnstileSolver(
            {
                "provider": "local",
                "captcha_timeout": 90,
                "local_attempt_timeout": 30,
                "local_max_attempts": 2,
                "local_concurrency": 6,
            },
            transport=transport,
        )
        self.addCleanup(solver.close)

        token = solver.solve(
            website_url="https://accounts.x.ai/sign-up",
            sitekey="0x-test",
        )

        self.assertEqual(token, "retry-token")
        self.assertEqual(len(calls), 2)
        self.assertEqual([call["timeout_s"] for call in calls], [30, 30])
        self.assertEqual([call["queue_timeout_s"] for call in calls], [55, 55])
        self.assertEqual([call["concurrency"] for call in calls], [6, 6])

    def test_local_solver_reports_unsolved_response(self) -> None:
        solver = grok_protocol.TurnstileSolver(
            {"provider": "local", "local_max_attempts": 1},
            transport=lambda _url, _payload, _headers: {
                "solved": False,
                "error": "Token not received",
            },
        )
        self.addCleanup(solver.close)

        with self.assertRaisesRegex(grok_protocol.GrokProtocolError, "Token not received"):
            solver.solve(website_url="https://accounts.x.ai/sign-up", sitekey="0x-test")

    def test_local_solver_reserves_queue_time_for_last_short_attempt(self) -> None:
        calls: list[dict] = []

        def transport(_url: str, payload: dict, _headers: dict) -> dict:
            calls.append(payload)
            return {"solved": False, "error": "Token not received"}

        solver = grok_protocol.TurnstileSolver(
            {
                "provider": "local",
                "captcha_timeout": 24,
                "local_attempt_timeout": 45,
                "local_queue_timeout": 60,
                "local_max_attempts": 1,
            },
            transport=transport,
        )
        self.addCleanup(solver.close)

        with self.assertRaises(grok_protocol.GrokProtocolError):
            solver.solve(website_url="https://accounts.x.ai/sign-up", sitekey="0x-test")

        self.assertEqual(calls[0]["timeout_s"], 10)
        self.assertEqual(calls[0]["queue_timeout_s"], 9)

    def test_yescaptcha_json_task_flow(self) -> None:
        calls: list[tuple[str, dict, dict]] = []
        responses = iter(
            [
                {"errorId": 0, "taskId": "task-1"},
                {"errorId": 0, "status": "processing"},
                {"errorId": 0, "status": "ready", "solution": {"token": "turnstile-token"}},
            ]
        )

        def transport(url: str, payload: dict, headers: dict) -> dict:
            calls.append((url, payload, headers))
            return next(responses)

        solver = grok_protocol.TurnstileSolver(
            {
                "provider": "yescaptcha",
                "api_key": "secret",
                "captcha_timeout": 30,
                "captcha_poll_interval": 1,
            },
            transport=transport,
        )
        self.addCleanup(solver.close)
        with patch.object(grok_protocol.time, "sleep", return_value=None):
            token = solver.solve(
                website_url="https://accounts.x.ai/sign-up?redirect=grok-com",
                sitekey="0x4AAAA-test",
                action="signup",
            )

        self.assertEqual(token, "turnstile-token")
        self.assertEqual(calls[0][0], "https://api.yescaptcha.com/createTask")
        self.assertEqual(calls[0][1]["task"]["action"], "signup")
        self.assertEqual(calls[-1][1]["taskId"], "task-1")


class GrokWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_config = copy.deepcopy(grok_register.config)
        self.original_account_result_sink = grok_register.account_result_sink
        grok_register.config.clear()
        grok_register.config.update(
            {
                "mail": {
                    "request_timeout": 30,
                    "wait_timeout": 30,
                    "wait_interval": 1,
                    "api_use_register_proxy": True,
                    "providers": [
                        {
                            "type": "icloud_api",
                            "enable": True,
                            "project": "openai",
                            "keyword": "OpenAI",
                        }
                    ],
                },
                "proxy": "http://proxy.example.test:8080",
                "grok": {"max_mail_retries": 1, "provider": "yescaptcha", "api_key": "key"},
            }
        )

    def tearDown(self) -> None:
        grok_register.account_result_sink = self.original_account_result_sink
        grok_register.config.clear()
        grok_register.config.update(self.original_config)

    def test_runtime_mail_config_forces_grok_icloud_filters(self) -> None:
        mail = grok_register._mail_config("http://proxy.example.test:8080")

        self.assertEqual(mail["providers"][0]["project"], "grok")
        self.assertEqual(mail["providers"][0]["keyword"], "xAI")
        self.assertEqual(mail["proxy"], "http://proxy.example.test:8080")

    def test_global_proxy_resolves_once_for_mail_and_protocol(self) -> None:
        profile = SimpleNamespace(
            proxy_url="http://127.0.0.1:40080",
            proxy_source="runtime",
        )
        with patch.object(grok_register.proxy_settings, "get_profile", return_value=profile) as get_profile:
            proxy, source = grok_register._resolve_register_proxy("")

        get_profile.assert_called_once_with(proxy="", upstream=True)
        self.assertEqual(proxy, "http://127.0.0.1:40080")
        self.assertEqual(source, "runtime")
        self.assertEqual(grok_register._mail_config(proxy)["proxy"], proxy)

    @patch.object(grok_register, "GrokProtocolClient")
    @patch.object(grok_register.mail_provider, "mark_mailbox_result")
    @patch.object(grok_register.mail_provider, "wait_for_code", return_value="  AB-C 123  ")
    @patch.object(grok_register.mail_provider, "create_mailbox")
    def test_worker_success_contract(
        self,
        create_mailbox: MagicMock,
        _wait_for_code: MagicMock,
        mark_result: MagicMock,
        client_type: MagicMock,
    ) -> None:
        create_mailbox.return_value = {
            "provider": "icloud_api",
            "address": "relay@icloud.example",
            "label": "Grok registration",
        }
        client = client_type.return_value
        client.solve_turnstile.return_value = "turnstile-token"
        client.create_user_and_session.return_value = {
            "sso": "sso-token",
            "redirect_url": "https://grok.com/",
        }
        client.session_cookies.return_value = {"sso": "sso-token", "session": "live-cookie"}

        log_messages: list[str] = []
        with patch.object(
            grok_register,
            "register_log_sink",
            side_effect=lambda text, _color: log_messages.append(text),
        ):
            result = grok_register.worker(1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["email"], "relay@icloud.example")
        self.assertEqual(result["result"]["sso"], "sso-token")
        self.assertEqual(
            result["result"]["oauth_session_cookies"],
            {"sso": "sso-token", "session": "live-cookie"},
        )
        self.assertEqual(result["result"]["source_type"], "protocol")
        client.verify_email_validation_code.assert_called_once_with(
            "relay@icloud.example",
            "AB-C 123",
        )
        self.assertEqual(
            log_messages[:-1],
            [
                "[任务1] 准备注册环境",
                "[任务1] 获取注册邮箱",
                "[任务1] 验证码已发送，等待邮件",
                "[任务1] 邮箱验证完成，正在进行安全校验",
                "[任务1] 安全校验完成，正在创建账号",
            ],
        )
        self.assertRegex(log_messages[-1], r"^\[任务1\] 注册成功（\d+\.\d 秒）$")
        self.assertNotIn("AB-C 123", "\n".join(log_messages))
        self.assertNotIn("relay@icloud.example", "\n".join(log_messages))
        call_names = [call[0] for call in client.method_calls]
        self.assertLess(call_names.index("verify_email_validation_code"), call_names.index("solve_turnstile"))
        self.assertLess(call_names.index("validate_password"), call_names.index("solve_turnstile"))
        mark_result.assert_called_once_with(create_mailbox.return_value, success=True)

    @patch.object(grok_register, "GrokProtocolClient")
    @patch.object(grok_register.mail_provider, "mark_mailbox_result")
    @patch.object(grok_register.mail_provider, "create_mailbox")
    def test_worker_failure_contract(
        self,
        create_mailbox: MagicMock,
        mark_result: MagicMock,
        client_type: MagicMock,
    ) -> None:
        create_mailbox.return_value = {
            "provider": "icloud_api",
            "address": "relay@icloud.example",
            "label": "Grok registration",
        }
        client_type.return_value.bootstrap.side_effect = grok_protocol.GrokProtocolError(
            "bootstrap failed",
            stage="bootstrap",
        )

        result = grok_register.worker(2)

        self.assertFalse(result["ok"])
        self.assertEqual(result["index"], 2)
        self.assertIn("bootstrap failed", result["error"])
        mark_result.assert_called_once()
        self.assertFalse(mark_result.call_args.kwargs["success"])

    @patch.object(grok_register, "_random_password", return_value="Secret123!")
    @patch.object(grok_register, "_random_name", return_value=("Test", "User"))
    @patch.object(grok_register, "GrokProtocolClient")
    @patch.object(grok_register.mail_provider, "mark_mailbox_result")
    @patch.object(grok_register.mail_provider, "wait_for_code", return_value="ABC-123")
    @patch.object(grok_register.mail_provider, "create_mailbox")
    def test_worker_persists_pending_sso_and_consumes_mailbox(
        self,
        create_mailbox: MagicMock,
        _wait_for_code: MagicMock,
        mark_result: MagicMock,
        client_type: MagicMock,
        _random_name: MagicMock,
        _random_password: MagicMock,
    ) -> None:
        create_mailbox.return_value = {
            "provider": "icloud_api",
            "address": "relay@icloud.example",
            "label": "Grok registration",
        }
        client = client_type.return_value
        client.solve_turnstile.return_value = "turnstile-token"
        pending = {
            "email": "relay@icloud.example",
            "password": "Secret123!",
            "sso": "",
            "profile": {"session_state": "missing", "session_reason": "callback_no_sso"},
            "status": "pending_sso",
        }
        client.create_user_and_session.side_effect = grok_protocol.GrokProtocolError(
            "missing sso",
            stage="session_exchange",
            reason_code="callback_no_sso",
            account_created=True,
            partial_result=pending,
        )
        sink = MagicMock()
        grok_register.account_result_sink = sink

        result = grok_register.worker(3)

        self.assertFalse(result["ok"])
        self.assertEqual(result["account"]["status"], "pending_sso")
        self.assertTrue(result["account_persisted"])
        self.assertEqual(sink.call_count, 2)
        self.assertEqual(sink.call_args_list[0].args[0]["status"], "submitting")
        self.assertEqual(sink.call_args_list[1].args[0]["status"], "pending_sso")
        mark_result.assert_called_once_with(create_mailbox.return_value, success=True)


if __name__ == "__main__":
    unittest.main()
