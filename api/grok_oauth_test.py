from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import grok_oauth


class GrokOAuthAdminRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        self.app.include_router(grok_oauth.create_router())
        self.client = TestClient(self.app)

    def test_management_routes_read_bearer_authorization_header(self) -> None:
        with patch("api.grok_oauth.require_admin", return_value={"id": "admin", "role": "admin"}) as authorize:
            response = self.client.get(
                "/api/grok/oauth/accounts",
                headers={"Authorization": "Bearer test-admin-key"},
            )

        self.assertEqual(response.status_code, 200)
        authorize.assert_called_once_with("Bearer test-admin-key")
        self.assertIn("items", response.json())

    def test_protocol_job_start_and_status_routes(self) -> None:
        job = {
            "id": "xai-protocol-job-one",
            "status": "running",
            "stage": "turnstile",
            "message": "求解登录 Turnstile",
            "error": "",
            "source_account_id": "grok-one",
            "created_at": 1,
            "updated_at": 2,
            "models": [],
        }
        with patch("api.grok_oauth.require_admin", return_value={"id": "admin", "role": "admin"}), patch.object(
            grok_oauth.xai_cli_oauth_service,
            "start_protocol_authorization",
            new=AsyncMock(return_value={"reused": False, "job": job}),
        ), patch.object(
            grok_oauth.xai_cli_oauth_service,
            "get_protocol_authorization_job",
            return_value=job,
        ):
            started = self.client.post(
                "/api/grok/oauth/protocol/start",
                json={"account_id": "grok-one"},
                headers={"Authorization": "Bearer test-admin-key"},
            )
            status = self.client.get(
                "/api/grok/oauth/protocol/jobs/xai-protocol-job-one",
                headers={"Authorization": "Bearer test-admin-key"},
            )

        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["job"]["stage"], "turnstile")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["job"]["status"], "running")

    def test_account_probe_route_targets_requested_oauth_account(self) -> None:
        result = {
            "account_id": "oauth-one",
            "account": {"id": "oauth-one", "email": "oa***e@example.com"},
            "model": "grok-4.5",
            "content": "OK",
            "elapsed_ms": 120,
        }
        with patch("api.grok_oauth.require_admin", return_value={"id": "admin", "role": "admin"}), patch.object(
            grok_oauth.xai_cli_oauth_service,
            "test_account",
            new=AsyncMock(return_value=result),
        ) as test_account:
            response = self.client.post(
                "/api/grok/oauth/accounts/oauth-one/test",
                json={"model": "grok-4.5", "prompt": "只回复 OK"},
                headers={"Authorization": "Bearer test-admin-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account_id"], "oauth-one")
        test_account.assert_awaited_once_with("oauth-one", model="grok-4.5", prompt="只回复 OK")


if __name__ == "__main__":
    unittest.main()
