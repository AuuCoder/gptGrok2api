from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from utils import sentinel


class SentinelTokenTest(unittest.TestCase):
    def test_turnstile_token_is_not_reused_as_session_observer_token(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.text = "response"
        response.headers = {"set-cookie": "oai-sc=0server-cookie; Domain=.chatgpt.com; Path=/"}
        response.json.return_value = {
            "token": "challenge-token",
            "proofofwork": {"required": False},
            "turnstile": {"required": True, "dx": "turnstile-dx"},
            "so": {"required": True, "snapshot_dx": "opaque-input"},
        }
        session = MagicMock()
        session.post.return_value = response

        with (
            patch.object(sentinel, "_get_vm_sentinel_token", return_value=None),
            patch.object(sentinel, "solve_turnstile_token", return_value="turnstile-token"),
        ):
            sentinel_value, so_token, oai_sc = sentinel.build_sentinel_with_so_token(
                session,
                "device-id",
                "authorize_continue",
            )

        self.assertEqual(so_token, "")
        self.assertEqual(oai_sc, "0server-cookie")
        self.assertEqual(json.loads(sentinel_value)["t"], "turnstile-token")

    def test_missing_session_observer_token_does_not_reuse_turnstile_token(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.text = "response"
        response.headers = {"set-cookie": "oai-sc=0server-cookie; Domain=.chatgpt.com; Path=/"}
        response.json.return_value = {
            "token": "challenge-token",
            "proofofwork": {"required": False},
            "turnstile": {"required": True, "dx": "turnstile-dx"},
        }
        session = MagicMock()
        session.post.return_value = response

        with (
            patch.object(sentinel, "_get_vm_sentinel_token", return_value=None),
            patch.object(sentinel, "solve_turnstile_token", return_value="turnstile-token"),
        ):
            sentinel_value, so_token, _oai_sc = sentinel.build_sentinel_with_so_token(
                session,
                "device-id",
                "authorize_continue",
            )

        self.assertEqual(so_token, "")
        self.assertEqual(json.loads(sentinel_value)["t"], "turnstile-token")

    def test_vm_primary_token_is_used_before_synthetic_fallback(self) -> None:
        session = MagicMock()
        vm_token = json.dumps(
            {"p": "sdk-proof", "t": "sdk-turnstile", "c": "challenge-token", "id": "device-id", "flow": "authorize_continue"}
        )

        with patch.object(
            sentinel,
            "_get_vm_sentinel_token",
            return_value=(vm_token, "0vm-cookie"),
        ):
            sentinel_value, so_token, oai_sc = sentinel.build_sentinel_with_so_token(
                session,
                "device-id",
                "authorize_continue",
            )

        self.assertEqual(sentinel_value, vm_token)
        self.assertEqual(so_token, "")
        self.assertEqual(oai_sc, "0vm-cookie")


if __name__ == "__main__":
    unittest.main()
