from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

from services.register import openai_register


class PasswordlessSignupFallbackTest(unittest.TestCase):
    @staticmethod
    def _registrar() -> openai_register.PlatformRegistrar:
        registrar = object.__new__(openai_register.PlatformRegistrar)
        registrar.proxy = ""
        registrar.session = MagicMock()
        registrar.clearance_user_agent = ""
        registrar.clearance_failure_reason = ""
        registrar.device_id = "test-device"
        registrar.fingerprint = {}
        registrar.passwordless_signup = False
        registrar.platform_auth_code = ""
        registrar.last_otp_continue_url = ""
        return registrar

    @staticmethod
    def _response(payload: dict, status_code: int) -> MagicMock:
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = payload
        response.text = str(payload)
        return response

    def test_explicit_disabled_response_raises_dedicated_fallback_signal(self) -> None:
        registrar = self._registrar()
        response = self._response(
            {"page": {"payload": {"passwordless_disabled": True}}},
            400,
        )
        registrar._json_headers = MagicMock(return_value={})

        with (
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")),
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            self.assertRaises(openai_register.PasswordlessSignupUnavailable),
        ):
            registrar._start_passwordless_signup(1)

    def test_non_explicit_response_does_not_raise_fallback_signal(self) -> None:
        registrar = self._registrar()
        response = self._response({"error": {"code": "temporarily_unavailable"}}, 503)
        registrar._json_headers = MagicMock(return_value={})

        with (
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")),
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            self.assertRaises(RuntimeError) as raised,
        ):
            registrar._start_passwordless_signup(1)

        self.assertNotIsInstance(raised.exception, openai_register.PasswordlessSignupUnavailable)
        self.assertIn("passwordless_send_otp_http_503", str(raised.exception))

    def test_explicit_disabled_marker_uses_legacy_password_registration(self) -> None:
        registrar = self._registrar()
        registrar._platform_authorize = MagicMock(return_value="")
        registrar._start_passwordless_signup = MagicMock(
            side_effect=openai_register.PasswordlessSignupUnavailable("disabled")
        )
        registrar._register_user = MagicMock()
        registrar._send_otp = MagicMock()
        registrar._validate_otp = MagicMock()
        registrar._create_account = MagicMock()
        registrar._exchange_registered_tokens = MagicMock(
            return_value={"access_token": "access", "refresh_token": "refresh", "id_token": "id"}
        )
        mailbox = {"address": "fallback@example.test", "label": "test"}

        with (
            patch.object(openai_register, "create_mailbox", return_value=mailbox),
            patch.object(openai_register, "wait_for_code", return_value="123456") as wait_for_code,
            patch.object(openai_register.mail_provider, "mark_mailbox_result") as mark_mailbox_result,
            patch.object(openai_register, "_random_password", return_value="SafePassword1!"),
            patch.object(openai_register, "_random_name", return_value=("Ada", "Lovelace")),
            patch.object(openai_register, "_random_birthdate", return_value="1990-01-01"),
        ):
            result = registrar.register(1)

        self.assertEqual(result["password"], "SafePassword1!")
        self.assertEqual(result["source_type"], "web")
        registrar._register_user.assert_called_once_with("fallback@example.test", "SafePassword1!", 1)
        registrar._send_otp.assert_called_once_with(1)
        wait_for_code.assert_called_once_with(mailbox, register_proxy="")
        registrar._validate_otp.assert_called_once_with("123456", 1)
        registrar._create_account.assert_called_once_with("Ada Lovelace", "1990-01-01", 1)
        mark_mailbox_result.assert_called_once_with(mailbox, success=True)

    def test_other_passwordless_failure_does_not_use_legacy_registration(self) -> None:
        registrar = self._registrar()
        registrar._platform_authorize = MagicMock(return_value="")
        registrar._start_passwordless_signup = MagicMock(side_effect=RuntimeError("temporary upstream failure"))
        registrar._register_user = MagicMock()
        registrar._send_otp = MagicMock()
        mailbox = {"address": "no-fallback@example.test", "label": "test"}

        with (
            patch.object(openai_register, "create_mailbox", return_value=mailbox),
            patch.object(openai_register, "wait_for_code") as wait_for_code,
            patch.object(openai_register.mail_provider, "mark_mailbox_result") as mark_mailbox_result,
        ):
            with self.assertRaisesRegex(RuntimeError, "temporary upstream failure"):
                registrar.register(1)

        registrar._register_user.assert_not_called()
        registrar._send_otp.assert_not_called()
        wait_for_code.assert_not_called()
        mark_mailbox_result.assert_called_once()
        self.assertFalse(mark_mailbox_result.call_args.kwargs["success"])


class ValidateOtpTest(unittest.TestCase):
    def test_validate_otp_submits_the_code_once(self) -> None:
        session = MagicMock()
        response = MagicMock(status_code=409)

        with (
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")) as request,
            patch.object(openai_register, "build_sentinel_token") as sentinel,
        ):
            result, error = openai_register.validate_otp(session, "test-device", "123456")

        self.assertIs(result, response)
        self.assertEqual(error, "")
        request.assert_called_once()
        self.assertEqual(request.call_args.args[:3], (session, "post", "https://auth.openai.com/api/accounts/email-otp/validate"))
        self.assertEqual(request.call_args.kwargs["json"], {"code": "123456"})
        self.assertEqual(request.call_args.kwargs["retry_attempts"], 1)
        sentinel.assert_not_called()


