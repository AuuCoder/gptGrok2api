from __future__ import annotations

import unittest
from unittest.mock import patch

from services.openai_checkout_service import CheckoutSessionError
from services.openai_final_link_service import (
    OpenAIFinalLinkService,
    _with_rotated_sticky_session,
    is_final_checkout_url,
    is_final_pix_url,
    is_final_upi_url,
)


UPI_FINAL_URL = "https://payments.stripe.com/upi/instructions/upi_test_123?client_secret=pi_secret"
PIX_FINAL_URL = "https://payments.stripe.com/qr/instructions/pix_test_123"


class OpenAIFinalLinkServiceTest(unittest.TestCase):
    def test_validates_only_complete_upi_instruction_urls(self) -> None:
        self.assertTrue(is_final_upi_url(UPI_FINAL_URL))
        self.assertTrue(is_final_checkout_url(UPI_FINAL_URL))
        self.assertFalse(is_final_upi_url("http://payments.stripe.com/upi/instructions/upi_test"))
        self.assertFalse(is_final_upi_url("https://payments.stripe.com/upi/instructions/"))
        self.assertFalse(is_final_upi_url("https://payments.stripe.com@invalid.test/upi/instructions/upi_test"))

    def test_validates_only_complete_pix_instruction_urls(self) -> None:
        self.assertTrue(is_final_pix_url(PIX_FINAL_URL))
        self.assertTrue(is_final_checkout_url(PIX_FINAL_URL, "pix"))
        self.assertFalse(is_final_checkout_url(PIX_FINAL_URL, "upi"))
        self.assertFalse(is_final_pix_url("http://payments.stripe.com/qr/instructions/pix_test"))
        self.assertFalse(is_final_pix_url("https://payments.stripe.com/qr/instructions/"))

    def test_prepare_pix_proxies_reuses_one_br_sticky_session(self) -> None:
        source = (
            "http://account-zone-custom-region-US-session-12345678:secret"
            "@proxy.example.test:8000"
        )
        checkout, promotion, provider = OpenAIFinalLinkService._prepare_pix_proxies(
            checkout_proxy=source,
            promotion_proxy="",
            provider_proxy=source,
            proxy_rotation=3,
        )
        self.assertEqual(checkout, promotion)
        self.assertEqual(checkout, provider)
        self.assertIn("region-BR", checkout)

    def test_prepare_pix_proxies_accepts_rotating_gateway_without_global_lock(self) -> None:
        source = "http://account-region-BR:secret@rotating.example.test:8000"

        checkout, promotion, provider = OpenAIFinalLinkService._prepare_pix_proxies(
            checkout_proxy=source,
            promotion_proxy="",
            provider_proxy=source,
            proxy_rotation=3,
        )

        self.assertEqual(checkout, source)
        self.assertEqual(checkout, promotion)
        self.assertEqual(checkout, provider)

    def test_prepare_pix_proxies_rotates_explicit_sticky_sessions_per_account(self) -> None:
        source = (
            "http://account-zone-custom-region-US-session-12345678:secret"
            "@proxy.example.test:8000"
        )

        first, _, _ = OpenAIFinalLinkService._prepare_pix_proxies(
            checkout_proxy=source,
            promotion_proxy="",
            provider_proxy=source,
            proxy_rotation=1,
        )
        second, _, _ = OpenAIFinalLinkService._prepare_pix_proxies(
            checkout_proxy=source,
            promotion_proxy="",
            provider_proxy=source,
            proxy_rotation=2,
        )

        self.assertNotEqual(first, second)

    def test_prepare_scheme3_pix_proxies_splits_br_and_vn(self) -> None:
        source = (
            "http://account-zone-custom-region-US-session-12345678:secret"
            "@proxy.example.test:8000"
        )
        checkout, promotion, provider = OpenAIFinalLinkService._prepare_pix_proxies(
            checkout_proxy=source,
            promotion_proxy="",
            provider_proxy=source,
            proxy_rotation=3,
            pix_protocol="standalone",
        )

        self.assertEqual(checkout, provider)
        self.assertIn("region-BR", checkout)
        self.assertIn("region-VN", promotion)
        self.assertNotEqual(checkout, promotion)

    def test_prepare_upi_proxies_shares_in_sticky_session(self) -> None:
        in_proxy = (
            "socks5h://account:secret-IN-session-77297054-life-5m"
            "@gate.kookeey.info:1000"
        )
        checkout, promotion, provider = OpenAIFinalLinkService._prepare_upi_proxies(
            checkout_proxy=in_proxy,
            promotion_proxy="http://vn-promotion.example.test:8001",
            provider_proxy=in_proxy,
            proxy_rotation=0,
        )

        self.assertEqual(checkout, provider)
        self.assertNotEqual(checkout, in_proxy)
        self.assertEqual(promotion, "http://vn-promotion.example.test:8001")

    def test_prepare_upi_proxies_rotates_shared_in_session_together(self) -> None:
        in_proxy = (
            "socks5h://account:secret-IN-session-77297054-life-5m"
            "@gate.kookeey.info:1000"
        )
        checkout_a, _promotion_a, provider_a = OpenAIFinalLinkService._prepare_upi_proxies(
            checkout_proxy=in_proxy,
            promotion_proxy="http://vn-promotion.example.test:8001",
            provider_proxy=in_proxy,
            proxy_rotation=1,
        )
        checkout_b, _promotion_b, provider_b = OpenAIFinalLinkService._prepare_upi_proxies(
            checkout_proxy=in_proxy,
            promotion_proxy="http://vn-promotion.example.test:8001",
            provider_proxy=in_proxy,
            proxy_rotation=2,
        )

        self.assertEqual(checkout_a, provider_a)
        self.assertEqual(checkout_b, provider_b)
        self.assertNotEqual(checkout_a, checkout_b)

    def test_prepare_upi_proxies_forces_expected_role_countries(self) -> None:
        checkout_source = (
            "http://account-zone-custom-region-BR-session-12345678:secret"
            "@proxy.example.test:8000"
        )
        promotion_source = (
            "http://account-zone-custom-region-US-session-87654321:secret"
            "@proxy.example.test:8001"
        )

        checkout, promotion, provider = OpenAIFinalLinkService._prepare_upi_proxies(
            checkout_proxy=checkout_source,
            promotion_proxy=promotion_source,
            provider_proxy=checkout_source,
            proxy_rotation=4,
        )

        self.assertIn("region-IN", checkout)
        self.assertIn("region-VN", promotion)
        self.assertEqual(checkout, provider)

    def test_prepare_upi_proxies_rejects_different_in_sessions(self) -> None:
        with self.assertRaisesRegex(CheckoutSessionError, "sticky session"):
            OpenAIFinalLinkService._prepare_upi_proxies(
                checkout_proxy="http://in-checkout.example.test:8000",
                promotion_proxy="http://vn-promotion.example.test:8001",
                provider_proxy="http://in-provider.example.test:8002",
                proxy_rotation=0,
            )

    def test_password_sticky_session_rotates_without_changing_country(self) -> None:
        compact = "socks5h://account:secret-IN-77297054@gate.kookeey.info:1000"
        session_form = (
            "socks5h://account:secret-IN-session-77297054-life-5m"
            "@gate.kookeey.info:1000"
        )
        session_a = _with_rotated_sticky_session(session_form, role="upi-in-provider", rotation=1)
        session_b = _with_rotated_sticky_session(session_form, role="upi-in-provider", rotation=2)

        self.assertEqual(_with_rotated_sticky_session(compact, role="upi-in-provider", rotation=1), compact)
        self.assertNotEqual(session_a, session_form)
        self.assertNotEqual(session_a, session_b)
        self.assertIn("secret-IN-session-", session_a)
        self.assertIn("-life-5m", session_a)

    def test_create_final_link_returns_upi_metadata(self) -> None:
        account = {"access_token": "live-token", "checkout_attempt_count": 1}
        protocol_result = {
            "ok": True,
            "provider": "upi_open_source",
            "payment_method": "upi",
            "checkout_final_url": UPI_FINAL_URL,
            "checkout_session_id": "cs_test_123",
            "processor_entity": "openai_llc",
            "pay_amount_minor": 0,
            "is_free_trial": True,
            "checkout_billing_name": "Aisha Sharma",
            "checkout_billing_email": "member@example.test",
            "checkout_billing_address": {
                "country": "IN",
                "line1": "24 Park Street",
                "line2": "",
                "city": "Kolkata",
                "state": "WB",
                "postal_code": "700016",
            },
        }
        service = OpenAIFinalLinkService()
        with (
            patch("services.openai_final_link_service.account_service.resolve_access_token", return_value="live-token"),
            patch("services.openai_final_link_service.account_service.get_account", return_value=account),
            patch("services.openai_final_link_service.account_service.refresh_access_token", return_value="live-token"),
            patch.object(service, "_run_upi_protocol", return_value=protocol_result),
        ):
            result = service.create_final_link(
                "live-token",
                checkout_proxy="http://checkout.example.test:8000",
                promotion_proxy="http://promotion.example.test:8001",
                provider_proxy="http://checkout.example.test:8000",
            )

        self.assertEqual(result["checkout_final_url"], UPI_FINAL_URL)
        self.assertEqual(result["checkout_channel"], "upi")
        self.assertEqual(result["country"], "IN")
        self.assertEqual(result["currency"], "INR")
        self.assertEqual(result["checkout_payment_method"], "upi")
        self.assertEqual(result["checkout_billing_name"], "Aisha Sharma")
        self.assertEqual(result["checkout_billing_email"], "member@example.test")
        self.assertEqual(result["checkout_billing_address"]["postal_code"], "700016")

    def test_protocol_error_is_mapped_to_checkout_error(self) -> None:
        account = {"access_token": "live-token"}
        service = OpenAIFinalLinkService()
        with (
            patch("services.openai_final_link_service.account_service.resolve_access_token", return_value="live-token"),
            patch("services.openai_final_link_service.account_service.get_account", return_value=account),
            patch("services.openai_final_link_service.account_service.refresh_access_token", return_value="live-token"),
            patch.object(
                service,
                "_run_upi_protocol",
                return_value={"ok": False, "error": "UPI unavailable", "error_code": "upi_unavailable"},
            ),
        ):
            with self.assertRaises(CheckoutSessionError) as context:
                service.create_final_link(
                    "live-token",
                    checkout_proxy="http://checkout.example.test:8000",
                    promotion_proxy="http://promotion.example.test:8001",
                    provider_proxy="http://checkout.example.test:8000",
                )
        self.assertEqual(context.exception.status_code, 422)

    def test_create_final_link_returns_pix_metadata(self) -> None:
        account = {"access_token": "live-token"}
        protocol_result = {
            "ok": True,
            "provider": "pix_br",
            "payment_method": "pix",
            "checkout_final_url": PIX_FINAL_URL,
            "checkout_session_id": "cs_pix_123",
            "processor_entity": "openai_ie",
            "pay_amount_minor": 50,
            "is_free_trial": False,
            "qr_image_url_png": "https://qr.stripe.com/live_pix.png",
            "qr_expires_at": 1_900_000_000,
            "checkout_billing_name": "Gabriel Almeida",
            "checkout_billing_email": "member@example.test",
            "checkout_billing_address": {
                "country": "BR",
                "line1": "Avenida Paulista 1000",
                "line2": "",
                "city": "Sao Paulo",
                "state": "SP",
                "postal_code": "01310100",
            },
            "checkout_billing_tax_id": "060.543.916-05",
        }
        service = OpenAIFinalLinkService()
        with (
            patch("services.openai_final_link_service.account_service.resolve_access_token", return_value="live-token"),
            patch("services.openai_final_link_service.account_service.get_account", return_value=account),
            patch("services.openai_final_link_service.account_service.refresh_access_token", return_value="live-token"),
            patch.object(service, "_run_pix_protocol", return_value=protocol_result),
        ):
            result = service.create_final_link(
                "live-token",
                checkout_channel="pix",
                checkout_proxy="http://br.example.test:8000",
            )

        self.assertEqual(result["checkout_final_url"], PIX_FINAL_URL)
        self.assertEqual(result["checkout_channel"], "pix")
        self.assertEqual(result["country"], "BR")
        self.assertEqual(result["currency"], "BRL")
        self.assertEqual(result["checkout_qr_image_url_png"], "https://qr.stripe.com/live_pix.png")
        self.assertEqual(result["checkout_billing_name"], "Gabriel Almeida")
        self.assertEqual(result["checkout_billing_email"], "member@example.test")
        self.assertEqual(result["checkout_billing_address"]["postal_code"], "01310100")
        self.assertEqual(result["checkout_billing_tax_id"], "060.543.916-05")

    def test_extract_and_store_persists_pix_billing_snapshot(self) -> None:
        account = {"access_token": "live-token", "checkout_attempt_count": 0}
        billing_address = {
            "country": "BR",
            "line1": "Avenida Paulista 1000",
            "line2": "",
            "city": "Sao Paulo",
            "state": "SP",
            "postal_code": "01310100",
        }
        checkout = {
            "checkout_url": PIX_FINAL_URL,
            "checkout_final_url": PIX_FINAL_URL,
            "checkout_final_kind": "stripe_pix_instructions",
            "checkout_payment_method": "pix",
            "checkout_amount_minor": 50,
            "checkout_amount": 0.5,
            "checkout_is_free_trial": False,
            "checkout_session_id": "cs_pix_123",
            "checkout_processor_entity": "openai_ie",
            "chatgpt_checkout_url": "https://chatgpt.com/checkout/openai_ie/cs_pix_123",
            "checkout_channel": "pix",
            "country": "BR",
            "currency": "BRL",
            "checkout_provider": "pix_br",
            "checkout_hosted_instructions_url": PIX_FINAL_URL,
            "checkout_billing_name": "Gabriel Almeida",
            "checkout_billing_email": "member@example.test",
            "checkout_billing_address": billing_address,
            "checkout_billing_tax_id": "060.543.916-05",
            "checkout_proxy_used": True,
            "checkout_promotion_proxy_used": True,
            "checkout_provider_proxy_used": True,
        }
        updated = {"access_token": "live-token", "checkout_link_status": "ready"}
        service = OpenAIFinalLinkService()
        with (
            patch("services.openai_final_link_service.account_service.resolve_access_token", return_value="live-token"),
            patch("services.openai_final_link_service.account_service.get_account", return_value=account),
            patch("services.openai_final_link_service.account_service.update_account", return_value=updated) as update_account,
            patch.object(service, "create_final_link", return_value=checkout),
        ):
            result = service.extract_and_store_final_link("live-token", checkout_channel="pix")

        self.assertIs(result["item"], updated)
        updates = update_account.call_args.args[1]
        self.assertEqual(updates["checkout_billing_name"], "Gabriel Almeida")
        self.assertEqual(updates["checkout_billing_email"], "member@example.test")
        self.assertEqual(updates["checkout_billing_address"], billing_address)
        self.assertEqual(updates["checkout_billing_tax_id"], "060.543.916-05")

    def test_extract_and_store_persists_upi_result(self) -> None:
        account = {"access_token": "live-token", "checkout_attempt_count": 2}
        checkout = {
            "checkout_url": UPI_FINAL_URL,
            "checkout_final_url": UPI_FINAL_URL,
            "checkout_final_kind": "upi_instructions",
            "checkout_payment_method": "upi",
            "checkout_amount_minor": 0,
            "checkout_amount": 0,
            "checkout_is_free_trial": True,
            "checkout_session_id": "cs_test_123",
            "checkout_processor_entity": "openai_llc",
            "chatgpt_checkout_url": "https://chatgpt.com/checkout/openai_llc/cs_test_123",
            "checkout_provider": "upi_open_source",
            "checkout_billing_name": "Aisha Sharma",
            "checkout_billing_email": "member@example.test",
            "checkout_billing_address": {
                "country": "IN",
                "line1": "24 Park Street",
                "line2": "",
                "city": "Kolkata",
                "state": "WB",
                "postal_code": "700016",
            },
            "checkout_proxy_used": True,
            "checkout_promotion_proxy_used": True,
            "checkout_provider_proxy_used": True,
        }
        updated = {"access_token": "live-token", "checkout_link_status": "ready"}
        service = OpenAIFinalLinkService()
        with (
            patch("services.openai_final_link_service.account_service.resolve_access_token", return_value="live-token"),
            patch("services.openai_final_link_service.account_service.get_account", return_value=account),
            patch("services.openai_final_link_service.account_service.update_account", return_value=updated) as update_account,
            patch.object(service, "create_final_link", return_value=checkout),
        ):
            result = service.extract_and_store_final_link("live-token")

        self.assertIs(result["item"], updated)
        updates = update_account.call_args.args[1]
        self.assertEqual(updates["checkout_channel"], "upi")
        self.assertEqual(updates["checkout_country"], "IN")
        self.assertEqual(updates["checkout_currency"], "INR")
        self.assertEqual(updates["checkout_final_url"], UPI_FINAL_URL)
        self.assertEqual(updates["checkout_billing_name"], "Aisha Sharma")
        self.assertEqual(updates["checkout_billing_email"], "member@example.test")
        self.assertEqual(updates["checkout_billing_address"]["postal_code"], "700016")


if __name__ == "__main__":
    unittest.main()
