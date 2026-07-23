from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.xai_device_oauth_protocol import (
    XaiDeviceOAuthProtocol,
    XaiDeviceOAuthProtocolError,
    parse_device_consent_form,
)


class DeviceConsentFormTest(unittest.TestCase):
    HTML = """
    <html><body>
      <form method="post" action="https://auth.x.ai/oauth2/device/approve">
        <input type="hidden" name="user_code" value="ABCD-EFGH">
        <input type="hidden" name="action" value="">
        <select name="principal_type">
          <option value="user" selected>User</option>
        </select>
        <select name="principal_id">
          <option value="principal-123" selected>person@example.com</option>
        </select>
        <button type="submit" name="action" value="deny">Deny</button>
        <button type="submit" name="action" value="allow">Allow</button>
      </form>
    </body></html>
    """

    def test_extracts_selected_principal_and_allow_action(self) -> None:
        action, payload = parse_device_consent_form(
            self.HTML,
            base_url="https://accounts.x.ai/oauth2/device/consent",
            user_code="ABCD-EFGH",
        )

        self.assertEqual(action, "https://auth.x.ai/oauth2/device/approve")
        self.assertEqual(
            payload,
            {
                "user_code": "ABCD-EFGH",
                "action": "allow",
                "principal_type": "user",
                "principal_id": "principal-123",
            },
        )

    def test_rejects_wrong_user_code(self) -> None:
        with self.assertRaisesRegex(XaiDeviceOAuthProtocolError, "wrong user code"):
            parse_device_consent_form(
                self.HTML,
                base_url="https://accounts.x.ai/oauth2/device/consent",
                user_code="OTHER-CODE",
            )

    def test_extracts_user_principal_from_next_flight_session(self) -> None:
        session_payload = [
            "$",
            "$L27",
            None,
            {
                "dehydratedState": {
                    "mutations": [],
                    "queries": [
                        {
                            "state": {
                                "data": {
                                    "user": {"userId": "user-principal-id", "email": "person@example.com"},
                                    "sessionId": "different-session-id",
                                }
                            },
                            "queryKey": ["session"],
                        }
                    ],
                }
            },
        ]
        flight = f"26:{json.dumps(session_payload, separators=(',', ':'))}\n"
        html = f"""
        <html><body>
          <form method="post" action="https://auth.x.ai/oauth2/device/approve">
            <input type="hidden" name="user_code" value="ABCD-EFGH">
            <input type="hidden" name="action" value="">
            <input type="hidden" name="principal_type" value="User">
            <input type="hidden" name="principal_id" value="">
            <button type="submit">Deny</button>
            <button type="submit">Allow</button>
          </form>
          <script>self.__next_f.push({json.dumps([1, flight])})</script>
        </body></html>
        """

        _, payload = parse_device_consent_form(
            html,
            base_url="https://accounts.x.ai/oauth2/device/consent",
            user_code="ABCD-EFGH",
        )

        self.assertEqual(payload["principal_type"], "User")
        self.assertEqual(payload["principal_id"], "user-principal-id")
        self.assertNotEqual(payload["principal_id"], "different-session-id")

    def test_rejects_untrusted_approval_host(self) -> None:
        html = self.HTML.replace("https://auth.x.ai", "https://invalid.example")
        with self.assertRaisesRegex(XaiDeviceOAuthProtocolError, "not found") as raised:
            parse_device_consent_form(
                html,
                base_url="https://accounts.x.ai/oauth2/device/consent",
                user_code="ABCD-EFGH",
            )

        self.assertEqual(raised.exception.stage, "consent")
        self.assertTrue(raised.exception.retryable)

    def test_turnstile_solver_reuses_protocol_proxy(self) -> None:
        protocol = XaiDeviceOAuthProtocol({}, proxy="socks5h://proxy.example:1080")
        self.assertEqual(
            protocol._turnstile_solver_config()["proxy"],
            "socks5h://proxy.example:1080",
        )

        direct = XaiDeviceOAuthProtocol({}, proxy="direct")
        self.assertNotIn("proxy", direct._turnstile_solver_config())

    def test_saved_sso_authorizes_without_password_login_or_turnstile(self) -> None:
        def response(*, status: int = 200, url: str = "", headers=None, payload=None, text: str = ""):
            item = MagicMock()
            item.status_code = status
            item.url = url
            item.headers = headers or {}
            item.text = text
            item.json.return_value = payload or {}
            return item

        client = MagicMock()
        client._request.side_effect = [
            response(payload={"device_code": "device-code", "user_code": "ABCD-EFGH", "expires_in": 300}),
            response(
                status=302,
                url="https://auth.x.ai/oauth2/device/verify",
                headers={"location": "https://accounts.x.ai/oauth2/device/consent?user_code=ABCD-EFGH"},
            ),
            response(
                url="https://accounts.x.ai/oauth2/device/consent?user_code=ABCD-EFGH",
                text=self.HTML,
            ),
            response(status=302, url="https://auth.x.ai/oauth2/device/approve"),
            response(payload={"access_token": "access-token", "refresh_token": "refresh-token", "expires_in": 21_600}),
        ]

        with patch("services.xai_device_oauth_protocol.GrokProtocolClient", return_value=client), patch(
            "services.xai_device_oauth_protocol.TurnstileSolver",
        ) as solver_type:
            result = XaiDeviceOAuthProtocol({}, proxy="direct").authorize(
                email="person@example.com",
                password="",
                sso="saved-sso-token",
            )

        self.assertEqual(result["access_token"], "access-token")
        self.assertEqual(result["sso"], "saved-sso-token")
        client.session.cookies.set.assert_called_once_with(
            "sso",
            "saved-sso-token",
            domain=".x.ai",
            path="/",
            secure=True,
        )
        client.bootstrap.assert_not_called()
        client.create_castle_token.assert_not_called()
        solver_type.assert_not_called()
        self.assertNotIn("saved-sso-token", repr(client._request.call_args_list))

    def test_expired_saved_sso_falls_back_to_password_login(self) -> None:
        def response(*, status: int = 200, url: str = "", headers=None, payload=None, text: str = ""):
            item = MagicMock()
            item.status_code = status
            item.url = url
            item.headers = headers or {}
            item.text = text
            item.json.return_value = payload or {}
            return item

        client = MagicMock()
        client.base_url = "https://accounts.x.ai"
        client.bootstrap.return_value = SimpleNamespace(sitekey="turnstile-sitekey")
        client.create_castle_token.return_value = "castle-token"
        client._cookie_value_for_domain.return_value = "new-sso-token"
        client._request.side_effect = [
            response(payload={"device_code": "device-code", "user_code": "ABCD-EFGH", "expires_in": 300}),
            response(
                status=302,
                url="https://auth.x.ai/oauth2/device/verify",
                headers={"location": "https://accounts.x.ai/sign-in"},
            ),
            response(status=401, url="https://accounts.x.ai/sign-in"),
            response(
                status=302,
                url="https://auth.x.ai/oauth2/device/verify",
                headers={"location": "https://accounts.x.ai/sign-in"},
            ),
            response(url="https://accounts.x.ai/sign-in"),
            response(payload={"response": {"createSessionResponse": {"authenticated": True}}}),
            response(
                status=302,
                url="https://auth.x.ai/oauth2/device/verify",
                headers={"location": "https://accounts.x.ai/oauth2/device/consent"},
            ),
            response(url="https://accounts.x.ai/oauth2/device/consent", text=self.HTML),
            response(status=302, url="https://auth.x.ai/oauth2/device/approve"),
            response(payload={"access_token": "access-token", "refresh_token": "refresh-token", "expires_in": 21_600}),
        ]
        solver = MagicMock()
        solver.solve.return_value = "turnstile-token"

        with patch("services.xai_device_oauth_protocol.GrokProtocolClient", return_value=client), patch(
            "services.xai_device_oauth_protocol.TurnstileSolver",
            return_value=solver,
        ):
            result = XaiDeviceOAuthProtocol({}, proxy="direct").authorize(
                email="person@example.com",
                password="password",
                sso="expired-sso-token",
            )

        self.assertEqual(result["access_token"], "access-token")
        self.assertEqual(result["sso"], "new-sso-token")
        client.session.cookies.delete.assert_called_once_with("sso", domain=".x.ai", path="/")
        client.bootstrap.assert_called_once_with()
        client.create_castle_token.assert_called_once_with(page_url="https://accounts.x.ai/sign-in")
        solver.solve.assert_called_once_with(
            website_url="https://accounts.x.ai/sign-in",
            sitekey="turnstile-sitekey",
        )

    def test_expired_saved_sso_without_password_stops_before_login_fallback(self) -> None:
        def response(*, status: int = 200, url: str = "", headers=None, payload=None):
            item = MagicMock()
            item.status_code = status
            item.url = url
            item.headers = headers or {}
            item.json.return_value = payload or {}
            return item

        client = MagicMock()
        client._request.side_effect = [
            response(payload={"device_code": "device-code", "user_code": "ABCD-EFGH", "expires_in": 300}),
            response(
                status=302,
                url="https://auth.x.ai/oauth2/device/verify",
                headers={"location": "https://accounts.x.ai/sign-in"},
            ),
            response(status=401, url="https://accounts.x.ai/sign-in"),
        ]

        with patch("services.xai_device_oauth_protocol.GrokProtocolClient", return_value=client), patch(
            "services.xai_device_oauth_protocol.TurnstileSolver",
        ) as solver_type:
            with self.assertRaisesRegex(
                XaiDeviceOAuthProtocolError,
                "no login password",
            ) as raised:
                XaiDeviceOAuthProtocol({}, proxy="direct").authorize(
                    email="person@example.com",
                    password="",
                    sso="expired-sso-token",
                )

        self.assertEqual(raised.exception.stage, "sso")
        client.session.cookies.delete.assert_called_once_with("sso", domain=".x.ai", path="/")
        client.bootstrap.assert_not_called()
        solver_type.assert_not_called()

    def test_password_login_accepts_auth_x_ai_navigation(self) -> None:
        def response(*, status: int = 200, url: str = "", headers=None, payload=None, text: str = ""):
            item = MagicMock()
            item.status_code = status
            item.url = url
            item.headers = headers or {}
            item.text = text
            item.json.return_value = payload or {}
            return item

        client = MagicMock()
        client.base_url = "https://accounts.x.ai"
        client.bootstrap.return_value = SimpleNamespace(sitekey="turnstile-sitekey")
        client.create_castle_token.return_value = "castle-token"
        client._cookie_value_for_domain.return_value = "new-sso-token"
        client._request.side_effect = [
            response(payload={"device_code": "device-code", "user_code": "ABCD-EFGH", "expires_in": 300}),
            response(
                url="https://auth.x.ai/oauth2/device/verify",
                headers={"location": "https://auth.x.ai/sign-in"},
            ),
            response(url="https://auth.x.ai/sign-in"),
            response(
                payload={
                    "response": {
                        "createSessionResponse": {
                            "cookieSetterUrl": "https://grok.com/auth/callback",
                        }
                    }
                }
            ),
            response(url="https://grok.com/"),
        ]
        solver = MagicMock()
        solver.solve.return_value = "turnstile-token"

        with patch("services.xai_device_oauth_protocol.GrokProtocolClient", return_value=client), patch(
            "services.xai_device_oauth_protocol.TurnstileSolver",
            return_value=solver,
        ):
            result = XaiDeviceOAuthProtocol({}, proxy="direct").authorize(
                email="person@example.com",
                password="password",
                sso_only=True,
            )

        self.assertEqual(result, {"sso": "new-sso-token"})

    def test_untrusted_navigation_error_reports_only_host_and_path(self) -> None:
        client = MagicMock()
        client.bootstrap.return_value = SimpleNamespace(sitekey="turnstile-sitekey")
        verify = MagicMock()
        verify.status_code = 302
        verify.url = "https://auth.x.ai/oauth2/device/verify"
        verify.headers = {"location": "https://invalid.example/sign-in?secret=value"}
        start = MagicMock()
        start.status_code = 200
        start.json.return_value = {
            "device_code": "device-code",
            "user_code": "ABCD-EFGH",
            "expires_in": 300,
        }
        client._request.side_effect = [start, verify]

        with patch("services.xai_device_oauth_protocol.GrokProtocolClient", return_value=client):
            with self.assertRaisesRegex(
                XaiDeviceOAuthProtocolError,
                r"invalid\.example/sign-in$",
            ):
                XaiDeviceOAuthProtocol({}, proxy="direct").authorize(
                    email="person@example.com",
                    password="password",
                )

    def test_password_login_accepts_relative_nested_cookie_setter_url(self) -> None:
        def response(*, status: int = 200, url: str = "", headers=None, payload=None, text: str = ""):
            item = MagicMock()
            item.status_code = status
            item.url = url
            item.headers = headers or {}
            item.text = text
            item.json.return_value = payload or {}
            return item

        client = MagicMock()
        client.base_url = "https://accounts.x.ai"
        client.bootstrap.return_value = SimpleNamespace(sitekey="turnstile-sitekey")
        client.create_castle_token.return_value = "castle-token"
        client._cookie_value_for_domain.return_value = "new-sso-token"
        client._request.side_effect = [
            response(payload={"device_code": "device-code", "user_code": "ABCD-EFGH", "expires_in": 300}),
            response(
                url="https://auth.x.ai/oauth2/device/verify",
                headers={"location": "https://accounts.x.ai/sign-in"},
            ),
            response(url="https://accounts.x.ai/sign-in"),
            response(payload={"result": {"cookie_setter_url": "/auth/callback"}}),
            response(url="https://accounts.x.ai/auth/callback"),
        ]
        solver = MagicMock()
        solver.solve.return_value = "turnstile-token"

        with patch("services.xai_device_oauth_protocol.GrokProtocolClient", return_value=client), patch(
            "services.xai_device_oauth_protocol.TurnstileSolver",
            return_value=solver,
        ):
            result = XaiDeviceOAuthProtocol({}, proxy="direct").authorize(
                email="person@example.com",
                password="password",
                sso_only=True,
            )

        self.assertEqual(result, {"sso": "new-sso-token"})
        self.assertEqual(client._request.call_args_list[4].args[1], "https://accounts.x.ai/auth/callback")

    def test_full_oauth_continues_when_create_session_sets_cookies_directly(self) -> None:
        def response(*, status: int = 200, url: str = "", headers=None, payload=None, text: str = ""):
            item = MagicMock()
            item.status_code = status
            item.url = url
            item.headers = headers or {}
            item.text = text
            item.json.return_value = payload or {}
            return item

        consent_html = DeviceConsentFormTest.HTML
        client = MagicMock()
        client.base_url = "https://accounts.x.ai"
        client.bootstrap.return_value = SimpleNamespace(sitekey="turnstile-sitekey")
        client.create_castle_token.return_value = "castle-token"
        client._cookie_value_for_domain.return_value = ""
        client._request.side_effect = [
            response(payload={"device_code": "device-code", "user_code": "ABCD-EFGH", "expires_in": 300}),
            response(
                url="https://auth.x.ai/oauth2/device/verify",
                headers={"location": "https://accounts.x.ai/sign-in"},
            ),
            response(url="https://accounts.x.ai/sign-in"),
            response(payload={"response": {"createSessionResponse": {"authenticated": True}}}),
            response(
                url="https://auth.x.ai/oauth2/device/verify",
                headers={"location": "https://accounts.x.ai/oauth2/device/consent"},
            ),
            response(url="https://accounts.x.ai/oauth2/device/consent", text=consent_html),
            response(status=302, url="https://auth.x.ai/oauth2/device/approve"),
            response(
                payload={
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 21_600,
                }
            ),
        ]
        solver = MagicMock()
        solver.solve.return_value = "turnstile-token"

        with patch("services.xai_device_oauth_protocol.GrokProtocolClient", return_value=client), patch(
            "services.xai_device_oauth_protocol.TurnstileSolver",
            return_value=solver,
        ):
            result = XaiDeviceOAuthProtocol({}, proxy="direct").authorize(
                email="person@example.com",
                password="password",
            )

        self.assertEqual(result["access_token"], "access-token")
        self.assertEqual(result["refresh_token"], "refresh-token")
        self.assertEqual(client._request.call_count, 8)

    def test_create_session_error_is_reported_before_consent_navigation(self) -> None:
        def response(*, status: int = 200, url: str = "", headers=None, payload=None, text: str = ""):
            item = MagicMock()
            item.status_code = status
            item.url = url
            item.headers = headers or {}
            item.text = text
            item.json.return_value = payload or {}
            return item

        client = MagicMock()
        client.base_url = "https://accounts.x.ai"
        client.bootstrap.return_value = SimpleNamespace(sitekey="turnstile-sitekey")
        client.create_castle_token.return_value = "castle-token"
        client._request.side_effect = [
            response(payload={"device_code": "device-code", "user_code": "ABCD-EFGH", "expires_in": 300}),
            response(
                url="https://auth.x.ai/oauth2/device/verify",
                headers={"location": "https://accounts.x.ai/sign-in"},
            ),
            response(url="https://accounts.x.ai/sign-in"),
            response(payload={"response": {"error": {"code": "invalid_credentials", "message": "Login rejected"}}}),
        ]
        solver = MagicMock()
        solver.solve.return_value = "turnstile-token"

        with patch("services.xai_device_oauth_protocol.GrokProtocolClient", return_value=client), patch(
            "services.xai_device_oauth_protocol.TurnstileSolver",
            return_value=solver,
        ):
            with self.assertRaisesRegex(XaiDeviceOAuthProtocolError, "xAI 账号登录失败：invalid_credentials: Login rejected"):
                XaiDeviceOAuthProtocol({}, proxy="direct").authorize(
                    email="person@example.com",
                    password="password",
                )

        self.assertEqual(client._request.call_count, 4)

    def test_password_login_can_return_grok_sso_without_device_consent(self) -> None:
        def response(*, status: int = 200, url: str = "", headers=None, payload=None, text: str = ""):
            item = MagicMock()
            item.status_code = status
            item.url = url
            item.headers = headers or {}
            item.text = text
            item.json.return_value = payload or {}
            return item

        client = MagicMock()
        client.base_url = "https://accounts.x.ai"
        client.bootstrap.return_value = SimpleNamespace(sitekey="turnstile-sitekey")
        client.create_castle_token.return_value = "castle-token"
        client._cookie_value_for_domain.return_value = "new-sso-token"
        client._request.side_effect = [
            response(payload={"device_code": "device-code", "user_code": "ABCD-EFGH", "expires_in": 300}),
            response(
                url="https://auth.x.ai/oauth2/device/verify",
                headers={"location": "https://accounts.x.ai/sign-in"},
            ),
            response(url="https://accounts.x.ai/sign-in"),
            response(payload={"cookieSetterUrl": "https://grok.com/auth/callback"}),
            response(url="https://grok.com/"),
        ]
        solver = MagicMock()
        solver.solve.return_value = "turnstile-token"

        with patch("services.xai_device_oauth_protocol.GrokProtocolClient", return_value=client), patch(
            "services.xai_device_oauth_protocol.TurnstileSolver",
            return_value=solver,
        ):
            result = XaiDeviceOAuthProtocol({}, proxy="direct").authorize(
                email="person@example.com",
                password="password",
                sso_only=True,
            )

        self.assertEqual(result, {"sso": "new-sso-token"})
        self.assertEqual(client._request.call_count, 5)
        solver.close.assert_called_once_with()
        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