class ChatGPTWebRegistrarTest(unittest.TestCase):
    @staticmethod
    def _registrar() -> openai_register.ChatGPTWebRegistrar:
        registrar = object.__new__(openai_register.ChatGPTWebRegistrar)
        registrar.proxy = ""
        registrar.session = MagicMock()
        registrar.clearance_user_agent = ""
        registrar.clearance_failure_reason = ""
        registrar.device_id = "initial-device"
        registrar.fingerprint = {
            "user_agent": "test-agent",
            "impersonate": "chrome142",
            "sec_ch_ua": '"Google Chrome";v="142"',
        }
        registrar.passwordless_signup = False
        registrar.platform_auth_code = ""
        registrar.last_otp_continue_url = ""
        return registrar

    @staticmethod
    def _response(payload: dict, status_code: int = 200, *, url: str = "") -> MagicMock:
        response = MagicMock()
        response.status_code = status_code
        response.url = url
        response.headers = {}
        response.text = str(payload)
        response.json.return_value = payload
        return response

    def test_authorize_continue_uses_chatgpt_username_shape(self) -> None:
        registrar = self._registrar()
        registrar._json_headers = MagicMock(return_value={})
        response = self._response({"page": {"type": "email_otp_verification"}})

        with (
            patch.object(openai_register, "build_sentinel_with_so_token", return_value=("sentinel", "observer", "oai-sc-token")),
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")) as request,
        ):
            registrar._chatgpt_continue_username("person@example.test", 1)

        self.assertEqual(
            request.call_args.kwargs["json"],
            {
                "username": {"kind": "email", "value": "person@example.test"},
                "screen_hint": "login_or_signup",
            },
        )
        self.assertTrue(request.call_args.kwargs["allow_redirects"] is False)
        self.assertEqual(request.call_args.kwargs["headers"]["openai-sentinel-token"], "sentinel")
        self.assertEqual(request.call_args.kwargs["headers"]["openai-sentinel-so-token"], "observer")
        registrar.session.cookies.set.assert_any_call("oai-sc", "oai-sc-token", domain=".auth.openai.com")
        self.assertEqual(registrar._chatgpt_otp_sentinel_token, "sentinel")
        self.assertEqual(registrar._chatgpt_otp_sentinel_so_token, "observer")

    def test_send_otp_reuses_chatgpt_authorize_sentinel_pair(self) -> None:
        registrar = self._registrar()
        registrar._json_headers = MagicMock(return_value={"content-type": "application/json", "origin": "https://auth.openai.com"})
        registrar._chatgpt_otp_sentinel_token = "sentinel"
        registrar._chatgpt_otp_sentinel_so_token = "observer"
        response = self._response({})

        with (
            patch.object(openai_register, "build_sentinel_with_so_token") as sentinel,
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")) as request,
        ):
            registrar._send_chatgpt_otp(1)

        self.assertEqual(request.call_args.kwargs["headers"]["openai-sentinel-token"], "sentinel")
        self.assertEqual(request.call_args.kwargs["headers"]["openai-sentinel-so-token"], "observer")
        self.assertEqual(request.call_args.kwargs["headers"]["content-type"], "application/json")
        self.assertEqual(request.call_args.kwargs["headers"]["origin"], "https://auth.openai.com")
        registrar._json_headers.assert_called_once_with("https://auth.openai.com/email-verification")
        sentinel.assert_not_called()

    def test_send_otp_requires_authorize_sentinel_context(self) -> None:
        registrar = self._registrar()

        with self.assertRaisesRegex(RuntimeError, "缺少 authorize/continue"):
            registrar._send_chatgpt_otp(1)

    def test_build_sentinel_token_keeps_oai_sc_cookie(self) -> None:
        session = MagicMock()
        with patch.object(openai_register, "_build_sentinel_token_tuple", return_value=("sentinel", "oai-sc-token")):
            result = openai_register.build_sentinel_token(session, "device", "authorize_continue")

        self.assertEqual(result, "sentinel")
        session.cookies.set.assert_any_call("oai-sc", "oai-sc-token", domain=".auth.openai.com")
        session.cookies.set.assert_any_call("oai-sc", "oai-sc-token", domain="auth.openai.com")

    def test_cookie_presence_reads_curl_cffi_cookie_jar(self) -> None:
        cookie = SimpleNamespace(name="oai-did")
        session = MagicMock()
        session.cookies.get_dict.return_value = {"oai-sc": "secret"}
        session.cookies.jar = [cookie]

        state = openai_register._cookie_presence(session, ("oai-did", "oai-sc", "missing"))

        self.assertEqual(state, "oai-did=yes,oai-sc=yes,missing=no")

    def test_send_otp_surfaces_upstream_error_message(self) -> None:
        registrar = self._registrar()
        registrar._json_headers = MagicMock(return_value={})
        registrar._chatgpt_otp_sentinel_token = "sentinel"
        registrar._chatgpt_otp_sentinel_so_token = "observer"
        response = self._response({"error": {"message": "request rejected"}})

        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")),
            self.assertRaisesRegex(RuntimeError, "request rejected"),
        ):
            registrar._send_chatgpt_otp(1)

    def test_web_authorize_warms_nextauth_and_uses_returned_device_id(self) -> None:
        registrar = self._registrar()
        registrar._chatgpt_csrf = MagicMock(side_effect=["csrf-first", "csrf-second"])
        authorize_url = (
            "https://auth.openai.com/api/accounts/authorize?client_id=chatgpt-client&device_id=browser-device"
        )
        registrar._chatgpt_begin_signin = MagicMock(
            side_effect=["https://chatgpt.com/api/auth/signin?csrf=true", authorize_url]
        )
        registrar._navigate_headers = MagicMock(return_value={})
        landing = self._response({}, url="https://auth.openai.com/log-in-or-create-account")

        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(landing, "")),
        ):
            registrar._chatgpt_web_authorize(1)

        self.assertEqual(registrar.device_id, "browser-device")
        self.assertEqual(registrar._chatgpt_csrf.call_count, 2)
        self.assertEqual(registrar._chatgpt_begin_signin.call_count, 2)
        registrar.session.cookies.set.assert_any_call("oai-did", "browser-device", domain=".auth.openai.com")

    def test_callback_reads_chatgpt_session_access_token(self) -> None:
        registrar = self._registrar()
        registrar._navigate_headers = MagicMock(return_value={})
        callback = self._response({}, url="https://chatgpt.com/")
        session = self._response({"accessToken": "chatgpt-access", "user": {"email": "person@example.test"}})
        registrar._chatgpt_request = MagicMock(return_value=(session, ""))

        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(callback, "")),
        ):
            data = registrar._complete_chatgpt_callback(
                "https://chatgpt.com/api/auth/callback/openai?code=opaque",
                1,
            )

        self.assertEqual(data["accessToken"], "chatgpt-access")
        self.assertEqual(registrar._chatgpt_request.call_args.args[:3], ("get", "/api/auth/session", 1))

    def test_callback_retries_until_chatgpt_session_access_token_is_ready(self) -> None:
        registrar = self._registrar()
        registrar._navigate_headers = MagicMock(return_value={})
        callback = self._response({}, url="https://chatgpt.com/")
        pending_session = self._response({"user": {"email": "person@example.test"}})
        ready_session = self._response({"accessToken": "chatgpt-access"})
        registrar._chatgpt_request = MagicMock(
            side_effect=[(pending_session, ""), (ready_session, "")]
        )

        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(callback, "")),
            patch.object(openai_register.time, "sleep") as sleep,
        ):
            data = registrar._complete_chatgpt_callback(
                "https://chatgpt.com/api/auth/callback/openai?code=opaque",
                1,
            )

        self.assertEqual(data["accessToken"], "chatgpt-access")
        self.assertEqual(registrar._chatgpt_request.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_callback_stops_after_chatgpt_session_access_token_timeout(self) -> None:
        registrar = self._registrar()
        registrar._navigate_headers = MagicMock(return_value={})
        callback = self._response({}, url="https://chatgpt.com/")
        pending_session = self._response({"user": {"email": "person@example.test"}})
        registrar._chatgpt_request = MagicMock(return_value=(pending_session, ""))

        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(callback, "")),
            patch.object(openai_register.time, "sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "/api/auth/session 未返回 accessToken"),
        ):
            registrar._complete_chatgpt_callback(
                "https://chatgpt.com/api/auth/callback/openai?code=opaque",
                1,
            )

        self.assertEqual(registrar._chatgpt_request.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 3])

    def test_profile_keeps_chatgpt_callback_url_without_platform_token_exchange(self) -> None:
        registrar = self._registrar()
        registrar._json_headers = MagicMock(return_value={})
        callback_url = "https://chatgpt.com/api/auth/callback/openai?code=opaque&state=state"
        response = self._response({"continue_url": callback_url})

        with (
            patch.object(openai_register, "build_sentinel_with_so_token", return_value=("sentinel", "", "")),
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")),
        ):
            result = registrar._create_chatgpt_profile("Ada Lovelace", "1990-01-01", 1)

        self.assertEqual(result, callback_url)

    def test_worker_defaults_to_platform_registrar(self) -> None:
        fake_registrar = MagicMock()
        fake_registrar.register.return_value = {
            "email": "registered@example.test",
            "access_token": "chatgpt-access",
            "source_type": "chatgpt_web",
        }
        original_stats = dict(openai_register.stats)
        openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})
        try:
            with (
                patch.object(openai_register, "PlatformRegistrar", return_value=fake_registrar) as registrar_type,
                patch.object(openai_register.account_service, "add_account_items"),
                patch.object(openai_register.account_service, "refresh_accounts", return_value={"errors": []}),
                patch.object(openai_register, "_checkout_config", return_value={"enabled": False}),
            ):
                result = openai_register.worker(1)
        finally:
            openai_register.stats.update(original_stats)

        self.assertTrue(result["ok"])
        registrar_type.assert_called_once_with(openai_register.config["proxy"])
        fake_registrar.close.assert_called_once()

    def test_register_imports_chatgpt_web_session_not_platform_tokens(self) -> None:
        registrar = self._registrar()
        registrar._chatgpt_web_authorize = MagicMock()
        registrar._chatgpt_continue_username = MagicMock(return_value={"page": {"type": "email_otp_verification"}})
        registrar._send_chatgpt_otp = MagicMock()
        registrar._validate_chatgpt_otp = MagicMock(return_value="https://auth.openai.com/about-you")
        registrar._create_chatgpt_profile = MagicMock(
            return_value="https://chatgpt.com/api/auth/callback/openai?code=opaque"
        )
        registrar._complete_chatgpt_callback = MagicMock(
            return_value={"accessToken": "chatgpt-access", "user": {"email": "registered@example.test", "id": "u-1"}}
        )
        mailbox = {"address": "registered@example.test", "label": "test"}

        with (
            patch.object(openai_register, "create_mailbox", return_value=mailbox),
            patch.object(openai_register, "wait_for_code", return_value="123456") as wait_for_code,
            patch.object(openai_register.mail_provider, "mark_mailbox_result") as mark_mailbox_result,
            patch.object(openai_register, "_random_name", return_value=("Ada", "Lovelace")),
            patch.object(openai_register, "_random_birthdate", return_value="1990-01-01"),
        ):
            result = registrar.register(1)

        self.assertEqual(result["access_token"], "chatgpt-access")
        self.assertEqual(result["source_type"], "chatgpt_web")
        self.assertEqual(result["user_id"], "u-1")
        self.assertEqual(mailbox["_icloud_keyword"], "ChatGPT")
        wait_for_code.assert_called_once_with(mailbox, register_proxy="")
        mark_mailbox_result.assert_called_once_with(mailbox, success=True)

    def test_register_explains_when_chatgpt_mailbox_never_receives_otp(self) -> None:
        registrar = self._registrar()
        registrar._chatgpt_web_authorize = MagicMock()
        registrar._chatgpt_continue_username = MagicMock(return_value={"page": {"type": "email_otp_verification"}})
        registrar._send_chatgpt_otp = MagicMock()
        mailbox = {"address": "missing@example.test", "label": "test"}

        with (
            patch.object(openai_register, "create_mailbox", return_value=mailbox),
            patch.object(openai_register, "wait_for_code", return_value=None),
            patch.object(openai_register.mail_provider, "mark_mailbox_result") as mark_mailbox_result,
            patch.object(openai_register, "_random_name", return_value=("Ada", "Lovelace")),
        ):
            with self.assertRaisesRegex(RuntimeError, "邮箱服务未返回新邮件"):
                registrar.register(1)

        mark_mailbox_result.assert_called_once()
        self.assertFalse(mark_mailbox_result.call_args.kwargs["success"])


