from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from services import cpa_service


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 300


class CPAOAuthUploadTest(unittest.TestCase):
    def _pool(self) -> dict:
        return {
            "id": "cpa-pool-one",
            "name": "Primary CPA",
            "base_url": "https://cpa.example.test",
            "secret_key": "management-secret",
        }

    def _account(self) -> dict:
        return {
            "email": "person@example.test",
            "subject": "principal-one",
            "access_token": "access-must-not-leak",
            "refresh_token": "refresh-must-not-leak",
            "id_token": "id-must-not-leak",
            "token_type": "Bearer",
            "expires_at": "2030-01-01T00:00:00+00:00",
        }

    def test_upload_uses_cliproxyapi_raw_json_contract(self) -> None:
        session = MagicMock()
        session.post.return_value = _Response(200)
        with patch("services.cpa_service.Session", return_value=session):
            result = cpa_service.upload_xai_oauth_file(self._pool(), self._account())

        self.assertTrue(result["ok"])
        self.assertEqual(result["file_name"], "xai-person@example.test.json")
        call = session.post.call_args
        self.assertEqual(call.args[0], "https://cpa.example.test/v0/management/auth-files")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer management-secret")
        self.assertNotIn("Content-Type", call.kwargs["headers"])
        file_name, content, content_type = call.kwargs["files"]["file"]
        self.assertEqual(file_name, "xai-person@example.test.json")
        self.assertEqual(content_type, "application/json")
        payload = json.loads(content)
        self.assertEqual(payload["type"], "xai")
        self.assertEqual(payload["auth_kind"], "oauth")
        self.assertEqual(payload["sub"], "principal-one")
        self.assertEqual(payload["expired"], "2030-01-01T00:00:00+00:00")
        session.close.assert_called_once_with()

    def test_upload_error_does_not_expose_credentials_or_response_body(self) -> None:
        session = MagicMock()
        session.post.return_value = _Response(422)
        with patch("services.cpa_service.Session", return_value=session):
            with self.assertRaises(cpa_service.CPAUploadError) as raised:
                cpa_service.upload_xai_oauth_file(self._pool(), self._account())

        message = str(raised.exception)
        self.assertIn("HTTP 422", message)
        self.assertNotIn("access-must-not-leak", message)
        self.assertNotIn("refresh-must-not-leak", message)

    def test_upload_openai_uses_cliproxyapi_codex_contract(self) -> None:
        session = MagicMock()
        session.post.return_value = _Response(200)
        with patch("services.cpa_service.Session", return_value=session):
            result = cpa_service.upload_openai_oauth_file(self._pool(), self._account())

        self.assertTrue(result["ok"])
        self.assertEqual(result["file_name"], "codex-person@example.test.json")
        file_name, content, content_type = session.post.call_args.kwargs["files"]["file"]
        self.assertEqual(file_name, "codex-person@example.test.json")
        self.assertEqual(content_type, "application/json")
        payload = json.loads(content)
        self.assertEqual(payload["type"], "codex")
        self.assertEqual(payload["email"], "person@example.test")
        self.assertEqual(payload["access_token"], "access-must-not-leak")
        self.assertEqual(payload["refresh_token"], "refresh-must-not-leak")
        self.assertEqual(payload["id_token"], "id-must-not-leak")
        self.assertNotIn("password", payload)

    def test_upload_openai_requires_complete_oauth_credentials(self) -> None:
        account = self._account()
        account["id_token"] = ""

        with self.assertRaisesRegex(cpa_service.CPAUploadError, "缺少完整 OAuth 凭据"):
            cpa_service.upload_openai_oauth_file(self._pool(), account)

    def test_normalize_cpa_delivery_config(self) -> None:
        self.assertEqual(
            cpa_service.normalize_cpa_delivery_config({"enabled": "true", "pool_id": " pool-one "}),
            {"enabled": True, "pool_id": "pool-one"},
        )


if __name__ == "__main__":
    unittest.main()
