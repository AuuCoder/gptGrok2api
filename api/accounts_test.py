from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