class ChatGPTMailboxDeliveryTest(unittest.TestCase):
    def test_cf_mailbox_uses_short_wait_and_resends_once(self) -> None:
        mailbox = {
            "address": "cf@example.test",
            "provider": "cloudflare_temp_email",
            "provider_ref": "cloudflare_temp_email:cf",
            "label": "CF 邮箱",
        }
        resend = MagicMock()

        with (
            patch.object(openai_register, "_configured_mail_wait_timeout", return_value=180),
            patch.object(openai_register, "wait_for_code", side_effect=[None, "123456"]) as wait_for_code,
            patch.object(openai_register, "step"),
        ):
            code = openai_register._wait_for_chatgpt_registration_code(
                mailbox,
                1,
                "",
                resend=resend,
            )

        self.assertEqual(code, "123456")
        self.assertEqual(wait_for_code.call_count, 2)
        self.assertEqual(wait_for_code.call_args_list[0].kwargs["wait_timeout"], 60.0)
        self.assertEqual(wait_for_code.call_args_list[1].kwargs["wait_timeout"], 60.0)
        resend.assert_called_once_with()
        self.assertIn("_received_after", mailbox)

    def test_cf_mailbox_second_timeout_requests_provider_fallback(self) -> None:
        mailbox = {
            "address": "cf@example.test",
            "provider": "cloudflare_temp_email",
            "provider_ref": "cloudflare_temp_email:cf",
            "label": "CF 邮箱",
        }
        resend = MagicMock()

        with (
            patch.object(openai_register, "_configured_mail_wait_timeout", return_value=180),
            patch.object(openai_register, "wait_for_code", side_effect=[None, None]),
            patch.object(openai_register, "step"),
            self.assertRaises(openai_register.OpenAIMailboxDeliveryTimeout) as raised,
        ):
            openai_register._wait_for_chatgpt_registration_code(mailbox, 1, "", resend=resend)

        self.assertEqual(raised.exception.provider_ref, "cloudflare_temp_email:cf")
        self.assertIn("投递超时", str(raised.exception))
        resend.assert_called_once_with()

    def test_non_cf_mailbox_keeps_configured_wait_without_resend(self) -> None:
        mailbox = {
            "address": "icloud@example.test",
            "provider": "icloud_api",
            "provider_ref": "icloud_api:primary",
            "label": "iCloud",
        }
        resend = MagicMock()

        with (
            patch.object(openai_register, "wait_for_code", return_value="654321") as wait_for_code,
            patch.object(openai_register, "step"),
        ):
            code = openai_register._wait_for_chatgpt_registration_code(mailbox, 1, "", resend=resend)

        self.assertEqual(code, "654321")
        wait_for_code.assert_called_once_with(mailbox, register_proxy="")
        resend.assert_not_called()

    def test_non_cf_query_error_keeps_the_real_provider_failure(self) -> None:
        mailbox = {
            "address": "outlook@example.test",
            "provider": "outlook_token",
            "provider_ref": "outlook_token:primary",
            "label": "Outlook",
        }
        query_error = RuntimeError(
            "graph: OutlookToken 刷新失败: HTTP 400, AADSTS70000: The grant is expired"
        )
        resend = MagicMock()

        with (
            patch.object(openai_register, "wait_for_code", side_effect=query_error),
            patch.object(openai_register, "step"),
            self.assertRaises(RuntimeError) as raised,
        ):
            openai_register._wait_for_chatgpt_registration_code(
                mailbox,
                1,
                "",
                resend=resend,
            )

        self.assertIs(raised.exception, query_error)
        self.assertNotIsInstance(
            raised.exception,
            openai_register.OpenAIMailboxDeliveryTimeout,
        )
        resend.assert_not_called()


