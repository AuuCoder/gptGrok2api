from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.xai_reference_pkce_protocol import XaiReferencePkceProtocol


class XaiReferencePkceProtocolTest(unittest.TestCase):
    def test_live_authorization_reuses_exact_registration_session(self) -> None:
        registration_session = object()
        observed: dict[str, object] = {}

        class UnusedSession:
            closed = False

            def close(self) -> None:
                self.closed = True

        class FakeClient:
            def __init__(self, **_kwargs: object) -> None:
                self._s = UnusedSession()
                observed["client"] = self

            def login(self, *_args: object, **_kwargs: object) -> object:
                observed["login_session"] = self._s
                return types.SimpleNamespace(
                    token={
                        "access_token": "live-access",
                        "refresh_token": "live-refresh",
                        "expires_in": 3600,
                    }
                )

        package = types.ModuleType("xconsole_client")
        package.__path__ = []
        module = types.ModuleType("xconsole_client.oauth_protocol")
        module.ProtocolOAuthClient = FakeClient

        with tempfile.TemporaryDirectory() as temp_dir:
            reference_dir = Path(temp_dir)
            protocol_file = reference_dir / "xconsole_client" / "oauth_protocol.py"
            protocol_file.parent.mkdir()
            protocol_file.write_text("# test fixture\n", encoding="utf-8")
            protocol = XaiReferencePkceProtocol(str(reference_dir))
            with patch.dict(
                sys.modules,
                {
                    "xconsole_client": package,
                    "xconsole_client.oauth_protocol": module,
                },
            ):
                credential = protocol.authorize_live_session(
                    email="person@example.com",
                    password="password",
                    sso="sso-token",
                    session=registration_session,
                )

        client = observed["client"]
        self.assertIs(observed["login_session"], registration_session)
        self.assertIs(client._s, registration_session)
        self.assertEqual(credential["access_token"], "live-access")
        self.assertEqual(credential["refresh_token"], "live-refresh")

    def test_live_authorization_uses_system_turnstile_solver(self) -> None:
        observed: dict[str, object] = {}
        system_solver = MagicMock()
        system_solver.solve.return_value = "system-turnstile-token"

        class FakeSession:
            def close(self) -> None:
                pass

        class FakeClient:
            def __init__(self, **_kwargs: object) -> None:
                self._s = FakeSession()
                self.solver = None

            def login(self, *_args: object, **_kwargs: object) -> object:
                observed["token"] = self.solver.solve_turnstile(
                    "https://accounts.x.ai/sign-in",
                    "0x-site-key",
                )
                return types.SimpleNamespace(
                    token={"access_token": "access", "refresh_token": "refresh"}
                )

        package = types.ModuleType("xconsole_client")
        package.__path__ = []
        module = types.ModuleType("xconsole_client.oauth_protocol")
        module.ProtocolOAuthClient = FakeClient

        with tempfile.TemporaryDirectory() as temp_dir:
            reference_dir = Path(temp_dir)
            protocol_file = reference_dir / "xconsole_client" / "oauth_protocol.py"
            protocol_file.parent.mkdir()
            protocol_file.write_text("# test fixture\n", encoding="utf-8")
            protocol = XaiReferencePkceProtocol(
                str(reference_dir),
                turnstile_config={"provider": "local", "action": "oauth-action"},
            )
            with patch.dict(
                sys.modules,
                {
                    "xconsole_client": package,
                    "xconsole_client.oauth_protocol": module,
                },
            ), patch(
                "services.register.grok_protocol.TurnstileSolver",
                return_value=system_solver,
            ):
                protocol.authorize_live_session(
                    email="person@example.com",
                    password="password",
                    session=object(),
                )

        self.assertEqual(observed["token"], "system-turnstile-token")
        system_solver.solve.assert_called_once_with(
            website_url="https://accounts.x.ai/sign-in",
            sitekey="0x-site-key",
            action="oauth-action",
        )
        system_solver.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
