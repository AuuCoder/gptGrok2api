from __future__ import annotations

import io
import json
import unittest
import zipfile
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.accounts as accounts_api
from api.accounts import _account_for_api, _status_matches_filter


class AccountApiSerializationTest(unittest.TestCase):
    def test_hides_browser_and_oauth_session_material(self) -> None:
        item = _account_for_api({
            "access_token": "displayed-admin-token",
            "cookie_header": "__Secure-next-auth.session-token=secret",
            "session_token": "session-secret",
            "refresh_token": "refresh-secret",
            "id_token": "id-secret",
            "status": "正常",
        })

        self.assertEqual(item["access_token"], "displayed-admin-token")
        self.assertNotIn("cookie_header", item)
        self.assertNotIn("session_token", item)
        self.assertNotIn("refresh_token", item)
        self.assertNotIn("id_token", item)

    def test_valid_checkout_filter_requires_ready_final_url_and_future_expiry(self) -> None:
        base = {
            "checkout_link_status": "ready",
            "checkout_channel": "pix",
            "checkout_final_url": "https://payments.stripe.com/qr/instructions/pix-test",
            "checkout_qr_expires_at": 1_800_000_100,
        }
        self.assertTrue(_status_matches_filter(base, "valid_checkout"))
        self.assertFalse(_status_matches_filter({**base, "checkout_qr_expires_at": 1}, "valid_checkout"))
        self.assertFalse(_status_matches_filter({**base, "checkout_link_status": "pending"}, "valid_checkout"))
        self.assertFalse(_status_matches_filter({**base, "checkout_final_url": "https://example.test"}, "valid_checkout"))
        self.assertFalse(_status_matches_filter({**base, "checkout_qr_expires_at": ""}, "valid_checkout"))


class AccountExportApiTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(accounts_api.create_router())
        self.client = TestClient(app)
        self.item = {
            "type": "codex",
            "email": "person@example.test",
            "account_id": "chatgpt-account",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
            "expired": "2030-01-01T00:00:00+00:00",
            "last_refresh": "2029-01-01T00:00:00+00:00",
        }

    @staticmethod
    def _agent_identity(account_id: str, email: str) -> dict:
        return {
            "auth_mode": "agent_identity",
            "agent_identity": {
                "agent_runtime_id": f"runtime-{account_id}",
                "agent_private_key": f"private-{account_id}",
                "account_id": account_id,
                "chatgpt_user_id": f"user-{account_id}",
                "email": email,
                "plan_type": "free",
                "chatgpt_account_is_fedramp": False,
            },
        }

    def test_sub2api_export_uses_standard_account_envelope(self) -> None:
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api.account_service,
            "build_export_items",
            return_value=[self.item],
        ):
            response = self.client.post(
                "/api/accounts/export",
                json={"access_tokens": ["access-token"], "format": "sub2api"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["proxies"], [])
        self.assertEqual(len(payload["accounts"]), 1)
        account = payload["accounts"][0]
        self.assertEqual(account["platform"], "openai")
        self.assertEqual(account["type"], "oauth")
        self.assertEqual(account["credentials"]["chatgpt_account_id"], "chatgpt-account")
        self.assertEqual(account["credentials"]["refresh_token"], "refresh-token")

    def test_cpa_export_is_zip_of_codex_auth_files(self) -> None:
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api.account_service,
            "build_export_items",
            return_value=[self.item],
        ):
            response = self.client.post(
                "/api/accounts/export",
                json={"access_tokens": ["access-token"], "format": "cpa"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), ["person-example.test.json"])
            exported = json.loads(archive.read(archive.namelist()[0]))
        self.assertEqual(exported["type"], "codex")
        self.assertEqual(exported["access_token"], "access-token")

    def test_single_agent_identity_export_is_auth_json(self) -> None:
        auth_json = self._agent_identity("one", "one@example.test")
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api.account_service,
            "list_accounts",
            return_value=[self.item],
        ), patch.object(
            accounts_api,
            "ensure_openai_agent_identity",
            return_value=auth_json,
        ):
            response = self.client.post(
                "/api/accounts/export",
                json={"access_tokens": ["access-token"], "format": "agent_identity"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="auth.json"')
        self.assertEqual(response.json(), auth_json)

    def test_multiple_agent_identity_export_is_zip(self) -> None:
        accounts = [
            {**self.item, "access_token": "token-one", "email": "one@example.test"},
            {**self.item, "access_token": "token-two", "email": "two@example.test"},
        ]
        identities = [
            self._agent_identity("one", "one@example.test"),
            self._agent_identity("two", "two@example.test"),
        ]
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api.account_service,
            "list_accounts",
            return_value=accounts,
        ), patch.object(
            accounts_api,
            "ensure_openai_agent_identity",
            side_effect=identities,
        ):
            response = self.client.post(
                "/api/accounts/export",
                json={"access_tokens": [], "format": "agent_identity"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), [
                "one-example.test/auth.json",
                "two-example.test/auth.json",
            ])
            exported = [json.loads(archive.read(name)) for name in archive.namelist()]
        self.assertEqual(exported, identities)

    def test_agent_identity_list_does_not_include_private_keys(self) -> None:
        summaries = [{
            "account_id": "one",
            "email": "one@example.test",
            "agent_runtime_id": "runtime-one",
            "updated_at": "2030-01-01T00:00:00+00:00",
        }]
        with patch.object(accounts_api, "require_admin"), patch.object(
            accounts_api.openai_agent_identity_store,
            "summary",
            return_value=summaries,
        ):
            response = self.client.get("/api/accounts/agent-identities")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"total": 1, "items": summaries})
        self.assertNotIn("agent_private_key", response.text)


if __name__ == "__main__":
    unittest.main()
