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


class MailProviderSelectionTest(unittest.TestCase):
    def test_next_entry_skips_excluded_provider_ref(self) -> None:
        mail_config = {
            "providers": [
                {
                    "id": "cf",
                    "enable": True,
                    "type": "cloudflare_temp_email",
                },
                {
                    "id": "primary",
                    "enable": True,
                    "type": "icloud_local",
                },
            ]
        }

        entry = mail_provider._next_entry(
            mail_config,
            {"cloudflare_temp_email:cf"},
        )

        self.assertEqual(entry["provider_ref"], "icloud_local:primary")

    def test_wait_timeout_override_is_limited_to_current_provider(self) -> None:
        mail_config = {
            "wait_timeout": 180,
            "providers": [
                {
                    "id": "cf",
                    "enable": True,
                    "type": "cloudflare_temp_email",
                }
            ],
        }
        mailbox = {
            "provider": "cloudflare_temp_email",
            "provider_ref": "cloudflare_temp_email:cf",
            "address": "cf@example.test",
        }
        provider = MagicMock()
        provider.conf = {"wait_timeout": 180, "wait_interval": 2}
        provider.wait_for_code.return_value = "123456"

        with patch.object(mail_provider, "_create_provider", return_value=provider):
            code = mail_provider.wait_for_code(
                mail_config,
                mailbox,
                wait_timeout=60,
            )

        self.assertEqual(code, "123456")
        self.assertEqual(provider.conf["wait_timeout"], 60.0)
        self.assertEqual(mail_config["wait_timeout"], 180)
        provider.wait_for_code.assert_called_once_with(mailbox)
        provider.close.assert_called_once_with()


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


class OutlookTokenProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conf = {
            "request_timeout": 7,
            "wait_timeout": 1,
            "wait_interval": 0.001,
            "user_agent": "mail-provider-test",
            "proxy": "direct",
        }
        self.entry = {
            "type": "outlook_token",
            "provider_ref": "outlook_token:test",
            "mode": "auto",
            "mailboxes": "person@outlook.com----password----client-id----refresh-token",
        }

    def _provider(self) -> mail_provider.OutlookTokenProvider:
        with patch.object(mail_provider, "_create_session", return_value=MagicMock()):
            return mail_provider.OutlookTokenProvider(dict(self.entry), dict(self.conf))

    def test_auto_mode_falls_back_to_imap_when_graph_scope_is_unavailable(self) -> None:
        provider = self._provider()
        mailbox = {
            "address": "person@outlook.com",
            "login_email": "person@outlook.com",
            "client_id": "client-id",
            "refresh_token": "refresh-token",
        }

        def access_token(_mailbox, _client_id, _refresh_token, scope):
            if scope == mail_provider.OUTLOOK_GRAPH_SCOPE:
                raise mail_provider.OutlookTokenError(
                    "OutlookToken 刷新失败: HTTP 400, AADSTS70000: requested scope unauthorized"
                )
            return "imap-access-token"

        with (
            patch.object(provider, "_access_token", side_effect=access_token) as get_access_token,
            patch.object(provider, "_imap_messages", return_value=[{"subject": "mail"}]) as imap_messages,
        ):
            first_messages = provider.fetch_recent_messages(mailbox)
            second_messages = provider.fetch_recent_messages(mailbox)

        self.assertEqual(first_messages, [{"subject": "mail"}])
        self.assertEqual(second_messages, [{"subject": "mail"}])
        scopes = [call.args[3] for call in get_access_token.call_args_list]
        self.assertEqual(scopes.count(mail_provider.OUTLOOK_GRAPH_SCOPE), 1)
        self.assertEqual(scopes.count(mail_provider.OUTLOOK_IMAP_SCOPE), 2)
        self.assertEqual(imap_messages.call_count, 2)

    def test_client_request_loop_is_treated_as_temporary_rate_limit(self) -> None:
        self.assertTrue(
            mail_provider._is_outlook_token_rate_limited(
                400,
                "AADSTS50196: The server encountered a client request loop",
            )
        )

    def test_client_request_loop_does_not_retry_token_exchange(self) -> None:
        provider = self._provider()
        response = MagicMock()
        response.status_code = 400
        response.text = "AADSTS50196"
        response.json.return_value = {
            "error_description": "AADSTS50196: The server encountered a client request loop"
        }
        provider.session.post.return_value = response

        with self.assertRaises(mail_provider.OutlookTokenRateLimitError):
            provider._exchange_refresh_token(
                "client-id",
                "refresh-token",
                mail_provider.OUTLOOK_GRAPH_SCOPE,
            )

        provider.session.post.assert_called_once()

    def test_imap_connection_uses_configured_request_timeout(self) -> None:
        provider = self._provider()
        mailbox = {"address": "person@outlook.com", "login_email": "person@outlook.com"}
        imap = MagicMock()
        imap.authenticate.return_value = ("OK", [])
        imap.select.return_value = ("OK", [])
        imap.uid.return_value = ("OK", [b""])

        with patch.object(mail_provider.imaplib, "IMAP4_SSL", return_value=imap) as imap_type:
            messages = provider._imap_messages(mailbox, "access-token")

        self.assertEqual(messages, [])
        imap_type.assert_called_once_with("outlook.office365.com", timeout=7.0)

    def test_imap_connection_is_reused_during_mailbox_polling(self) -> None:
        entry = {**self.entry, "mode": "imap"}
        with patch.object(mail_provider, "_create_session", return_value=MagicMock()):
            provider = mail_provider.OutlookTokenProvider(entry, dict(self.conf))
        mailbox = {"address": "person@outlook.com", "login_email": "person@outlook.com"}
        imap = MagicMock()
        imap.authenticate.return_value = ("OK", [])
        imap.select.return_value = ("OK", [])
        imap.uid.side_effect = [("OK", [b""]), ("OK", [b""])]

        with patch.object(mail_provider.imaplib, "IMAP4_SSL", return_value=imap) as imap_type:
            self.assertEqual(provider._imap_messages(mailbox, "access-token"), [])
            self.assertEqual(provider._imap_messages(mailbox, "access-token"), [])
            provider.close()

        imap_type.assert_called_once_with("outlook.office365.com", timeout=7.0)
        imap.authenticate.assert_called_once()
        self.assertEqual(imap.uid.call_count, 2)
        imap.logout.assert_called_once()

    def test_wait_for_code_retries_a_transient_imap_timeout(self) -> None:
        provider = self._provider()
        mailbox = {"address": "person@outlook.com"}
        message = {
            "provider": "outlook_token",
            "mailbox": "person@outlook.com",
            "message_id": "message-1",
            "subject": "Your verification code is 654321",
            "to": "person@outlook.com",
            "text_content": "Verification code: 654321",
            "received_at": datetime.now(timezone.utc),
        }

        with patch.object(
            provider,
            "fetch_recent_messages",
            side_effect=[TimeoutError("IMAP read timed out"), [message]],
        ) as fetch:
            code = provider.wait_for_code(mailbox)

        self.assertEqual(code, "654321")
        self.assertEqual(fetch.call_count, 2)

    def test_wait_for_code_retries_authenticated_but_disconnected_imap_session(self) -> None:
        provider = self._provider()
        mailbox = {"address": "person@outlook.com"}
        message = {
            "provider": "outlook_token",
            "mailbox": "person@outlook.com",
            "message_id": "message-2",
            "subject": "Your temporary OpenAI verification code",
            "to": "person@outlook.com",
            "text_content": "Enter this temporary verification code to continue: 246802",
            "received_at": datetime.now(timezone.utc),
        }

        with patch.object(
            provider,
            "fetch_recent_messages",
            side_effect=[
                mail_provider.imaplib.IMAP4.error("User is authenticated but not connected."),
                [message],
            ],
        ) as fetch:
            code = provider.wait_for_code(mailbox)

        self.assertEqual(code, "246802")
        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
