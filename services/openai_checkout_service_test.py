from __future__ import annotations

import unittest
from unittest.mock import patch

from services.openai_checkout_service import (
    CheckoutSessionError,
    OpenAICheckoutService,
    _normalise_checkout_proxy_list,
)


class OpenAICheckoutServiceTest(unittest.TestCase):
    def test_normalises_multiline_proxy_input(self) -> None:
        values = (
            "first.example.test:8000:checkout-user:checkout-password\n"
            "checkout-user:checkout-password@second.example.test:8001"
        )
        self.assertEqual(
            _normalise_checkout_proxy_list(values),
            [
                "http://checkout-user:checkout-password@first.example.test:8000",
                "http://checkout-user:checkout-password@second.example.test:8001",
            ],
        )

    def test_reports_invalid_proxy_row(self) -> None:
        with self.assertRaisesRegex(CheckoutSessionError, "第 2 行"):
            _normalise_checkout_proxy_list("http://valid.example.test:8000\nnot a proxy")

    def test_delegates_to_upi_final_link_service(self) -> None:
        expected = {"checkout_final_url": "https://payments.stripe.com/upi/instructions/upi_test", "item": {}}
        progress = lambda message: None
        with patch(
            "services.openai_final_link_service.openai_final_link_service.extract_and_store_final_link",
            return_value=expected,
        ) as extract:
            result = OpenAICheckoutService().extract_and_store_checkout_link(
                "live-token",
                checkout_proxy="http://checkout.example.test:8000",
                promotion_proxy="http://promotion.example.test:8001",
                provider_proxy="http://provider.example.test:8002",
                proxy_rotation=7,
                progress=progress,
            )

        self.assertEqual(result, expected)
        extract.assert_called_once_with(
            "live-token",
            checkout_channel="upi",
            pix_protocol="enhanced",
            checkout_proxy="http://checkout.example.test:8000",
            promotion_proxy="http://promotion.example.test:8001",
            provider_proxy="http://provider.example.test:8002",
            proxy_rotation=7,
            progress=progress,
        )


if __name__ == "__main__":
    unittest.main()