class TraditionalChatGPTRegistrarTest(unittest.TestCase):
    @staticmethod
    def _registrar() -> openai_register.TraditionalChatGPTRegistrar:
        registrar = object.__new__(openai_register.TraditionalChatGPTRegistrar)
        registrar.proxy = ""
        registrar.session = MagicMock()
        registrar.session.cookies.get.return_value = ""
        registrar.clearance_user_agent = ""
        registrar.clearance_failure_reason = ""
        registrar.device_id = "initial-device"
        registrar.fingerprint = {
            "user_agent": "test-agent",
            "impersonate": "chrome142",
            "sec_ch_ua": '"Google Chrome";v="142"',
        }
        return registrar

    @staticmethod
    def _response(payload: dict, status_code: int = 200, *, url: str = "") -> MagicMock:
        response = MagicMock()
        response.status_code = status_code
        response.url = url
        response.headers = {}
        response.text = str(payload)
        response.json.return_value = payload
        return response

    def test_reference_authorization_uses_one_plain_nextauth_signin(self) -> None:
        registrar = self._registrar()
        csrf = self._response({"csrfToken": "csrf-token"})
        authorize_url = "https://auth.openai.com/api/accounts/authorize?device_id=browser-device"
        signin = self._response({"url": authorize_url})
        landing = self._response({}, url="https://auth.openai.com/create-account")
        registrar._chatgpt_request = MagicMock(side_effect=[(csrf, ""), (signin, "")])

        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(landing, "")),
        ):
            registrar._reference_start_authorization(1)

        self.assertEqual(registrar._chatgpt_request.call_count, 2)
        signin_call = registrar._chatgpt_request.call_args_list[1]
        self.assertEqual(signin_call.args[:3], ("post", "/api/auth/signin/openai", 1))
        self.assertEqual(signin_call.kwargs["data"]["csrfToken"], "csrf-token")
        self.assertNotIn("x-openai-target-path", signin_call.kwargs["headers"])
        self.assertNotIn("x-openai-target-route", signin_call.kwargs["headers"])
        self.assertEqual(registrar.device_id, "browser-device")

    def test_reference_authorization_retries_only_csrf_bootstrap(self) -> None:
        registrar = self._registrar()
        csrf_first = self._response({"csrfToken": "csrf-first"})
        bootstrap = self._response({"url": "https://chatgpt.com/api/auth/signin?csrf=true"})
        csrf_second = self._response({"csrfToken": "csrf-second"})
        authorize_url = "https://auth.openai.com/api/accounts/authorize?device_id=browser-device"
        signin = self._response({"url": authorize_url})
        landing = self._response({}, url="https://auth.openai.com/create-account")
        registrar._chatgpt_request = MagicMock(
            side_effect=[(csrf_first, ""), (bootstrap, ""), (csrf_second, ""), (signin, "")]
        )

        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(landing, "")),
        ):
            registrar._reference_start_authorization(1)

        self.assertEqual(registrar._chatgpt_request.call_count, 4)
        second_signin = registrar._chatgpt_request.call_args_list[3]
        self.assertEqual(second_signin.kwargs["data"]["csrfToken"], "csrf-second")

    def test_reference_signup_requires_password_registration_page(self) -> None:
        registrar = self._registrar()
        response = self._response({"page": {"type": "create_account_password"}})

        with (
            patch.object(openai_register, "build_sentinel_token", return_value="authorize-sentinel"),
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")) as request,
        ):
            mode = registrar._reference_signup("person@example.test", 1)

        self.assertEqual(mode, "password")
        self.assertEqual(request.call_args.kwargs["json"], {
            "username": {"value": "person@example.test", "kind": "email"},
            "screen_hint": "signup",
        })
        self.assertEqual(request.call_args.kwargs["headers"]["referer"], "https://auth.openai.com/create-account")
        self.assertEqual(request.call_args.kwargs["headers"]["openai-sentinel-token"], "authorize-sentinel")

    def test_reference_signup_accepts_otp_first_state_without_password_registration(self) -> None:
        registrar = self._registrar()
        response = self._response({
            "page": {
                "type": "email_otp_verification",
                "payload": {"email_verification_mode": "passwordless_signup"},
            },
            "continue_url": "https://auth.openai.com/email-verification",
        })

        with (
            patch.object(openai_register, "build_sentinel_token", return_value="authorize-sentinel"),
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")),
        ):
            mode = registrar._reference_signup("person@example.test", 1)

        self.assertEqual(mode, "otp")
        self.assertEqual(registrar._reference_email_verification_mode, "passwordless_signup")
        self.assertEqual(registrar._reference_authorize_sentinel, "authorize-sentinel")

    def test_reference_signup_rejects_conflicting_page_and_continue_state(self) -> None:
        registrar = self._registrar()
        response = self._response({
            "page": {
                "type": "email_otp_verification",
                "payload": {"email_verification_mode": "passwordless_signup"},
            },
            "continue_url": "https://auth.openai.com/create-account/password",
        })

        with (
            patch.object(openai_register, "build_sentinel_token", return_value="authorize-sentinel"),
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")),
        ):
            with self.assertRaisesRegex(RuntimeError, "状态冲突"):
                registrar._reference_signup("person@example.test", 1)

    def test_reference_otp_first_uses_resend_with_original_authorize_sentinel(self) -> None:
        registrar = self._registrar()
        registrar._reference_authorize_sentinel = "authorize-sentinel"
        response = self._response({})

        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")) as request,
        ):
            registrar._reference_resend_otp(1)

        self.assertEqual(request.call_args.args[1:3], ("post", "https://auth.openai.com/api/accounts/email-otp/resend"))
        self.assertEqual(request.call_args.kwargs["headers"]["openai-sentinel-token"], "authorize-sentinel")
        self.assertEqual(request.call_args.kwargs["headers"]["referer"], "https://auth.openai.com/email-verification")
        self.assertEqual(request.call_args.kwargs["retry_attempts"], 1)

    def test_reference_otp_first_rejects_success_status_with_error_body(self) -> None:
        registrar = self._registrar()
        registrar._reference_authorize_sentinel = "authorize-sentinel"
        response = self._response({"error": {"message": "OTP resend rate limited"}})

        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")),
        ):
            with self.assertRaisesRegex(RuntimeError, "OTP resend rate limited"):
                registrar._reference_resend_otp(1)

    def test_reference_otp_first_reports_cloudflare_before_waiting_for_email(self) -> None:
        registrar = self._registrar()
        registrar._reference_authorize_sentinel = "authorize-sentinel"
        response = self._response({}, status_code=403)
        response.text = "<title>Just a moment...</title>"
        registrar._refresh_cloudflare_clearance = MagicMock(return_value=None)

        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")),
        ):
            with self.assertRaisesRegex(RuntimeError, "被 Cloudflare 拦截"):
                registrar._reference_resend_otp(1)

        registrar._refresh_cloudflare_clearance.assert_called_once_with(openai_register.auth_base, 1)

    def test_register_handles_otp_first_without_sending_a_password(self) -> None:
        registrar = self._registrar()
        registrar._reference_start_authorization = MagicMock()
        registrar._reference_signup = MagicMock(return_value="otp")
        registrar._reference_email_verification_mode = "passwordless_signup"
        registrar._reference_validate_otp = MagicMock()
        registrar._reference_create_account = MagicMock(
            return_value="https://auth.openai.com/continue"
        )
        registrar._reference_capture_callback = MagicMock(
            return_value="https://chatgpt.com/api/auth/callback/openai?code=opaque"
        )
        registrar._complete_chatgpt_callback = MagicMock(
            return_value={"accessToken": "chatgpt-access", "user": {"email": "registered@example.test"}}
        )
        registrar._chatgpt_session_result = MagicMock(
            return_value={"email": "registered@example.test", "access_token": "chatgpt-access", "source_type": "chatgpt_web"}
        )
        registrar._reference_register_password = MagicMock()
        registrar._reference_send_otp = MagicMock()
        mailbox = {"address": "registered@example.test", "label": "test"}
        registrar._reference_resend_otp = MagicMock(
            side_effect=lambda _index: mailbox.update({"_received_after": "before-resend"})
        )

        with (
            patch.object(openai_register, "create_mailbox", return_value=mailbox),
            patch.object(openai_register, "wait_for_code", return_value="123456") as wait_for_code,
            patch.object(openai_register.mail_provider, "mark_mailbox_result") as mark_mailbox_result,
            patch.object(openai_register, "_random_name", return_value=("Ada", "Lovelace")),
            patch.object(openai_register, "_random_birthdate", return_value="1990-01-01"),
        ):
            result = registrar.register(1)

        self.assertEqual(result["access_token"], "chatgpt-access")
        self.assertNotIn("password", result)
        registrar._reference_resend_otp.assert_called_once_with(1)
        registrar._reference_register_password.assert_not_called()
        registrar._reference_send_otp.assert_not_called()
        registrar._reference_validate_otp.assert_called_once_with("123456", 1)
        registrar._reference_create_account.assert_called_once_with("Ada Lovelace", "1990-01-01", 1)
        registrar._reference_capture_callback.assert_called_once_with("https://auth.openai.com/continue", 1)
        wait_for_code.assert_called_once_with(mailbox, register_proxy="")
        self.assertNotEqual(mailbox["_received_after"], "before-resend")
        mark_mailbox_result.assert_called_once_with(mailbox, success=True)

    def test_register_rejects_unknown_otp_mode_without_resending(self) -> None:
        registrar = self._registrar()
        registrar._reference_start_authorization = MagicMock()
        registrar._reference_signup = MagicMock(return_value="otp")
        registrar._reference_email_verification_mode = "unrecognized_mode"
        registrar._reference_resend_otp = MagicMock()
        mailbox = {"address": "registered@example.test", "label": "test"}

        with (
            patch.object(openai_register, "create_mailbox", return_value=mailbox),
            patch.object(openai_register, "wait_for_code") as wait_for_code,
            patch.object(openai_register.mail_provider, "mark_mailbox_result") as mark_mailbox_result,
        ):
            with self.assertRaisesRegex(RuntimeError, "缺少可确认的新账号模式"):
                registrar.register(1)

        registrar._reference_resend_otp.assert_not_called()
        wait_for_code.assert_not_called()
        mark_mailbox_result.assert_called_once_with(mailbox, success=False, error=unittest.mock.ANY)

    def test_register_marks_existing_openai_email_as_claimed(self) -> None:
        registrar = self._registrar()
        registrar._reference_start_authorization = MagicMock()
        registrar._reference_signup = MagicMock(return_value="otp")
        registrar._reference_email_verification_mode = "passwordless_login"
        registrar._reference_resend_otp = MagicMock()
        mailbox = {"address": "existing@example.test", "label": "test"}

        with (
            patch.object(openai_register, "create_mailbox", return_value=mailbox),
            patch.object(openai_register.mail_provider, "mark_mailbox_result") as mark_mailbox_result,
            self.assertRaises(openai_register.OpenAIEmailAlreadyRegistered) as raised,
        ):
            registrar.register(1)

        self.assertEqual(raised.exception.email, "existing@example.test")
        registrar._reference_resend_otp.assert_not_called()
        mark_mailbox_result.assert_called_once_with(mailbox, success=True)

    def test_reference_password_registration_precedes_email_otp_send(self) -> None:
        registrar = self._registrar()
        page = self._response({})
        registered = self._response({})

        with (
            patch.object(openai_register, "build_sentinel_token", return_value="password-sentinel"),
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", side_effect=[(page, ""), (registered, "")]) as request,
        ):
            registrar._reference_register_password("person@example.test", "password", 1)

        self.assertEqual(request.call_args_list[0].args[1:3], ("get", "https://auth.openai.com/create-account/password"))
        self.assertEqual(request.call_args_list[1].args[1:3], ("post", "https://auth.openai.com/api/accounts/user/register"))
        self.assertEqual(request.call_args_list[1].kwargs["json"], {"password": "password", "username": "person@example.test"})

        send_response = self._response({})
        with (
            patch.object(openai_register, "build_sentinel_token") as sentinel,
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(send_response, "")) as request,
        ):
            registrar._reference_send_otp(1)

        self.assertEqual(request.call_args.args[1:3], ("get", "https://auth.openai.com/api/accounts/email-otp/send"))
        self.assertEqual(request.call_args.kwargs["headers"]["openai-sentinel-token"], "password-sentinel")
        sentinel.assert_not_called()

    def test_reference_validate_does_not_follow_continue_url(self) -> None:
        registrar = self._registrar()
        response = self._response({"continue_url": "https://auth.openai.com/about-you"})

        with patch.object(openai_register, "validate_otp", return_value=(response, "")) as validate:
            registrar._reference_validate_otp("123456", 1)

        validate.assert_called_once_with(registrar.session, registrar.device_id, "123456", registrar.fingerprint)

    def test_register_uses_traditional_reference_order(self) -> None:
        registrar = self._registrar()
        registrar._reference_start_authorization = MagicMock()
        registrar._reference_signup = MagicMock(return_value="password")
        registrar._reference_register_password = MagicMock()
        registrar._reference_send_otp = MagicMock()
        registrar._reference_validate_otp = MagicMock()
        registrar._reference_create_account = MagicMock(return_value="https://auth.openai.com/continue")
        registrar._reference_capture_callback = MagicMock(return_value="https://chatgpt.com/api/auth/callback/openai?code=opaque")
        registrar._complete_chatgpt_callback = MagicMock(return_value={"accessToken": "chatgpt-access", "user": {"email": "registered@example.test"}})
        registrar._chatgpt_session_result = MagicMock(return_value={"email": "registered@example.test", "access_token": "chatgpt-access", "source_type": "chatgpt_web"})
        mailbox = {"address": "registered@example.test", "label": "test"}

        with (
            patch.object(openai_register, "create_mailbox", return_value=mailbox),
            patch.object(openai_register, "wait_for_code", return_value="123456") as wait_for_code,
            patch.object(openai_register.mail_provider, "mark_mailbox_result") as mark_mailbox_result,
            patch.object(openai_register, "_random_name", return_value=("Ada", "Lovelace")),
            patch.object(openai_register, "_random_birthdate", return_value="1990-01-01"),
        ):
            result = registrar.register(1)

        self.assertEqual(result["access_token"], "chatgpt-access")
        self.assertEqual(result["password"], "registeredexample.test")
        registrar._reference_start_authorization.assert_called_once_with(1)
        registrar._reference_signup.assert_called_once_with("registered@example.test", 1)
        registrar._reference_register_password.assert_called_once_with("registered@example.test", "registeredexample.test", 1)
        registrar._reference_send_otp.assert_called_once_with(1)
        registrar._reference_validate_otp.assert_called_once_with("123456", 1)
        wait_for_code.assert_called_once_with(mailbox, register_proxy="")
        mark_mailbox_result.assert_called_once_with(mailbox, success=True)


