from __future__ import annotations

import json
import unittest

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
        with self.assertRaisesRegex(XaiDeviceOAuthProtocolError, "not found"):
            parse_device_consent_form(
                html,
                base_url="https://accounts.x.ai/oauth2/device/consent",
                user_code="ABCD-EFGH",
            )

    def test_turnstile_solver_reuses_protocol_proxy(self) -> None:
        protocol = XaiDeviceOAuthProtocol({}, proxy="socks5h://proxy.example:1080")
        self.assertEqual(
            protocol._turnstile_solver_config()["proxy"],
            "socks5h://proxy.example:1080",
        )

        direct = XaiDeviceOAuthProtocol({}, proxy="direct")
        self.assertNotIn("proxy", direct._turnstile_solver_config())


if __name__ == "__main__":
    unittest.main()
