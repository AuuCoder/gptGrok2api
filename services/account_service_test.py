import unittest
from datetime import datetime, timezone
from threading import Lock
from unittest.mock import patch

from services.account_service import AccountService


class AccountServiceSessionImportTests(unittest.TestCase):
    def test_prepare_account_payload_keeps_camel_case_session_tokens(self) -> None:
        payload = AccountService._prepare_account_payload(
            {
                "type": "codex",
                "accessToken": "access-token",
                "refreshToken": "refresh-token",
                "idToken": "id-token",
                "sessionToken": "session-token",
                "expiresAt": "2026-07-12T00:00:00Z",
            }
        )

        self.assertEqual(
            payload,
            {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "session_token": "session-token",
                "expired": "2026-07-12T00:00:00Z",
                "export_type": "codex",
                "source_type": "codex",
            },
        )

    def test_prepare_account_payload_prefers_existing_snake_case_tokens(self) -> None:
        payload = AccountService._prepare_account_payload(
            {
                "access_token": "access-token",
                "refresh_token": "stored-refresh-token",
                "refreshToken": "browser-refresh-token",
            }
        )

        self.assertEqual(payload["refresh_token"], "stored-refresh-token")
        self.assertNotIn("refreshToken", payload)

    def test_chatgpt_web_session_is_not_scheduled_for_platform_oauth_refresh(self) -> None:
        service = object.__new__(AccountService)
        due_at = service._refresh_token_keepalive_due_at(
            {"source_type": "chatgpt_web", "refresh_token": "browser-session-refresh"},
            datetime.now(timezone.utc),
        )

        self.assertIsNone(due_at)

    def test_ready_pix_checkout_rejects_later_failure_state(self) -> None:
        final_url = "https://payments.stripe.com/qr/instructions/pix-ready"
        service = object.__new__(AccountService)
        service._lock = Lock()
        service._token_aliases = {}
        service._image_inflight = {}
        service._accounts = {
            "live-token": {
                "access_token": "live-token",
                "checkout_link_status": "ready",
                "checkout_final_url": final_url,
                "checkout_attempt_count": 3,
                "checkout_last_error": None,
                "checkout_billing_address": {
                    "country": "BR",
                    "postal_code": "01310100",
                },
            }
        }

        with patch.object(service, "_save_accounts"):
            updated = service.update_account(
                "live-token",
                {
                    "checkout_link_status": "pending",
                    "checkout_attempt_count": 4,
                    "checkout_last_error": "approve blocked",
                    "checkout_last_error_at": "2026-07-14T15:00:00+00:00",
                    "checkout_billing_address": None,
                },
                quiet=True,
            )

        self.assertIsNotNone(updated)
        self.assertEqual(updated["checkout_link_status"], "ready")
        self.assertEqual(updated["checkout_final_url"], final_url)
        self.assertEqual(updated["checkout_attempt_count"], 3)
        self.assertIsNone(updated["checkout_last_error"])
        self.assertEqual(
            updated["checkout_billing_address"],
            {"country": "BR", "postal_code": "01310100"},
        )

    def test_ready_pix_checkout_accepts_a_complete_new_success(self) -> None:
        old_url = "https://payments.stripe.com/qr/instructions/pix-old"
        new_url = "https://payments.stripe.com/qr/instructions/pix-new"
        service = object.__new__(AccountService)
        service._lock = Lock()
        service._token_aliases = {}
        service._image_inflight = {}
        service._accounts = {
            "live-token": {
                "access_token": "live-token",
                "checkout_link_status": "ready",
                "checkout_final_url": old_url,
            }
        }

        with patch.object(service, "_save_accounts"):
            updated = service.update_account(
                "live-token",
                {
                    "checkout_link_status": "ready",
                    "checkout_final_url": new_url,
                    "checkout_last_error": None,
                },
                quiet=True,
            )

        self.assertIsNotNone(updated)
        self.assertEqual(updated["checkout_final_url"], new_url)

    def test_ready_upi_checkout_rejects_later_failure_state(self) -> None:
        final_url = "https://payments.stripe.com/upi/instructions/upi-ready?client_secret=secret"
        service = object.__new__(AccountService)
        service._lock = Lock()
        service._token_aliases = {}
        service._image_inflight = {}
        service._accounts = {
            "live-token": {
                "access_token": "live-token",
                "checkout_link_status": "ready",
                "checkout_final_url": final_url,
                "checkout_attempt_count": 5,
                "checkout_billing_address": {"country": "IN", "postal_code": "700016"},
            }
        }

        with patch.object(service, "_save_accounts"):
            updated = service.update_account(
                "live-token",
                {
                    "checkout_link_status": "failed",
                    "checkout_attempt_count": 6,
                    "checkout_last_error": "approve blocked",
                    "checkout_billing_address": None,
                },
                quiet=True,
            )

        self.assertIsNotNone(updated)
        self.assertEqual(updated["checkout_link_status"], "ready")
        self.assertEqual(updated["checkout_final_url"], final_url)
        self.assertEqual(updated["checkout_attempt_count"], 5)
        self.assertEqual(
            updated["checkout_billing_address"],
            {"country": "IN", "postal_code": "700016"},
        )


if __name__ == "__main__":
    unittest.main()