class OpenAIExistingEmailRetryTest(unittest.TestCase):
    def test_single_enabled_provider_delivery_timeout_stops_without_looping(self) -> None:
        failed_mailbox = {
            "address": "cf@example.test",
            "provider": "cloudflare_temp_email",
            "provider_ref": "cloudflare_temp_email:cf",
            "label": "CF 邮箱",
        }
        failed = MagicMock()
        failed.register.side_effect = openai_register.OpenAIMailboxDeliveryTimeout(failed_mailbox)

        with (
            patch.object(openai_register, "_enabled_mail_provider_count", return_value=1),
            patch.object(openai_register, "PlatformRegistrar", return_value=failed) as factory,
            patch.object(openai_register, "step"),
            self.assertRaisesRegex(RuntimeError, "所有启用邮箱来源均未收到 ChatGPT 验证码"),
        ):
            openai_register._register_with_fresh_email(3)

        factory.assert_called_once_with(openai_register.config["proxy"])
        failed.close.assert_called_once_with()

    def test_mailbox_delivery_timeout_excludes_failed_provider_on_fresh_session(self) -> None:
        failed_mailbox = {
            "address": "cf@example.test",
            "provider": "cloudflare_temp_email",
            "provider_ref": "cloudflare_temp_email:cf",
            "label": "CF 邮箱",
        }
        failed = MagicMock()
        failed.register.side_effect = openai_register.OpenAIMailboxDeliveryTimeout(failed_mailbox)
        fresh = MagicMock()
        fresh.register.return_value = {"email": "fresh@example.test", "access_token": "access"}

        with (
            patch.object(openai_register, "_enabled_mail_provider_count", return_value=2),
            patch.object(openai_register, "PlatformRegistrar", side_effect=[failed, fresh]),
            patch.object(openai_register, "step") as step,
        ):
            registrar, result = openai_register._register_with_fresh_email(3)

        self.assertIs(registrar, fresh)
        self.assertEqual(result["email"], "fresh@example.test")
        self.assertEqual(failed.excluded_mail_provider_refs, set())
        self.assertEqual(
            fresh.excluded_mail_provider_refs,
            {"cloudflare_temp_email:cf"},
        )
        failed.close.assert_called_once_with()
        fresh.close.assert_not_called()
        self.assertTrue(any("正在切换下一个邮箱来源" in str(call) for call in step.call_args_list))

    def test_replaces_existing_account_email_with_fresh_registrar(self) -> None:
        existing = MagicMock()
        existing.register.side_effect = openai_register.OpenAIEmailAlreadyRegistered("used@example.test")
        fresh = MagicMock()
        fresh.register.return_value = {"email": "fresh@example.test", "access_token": "access"}

        with (
            patch.object(openai_register, "PlatformRegistrar", side_effect=[existing, fresh]) as factory,
            patch.object(openai_register, "step") as step,
        ):
            registrar, result = openai_register._register_with_fresh_email(3)

        self.assertIs(registrar, fresh)
        self.assertEqual(result["email"], "fresh@example.test")
        self.assertEqual(factory.call_count, 2)
        existing.close.assert_called_once_with()
        fresh.close.assert_not_called()
        self.assertTrue(any("已标记 GPT；自动更换邮箱" in str(call) for call in step.call_args_list))

    def test_stops_after_existing_account_retry_limit(self) -> None:
        registrars = [MagicMock(), MagicMock()]
        for index, registrar in enumerate(registrars, start=1):
            registrar.register.side_effect = openai_register.OpenAIEmailAlreadyRegistered(
                f"used-{index}@example.test"
            )

        with (
            patch.object(openai_register, "OPENAI_EXISTING_EMAIL_RETRY_LIMIT", 2),
            patch.object(openai_register, "PlatformRegistrar", side_effect=registrars),
            patch.object(openai_register, "step"),
            self.assertRaisesRegex(RuntimeError, "连续 2 个邮箱已存在 GPT 账号"),
        ):
            openai_register._register_with_fresh_email(1)

        for registrar in registrars:
            registrar.close.assert_called_once_with()


class RegistrationCheckoutWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._config = dict(openai_register.config)
        self._stats = dict(openai_register.stats)
        openai_register.config.update({
            "proxy": "",
            "sub2api_sync": {
                "enabled": False,
                "server_id": "",
                "group_mode": "existing",
                "group_id": "",
                "group_name": "",
            },
            "cpa_sync": {
                "enabled": False,
                "pool_id": "",
            },
            "checkout": {
                "enabled": True,
                "channel": "upi",
                "country": "IN",
                "currency": "INR",
                "checkout_ui_mode": "custom",
                "checkout_proxy_enabled": True,
                "checkout_proxy_url": "http://checkout-user:checkout-password@residential.example.test:8888",
                "promotion_proxy_enabled": True,
                "promotion_proxy_url": "http://promotion-user:promotion-password@promotion.example.test:8888",
                "provider_proxy_enabled": True,
                "provider_proxy_url": "http://provider-user:provider-password@provider.example.test:8888",
            },
        })
        openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})

    def tearDown(self) -> None:
        openai_register.config.clear()
        openai_register.config.update(self._config)
        openai_register.stats.clear()
        openai_register.stats.update(self._stats)

    def test_checkout_config_migrates_legacy_proxy_to_shared_in_stages(self) -> None:
        openai_register.config["checkout"] = {
            "residential_proxy_enabled": True,
            "residential_proxy_url": "http://legacy-user:legacy-password@legacy.example.test:8888",
            "promotion_proxy_enabled": True,
            "promotion_proxy_url": "http://promotion-user:promotion-password@promotion.example.test:9999",
            "provider_proxy_enabled": True,
            "provider_proxy_url": "http://provider-user:provider-password@provider.example.test:9999",
        }

        settings = openai_register._checkout_config()

        self.assertTrue(settings["checkout_proxy_enabled"])
        self.assertEqual(
            settings["checkout_proxy_url"],
            "http://legacy-user:legacy-password@legacy.example.test:8888",
        )
        self.assertTrue(settings["promotion_proxy_enabled"])
        self.assertEqual(
            settings["promotion_proxy_url"],
            "http://promotion-user:promotion-password@promotion.example.test:9999",
        )
        self.assertTrue(settings["provider_proxy_enabled"])
        self.assertEqual(
            settings["provider_proxy_url"],
            "http://legacy-user:legacy-password@legacy.example.test:8888",
        )

    def test_invalid_checkout_config_falls_back_to_upi(self) -> None:
        openai_register.config["checkout"] = {
            "enabled": True,
            "channel": "other",
            "country": "US",
            "currency": "USD",
            "checkout_ui_mode": "hosted",
            "threads": 9,
            "checkout_proxy_enabled": True,
            "checkout_proxy_url": "http://in-checkout.example.test:8888",
            "promotion_proxy_enabled": True,
            "promotion_proxy_url": "http://vn-promotion.example.test:8888",
            "provider_proxy_enabled": True,
            "provider_proxy_url": "http://in-provider.example.test:8888",
        }

        settings = openai_register._checkout_config()

        self.assertEqual(settings["channel"], "upi")
        self.assertEqual(settings["country"], "IN")
        self.assertEqual(settings["currency"], "INR")
        self.assertEqual(settings["checkout_ui_mode"], "custom")
        self.assertEqual(settings["threads"], 9)
        self.assertEqual(settings["checkout_proxy_url"], "http://in-checkout.example.test:8888")
        self.assertEqual(settings["promotion_proxy_url"], "http://vn-promotion.example.test:8888")
        self.assertEqual(settings["provider_proxy_url"], "http://in-checkout.example.test:8888")

    def test_checkout_config_accepts_independent_pix_channel(self) -> None:
        openai_register.config["checkout"] = {
            "enabled": True,
            "channel": "pix",
            "country": "IN",
            "currency": "INR",
            "checkout_proxy_enabled": True,
            "checkout_proxy_url": "http://br-shared.example.test:8888",
        }

        settings = openai_register._checkout_config()

        self.assertEqual(settings["channel"], "pix")
        self.assertEqual(settings["country"], "BR")
        self.assertEqual(settings["currency"], "BRL")
        self.assertEqual(settings["checkout_proxy_url"], "http://br-shared.example.test:8888")
        self.assertEqual(settings["provider_proxy_url"], "http://br-shared.example.test:8888")

    def test_worker_saves_account_before_syncing_to_sub2api_and_records_success(self) -> None:
        openai_register.config["checkout"] = {"enabled": False}
        openai_register.config["sub2api_sync"] = {
            "enabled": True,
            "server_id": "sub2api-primary",
            "group_mode": "existing",
            "group_id": "42",
            "group_name": "新注册账号",
        }
        registrar = MagicMock()
        registrar.register.return_value = {
            "email": "new@example.test",
            "access_token": "chatgpt-access",
            "refresh_token": "chatgpt-refresh",
            "source_type": "chatgpt_web",
        }
        server = {"id": "sub2api-primary", "name": "主 Sub2API", "base_url": "https://sub2api.example.test"}
        call_order: list[str] = []

        def save_account(_items: list[dict]) -> None:
            call_order.append("saved")

        def sync_account(_server: dict, _account: dict, _settings: dict) -> dict:
            call_order.append("synced")
            return {
                "ok": True,
                "server_name": "主 Sub2API",
                "group_id": "42",
                "group_name": "新注册账号",
                "account_id": "remote-account-1",
            }

        with (
            patch.object(openai_register, "PlatformRegistrar", return_value=registrar),
            patch.object(openai_register.account_service, "add_account_items", side_effect=save_account),
            patch.object(openai_register.sub2api_config, "get_server", return_value=server) as get_server,
            patch.object(openai_register, "sync_openai_account", side_effect=sync_account) as sync,
            patch.object(openai_register.account_service, "update_account") as update_account,
            patch.object(openai_register.account_service, "refresh_accounts", return_value={"errors": []}),
        ):
            result = openai_register.worker(1)

        self.assertTrue(result["ok"])
        self.assertEqual(call_order, ["saved", "synced"])
        get_server.assert_called_once_with("sub2api-primary")
        sync.assert_called_once_with(server, {
            "email": "new@example.test",
            "access_token": "chatgpt-access",
            "refresh_token": "chatgpt-refresh",
            "source_type": "chatgpt_web",
        }, {
            "enabled": True,
            "server_id": "sub2api-primary",
            "group_mode": "existing",
            "group_id": "42",
            "group_name": "新注册账号",
        })
        update_account.assert_called_once_with(
            "chatgpt-access",
            {
                "sub2api_sync_status": "success",
                "sub2api_sync_server_id": "sub2api-primary",
                "sub2api_sync_server_name": "主 Sub2API",
                "sub2api_sync_group_id": "42",
                "sub2api_sync_group_name": "新注册账号",
                "sub2api_sync_account_id": "remote-account-1",
                "sub2api_sync_at": ANY,
                "sub2api_sync_error": None,
            },
            quiet=True,
        )
        self.assertEqual(result["result"]["sub2api_sync_status"], "success")
        registrar.close.assert_called_once()

    def test_sub2api_sync_extracts_durable_oauth_credentials_without_replacing_web_account(self) -> None:
        registrar = MagicMock()
        registrar.extract_platform_oauth_credentials.return_value = {
            "access_token": "platform-access",
            "refresh_token": "platform-refresh",
            "id_token": "platform-id",
        }
        web_account = {
            "email": "new@example.test",
            "access_token": "chatgpt-web-access",
            "source_type": "chatgpt_web",
        }

        payload = openai_register._sub2api_sync_account_payload(registrar, web_account, 1)

        registrar.extract_platform_oauth_credentials.assert_called_once_with("new@example.test", 1)
        self.assertEqual(payload["access_token"], "platform-access")
        self.assertEqual(payload["refresh_token"], "platform-refresh")
        self.assertEqual(payload["id_token"], "platform-id")
        self.assertEqual(web_account["access_token"], "chatgpt-web-access")
        self.assertNotIn("refresh_token", web_account)

    def test_worker_keeps_registration_successful_when_sub2api_sync_fails(self) -> None:
        openai_register.config["checkout"] = {"enabled": False}
        openai_register.config["sub2api_sync"] = {
            "enabled": True,
            "server_id": "sub2api-primary",
            "group_mode": "custom",
            "group_id": "",
            "group_name": "新注册账号",
        }
        registrar = MagicMock()
        registrar.register.return_value = {
            "email": "new@example.test",
            "access_token": "chatgpt-access",
            "refresh_token": "chatgpt-refresh-secret",
            "source_type": "chatgpt_web",
        }
        server = {"id": "sub2api-primary", "name": "主 Sub2API", "base_url": "https://sub2api.example.test"}
        call_order: list[str] = []

        def save_account(_items: list[dict]) -> None:
            call_order.append("saved")

        def fail_sync(*_args: object, **_kwargs: object) -> dict:
            call_order.append("sync_attempted")
            raise RuntimeError("remote rejected chatgpt-refresh-secret")

        with (
            patch.object(openai_register, "PlatformRegistrar", return_value=registrar),
            patch.object(openai_register.account_service, "add_account_items", side_effect=save_account),
            patch.object(openai_register.sub2api_config, "get_server", return_value=server),
            patch.object(openai_register, "sync_openai_account", side_effect=fail_sync),
            patch.object(openai_register.account_service, "update_account") as update_account,
            patch.object(openai_register.account_service, "refresh_accounts", return_value={"errors": []}),
        ):
            result = openai_register.worker(1)

        self.assertTrue(result["ok"])
        self.assertEqual(call_order, ["saved", "sync_attempted"])
        update_account.assert_called_once()
        saved_status = update_account.call_args.args[1]
        self.assertEqual(saved_status["sub2api_sync_status"], "failed")
        self.assertEqual(saved_status["sub2api_sync_server_id"], "sub2api-primary")
        self.assertEqual(saved_status["sub2api_sync_group_name"], "新注册账号")
        self.assertIn("***", saved_status["sub2api_sync_error"])
        self.assertNotIn("chatgpt-refresh-secret", saved_status["sub2api_sync_error"])
        self.assertEqual(result["result"]["sub2api_sync_status"], "failed")
        registrar.close.assert_called_once()

    def test_worker_saves_account_before_uploading_to_cpa_and_records_success(self) -> None:
        openai_register.config["checkout"] = {"enabled": False}
        openai_register.config["cpa_sync"] = {"enabled": True, "pool_id": "cpa-primary"}
        registrar = MagicMock()
        registrar.register.return_value = {
            "email": "new@example.test",
            "access_token": "chatgpt-access",
            "refresh_token": "chatgpt-refresh",
            "id_token": "chatgpt-id",
            "source_type": "web",
        }
        pool = {"id": "cpa-primary", "name": "主 CPA", "base_url": "https://cpa.example.test"}
        call_order: list[str] = []

        def save_account(_items: list[dict]) -> None:
            call_order.append("saved")

        def upload_account(_pool: dict, _account: dict) -> dict:
            call_order.append("uploaded")
            return {
                "ok": True,
                "pool_id": "cpa-primary",
                "pool_name": "主 CPA",
                "file_name": "codex-new@example.test.json",
            }

        with (
            patch.object(openai_register, "PlatformRegistrar", return_value=registrar),
            patch.object(openai_register.account_service, "add_account_items", side_effect=save_account),
            patch.object(openai_register.cpa_config, "get_pool", return_value=pool) as get_pool,
            patch.object(openai_register, "upload_openai_oauth_file", side_effect=upload_account) as upload,
            patch.object(openai_register.account_service, "update_account") as update_account,
            patch.object(openai_register.account_service, "refresh_accounts", return_value={"errors": []}),
        ):
            result = openai_register.worker(1)

        self.assertTrue(result["ok"])
        self.assertEqual(call_order, ["saved", "uploaded"])
        get_pool.assert_called_once_with("cpa-primary")
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[0], pool)
        self.assertEqual(
            upload.call_args.args[1],
            {
                "email": "new@example.test",
                "access_token": "chatgpt-access",
                "refresh_token": "chatgpt-refresh",
                "id_token": "chatgpt-id",
                "source_type": "web",
            },
        )
        update_account.assert_called_once_with(
            "chatgpt-access",
            {
                "cpa_sync_status": "success",
                "cpa_sync_pool_id": "cpa-primary",
                "cpa_sync_pool_name": "主 CPA",
                "cpa_sync_file_name": "codex-new@example.test.json",
                "cpa_sync_at": ANY,
                "cpa_sync_error": None,
            },
            quiet=True,
        )
        self.assertEqual(result["result"]["cpa_sync_status"], "success")
        registrar.close.assert_called_once()

    def test_worker_keeps_registration_successful_when_cpa_upload_fails(self) -> None:
        openai_register.config["checkout"] = {"enabled": False}
        openai_register.config["cpa_sync"] = {"enabled": True, "pool_id": "cpa-primary"}
        registrar = MagicMock()
        registrar.register.return_value = {
            "email": "new@example.test",
            "access_token": "chatgpt-access",
            "refresh_token": "chatgpt-refresh-secret",
            "id_token": "chatgpt-id-secret",
            "source_type": "web",
        }
        pool = {"id": "cpa-primary", "name": "主 CPA", "base_url": "https://cpa.example.test"}

        with (
            patch.object(openai_register, "PlatformRegistrar", return_value=registrar),
            patch.object(openai_register.account_service, "add_account_items"),
            patch.object(openai_register.cpa_config, "get_pool", return_value=pool),
            patch.object(
                openai_register,
                "upload_openai_oauth_file",
                side_effect=RuntimeError("remote rejected chatgpt-refresh-secret"),
            ),
            patch.object(openai_register.account_service, "update_account") as update_account,
            patch.object(openai_register.account_service, "refresh_accounts", return_value={"errors": []}),
        ):
            result = openai_register.worker(1)

        self.assertTrue(result["ok"])
        saved_status = update_account.call_args.args[1]
        self.assertEqual(saved_status["cpa_sync_status"], "failed")
        self.assertEqual(saved_status["cpa_sync_pool_id"], "cpa-primary")
        self.assertIn("***", saved_status["cpa_sync_error"])
        self.assertNotIn("chatgpt-refresh-secret", saved_status["cpa_sync_error"])
        self.assertEqual(result["result"]["cpa_sync_status"], "failed")
        registrar.close.assert_called_once()

    def test_worker_extracts_checkout_before_closing_registration_session(self) -> None:
        registrar = MagicMock()
        registrar.register.return_value = {
            "email": "new@example.test",
            "access_token": "chatgpt-access",
            "source_type": "chatgpt_web",
        }
        checkout_result = {"checkout_final_url": "https://payments.stripe.com/upi/instructions/upi_123"}
        call_order: list[str] = []

        def save_account(_items: list[dict]) -> None:
            call_order.append("account_saved")

        def extract_checkout(*_args, **_kwargs) -> dict:
            call_order.append("checkout_extracted")
            _kwargs["progress"]("初始化 Stripe Checkout")
            return checkout_result

        with (
            patch.object(openai_register, "PlatformRegistrar", return_value=registrar),
            patch.object(openai_register.account_service, "add_account_items", side_effect=save_account) as add_account,
            patch.object(
                openai_register.openai_checkout_service,
                "extract_and_store_checkout_link",
                side_effect=extract_checkout,
            ) as extract,
            patch.object(openai_register, "register_checkout_log_sink") as checkout_log_sink,
            patch.object(openai_register, "register_checkout_task_sink") as checkout_task_sink,
            patch.object(openai_register.account_service, "refresh_accounts", return_value={"errors": []}),
        ):
            result = openai_register.worker(1)

        self.assertTrue(result["ok"])
        self.assertEqual(call_order, ["account_saved", "checkout_extracted"])
        add_account.assert_called_once_with([registrar.register.return_value])
        extract.assert_called_once_with(
            "chatgpt-access",
            checkout_channel="upi",
            checkout_proxy="http://checkout-user:checkout-password@residential.example.test:8888",
            promotion_proxy="http://promotion-user:promotion-password@promotion.example.test:8888",
            provider_proxy="http://checkout-user:checkout-password@residential.example.test:8888",
            proxy_rotation=ANY,
            progress=ANY,
        )
        checkout_log_sink.assert_any_call("[任务1] 初始化 Stripe Checkout", "")
        task_updates = [call.args[0] for call in checkout_task_sink.call_args_list]
        self.assertEqual([item["status"] for item in task_updates if "status" in item], ["running", "success"])
        self.assertEqual(task_updates[0]["email"], "new@example.test")
        self.assertEqual(task_updates[1]["stage"], "stripe_init")
        self.assertEqual(task_updates[-1]["payment_link"], checkout_result["checkout_final_url"])
        self.assertNotIn("chatgpt-access", str(task_updates))
        self.assertNotIn("checkout-password", str(task_updates))
        registrar.close.assert_called_once()

    def test_worker_keeps_registration_successful_when_checkout_extraction_fails(self) -> None:
        openai_register.config["checkout"] = {
            **openai_register.config["checkout"],
            "continuous_retry": False,
        }
        registrar = MagicMock()
        registrar.register.return_value = {
            "email": "new@example.test",
            "access_token": "chatgpt-access",
            "source_type": "chatgpt_web",
        }
        with (
            patch.object(openai_register, "PlatformRegistrar", return_value=registrar),
            patch.object(openai_register.account_service, "add_account_items"),
            patch.object(
                openai_register.openai_checkout_service,
                "extract_and_store_checkout_link",
                side_effect=openai_register.CheckoutSessionError("checkout unavailable", upstream_status=403),
            ),
            patch.object(openai_register, "register_checkout_task_sink") as checkout_task_sink,
            patch.object(openai_register.account_service, "refresh_accounts", return_value={"errors": []}),
        ):
            result = openai_register.worker(1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["checkout_link_status"], "failed")
        failed = checkout_task_sink.call_args_list[-1].args[0]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["stage"], "failed")
        self.assertEqual(failed["error_short"], "上游拒绝访问")
        self.assertNotIn("checkout unavailable", str(failed))
        registrar.close.assert_called_once()

    def test_upi_failure_queues_country_aware_continuous_retry(self) -> None:
        openai_register.config["checkout"] = {
            "enabled": True,
            "channel": "upi",
            "country": "IN",
            "currency": "INR",
            "checkout_ui_mode": "custom",
            "checkout_proxy_enabled": True,
            "checkout_proxy_url": "http://in-checkout-0.example.test:8000\nhttp://in-checkout-1.example.test:8000",
            "promotion_proxy_enabled": True,
            "promotion_proxy_url": "http://vn-0.example.test:8001\nhttp://vn-1.example.test:8001",
            "provider_proxy_enabled": True,
            "provider_proxy_url": "http://in-provider-0.example.test:8002\nhttp://in-provider-1.example.test:8002",
            "continuous_retry": True,
        }
        registrar = MagicMock()
        registrar.register.return_value = {
            "email": "new@example.test",
            "access_token": "chatgpt-access",
            "source_type": "chatgpt_web",
        }
        with (
            patch.object(openai_register, "PlatformRegistrar", return_value=registrar),
            patch.object(openai_register.account_service, "add_account_items"),
            patch.object(openai_register.account_service, "update_account") as update_account,
            patch.object(
                openai_register.openai_checkout_service,
                "extract_and_store_checkout_link",
                side_effect=openai_register.CheckoutSessionError("UPI 最终支付链接未生成"),
            ) as extract,
            patch.object(openai_register, "register_checkout_retry_sink") as retry_sink,
            patch.object(openai_register, "register_checkout_task_sink") as checkout_task_sink,
            patch.object(openai_register.account_service, "refresh_accounts", return_value={"errors": []}),
            patch.object(openai_register.random, "randrange", return_value=17),
        ):
            result = openai_register.worker(1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["checkout_link_status"], "pending")
        extract.assert_called_once()
        self.assertEqual(extract.call_args.kwargs["proxy_rotation"], 17)
        update_account.assert_called_once()
        retry_payload = retry_sink.call_args.args[0]
        self.assertEqual(retry_payload["next_proxy_rotation"], 18)
        self.assertEqual(retry_payload["checkout"]["channel"], "upi")
        self.assertEqual(retry_payload["checkout"]["checkout_proxy_url"].splitlines()[0], "http://in-checkout-0.example.test:8000")
        self.assertEqual(
            retry_payload["checkout"]["provider_proxy_url"],
            retry_payload["checkout"]["checkout_proxy_url"],
        )
        task_updates = [call.args[0] for call in checkout_task_sink.call_args_list]
        self.assertEqual(task_updates[-1]["status"], "retrying")
        self.assertNotIn("chatgpt-access", str(task_updates))
        registrar.close.assert_called_once()

    def test_pix_registration_queues_without_blocking_registration_worker(self) -> None:
        openai_register.config["checkout"] = {
            "enabled": True,
            "channel": "pix",
            "country": "BR",
            "currency": "BRL",
            "checkout_ui_mode": "custom",
            "checkout_proxy_enabled": True,
            "checkout_proxy_url": "http://account-region-BR:secret@rotating.example.test:8000",
            "promotion_proxy_enabled": False,
            "promotion_proxy_url": "",
            "provider_proxy_enabled": True,
            "provider_proxy_url": "http://account-region-BR:secret@rotating.example.test:8000",
            "continuous_retry": True,
            "threads": 3,
        }
        registrar = MagicMock()
        registrar.register.return_value = {
            "email": "new-pix@example.test",
            "access_token": "chatgpt-access",
            "source_type": "chatgpt_web",
        }
        with (
            patch.object(openai_register, "PlatformRegistrar", return_value=registrar),
            patch.object(openai_register.account_service, "add_account_items"),
            patch.object(openai_register.account_service, "update_account") as update_account,
            patch.object(
                openai_register.openai_checkout_service,
                "extract_and_store_checkout_link",
            ) as extract,
            patch.object(openai_register, "register_checkout_retry_sink") as retry_sink,
            patch.object(openai_register, "register_checkout_task_sink") as checkout_task_sink,
            patch.object(openai_register.account_service, "refresh_accounts", return_value={"errors": []}),
            patch.object(openai_register.random, "randrange", return_value=17),
        ):
            result = openai_register.worker(1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["checkout_link_status"], "pending")
        extract.assert_not_called()
        retry_payload = retry_sink.call_args.args[0]
        self.assertEqual(retry_payload["attempt"], 0)
        self.assertEqual(retry_payload["next_proxy_rotation"], 17)
        self.assertEqual(retry_payload["checkout"]["channel"], "pix")
        update_account.assert_called_once_with(
            "chatgpt-access",
            {"checkout_link_status": "pending"},
            quiet=True,
        )
        task_updates = [call.args[0] for call in checkout_task_sink.call_args_list]
        self.assertEqual(task_updates[0]["status"], "queued")
        self.assertEqual(task_updates[-1]["error_short"], "等待 Pix 独立提链线程")
        registrar.close.assert_called_once()

    def test_checkout_failure_short_classifies_upstream_rejection_without_raw_body(self) -> None:
        error = openai_register.CheckoutSessionError(
            "UPI 最终支付链接协议请求失败: We're sorry, but we're unable to serve your request.",
        )

        self.assertEqual(openai_register._checkout_failure_short(error), "上游支付页面拒绝请求")

    def test_checkout_failure_short_uses_pix_channel_label(self) -> None:
        error = RuntimeError("unclassified final-link failure")

        self.assertEqual(
            openai_register._checkout_failure_short(error, "pix"),
            "Pix 提链失败，请稍后重试",
        )

    def test_checkout_failure_short_preserves_stripe_poll_timeout(self) -> None:
        error = openai_register.CheckoutSessionError(
            "UPI 最终支付链接协议请求失败: Stripe 轮询代理连接超时",
        )

        self.assertEqual(openai_register._checkout_failure_short(error), "Stripe 轮询代理超时")

    def test_checkout_failure_short_identifies_non_free_trial_without_exposing_protocol_detail(self) -> None:
        error = openai_register.CheckoutSessionError(
            "UPI 最终支付链接协议请求失败: 当前 Checkout 不是 0 元试用资格（amount=1000）",
            code="checkout_amount_mismatch",
        )

        self.assertEqual(openai_register._checkout_failure_short(error), "当前代理无 0 元试用资格")

    def test_checkout_failure_short_identifies_second_tax_update(self) -> None:
        error = openai_register.CheckoutSessionError(
            "最终支付链接协议请求失败: StripeFinalLinkError: "
            "stripe_tax_2: This Checkout Session is no longer active. (request_id=req_test)",
            upstream_status=400,
        )

        self.assertEqual(
            openai_register._checkout_failure_short(error),
            "Stripe 税务地区第二次更新失败：Session 已失效",
        )

    def test_checkout_failure_short_identifies_inactive_checkout_session(self) -> None:
        error = openai_register.CheckoutSessionError(
            "最终支付链接协议请求失败: StripeFinalLinkError: "
            "stripe_init: This Checkout Session is no longer active. "
            "(stripe_code=checkout_not_active_session)",
            code="checkout_session_inactive",
            upstream_status=400,
        )

        self.assertEqual(
            openai_register._checkout_failure_short(error),
            "Stripe Checkout 初始化失败：Session 已失效",
        )

    def test_checkout_progress_is_reduced_to_stable_stage_codes(self) -> None:
        self.assertEqual(openai_register._checkout_stage_code("[stripe_provider] IN Provider 复核"), "stripe_provider")
        self.assertEqual(openai_register._checkout_progress_detail("[stripe_provider] IN Provider 复核"), "IN Provider 复核")
        self.assertEqual(openai_register._checkout_stage_code("创建 OpenAI Checkout（US/USD）"), "checkout")
        self.assertEqual(openai_register._checkout_stage_code("通过 VN 更新 Checkout 优惠（1/2）"), "promotion")
        self.assertEqual(openai_register._checkout_stage_code("OpenAI checkout update"), "checkout_update")
        self.assertEqual(openai_register._checkout_stage_code("初始化 Stripe Checkout"), "stripe_init")
        self.assertEqual(openai_register._checkout_stage_code("刷新 Stripe Elements 会话"), "stripe_elements")
        self.assertEqual(openai_register._checkout_stage_code("同步 Stripe 税务地区"), "stripe_tax")
        self.assertEqual(openai_register._checkout_stage_code("同步 ChatGPT 账单快照"), "snapshot")
        self.assertEqual(openai_register._checkout_stage_code("创建 UPI 支付方式"), "payment_method")
        self.assertEqual(openai_register._checkout_stage_code("确认 Stripe Checkout"), "confirm")
        self.assertEqual(openai_register._checkout_stage_code("轮询 Stripe 跳转"), "poll")
        self.assertEqual(openai_register._checkout_stage_code("解析 UPI 最终支付链接"), "extract")

