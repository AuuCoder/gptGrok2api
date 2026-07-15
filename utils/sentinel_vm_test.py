from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from utils import sentinel_vm


class SentinelVmTest(unittest.TestCase):
    def test_builds_primary_token_from_official_vm_results(self) -> None:
        session = MagicMock()
        actions = [
            {"request_p": "requirements-proof"},
            {"final_p": "sdk-proof", "t": "sdk-turnstile"},
        ]
        with (
            patch.object(sentinel_vm, "_ensure_sdk", return_value=Path("/tmp/sdk.js")),
            patch.object(sentinel_vm, "_run_action", side_effect=actions),
            patch.object(
                sentinel_vm,
                "_fetch_challenge",
                return_value=({"token": "challenge-token"}, "real-oai-sc"),
            ),
        ):
            bundle = sentinel_vm.get_sentinel_token_via_vm(session, "device-id", "authorize_continue")

        self.assertIsNotNone(bundle)
        token, oai_sc = bundle or ("", "")
        payload = json.loads(token)
        self.assertEqual(payload["p"], "sdk-proof")
        self.assertEqual(payload["t"], "sdk-turnstile")
        self.assertEqual(payload["c"], "challenge-token")
        self.assertEqual(oai_sc, "real-oai-sc")

    def test_returns_none_when_vm_does_not_return_turnstile_value(self) -> None:
        session = MagicMock()
        actions = [
            {"request_p": "requirements-proof"},
            {"final_p": "sdk-proof", "t": None},
        ]
        with (
            patch.object(sentinel_vm, "_ensure_sdk", return_value=Path("/tmp/sdk.js")),
            patch.object(sentinel_vm, "_run_action", side_effect=actions),
            patch.object(
                sentinel_vm,
                "_fetch_challenge",
                return_value=({"token": "challenge-token"}, "real-oai-sc"),
            ),
        ):
            token = sentinel_vm.get_sentinel_token_via_vm(session, "device-id", "authorize_continue")

        self.assertIsNone(token)

    def test_replaces_malformed_vm_turnstile_with_protocol_solver(self) -> None:
        session = MagicMock()
        actions = [
            {"request_p": "requirements-proof"},
            {"final_p": "sdk-proof", "t": "1"},
        ]
        challenge = {
            "token": "challenge-token",
            "turnstile": {"required": True, "dx": "turnstile-dx"},
        }
        with (
            patch.object(sentinel_vm, "_ensure_sdk", return_value=Path("/tmp/sdk.js")),
            patch.object(sentinel_vm, "_run_action", side_effect=actions),
            patch.object(
                sentinel_vm,
                "_fetch_challenge",
                return_value=(challenge, "real-oai-sc"),
            ),
            patch.object(
                sentinel_vm,
                "solve_turnstile_token",
                return_value="protocol-turnstile-token",
            ) as solve_turnstile,
        ):
            bundle = sentinel_vm.get_sentinel_token_via_vm(
                session,
                "device-id",
                "checkout_session_approval",
            )

        self.assertIsNotNone(bundle)
        token, _oai_sc = bundle or ("", "")
        self.assertEqual(json.loads(token)["t"], "protocol-turnstile-token")
        solve_turnstile.assert_called_once_with("turnstile-dx", "requirements-proof")

    def test_extracts_oai_sc_from_set_cookie_instead_of_challenge_token(self) -> None:
        response = MagicMock()
        response.headers = {
            "set-cookie": "oai-sc=0server-cookie; Domain=.chatgpt.com; Path=/\n__cflb=other; Path=/"
        }

        self.assertEqual(
            sentinel_vm._response_cookie_value(response, "oai-sc"),
            "0server-cookie",
        )
