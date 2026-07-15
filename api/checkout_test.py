from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import checkout


class CheckoutRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(checkout.create_router())
        self.client = TestClient(app)

    def test_creates_upi_link_for_admin(self) -> None:
        created = {
            "checkout_final_url": "https://payments.stripe.com/upi/instructions/upi_test_123",
            "session_token": "top-level-secret-session",
            "publishable_key": "pk_top-level-secret",
            "item": {"access_token": "stored-access-token", "cookie_header": "secret-cookie"},
        }
        with (
            patch("api.checkout.require_admin", return_value={"id": "admin", "role": "admin"}) as authorize,
            patch.object(
                checkout.register_service,
                "get",
                return_value={
                    "checkout": {
                        "checkout_proxy_enabled": True,
                        "checkout_proxy_url": " http://checkout.example.test:8000 ",
                        "promotion_proxy_enabled": True,
                        "promotion_proxy_url": "http://promotion.example.test:8001",
                        "provider_proxy_enabled": True,
                        "provider_proxy_url": "http://provider.example.test:8002",
                    }
                },
            ),
            patch.object(checkout.openai_checkout_service, "extract_and_store_checkout_link", return_value=created) as create,
        ):
            response = self.client.post(
                "/api/accounts/checkout-session",
                headers={"Authorization": "Bearer test-admin-key"},
                json={"access_token": "stored-access-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["checkout_final_url"], created["checkout_final_url"])
        self.assertNotIn("cookie_header", response.json()["item"])
        self.assertNotIn("session_token", response.json())
        self.assertNotIn("publishable_key", response.json())
        authorize.assert_called_once_with("Bearer test-admin-key")
        create.assert_called_once_with(
            "stored-access-token",
            checkout_channel="upi",
            checkout_proxy="http://checkout.example.test:8000",
            promotion_proxy="http://promotion.example.test:8001",
            provider_proxy="http://checkout.example.test:8000",
        )

    def test_rejects_enabled_blank_proxy(self) -> None:
        with (
            patch("api.checkout.require_admin", return_value={"id": "admin", "role": "admin"}),
            patch.object(
                checkout.register_service,
                "get",
                return_value={"checkout": {"checkout_proxy_enabled": True, "checkout_proxy_url": ""}},
            ),
            patch.object(checkout.openai_checkout_service, "extract_and_store_checkout_link") as create,
        ):
            response = self.client.post(
                "/api/accounts/checkout-session",
                headers={"Authorization": "Bearer test-admin-key"},
                json={"access_token": "stored-access-token"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("未填写代理 URL", response.json()["detail"]["error"])
        create.assert_not_called()

    def test_uses_configured_pix_channel_and_one_br_proxy(self) -> None:
        with (
            patch("api.checkout.require_admin", return_value={"id": "admin", "role": "admin"}),
            patch.object(
                checkout.register_service,
                "get",
                return_value={
                    "checkout": {
                        "channel": "pix",
                        "pix_protocol": "reference",
                        "checkout_proxy_enabled": True,
                        "checkout_proxy_url": "http://br-shared.example.test:8000",
                        "promotion_proxy_enabled": True,
                        "promotion_proxy_url": "http://ignored.example.test:8001",
                    }
                },
            ),
            patch.object(
                checkout.openai_checkout_service,
                "extract_and_store_checkout_link",
                return_value={"checkout_final_url": "https://payments.stripe.com/qr/instructions/pix_test"},
            ) as create,
        ):
            response = self.client.post(
                "/api/accounts/checkout-session",
                headers={"Authorization": "Bearer test-admin-key"},
                json={"access_token": "stored-access-token"},
            )

        self.assertEqual(response.status_code, 200)
        create.assert_called_once_with(
            "stored-access-token",
            checkout_channel="pix",
            pix_protocol="reference",
            checkout_proxy="http://br-shared.example.test:8000",
            promotion_proxy="http://br-shared.example.test:8000",
            provider_proxy="http://br-shared.example.test:8000",
        )

    def test_scheme3_uses_configured_vn_promotion_proxy(self) -> None:
        with (
            patch("api.checkout.require_admin", return_value={"id": "admin", "role": "admin"}),
            patch.object(
                checkout.register_service,
                "get",
                return_value={
                    "checkout": {
                        "channel": "pix",
                        "pix_protocol": "standalone",
                        "checkout_proxy_enabled": True,
                        "checkout_proxy_url": "http://br.example.test:8000",
                        "promotion_proxy_enabled": True,
                        "promotion_proxy_url": "http://vn.example.test:8001",
                    }
                },
            ),
            patch.object(
                checkout.openai_checkout_service,
                "extract_and_store_checkout_link",
                return_value={"checkout_final_url": "https://payments.stripe.com/qr/instructions/pix_scheme3"},
            ) as create,
        ):
            response = self.client.post(
                "/api/accounts/checkout-session",
                headers={"Authorization": "Bearer test-admin-key"},
                json={"access_token": "stored-access-token"},
            )

        self.assertEqual(response.status_code, 200)
        create.assert_called_once_with(
            "stored-access-token",
            checkout_channel="pix",
            pix_protocol="standalone",
            checkout_proxy="http://br.example.test:8000",
            promotion_proxy="http://vn.example.test:8001",
            provider_proxy="http://br.example.test:8000",
        )

    def test_enqueues_selected_accounts_for_continuous_retries(self) -> None:
        with (
            patch("api.checkout.require_admin", return_value={"id": "admin", "role": "admin"}),
            patch.object(
                checkout.register_service,
                "enqueue_checkout_retries_for_accounts",
                return_value={"queued": 2, "skipped": 1},
            ) as enqueue,
        ):
            response = self.client.post(
                "/api/accounts/checkout-retries",
                headers={"Authorization": "Bearer test-admin-key"},
                json={"access_tokens": ["token-a", "token-b", "token-a"]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"queued": 2, "skipped": 1})
        enqueue.assert_called_once_with(["token-a", "token-b", "token-a"])


if __name__ == "__main__":
    unittest.main()