class PlatformOAuthExtractionTest(unittest.TestCase):
    def test_authenticated_web_session_can_exchange_platform_oauth_code(self) -> None:
        registrar = object.__new__(openai_register.PlatformRegistrar)
        registrar.proxy = ""
        registrar.session = MagicMock()
        registrar.clearance_user_agent = ""
        registrar.device_id = "web-session-device"
        registrar.fingerprint = {
            "user_agent": "test-agent",
            "accept_language": "en-US,en;q=0.9",
            "sec_ch_ua": '"Google Chrome";v="136"',
        }
        registrar.code_verifier = ""
        registrar.platform_auth_code = ""
        registrar._platform_authorize_final_url = ""

        def authorize(email: str, index: int, screen_hint: str = "") -> str:
            self.assertEqual(email, "new@example.test")
            self.assertEqual(index, 1)
            self.assertEqual(screen_hint, "login")
            registrar.code_verifier = "pkce-verifier"
            registrar._platform_authorize_final_url = (
                "https://platform.openai.com/auth/callback?code=oauth-code&state=oauth-state"
            )
            return "callback"

        registrar._platform_authorize = authorize
        with (
            patch.object(
                openai_register,
                "request_platform_oauth_token",
                return_value={"access_token": "platform-access", "refresh_token": "platform-refresh", "id_token": "platform-id"},
            ) as exchange,
            patch.object(openai_register, "exchange_tokens_from_continue_url") as fallback,
        ):
            tokens = registrar.extract_platform_oauth_credentials("new@example.test", 1)

        self.assertEqual(tokens["access_token"], "platform-access")
        self.assertEqual(tokens["refresh_token"], "platform-refresh")
        exchange.assert_called_once_with(
            registrar.session,
            "oauth-code",
            "pkce-verifier",
            ANY,
            registrar.fingerprint,
        )
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
