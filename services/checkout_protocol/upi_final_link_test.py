from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import uuid
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from services.checkout_protocol import stripe_token, upi
from services.checkout_protocol.upi import generate_upi_final_link, is_upi_instructions_url


_CHECKOUT_PROXY = "http://checkout-user:checkout-password@checkout.example.test:8080"
_PROMOTION_PROXY = "http://promotion-user:promotion-password@promotion.example.test:8081"
_PROVIDER_PROXY = "socks5://provider-user:provider-password@provider.example.test:8082"
_FINAL_URL = "https://payments.stripe.com/upi/instructions/upi_123456?client_secret=pi_secret"


class UPIFinalLinkBridgeTest(unittest.TestCase):
    def test_stripe_browser_id_matches_stripe_checkout_shape(self) -> None:
        value = upi.stripe_browser_id()

        self.assertEqual(len(value), 44)
        self.assertEqual(str(uuid.UUID(value[:36])), value[:36])
        self.assertRegex(value[36:], r"^[0-9a-f]{8}$")

    def test_stripe_token_matches_reverse_engineered_reference(self) -> None:
        value = stripe_token.compute_js_checksum("test_ppage_id_abc", shift=11)

        self.assertEqual(value, "qto~d^n0=QU>QroyQlocavdxMlmRQleRoxU>rw")

    def test_elements_session_uses_real_stripe_response_ids(self) -> None:
        stripe = MagicMock()
        response = stripe.get.return_value
        response.status_code = 200
        response.json.return_value = {
            "session_id": "elements_session_real",
            "config_id": "elements_config_real",
        }
        ctx = {
            "stripe_js_id": "stripe-js-id",
            "checkout_amount": 0,
            "currency": "inr",
            "locale": "en",
            "stripe_version": "stripe-version",
        }

        payload = upi.stripe_elements_session(
            stripe, "cs_test_123", "pk_test_123", ctx
        )

        self.assertEqual(payload["session_id"], "elements_session_real")
        self.assertEqual(ctx["elements_session_id"], "elements_session_real")
        self.assertEqual(ctx["elements_session_config_id"], "elements_config_real")
        params = stripe.get.call_args.kwargs["params"]
        self.assertEqual(params["checkout_session_id"], "cs_test_123")
        self.assertEqual(params["deferred_intent[payment_method_types][1]"], "upi")
        self.assertNotIn("deferred_intent[payment_method_types][2]", params)

    def test_stripe_confirm_uses_source_inline_protocol_and_tokens(self) -> None:
        stripe = MagicMock()
        response = stripe.post.return_value
        response.status_code = 200
        response.json.return_value = {}
        ctx = {
            "checkout_amount": 0,
            "guid": "g" * 44,
            "muid": "m" * 44,
            "sid": "s" * 44,
            "stripe_js_id": "stripe-js-id",
            "config_id": "checkout-config-id",
            "elements_session_id": "elements-session-id",
            "elements_session_config_id": "elements-config-id",
            "payment_element_config_id": "payment-element-config-id",
            "locale": "en",
            "init_checksum": "init-checksum",
            "runtime_version": "runtime-version",
            "stripe_version": "stripe-version",
        }
        billing = {
            "name": "Aisha Sharma",
            "email": "member@example.test",
            "country": "IN",
            "line1": "24 Park Street",
            "line2": "Taltala",
            "city": "Kolkata",
            "postal_code": "700016",
            "state": "West Bengal",
        }
        token_config = stripe_token.StripeTokenConfig(
            bundle_hash="bundle-hash",
            shift=11,
            rv_ts="2024-01-01 00:00:00 -0000",
            rv="runtime-version-full",
            sv="build-salt",
        )

        with patch.dict(os.environ, {"UPI_CONFIRM_INLINE_PM": "1"}):
            upi.stripe_confirm_upi(
                stripe,
                "cs_test_123",
                "",
                "pk_test_123",
                {"id": "ppage_test_123", "init_checksum": "init-checksum"},
                ctx,
                {"billing_country": "IN", "processor_entity": "openai_llc"},
                "https://checkout.stripe.com/c/pay/cs_test_123#fid-test",
                billing,
                token_config=token_config,
                variant="flow_qr",
            )

        body = stripe.post.call_args.kwargs["data"]
        self.assertNotIn("payment_method", body)
        self.assertEqual(body["payment_method_data[type]"], "upi")
        self.assertEqual(body["payment_method_data[upi][flow]"], "qr_code")
        self.assertEqual(body["elements_session_client[session_id]"], "elements-session-id")
        self.assertEqual(body["client_attribution_metadata[client_session_id]"], "stripe-js-id")
        return_url = urlsplit(body["return_url"])
        self.assertEqual(return_url.fragment, "fid-test")
        return_params = parse_qs(return_url.query)
        self.assertEqual(return_params["returned_from_redirect"], ["true"])
        self.assertEqual(return_params["ui_mode"], ["custom"])
        self.assertEqual(
            return_params["return_url"],
            [
                "https://chatgpt.com/checkout/verify?"
                "stripe_session_id=cs_test_123&processor_entity=openai_llc&plan_type=plus"
            ],
        )
        self.assertEqual(
            body["payment_method_data[billing_details][address][line2]"],
            "Taltala",
        )
        self.assertEqual(
            body["payment_method_data[client_attribution_metadata][checkout_config_id]"],
            "payment-element-config-id",
        )
        self.assertEqual(
            body["payment_method_data[client_attribution_metadata][payment_method_selection_flow]"],
            "automatic",
        )
        self.assertEqual(
            body["client_attribution_metadata[payment_method_selection_flow]"],
            "automatic",
        )
        self.assertEqual(body["version"], "runtime-version")
        self.assertTrue(body["js_checksum"])
        self.assertTrue(body["rv_timestamp"])

    def test_browser_billing_state_is_sent_to_stripe_and_chatgpt(self) -> None:
        billing = {
            "name": "Aarav Sharma",
            "email": "member@example.test",
            "country": "IN",
            "line1": "12 Mahatma Gandhi Road",
            "line2": "Sindhu Wadi, Ghatkopar East",
            "city": "Mumbai",
            "postal_code": "400077",
            "state": "Maharashtra",
        }
        ctx = {
            "checkout_amount": 0,
            "stripe_js_id": "stripe-js-id",
            "elements_session_id": "elements-session-id",
            "locale": "en",
        }
        stripe = MagicMock()
        stripe.post.return_value.status_code = 200
        stripe.post.return_value.text = "{}"

        self.assertTrue(
            upi.stripe_update_tax_region(
                stripe, "cs_test_123", "pk_test_123", ctx, billing
            )
        )
        tax_body = stripe.post.call_args.kwargs["data"]
        self.assertEqual(tax_body["tax_region[line2]"], billing["line2"])
        self.assertEqual(tax_body["tax_region[state]"], billing["state"])
        self.assertEqual(
            tax_body[
                "client_attribution_metadata[merchant_integration_additional_elements][2]"
            ],
            "address",
        )

        chatgpt = MagicMock()
        chatgpt.post.return_value.status_code = 204
        chatgpt.post.return_value.text = ""
        upi.checkout_snapshot(
            chatgpt,
            {
                "cs_id": "cs_test_123",
                "billing_country": "IN",
                "processor_entity": "openai_llc",
            },
            billing,
        )
        snapshot = chatgpt.post.call_args.kwargs["json"]["snapshot"]
        self.assertEqual(snapshot["billing_address"]["address"]["line2"], billing["line2"])
        self.assertEqual(snapshot["billing_address"]["address"]["state"], billing["state"])

    def test_protocol_uses_account_email_for_upi_billing(self) -> None:
        with (
            patch.object(upi, "build_chatgpt_session", return_value=MagicMock()),
            patch.object(
                upi,
                "create_checkout",
                return_value={
                    "cs_id": "cs_test_123",
                    "stripe_pk": "pk_test_123",
                    "currency": "INR",
                },
            ),
            patch.object(
                upi,
                "run_provider_flow",
                return_value=(_FINAL_URL, []),
            ) as provider_flow,
        ):
            result = upi.generate_upi_link_explicit(
                "access-token-value",
                _CHECKOUT_PROXY,
                _PROMOTION_PROXY,
                _PROVIDER_PROXY,
                account="member@example.test",
                session_token="session-token-value",
            )

        self.assertTrue(result["ok"])
        billing = provider_flow.call_args.args[8]
        self.assertEqual(billing["email"], "member@example.test")
        self.assertEqual(result["checkout_billing_name"], billing["name"])
        self.assertEqual(result["checkout_billing_email"], "member@example.test")
        self.assertEqual(
            result["checkout_billing_address"],
            {
                "country": billing["country"],
                "line1": billing["line1"],
                "line2": billing["line2"],
                "city": billing["city"],
                "state": billing["state"],
                "postal_code": billing["postal_code"],
            },
        )

    def test_failed_protocol_does_not_expose_upi_billing_snapshot(self) -> None:
        with (
            patch.object(upi, "build_chatgpt_session", return_value=MagicMock()),
            patch.object(
                upi,
                "create_checkout",
                return_value={"cs_id": "cs_test_123", "stripe_pk": "pk_test", "currency": "INR"},
            ),
            patch.object(upi, "run_provider_flow", side_effect=RuntimeError("approve blocked")),
        ):
            result = upi.generate_upi_link_explicit(
                "access-token-value",
                _CHECKOUT_PROXY,
                _PROMOTION_PROXY,
                _PROVIDER_PROXY,
                account="member@example.test",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "approve_blocked")
        self.assertFalse(any(key.startswith("checkout_billing_") for key in result))

    def _run_provider_flow_with_methods(
        self,
        provider_methods: list[str],
        progress_messages: list[str] | None = None,
    ) -> tuple[str, list[str]]:
        stripe_session = MagicMock()
        stripe_session.headers = {}
        checkout = {
            "cs_id": "cs_test_123",
            "stripe_pk": "pk_test_123",
            "currency": "INR",
            "processor_entity": "openai_llc",
        }
        billing = {
            "country": "IN",
            "name": "Aisha Sharma",
            "line1": "24 Park Street",
            "city": "Kolkata",
            "postal_code": "700016",
            "email": "member@example.test",
        }
        with (
            patch.object(
                upi,
                "stripe_init",
                side_effect=[
                    {"payment_method_types": ["card", "link"]},
                    {"payment_method_types": provider_methods},
                ],
            ),
            patch.object(upi, "build_ctx", return_value={"checkout_amount": 0}),
            patch.object(upi, "build_chatgpt_session", return_value=MagicMock()),
            patch.object(upi, "proxy_for_country", side_effect=lambda proxy, _country: proxy),
            patch.object(upi, "update_checkout_promotion") as update_promotion,
            patch.object(upi, "record_proxy_result"),
            patch.object(upi, "record_checkout_zero_result"),
            patch.object(upi, "new_session", return_value=stripe_session),
            patch.object(upi, "checkout_snapshot"),
            patch.object(upi, "stripe_update_tax_region", return_value=True),
            patch.object(
                upi,
                "stripe_elements_session",
                return_value={"session_id": "elements-session-id"},
            ),
            patch.object(
                upi.stripe_token,
                "extract_config_live",
                return_value=stripe_token.StripeTokenConfig(
                    "bundle-hash", 11, "timestamp", "runtime123-full", "salt"
                ),
            ),
            patch.object(
                upi,
                "stripe_confirm_upi_variants",
                return_value=({}, "qr_code"),
            ),
            patch.object(
                upi,
                "resolve_confirm_payload_upi",
                return_value=(_FINAL_URL, [], ""),
            ),
        ):
            result = upi.run_provider_flow(
                "access-token-value",
                "session-token-value",
                _CHECKOUT_PROXY,
                _PROMOTION_PROXY,
                _CHECKOUT_PROXY,
                [_CHECKOUT_PROXY],
                "device-test-123",
                checkout,
                billing,
                progress=progress_messages.append if progress_messages is not None else None,
            )
        update_promotion.assert_called_once()
        return result

    def test_bootstrap_without_upi_continues_after_promotion(self) -> None:
        redirect_url, qr_urls = self._run_provider_flow_with_methods(["card", "link", "upi"])

        self.assertEqual(redirect_url, _FINAL_URL)
        self.assertEqual(qr_urls, [])

    def test_provider_flow_reports_detailed_milestones(self) -> None:
        messages: list[str] = []

        self._run_provider_flow_with_methods(["card", "link", "upi"], messages)

        stages = [message.split("]", 1)[0].lstrip("[") for message in messages]
        expected = [
            "stripe_bootstrap",
            "promotion",
            "stripe_provider",
            "stripe_elements",
            "billing",
            "stripe_token",
            "payment_method",
            "confirm",
            "extract",
        ]
        positions = [stages.index(stage) for stage in expected]
        self.assertEqual(positions, sorted(positions))

    def test_provider_still_requires_upi_after_promotion(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "当前账号支付方式不支持 UPI"):
            self._run_provider_flow_with_methods(["card", "link"])

    def test_generic_decline_after_approve_retries_confirm(self) -> None:
        self.assertTrue(
            upi.should_retry_second_confirm_after_approve(
                "Stripe 风控拒绝（generic_decline）：stripe payment_pages SetupIntent 创建失败"
            )
        )

    def test_setup_intent_error_summary_matches_current_payment_method(self) -> None:
        payload = {
            "setup_intent": {
                "object": "setup_intent",
                "status": "requires_payment_method",
                "last_setup_error": {
                    "type": "card_error",
                    "code": "setup_intent_payment_attempt_failed",
                    "decline_code": "generic_decline",
                    "payment_method": {"id": "pm_current", "type": "upi"},
                },
            }
        }

        summary = upi.setup_intent_error_summary(payload, current_pm_id="pm_current")

        self.assertEqual(summary["setup_intent_status"], "requires_payment_method")
        self.assertEqual(summary["decline_code"], "generic_decline")
        self.assertEqual(summary["payment_method_type"], "upi")
        self.assertTrue(summary["payment_method_matches_current"])

    def test_qr_candidate_summary_does_not_include_url_path_or_query(self) -> None:
        summary = upi.qr_candidate_summary(
            "https://example.test/private/qr/token-value?client_secret=secret&qr=1"
        )

        self.assertEqual(summary["host"], "example.test")
        self.assertTrue(summary["path_has_qr"])
        self.assertTrue(summary["query_has_qr"])
        self.assertNotIn("path", summary)
        self.assertNotIn("query", summary)

    def test_generic_decline_second_confirm_refreshes_elements_session(self) -> None:
        stripe_session = MagicMock()
        stripe_session.headers = {}
        checkout = {
            "cs_id": "cs_test_123",
            "stripe_pk": "pk_test_123",
            "currency": "INR",
            "processor_entity": "openai_llc",
        }
        billing = {
            "country": "IN",
            "name": "Aisha Sharma",
            "line1": "24 Park Street",
            "city": "Kolkata",
            "postal_code": "700016",
            "email": "member@example.test",
        }
        init_payload = {"payment_method_types": ["card", "upi"]}
        with (
            patch.object(upi, "stripe_init", side_effect=[init_payload, init_payload, init_payload]),
            patch.object(upi, "build_ctx", return_value={"checkout_amount": 0}),
            patch.object(upi, "build_chatgpt_session", return_value=MagicMock()),
            patch.object(upi, "proxy_for_country", side_effect=lambda proxy, _country: proxy),
            patch.object(upi, "update_checkout_promotion"),
            patch.object(upi, "record_proxy_result"),
            patch.object(upi, "record_checkout_zero_result"),
            patch.object(upi, "new_session", return_value=stripe_session),
            patch.object(upi, "checkout_snapshot"),
            patch.object(upi, "stripe_update_tax_region", return_value=True),
            patch.object(
                upi,
                "stripe_elements_session",
                side_effect=[
                    {"session_id": "elements-initial"},
                    {"session_id": "elements-retry"},
                ],
            ) as elements_session,
            patch.object(
                upi.stripe_token,
                "extract_config_live",
                return_value=stripe_token.StripeTokenConfig(
                    "bundle-hash", 11, "timestamp", "runtime123-full", "salt"
                ),
            ),
            patch.object(
                upi,
                "stripe_confirm_upi_variants",
                side_effect=[({}, "qr_code"), ({}, "flow_qr")],
            ) as confirm,
            patch.object(
                upi,
                "resolve_confirm_payload_upi",
                side_effect=[
                    RuntimeError(
                        "Stripe 风控拒绝（generic_decline）：stripe payment_pages SetupIntent 创建失败"
                    ),
                    (_FINAL_URL, [], ""),
                ],
            ),
        ):
            redirect_url, qr_urls = upi.run_provider_flow(
                "access-token-value",
                "session-token-value",
                _CHECKOUT_PROXY,
                _PROMOTION_PROXY,
                _CHECKOUT_PROXY,
                [_CHECKOUT_PROXY],
                "device-test-123",
                checkout,
                billing,
            )

        self.assertEqual(redirect_url, _FINAL_URL)
        self.assertEqual(qr_urls, [])
        self.assertEqual(elements_session.call_count, 2)
        self.assertEqual(confirm.call_count, 2)
        self.assertEqual(confirm.call_args_list[1].args[2], "")

    def test_kookeey_password_country_segment_is_rewritten(self) -> None:
        proxy = (
            "http://account:secret-IN-session-12345678-life-5m"
            "@gate.kookeey.info:1000"
        )

        rewritten = upi.proxy_for_country(proxy, "VN")

        self.assertIn("secret-VN-session-12345678-life-5m", rewritten)
        self.assertEqual(upi.proxy_chain_key(proxy), upi.proxy_chain_key(rewritten))

    def test_smartproxy_area_selector_preserves_lifetime_and_session(self) -> None:
        proxy = (
            "socks5h://account_area-BR_life-120_session-sessionvalue:secret"
            "@proxy.example.test:1000"
        )

        rewritten = upi.proxy_for_country(proxy, "VN")

        self.assertIn("account_area-VN_life-120_session-sessionvalue", rewritten)
        self.assertEqual(upi.proxy_chain_key(proxy), upi.proxy_chain_key(rewritten))

    def test_kookeey_compact_password_country_segment_is_rewritten(self) -> None:
        proxy = "socks5h://account:secret-NL-77297054@gate.kookeey.info:1000"

        rewritten = {
            country: upi.proxy_for_country(proxy, country)
            for country in ("BR", "VN", "US")
        }

        for country, candidate in rewritten.items():
            self.assertIn(f"secret-{country}-77297054", candidate)
            self.assertEqual(upi.proxy_chain_key(proxy), upi.proxy_chain_key(candidate))

    def test_kookeey_session_password_without_lifetime_is_rewritten(self) -> None:
        proxy = "socks5h://account:secret-NL-session-77297054@gate.kookeey.info:1000"

        rewritten = upi.proxy_for_country(proxy, "BR")

        self.assertIn("secret-BR-session-77297054", rewritten)
        self.assertEqual(upi.proxy_chain_key(proxy), upi.proxy_chain_key(rewritten))

    def test_kookeey_shaped_password_is_not_rewritten_on_other_hosts(self) -> None:
        proxy = (
            "http://account:secret-IN-session-12345678-life-5m"
            "@proxy.example.test:1000"
        )

        with self.assertRaisesRegex(RuntimeError, "Kookeey"):
            upi.proxy_for_country(proxy, "VN")

    def test_accepts_only_a_final_upi_instructions_url(self) -> None:
        self.assertTrue(is_upi_instructions_url(_FINAL_URL))
        self.assertFalse(is_upi_instructions_url("http://payments.stripe.com/upi/instructions/upi_123"))
        self.assertFalse(is_upi_instructions_url("https://hooks.stripe.com/redirect/upi_123"))
        self.assertFalse(is_upi_instructions_url("https://payments.stripe.com/upi/instructions/"))
        self.assertFalse(is_upi_instructions_url("https://payments.stripe.com@invalid.test/upi/instructions/upi_123"))

    @patch("services.checkout_protocol.upi.generate_upi_link_explicit")
    def test_returns_only_safe_metadata_from_the_protocol(self, generate: object) -> None:
        generate.return_value = {
            "ok": True,
            "upi_url": _FINAL_URL,
            "checkout_session_id": "cs_test_123",
            "processor_entity": "openai_ie",
            "chatgpt_checkout_url": "https://chatgpt.com/checkout/openai_ie/cs_test_123",
            "pay_amount_minor": 0,
            "pay_amount": 0.0,
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
            "session_token": "must-not-leak",
            "stripe_publishable_key": "pk_live_must-not-leak",
            "qr_urls": ["data:image/png;base64,must-not-leak"],
        }

        result = generate_upi_final_link(
            "access-token-value",
            checkout_proxy=_CHECKOUT_PROXY,
            promotion_proxy=_PROMOTION_PROXY,
            provider_proxy=_PROVIDER_PROXY,
            account={"email": "member@example.test", "session_token": "account-session-token"},
            plan="plus",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["checkout_final_url"], _FINAL_URL)
        self.assertEqual(result["checkout_session_id"], "cs_test_123")
        self.assertEqual(result["chatgpt_checkout_url"], "https://chatgpt.com/checkout/openai_ie/cs_test_123")
        self.assertEqual(result["checkout_billing_name"], "Aisha Sharma")
        self.assertEqual(result["checkout_billing_email"], "member@example.test")
        self.assertEqual(result["checkout_billing_address"]["postal_code"], "700016")
        self.assertNotIn("session_token", result)
        self.assertNotIn("stripe_publishable_key", result)
        self.assertNotIn("qr_urls", result)
        generate.assert_called_once_with(
            "access-token-value",
            _CHECKOUT_PROXY,
            _PROMOTION_PROXY,
            "socks5://provider-user:provider-password@provider.example.test:8082",
            account="member@example.test",
            session_token="account-session-token",
            plan="chatgptplusplan",
            progress=None,
        )

    @patch("services.checkout_protocol.upi.generate_upi_link_explicit")
    def test_rejects_an_intermediate_redirect(self, generate: object) -> None:
        generate.return_value = {
            "ok": True,
            "upi_url": "https://hooks.stripe.com/redirect/upi_123",
            "checkout_session_id": "cs_test_123",
        }

        result = generate_upi_final_link(
            "access-token-value",
            checkout_proxy=_CHECKOUT_PROXY,
            promotion_proxy=_PROMOTION_PROXY,
            provider_proxy=_PROVIDER_PROXY,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "upi_final_url_invalid")

    @patch("services.checkout_protocol.upi.generate_upi_link_explicit")
    def test_redacts_protocol_error_secrets(self, generate: object) -> None:
        generate.return_value = {
            "ok": False,
            "error_code": "proxy_failed",
            "error": "proxy failed at http://checkout-user:checkout-password@checkout.example.test:8080 Bearer access-token-value",
        }

        result = generate_upi_final_link(
            "access-token-value",
            checkout_proxy=_CHECKOUT_PROXY,
            promotion_proxy=_PROMOTION_PROXY,
            provider_proxy=_PROVIDER_PROXY,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "proxy_failed")
        self.assertNotIn("access-token-value", result["error"])
        self.assertNotIn("checkout-password", result["error"])

    @patch("services.checkout_protocol.upi.generate_upi_link_explicit")
    def test_rejects_missing_required_proxy_without_calling_protocol(self, generate: object) -> None:
        result = generate_upi_final_link(
            "access-token-value",
            checkout_proxy=_CHECKOUT_PROXY,
            promotion_proxy="",
            provider_proxy=_PROVIDER_PROXY,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "proxy_empty")
        generate.assert_not_called()

    def test_force_dump_is_still_disabled_without_upi_dump_opt_in(self) -> None:
        response = type(
            "Response",
            (),
            {
                "status_code": 200,
                "url": "https://api.stripe.com/v1/payment_pages/cs_test_123",
                "text": "client_secret=pi_secret__must_not_persist",
            },
        )()
        with TemporaryDirectory() as directory:
            with (
                patch.dict(os.environ, {"UPI_DUMP": "0"}, clear=False),
                patch.object(upi, "DUMP_DIR", Path(directory)),
            ):
                upi.dump_http(response, "test", {"client_secret": "pi_secret__must_not_persist"}, force=True)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_diagnostic_redaction_removes_cookie_and_client_secret(self) -> None:
        redacted = upi._redact_text(
            '{"cookie":"session-token-value","client_secret":"pi_secret_value",'
            '"url":"https://payments.stripe.com/upi/instructions/x?client_secret=pi_secret_query"}'
        )
        self.assertNotIn("session-token-value", redacted)
        self.assertNotIn("pi_secret_value", redacted)
        self.assertNotIn("pi_secret_query", redacted)
