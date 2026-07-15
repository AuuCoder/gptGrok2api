from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from api.icloud_privacy_mail import _existing_account_claims, _response_headers, _sync_existing_account_claims, _upstream_path


class ICloudPrivacyMailProxyTest(unittest.TestCase):
    def test_maps_only_supported_sidecar_routes(self) -> None:
        self.assertIsNone(_upstream_path("auth/login"))
        self.assertEqual(_upstream_path("session"), "/api/icloud/session")
        self.assertEqual(_upstream_path("icloud/protocol-login/start"), "/api/icloud/protocol-login/start")
        self.assertEqual(_upstream_path("icloud/scheduler/status"), "/api/icloud/scheduler/status")
        self.assertEqual(_upstream_path("apple-account/login/2fa"), "/api/apple-account/login/2fa")
        self.assertEqual(_upstream_path("mailboxes/mbx_1/messages"), "/api/mailboxes/mbx_1/messages")
        self.assertIsNone(_upstream_path("unknown/path"))
        self.assertIsNone(_upstream_path("accountsevil"))
        self.assertIsNone(_upstream_path("session-extra"))

    def test_preserves_safe_response_headers_without_sidecar_auth_cookies(self) -> None:
        upstream = httpx.Response(
            200,
            headers=[
                ("content-type", "application/json"),
                ("x-internal", "not-forwarded"),
            ],
        )
        headers = _response_headers(upstream)
        self.assertIn(("content-type", "application/json"), headers)
        self.assertNotIn(("set-cookie", "ipm_session=abc; Path=/; HttpOnly"), headers)
        self.assertNotIn(("x-internal", "not-forwarded"), headers)

    def test_existing_account_claims_are_normalized_and_deduplicated_per_project(self) -> None:
        with (
            patch("api.icloud_privacy_mail.account_service.list_accounts", return_value=[
                {"email": "GPT@icloud.com"},
                {"email": "gpt@icloud.com"},
                {"email": ""},
            ]),
            patch("api.icloud_privacy_mail.grok_account_store.list_accounts", return_value=[
                {"email": "Grok@icloud.com"},
            ]),
        ):
            claims = _existing_account_claims()

        self.assertEqual(claims, {"openai": ["gpt@icloud.com"], "grok": ["grok@icloud.com"]})


class ICloudPrivacyMailClaimSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_sync_existing_claims_uses_internal_header_and_separate_projects(self) -> None:
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"success": True, "updated": 1, "missing": []}
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("api.icloud_privacy_mail._base_url", return_value="http://icloud-sidecar:8787"),
            patch("api.icloud_privacy_mail._existing_account_claims", return_value={
                "openai": ["gpt@icloud.com"],
                "grok": ["grok@icloud.com"],
            }),
            patch("api.icloud_privacy_mail.httpx.AsyncClient", return_value=context),
        ):
            result = await _sync_existing_account_claims()

        self.assertTrue(result["success"])
        self.assertEqual(client.post.await_count, 2)
        openai_call, grok_call = client.post.await_args_list
        self.assertEqual(openai_call.kwargs["json"], {"project": "openai", "emails": ["gpt@icloud.com"], "claimed": True})
        self.assertEqual(grok_call.kwargs["json"], {"project": "grok", "emails": ["grok@icloud.com"], "claimed": True})
        self.assertEqual(openai_call.kwargs["headers"], {"X-ChatGPT2API-Internal": "icloud-privacy-mail"})


if __name__ == "__main__":
    unittest.main()
