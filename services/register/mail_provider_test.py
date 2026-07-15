from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qsl, urlsplit

from services.register import mail_provider


class VerificationCodeExtractionTest(unittest.TestCase):
    def test_extracts_xai_hyphenated_code_from_subject(self) -> None:
        code = mail_provider._extract_code(
            {
                "subject": "ABC-123 xAI email verification",
                "text_content": "Use the code above to continue.",
                "html_content": "",
            }
        )

        self.assertEqual(code, "ABC-123")

    def test_keeps_six_digit_code_support(self) -> None:
        code = mail_provider._extract_code(
            {
                "subject": "Your verification code",
                "text_content": "Verification code: 654321",
                "html_content": "",
            }
        )

        self.assertEqual(code, "654321")

    def test_strict_xai_body_context_skips_summary_prefix(self) -> None:
        code = mail_provider._extract_code(
            {
                "subject": "PER-100 xAI email verification",
                "text_content": "Your xAI verification code: ABC-123",
                "html_content": "",
            },
            expected_keyword="xAI",
            require_body_context=True,
        )

        self.assertEqual(code, "ABC-123")

    def test_strict_xai_body_context_rejects_subject_only_prefix(self) -> None:
        code = mail_provider._extract_code(
            {
                "subject": "PER-100 xAI email verification",
                "text_content": "",
                "html_content": "",
            },
            expected_keyword="xAI",
            require_body_context=True,
        )

        self.assertIsNone(code)


class CloudflareTempMailProviderTest(unittest.TestCase):
    @staticmethod
    def _response(payload: dict, status_code: int = 200) -> MagicMock:
        response = MagicMock()
        response.status_code = status_code
        response.text = str(payload)
        response.json.return_value = payload
        return response

    def test_loads_detail_before_extracting_grok_code(self) -> None:
        session = MagicMock()
        session.headers = {}
        session.request.side_effect = [
            self._response(
                {
                    "results": [
                        {
                            "id": "mail-1",
                            "to": "relay@example.test",
                            "subject": "PER-100 xAI email verification",
                        }
                    ]
                }
            ),
            self._response(
                {
                    "data": {
                        "id": "mail-1",
                        "subject": "PER-100 xAI email verification",
                        "text": "Your xAI verification code: ABC-123",
                        "to": "relay@example.test",
                    }
                }
            ),
        ]
        entry = {
            "api_base": "https://mail.example.test",
            "admin_password": "admin-secret",
            "domain": ["example.test"],
            "keyword": "xAI",
            "provider_ref": "cloudflare_temp_email:test",
        }
        conf = {
            "request_timeout": 30,
            "wait_timeout": 30,
            "wait_interval": 1,
            "user_agent": "mail-provider-test",
            "proxy": "direct",
        }
        with patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)

        message = provider.fetch_latest_message(
            {"address": "relay@example.test", "token": "mail-jwt"}
        )

        self.assertIsNotNone(message)
        self.assertEqual(
            mail_provider._extract_code(
                message,
                expected_keyword="xAI",
                require_body_context=True,
            ),
            "ABC-123",
        )
        self.assertEqual(session.request.call_count, 2)
        detail_url = session.request.call_args_list[1].args[1]
        self.assertEqual(detail_url, "https://mail.example.test/api/mail/mail-1")

    def test_parses_raw_mime_and_ignores_css_hyphenated_tokens(self) -> None:
        raw = (
            "From: xAI <noreply@x.ai>\r\n"
            "To: relay@example.test\r\n"
            "Subject: WDG-YWI xAI confirmation code\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<html><style>.mj-column-per-100{width:100%}.c{color:#333333}</style>"
            "<body>Please use the code below to validate your email address. WDG-YWI</body></html>"
        )
        session = MagicMock()
        session.headers = {}
        session.request.return_value = self._response(
            {"results": [{"id": "mail-raw", "to": "relay@example.test", "raw": raw}]}
        )
        entry = {
            "api_base": "https://mail.example.test",
            "admin_password": "admin-secret",
            "domain": ["example.test"],
            "keyword": "xAI",
            "provider_ref": "cloudflare_temp_email:test",
        }
        conf = {
            "request_timeout": 30,
            "wait_timeout": 30,
            "wait_interval": 1,
            "user_agent": "mail-provider-test",
            "proxy": "direct",
        }
        with patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.CloudflareTempMailProvider(entry, conf)

        message = provider.fetch_latest_message(
            {"address": "relay@example.test", "token": "mail-jwt"}
        )

        self.assertIsNotNone(message)
        self.assertEqual(message["subject"], "WDG-YWI xAI confirmation code")
        self.assertEqual(
            mail_provider._extract_code(
                message,
                expected_keyword="xAI",
                require_body_context=True,
                allow_subject_code=bool(message["_trusted_code_subject"]),
            ),
            "WDG-YWI",
        )
        self.assertEqual(session.request.call_count, 1)


class ICloudPrivacyMailProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.entry = {
            "api_base": "https://icloud-mail.example.test/",
            "api_key": "icloud-secret",
            "project": "openai",
            "purpose": "register",
            "keyword": "OpenAI",
            "wait_ms": 12000,
            "use_proxy": False,
            "provider_ref": "icloud_api:primary",
        }
        self.conf = {
            "request_timeout": 30.0,
            "wait_timeout": 30.0,
            "wait_interval": 2.0,
            "user_agent": "mail-provider-test",
            "proxy": "http://proxy.example.test:8080",
        }

    @staticmethod
    def _response(payload: dict, status_code: int = 200) -> MagicMock:
        response = MagicMock()
        response.status_code = status_code
        response.text = str(payload)
        response.json.return_value = payload
        return response

    @staticmethod
    def _session(response: MagicMock) -> MagicMock:
        session = MagicMock()
        session.headers = {}
        session.request.return_value = response
        session.get.return_value = response
        session.post.return_value = response
        return session

    def _provider(self, response: MagicMock):
        session = self._session(response)
        with patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.ICloudPrivacyMailProvider(dict(self.entry), dict(self.conf))
        return provider, session

    def _request_call(self, session: MagicMock, method: str):
        expected = method.upper()
        if session.request.called:
            args, kwargs = session.request.call_args
            self.assertGreaterEqual(len(args), 2)
            self.assertEqual(str(args[0]).upper(), expected)
            return str(args[1]), kwargs

        shortcut = session.post if expected == "POST" else session.get
        self.assertTrue(shortcut.called, f"expected an HTTP {expected} request")
        args, kwargs = shortcut.call_args
        self.assertGreaterEqual(len(args), 1)
        return str(args[0]), kwargs

    def test_claim_uses_bearer_payload_and_parses_mailbox(self) -> None:
        response = self._response(
            {
                "success": True,
                "mailbox": {
                    "email": "relay@icloud.example",
                    "api_url": "https://icloud-mail.example.test/api/v1/mailboxes/mbx-1/code?token=mail-token",
                    "label": "OpenAI registration",
                    "id": "mbx-1",
                    "api_active": True,
                    "icloud_active": True,
                },
            }
        )
        provider, session = self._provider(response)

        mailbox = provider.create_mailbox()

        url, kwargs = self._request_call(session, "POST")
        self.assertEqual(url, "https://icloud-mail.example.test/api/v1/mailboxes/claim")
        effective_headers = {**session.headers, **dict(kwargs.get("headers") or {})}
        self.assertEqual(effective_headers.get("Authorization"), "Bearer icloud-secret")
        self.assertEqual(
            kwargs.get("json"),
            {"project": "openai", "purpose": "register", "count": 1},
        )
        self.assertEqual(mailbox["provider"], "icloud_api")
        self.assertEqual(mailbox["provider_ref"], "icloud_api:primary")
        self.assertEqual(mailbox["address"], "relay@icloud.example")
        self.assertEqual(
            mailbox["api_url"],
            "https://icloud-mail.example.test/api/v1/mailboxes/mbx-1/code?token=mail-token",
        )
        self.assertEqual(mailbox["mailbox_id"], "mbx-1")
        self.assertEqual(mailbox["label"], "OpenAI registration")
        self.assertTrue(mailbox["supports_passwordless_login"])

    def test_local_claim_does_not_require_api_key_and_uses_internal_header(self) -> None:
        response = self._response(
            {
                "success": True,
                "mailbox": {
                    "email": "local@icloud.example",
                    "api_url": "https://icloud-mail.example.test/api/v1/mailboxes/mbx-local/code?token=mail-token",
                    "id": "mbx-local",
                    "api_active": True,
                    "icloud_active": True,
                },
            }
        )
        entry = {
            "type": "icloud_local",
            "api_base": "https://icloud-mail.example.test",
            "project": "grok",
            "purpose": "register",
            "keyword": "xAI",
            "provider_ref": "icloud_local:primary",
        }
        session = self._session(response)
        with patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.ICloudPrivacyMailProvider(entry, dict(self.conf))

        mailbox = provider.create_mailbox()

        url, kwargs = self._request_call(session, "POST")
        self.assertEqual(url, "https://icloud-mail.example.test/api/v1/mailboxes/claim")
        effective_headers = {**session.headers, **dict(kwargs.get("headers") or {})}
        self.assertEqual(effective_headers.get("X-ChatGPT2API-Internal"), "icloud-privacy-mail")
        self.assertNotIn("Authorization", effective_headers)
        self.assertEqual(mailbox["provider"], "icloud_api")
        self.assertTrue(mailbox["_icloud_claim_internal"])
        provider.close()

    def test_local_claim_status_keeps_gpt_and_grok_independent(self) -> None:
        response = self._response({"success": True, "updated": 1, "missing": []})
        entry = {
            "type": "icloud_local",
            "api_base": "https://icloud-mail.example.test",
            "project": "grok",
            "provider_ref": "icloud_local:primary",
        }
        session = self._session(response)
        with patch.object(mail_provider, "_create_session", return_value=session):
            provider = mail_provider.ICloudPrivacyMailProvider(entry, dict(self.conf))

        result = provider.sync_existing_claims(["Alias@icloud.example"])

        self.assertEqual(result["updated"], 1)
        url, kwargs = self._request_call(session, "POST")
        self.assertEqual(url, "https://icloud-mail.example.test/api/v1/mailboxes/claim-status")
        self.assertEqual(
            kwargs["json"],
            {"project": "grok", "emails": ["alias@icloud.example"], "claimed": True},
        )
        effective_headers = {**session.headers, **dict(kwargs.get("headers") or {})}
        self.assertEqual(effective_headers.get("X-ChatGPT2API-Internal"), "icloud-privacy-mail")
        provider.close()

    def test_mark_local_mailbox_result_updates_claim_status_on_success_and_failure(self) -> None:
        session = self._session(self._response({"success": True, "updated": 1, "missing": []}))
        with patch.object(mail_provider, "_create_session", return_value=session):
            mail_provider.mark_mailbox_result(
                {
                    "provider": "icloud_api",
                    "address": "grok@icloud.example",
                    "_icloud_claim_internal": True,
                    "_icloud_claim_base": "https://icloud-mail.example.test",
                    "_icloud_claim_project": "grok",
                },
                success=True,
            )
            mail_provider.mark_mailbox_result(
                {
                    "provider": "icloud_api",
                    "address": "grok@icloud.example",
                    "_icloud_claim_internal": True,
                    "_icloud_claim_base": "https://icloud-mail.example.test",
                    "_icloud_claim_project": "grok",
                },
                success=False,
                error="registration failed",
            )

        self.assertEqual(session.request.call_count, 2)
        for call in session.request.call_args_list:
            self.assertEqual(call.args[0], "POST")
            self.assertTrue(str(call.args[1]).endswith("/api/v1/mailboxes/claim-status"))
            self.assertEqual(call.kwargs["json"]["project"], "grok")
            self.assertEqual(call.kwargs["json"]["emails"], ["grok@icloud.example"])
        self.assertEqual(session.request.call_args_list[0].kwargs["json"]["claimed"], True)
        self.assertEqual(session.request.call_args_list[1].kwargs["json"]["claimed"], False)

    def test_code_request_appends_filters_and_normalizes_message(self) -> None:
        response = self._response(
            {
                "success": True,
                "code": "654321",
                "subject": "Your OpenAI verification code",
                "received_at": "2026-07-11T08:01:02Z",
                "message_id": "message-1",
            }
        )
        provider, session = self._provider(response)
        not_before = datetime(2026, 7, 11, 8, 0, 0, tzinfo=timezone.utc)
        mailbox = {
            "provider": "icloud_api",
            "provider_ref": "icloud_api:primary",
            "address": "relay@icloud.example",
            "api_url": "https://icloud-mail.example.test/api/v1/mailboxes/mbx-1/code?token=mail-token",
            "_code_not_before": not_before,
        }

        message = provider.fetch_latest_message(mailbox)

        self.assertIsNotNone(message)
        url, kwargs = self._request_call(session, "GET")
        split = urlsplit(url)
        query = dict(parse_qsl(split.query, keep_blank_values=True))
        query.update({key: str(value) for key, value in dict(kwargs.get("params") or {}).items()})
        self.assertEqual(query["token"], "mail-token")
        self.assertEqual(query["keyword"], "OpenAI")
        self.assertEqual(query["wait_ms"], "12000")
        parsed_after = datetime.fromisoformat(query["after"].replace("Z", "+00:00"))
        self.assertLessEqual(parsed_after, not_before)
        self.assertLessEqual((not_before - parsed_after).total_seconds(), 10)
        self.assertEqual(message["provider"], "icloud_api")
        self.assertEqual(message["mailbox"], "relay@icloud.example")
        self.assertEqual(message["message_id"], "message-1")
        self.assertEqual(message["subject"], "Your OpenAI verification code")
        self.assertEqual(message["received_at"], datetime(2026, 7, 11, 8, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(mail_provider._extract_code(message), "654321")

    def test_retryable_no_code_returns_none(self) -> None:
        provider, _session = self._provider(
            self._response({"success": False, "code": "no_code", "retryable": True})
        )

        message = provider.fetch_latest_message(
            {
                "address": "relay@icloud.example",
                "api_url": "https://icloud-mail.example.test/api/v1/mailboxes/mbx-1/code",
            }
        )

        self.assertIsNone(message)

    def test_xai_code_and_keyword_are_supported(self) -> None:
        self.entry.update({"project": "grok", "keyword": "xAI"})
        provider, session = self._provider(
            self._response(
                {
                    "success": True,
                    "code": "ABC-123",
                    "subject": "ABC-123 xAI email verification",
                    "received_at": "2026-07-11T08:01:02Z",
                    "message_id": "message-xai",
                }
            )
        )
        mailbox = {
            "provider": "icloud_api",
            "provider_ref": "icloud_api:primary",
            "address": "relay@icloud.example",
            "api_url": "https://icloud-mail.example.test/api/v1/mailboxes/mbx-xai/code?token=mail-token",
            "_code_not_before": datetime(2026, 7, 11, 8, 0, 0, tzinfo=timezone.utc),
        }

        message = provider.fetch_latest_message(mailbox)

        self.assertIsNotNone(message)
        url, _kwargs = self._request_call(session, "GET")
        query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        self.assertEqual(query["keyword"], "xAI")
        self.assertEqual(mail_provider._extract_code(message), "ABC-123")

    def test_received_after_replaces_initial_code_boundary(self) -> None:
        provider, session = self._provider(
            self._response(
                {
                    "success": True,
                    "code": "246802",
                    "received_at": "2026-07-11T08:00:07Z",
                    "message_id": "message-2",
                }
            )
        )
        mailbox = {
            "address": "relay@icloud.example",
            "api_url": "https://icloud-mail.example.test/api/v1/mailboxes/mbx-1/code?token=mail-token",
            "_code_not_before": datetime(2026, 7, 11, 8, 0, 0, tzinfo=timezone.utc),
            "_received_after": "2026-07-11T08:00:05+00:00",
        }

        provider.fetch_latest_message(mailbox)

        url, _kwargs = self._request_call(session, "GET")
        query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        self.assertEqual(query["after"], "2026-07-11T07:59:55+00:00")

    def test_mailbox_can_override_subject_keyword_for_chatgpt_web_flow(self) -> None:
        provider, session = self._provider(
            self._response(
                {
                    "success": True,
                    "code": "246802",
                    "subject": "Your verification code",
                    "received_at": "2026-07-11T08:00:07Z",
                }
            )
        )
        mailbox = {
            "address": "relay@icloud.example",
            "api_url": "https://icloud-mail.example.test/api/v1/mailboxes/mbx-1/code?token=mail-token&keyword=OpenAI",
            "_icloud_keyword": "ChatGPT",
        }

        message = provider.fetch_latest_message(mailbox)

        self.assertIsNotNone(message)
        url, _kwargs = self._request_call(session, "GET")
        query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        self.assertEqual(query["keyword"], "ChatGPT")

    def test_invalid_api_key_raises_non_retryable_error(self) -> None:
        provider, _session = self._provider(
            self._response(
                {
                    "success": False,
                    "code": "invalid_api_key",
                    "retryable": False,
                    "message": "invalid API key",
                },
                status_code=401,
            )
        )

        with self.assertRaises(mail_provider.ICloudPrivacyMailError) as raised:
            provider.fetch_latest_message(
                {
                    "address": "relay@icloud.example",
                    "api_url": "https://icloud-mail.example.test/api/v1/mailboxes/mbx-1/code",
                }
            )

        self.assertEqual(raised.exception.code, "invalid_api_key")
        self.assertFalse(raised.exception.retryable)

    def test_use_proxy_false_forces_direct_session(self) -> None:
        session = self._session(self._response({"success": True}))
        original_conf = dict(self.conf)

        with patch.object(mail_provider, "_create_session", return_value=session) as create_session:
            mail_provider.ICloudPrivacyMailProvider(dict(self.entry), self.conf)

        create_session.assert_called_once()
        session_conf = create_session.call_args.args[0]
        self.assertEqual(session_conf["proxy"], "direct")
        self.assertEqual(self.conf, original_conf)


if __name__ == "__main__":
    unittest.main()
