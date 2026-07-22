from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import MagicMock, patch

from services import sub2api_service


class _Response:
    def __init__(self, status_code: int, payload: object, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or repr(payload)
        self.ok = 200 <= status_code < 300

    def json(self) -> object:
        return self._payload


class OpenAIAgentIdentityTest(unittest.TestCase):
    def test_new_jwt_claims_fall_back_to_auth_user_id_and_subject(self) -> None:
        claims = {
            "sub": "auth0|account-subject",
            "https://api.openai.com/auth": {"user_id": "user-new"},
            "https://api.openai.com/profile": {"email": "new@example.test"},
        }
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        account = {"access_token": f"header.{payload}.signature"}
        session = MagicMock()
        session.post.return_value = _Response(200, {"agent_runtime_id": "agent-new"})

        with patch("services.sub2api_service.Session", return_value=session):
            auth = sub2api_service._build_openai_agent_identity(account)

        identity = auth["agent_identity"]
        self.assertEqual(auth["auth_mode"], "agentIdentity")
        self.assertEqual(identity["account_id"], "auth0|account-subject")
        self.assertEqual(identity["chatgpt_user_id"], "user-new")


class Sub2APIAccountSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        # Authentication is cached per saved server.  Keep the TLS assertions
        # isolated so an earlier login cannot bypass the mocked Session.
        with sub2api_service._token_cache_lock:
            sub2api_service._token_cache.clear()
        self.agent_identity_patcher = patch.object(
            sub2api_service,
            "_build_openai_agent_identity",
            return_value={"auth_mode": "agentIdentity", "agent_identity": {"agent_runtime_id": "agent-one"}},
        )
        self.agent_identity = self.agent_identity_patcher.start()

    def tearDown(self) -> None:
        self.agent_identity_patcher.stop()
        with sub2api_service._token_cache_lock:
            sub2api_service._token_cache.clear()

    def _server(self, **overrides: object) -> dict[str, object]:
        server: dict[str, object] = {
            "id": "remote-server",
            "base_url": "https://sub2api.example.test",
            "api_key": "remote-api-key",
        }
        server.update(overrides)
        return server

    def _account(self) -> dict[str, str]:
        return {
            "email": "new-account@example.test",
            "access_token": "access-token-must-not-leak",
            "refresh_token": "refresh-token-must-not-leak",
            "id_token": "id-token-must-not-leak",
            "expires_at": "2030-01-01T00:00:00+00:00",
        }

    @staticmethod
    def _post_call_for(session: MagicMock, suffix: str):
        for call in session.post.call_args_list:
            if str(call.args[0]).endswith(suffix):
                return call
        raise AssertionError(f"POST {suffix} was not made")

    def test_normalize_sync_config_keeps_server_and_custom_group(self) -> None:
        normalized = sub2api_service.normalize_sync_config({
            "enabled": " true ",
            "server_id": " remote-server ",
            "group_id": " 42 ",
            "group_name": "  New registrations  ",
        })

        self.assertTrue(normalized["enabled"])
        self.assertEqual(normalized["server_id"], "remote-server")
        self.assertEqual(normalized["group_id"], "42")
        self.assertEqual(normalized["group_name"], "New registrations")

    def test_sync_reuses_explicit_group_without_creating_one(self) -> None:
        session = MagicMock()
        session.get.return_value = _Response(200, {"data": {"items": [], "total": 0}})
        session.post.return_value = _Response(201, {"data": {"id": "remote-account-1"}})
        sync_config = sub2api_service.normalize_sync_config({
            "enabled": True,
            "server_id": "remote-server",
            "group_id": "42",
        })

        with patch("services.sub2api_service.Session", return_value=session):
            result = sub2api_service.sync_openai_account(self._server(), self._account(), sync_config)

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_id"], "remote-account-1")
        self.assertEqual(result["group_id"], "42")
        self.assertFalse(any(
            str(call.args[0]).endswith("/api/v1/admin/groups")
            for call in session.post.call_args_list
        ))

        account_call = self._post_call_for(session, "/api/v1/admin/accounts/import/codex-session")
        self.assertEqual(account_call.kwargs["headers"]["x-api-key"], "remote-api-key")
        payload = account_call.kwargs["json"]
        self.assertEqual(payload["group_ids"], [42])
        self.assertEqual(json.loads(payload["content"])["auth_mode"], "agentIdentity")

    def test_agent_identity_sync_accepts_access_token_without_refresh_token(self) -> None:
        account = self._account()
        account.pop("refresh_token")

        payload = sub2api_service.build_openai_oauth_export_account(account)

        self.assertEqual(payload["type"], "oauth")
        self.assertEqual(payload["credentials"]["access_token"], "access-token-must-not-leak")
        self.assertNotIn("refresh_token", payload["credentials"])

    def test_sync_falls_back_to_codex_session_for_unsupported_agent_delegator(self) -> None:
        session = MagicMock()
        session.post.return_value = _Response(201, {"data": {"created": 1, "updated": 0}})
        sync_config = sub2api_service.normalize_sync_config({
            "enabled": True,
            "server_id": "remote-server",
            "group_id": "42",
        })

        self.agent_identity.side_effect = sub2api_service.Sub2APISyncError(
            "当前 OpenAI Token 不支持 Agent Identity（unsupported_agent_delegator）"
        )
        with patch("services.sub2api_service.Session", return_value=session):
            sub2api_service.sync_openai_account(self._server(), self._account(), sync_config)

        account_call = self._post_call_for(session, "/api/v1/admin/accounts/import/codex-session")
        self.assertEqual(account_call.kwargs["json"]["content"], "access-token-must-not-leak")

    def test_sync_creates_custom_group_then_uses_its_id(self) -> None:
        session = MagicMock()
        session.get.return_value = _Response(200, {"data": {"items": [], "total": 0}})
        session.post.side_effect = [
            _Response(201, {"data": {"id": 13, "name": "Fresh accounts"}}),
            _Response(201, {"data": {"id": "remote-account-2"}}),
        ]
        sync_config = sub2api_service.normalize_sync_config({
            "enabled": True,
            "server_id": "remote-server",
            "group_mode": "custom",
            "group_name": "Fresh accounts",
        })

        with patch("services.sub2api_service.Session", return_value=session):
            result = sub2api_service.sync_openai_account(self._server(), self._account(), sync_config)

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_id"], "remote-account-2")
        self.assertEqual(result["group_id"], "13")
        self.assertEqual(result["group_name"], "Fresh accounts")

        group_call = self._post_call_for(session, "/api/v1/admin/groups")
        self.assertEqual(group_call.kwargs["json"]["name"], "Fresh accounts")
        account_call = self._post_call_for(session, "/api/v1/admin/accounts/import/codex-session")
        self.assertEqual(account_call.kwargs["json"]["group_ids"], [13])

    def test_sync_failure_raises_sanitized_error(self) -> None:
        session = MagicMock()
        session.get.return_value = _Response(200, {"data": {"items": [], "total": 0}})
        session.post.return_value = _Response(
            422,
            {"error": "invalid credentials"},
            "access-token-must-not-leak refresh-token-must-not-leak",
        )
        sync_config = sub2api_service.normalize_sync_config({
            "enabled": True,
            "server_id": "remote-server",
            "group_id": "42",
        })

        with patch("services.sub2api_service.Session", return_value=session):
            with self.assertRaises(RuntimeError) as raised:
                sub2api_service.sync_openai_account(self._server(), self._account(), sync_config)

        message = str(raised.exception)
        self.assertIn("HTTP 422", message)
        self.assertNotIn("access-token-must-not-leak", message)
        self.assertNotIn("refresh-token-must-not-leak", message)

    def test_sync_xai_oauth_uses_grok_platform_and_cli_credentials(self) -> None:
        session = MagicMock()
        session.post.return_value = _Response(201, {"data": {"id": "remote-xai-account"}})
        sync_config = sub2api_service.normalize_sync_config({
            "enabled": True,
            "server_id": "remote-server",
            "group_id": "52",
        })
        account = {
            **self._account(),
            "subject": "xai-principal-one",
            "token_type": "Bearer",
            "sso_token": "sso-session-must-not-leak",
        }

        with patch("services.sub2api_service.Session", return_value=session):
            result = sub2api_service.sync_xai_oauth_account(self._server(), account, sync_config)

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_id"], "remote-xai-account")
        account_call = self._post_call_for(session, "/api/v1/admin/accounts")
        payload = account_call.kwargs["json"]
        self.assertEqual(payload["platform"], "grok")
        self.assertEqual(payload["type"], "oauth")
        self.assertEqual(payload["group_ids"], [52])
        self.assertEqual(payload["credentials"]["subject"], "xai-principal-one")
        self.assertEqual(payload["credentials"]["sub"], "xai-principal-one")
        self.assertEqual(
            payload["credentials"]["client_id"],
            sub2api_service.XAI_OAUTH_CLIENT_ID,
        )
        self.assertEqual(payload["credentials"]["scope"], sub2api_service.XAI_OAUTH_SCOPE)
        self.assertEqual(payload["credentials"]["base_url"], sub2api_service.XAI_CLI_BASE_URL)
        self.assertEqual(payload["credentials"]["sso_token"], "sso-session-must-not-leak")
        self.assertNotIn("new-account@example.test", account_call.kwargs["headers"]["Idempotency-Key"])

    def test_sync_xai_custom_group_is_created_for_grok_platform(self) -> None:
        session = MagicMock()
        session.get.return_value = _Response(200, {"data": {"items": [], "total": 0}})
        session.post.side_effect = [
            _Response(201, {"data": {"id": 19, "name": "Grok OAuth"}}),
            _Response(201, {"data": {"id": "remote-xai-account"}}),
        ]
        sync_config = sub2api_service.normalize_sync_config({
            "enabled": True,
            "server_id": "remote-server",
            "group_mode": "custom",
            "group_name": "Grok OAuth",
        })

        with patch("services.sub2api_service.Session", return_value=session):
            sub2api_service.sync_xai_oauth_account(self._server(), self._account(), sync_config)

        group_call = self._post_call_for(session, "/api/v1/admin/groups")
        self.assertEqual(group_call.kwargs["json"]["platform"], "grok")


class Sub2APITLSVerificationTest(unittest.TestCase):
    @staticmethod
    def _server(**overrides: object) -> dict[str, object]:
        server: dict[str, object] = {
            "id": "tls-server",
            "base_url": "https://sub2api.example.test",
            "api_key": "remote-api-key",
        }
        server.update(overrides)
        return server

    @staticmethod
    def _account() -> dict[str, str]:
        return {
            "email": "new-account@example.test",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
        }

    def setUp(self) -> None:
        with sub2api_service._token_cache_lock:
            sub2api_service._token_cache.clear()

    def tearDown(self) -> None:
        with sub2api_service._token_cache_lock:
            sub2api_service._token_cache.clear()

    def test_saved_server_defaults_to_strict_tls_verification(self) -> None:
        normalized = sub2api_service._normalize_server({
            "id": "legacy-server",
            "base_url": "https://sub2api.example.test",
            "api_key": "remote-api-key",
        })

        self.assertIs(normalized["verify_tls"], True)

    def test_group_listing_uses_default_or_explicit_tls_policy(self) -> None:
        for verify_tls, expected_verify in ((None, True), (False, False)):
            with self.subTest(verify_tls=verify_tls):
                server = self._server()
                if verify_tls is not None:
                    server["verify_tls"] = verify_tls
                session = MagicMock()
                session.get.return_value = _Response(200, {"data": {"items": [], "total": 0}})

                with patch("services.sub2api_service.Session", return_value=session) as session_factory:
                    groups = sub2api_service.list_remote_groups(server)

                self.assertEqual(groups, [])
                session_factory.assert_called_once_with(verify=expected_verify)
                session.close.assert_called_once_with()

    def test_account_sync_uses_default_or_explicit_tls_policy(self) -> None:
        sync_config = sub2api_service.normalize_sync_config({
            "enabled": True,
            "server_id": "tls-server",
            "group_id": "42",
        })
        for verify_tls, expected_verify in ((None, True), (False, False)):
            with self.subTest(verify_tls=verify_tls):
                server = self._server()
                if verify_tls is not None:
                    server["verify_tls"] = verify_tls
                session = MagicMock()
                session.post.return_value = _Response(201, {"data": {"id": "remote-account"}})

                with patch(
                    "services.sub2api_service._build_openai_agent_identity",
                    return_value={"auth_mode": "agentIdentity", "agent_identity": {"agent_runtime_id": "agent-one"}},
                ), patch("services.sub2api_service.Session", return_value=session) as session_factory:
                    result = sub2api_service.sync_openai_account(server, self._account(), sync_config)

                self.assertTrue(result["ok"])
                session_factory.assert_called_once_with(verify=expected_verify)
                session.close.assert_called_once_with()

    def test_password_login_uses_default_or_explicit_tls_policy(self) -> None:
        for verify_tls, expected_verify in ((None, True), (False, False)):
            with self.subTest(verify_tls=verify_tls):
                server = self._server(
                    id=f"tls-login-{expected_verify}",
                    api_key="",
                    email="admin@example.test",
                    password="password",
                )
                if verify_tls is not None:
                    server["verify_tls"] = verify_tls
                session = MagicMock()
                session.post.return_value = _Response(200, {
                    "data": {"access_token": "jwt-token", "expires_in": 3600},
                })

                with patch("services.sub2api_service.Session", return_value=session) as session_factory:
                    headers = sub2api_service._auth_headers(server)

                self.assertEqual(headers["Authorization"], "Bearer jwt-token")
                session_factory.assert_called_once_with(verify=expected_verify)
                session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
