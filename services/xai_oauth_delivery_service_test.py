from __future__ import annotations

import unittest
from unittest.mock import patch

from services import xai_oauth_delivery_service as delivery_service


class XaiOAuthDeliveryServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.account = {
            "email": "delivery@example.test",
            "subject": "delivery-principal",
            "access_token": "delivery-access",
            "refresh_token": "delivery-refresh",
            "id_token": "delivery-id",
        }

    def test_default_configuration_skips_both_targets(self) -> None:
        with patch.object(delivery_service, "_deliver_sub2api") as sub2api, patch.object(
            delivery_service,
            "_deliver_cpa",
        ) as cpa:
            result = delivery_service.deliver_xai_oauth_account(self.account, {})

        self.assertEqual(result["sub2api"]["status"], "skipped")
        self.assertEqual(result["cpa"]["status"], "skipped")
        sub2api.assert_not_called()
        cpa.assert_not_called()

    def test_targets_run_independently_and_one_failure_does_not_cancel_the_other(self) -> None:
        config = {
            "sub2api": {
                "enabled": True,
                "server_id": "sub-server",
                "group_id": "42",
            },
            "cpa": {"enabled": True, "pool_id": "cpa-pool"},
        }
        with patch.object(
            delivery_service,
            "_deliver_sub2api",
            return_value={"ok": True, "account_id": "remote-account"},
        ), patch.object(
            delivery_service,
            "_deliver_cpa",
            side_effect=RuntimeError("delivery-refresh must not leak"),
        ):
            result = delivery_service.deliver_xai_oauth_account(self.account, config)

        self.assertEqual(result["sub2api"]["status"], "success")
        self.assertEqual(result["sub2api"]["target_id"], "sub-server")
        self.assertEqual(result["cpa"]["status"], "failed")
        self.assertEqual(result["cpa"]["target_id"], "cpa-pool")
        self.assertNotIn("delivery-refresh", result["cpa"]["error"])
        self.assertIn("[redacted]", result["cpa"]["error"])


if __name__ == "__main__":
    unittest.main()
