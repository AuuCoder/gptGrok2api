from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import requests

from services.checkout_protocol import pix, stripe_token


class FakeResponse:
    def __init__(self, payload: dict, *, status_code: int = 200, url: str = "https://api.stripe.com") -> None:
        self._payload = payload
        self.status_code = status_code
        self.url = url
        self.text = json.dumps(payload)
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._payload


class RecordingSession:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}

    def post(
        self,
        url: str,
        *,
        data: dict | None = None,
        json: dict | None = None,
        **_kwargs,
    ) -> FakeResponse:
        self.posts.append((url, dict(data if data is not None else json or {})))
        return FakeResponse(self.responses.pop(0), url=url)

    def get(self, url: str, *, params: dict | None = None, **_kwargs) -> FakeResponse:
        self.gets.append((url, dict(params or {})))
        return FakeResponse(self.responses.pop(0), url=url)


class PixProtocolReferenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.checkout = {
            "cs_id": "cs_pix_test",
            "processor_entity": "openai_ie",
            "billing_country": "BR",
            "currency": "BRL",
        }
        self.init_payload = {
            "id": "ppage_pix_test",
            "config_id": "cfg_pix",
            "init_checksum": "chk_pix",
            "currency": "brl",
            "total_summary": {"due": 0},
            "client_stripe_js_id": "stripe-js-pix",
            "_client_context": {"stripe_js_id": "stripe-js-pix"},
            "link_settings": {"link_brand": "link"},
            "stripe_hosted_url": (
                "https://checkout.stripe.com/c/pay/cs_pix_test"
                "?prefilled_email=test%40example.com#fidkdWxOYHwnPyd1blpxYHZxWjA0"
            ),
        }
        self.ctx = pix.build_ctx(self.init_payload, self.checkout)
        self.billing = {
            "name": "Gabriel Almeida",
            "email": "gabriel@example.com",
            "phone": "+5511998765432",
            "country": "BR",
            "line1": "Avenida Paulista 1000",
            "line2": "",
            "city": "Sao Paulo",
            "state": "SP",
            "postal_code": "01310-100",
            "tax_id": "06054391605",
        }

    def test_runtime_country_and_amount_policy_match_reference(self) -> None:
        self.assertEqual(pix.DEFAULT_STRIPE_RUNTIME_VERSION, "299e1ea907")
        self.assertEqual(pix.PIX_BOOTSTRAP_COUNTRY, "BR")
        self.assertEqual(pix.PIX_PROMOTION_COUNTRIES, ["BR"])
        self.assertEqual(pix.PIX_PROVIDER_COUNTRY, "BR")
        self.assertTrue(pix.pix_amount_is_eligible(0))
        self.assertTrue(pix.pix_amount_is_eligible(50))
        self.assertFalse(pix.pix_amount_is_eligible(51))

    def test_normalize_proxy_url_accepts_host_port_user_password(self) -> None:
        compact = "us.lajiaohttp.net:2000:test-user-region-BR:test-pass"

        self.assertEqual(
            pix.normalize_proxy_url(compact),
            "http://test-user-region-BR:test-pass@us.lajiaohttp.net:2000",
        )

    def test_normalize_proxy_url_keeps_configured_default_scheme(self) -> None:
        compact = "us.lajiaohttp.net:2000:test-user-region-BR:test-pass"

        with patch.dict(os.environ, {"PIX_PROXY_DEFAULT_SCHEME": "socks5h"}):
            self.assertEqual(
                pix.normalize_proxy_url(compact),
                "socks5h://test-user-region-BR:test-pass@us.lajiaohttp.net:2000",
            )

    def test_smartproxy_area_selector_preserves_lifetime_and_session(self) -> None:
        proxy = (
            "socks5h://account_area-BR_life-120_session-sessionvalue:secret"
            "@proxy.example.test:1000"
        )

        rewritten = pix.proxy_for_country(proxy, "VN")

        self.assertIn("account_area-VN_life-120_session-sessionvalue", rewritten)
        self.assertEqual(pix.proxy_chain_key(proxy), pix.proxy_chain_key(rewritten))

    def test_checkout_retries_transient_proxy_tunnel_abort_locally(self) -> None:
        response = FakeResponse({"checkout_session_id": "cs_retry"})
        session = MagicMock()
        session.post.side_effect = [
            RuntimeError("Failed to perform, curl: (56) Proxy CONNECT aborted"),
            response,
        ]

        with patch.object(pix.time, "sleep") as sleep:
            resolved = pix.request_with_proxy_connect_retry(
                session,
                "post",
                "https://chatgpt.com/backend-api/payments/checkout",
                stage="Checkout bootstrap",
                attempts=3,
            )

        self.assertIs(resolved, response)
        self.assertEqual(session.post.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_checkout_does_not_retry_non_proxy_request_error(self) -> None:
        session = MagicMock()
        session.post.side_effect = RuntimeError("response parse failed")

        with self.assertRaisesRegex(RuntimeError, "response parse failed"):
            pix.request_with_proxy_connect_retry(
                session,
                "post",
                "https://chatgpt.com/backend-api/payments/checkout",
                stage="Checkout bootstrap",
                attempts=3,
            )

        session.post.assert_called_once()

    def test_pix_confirm_mode_supports_reference_and_legacy_switches(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pix.pix_confirm_mode(), "inline")
        with patch.dict(os.environ, {"PIX_CONFIRM_MODE": "explicit"}, clear=True):
            self.assertEqual(pix.pix_confirm_mode(), "explicit")
        with patch.dict(os.environ, {"PIX_CONFIRM_MODE": "auto"}, clear=True):
            self.assertEqual(pix.pix_confirm_mode(), "auto")
        with patch.dict(os.environ, {"PIX_CONFIRM_INLINE_PM": "0"}, clear=True):
            self.assertEqual(pix.pix_confirm_mode(), "explicit")
        with patch.dict(os.environ, {"PIX_CONFIRM_MODE": "invalid"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "inline, explicit, auto"):
                pix.pix_confirm_mode()

    def test_pix_protocol_mode_is_explicit_per_task(self) -> None:
        self.assertEqual(pix.pix_protocol_mode(), "enhanced")
        self.assertEqual(pix.pix_protocol_mode("reference"), "reference")
        self.assertEqual(pix.pix_protocol_mode("standalone"), "standalone")
        with self.assertRaisesRegex(RuntimeError, "enhanced、reference 或 standalone"):
            pix.pix_protocol_mode("unknown")

    def test_scheme3_adapter_maps_vendored_result(self) -> None:
        final_url = "https://payments.stripe.com/qr/instructions/pix_scheme3"
        raw = {
            "cs_id": "cs_scheme3",
            "processor_entity": "openai_ie",
            "payment_method_id": "pm_scheme3",
            "pix_hosted_instructions_url": final_url,
            "pix_qr_code": "00020101021226890014br.gov.bcb.pix",
            "pix_qr_image_url_png": "https://qr.stripe.com/pix_scheme3.png",
            "pix_expires_at": 1_900_000_000,
            "stripe_amount": "0",
        }
        with patch.object(
            pix.pix_scheme3,
            "generate_opll_pix_long_link",
            return_value=raw,
        ) as generate:
            result = pix.generate_pix_final_link(
                {},
                "access-token",
                checkout_proxy="http://br.example.test:8000",
                promotion_proxy="http://vn.example.test:8001",
                provider_proxy="http://br.example.test:8000",
                protocol_mode="standalone",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "pix_br_vn_standalone")
        self.assertEqual(result["checkout_final_url"], final_url)
        self.assertEqual(result["pix_qr_code"], raw["pix_qr_code"])
        self.assertEqual(result["pay_amount_minor"], 0)
        generate.assert_called_once_with(
            "access-token",
            country="BR",
            currency="BRL",
            proxy_url="http://br.example.test:8000",
            promotion_proxy_url="http://vn.example.test:8001",
            log_cb=generate.call_args.kwargs["log_cb"],
        )

    def test_context_keeps_checkout_and_elements_config_sources_separate(self) -> None:
        self.assertEqual(self.ctx["stripe_js_id"], "stripe-js-pix")
        self.assertEqual(self.ctx["elements_session_id"], "")
        self.assertEqual(self.ctx["elements_session_config_id"], "")
        self.assertEqual(self.ctx["payment_element_config_id"], "cfg_pix")
        self.assertNotEqual(self.ctx["checkout_config_id"], "cfg_pix")
        self.assertEqual(self.ctx["config_id"], "cfg_pix")

    def test_chatgpt_session_is_reused_only_on_the_same_proxy(self) -> None:
        shared = object()
        proxy = "http://br-sticky.example:8000"
        with patch.object(pix, "build_chatgpt_session", return_value=object()) as build:
            reused = pix.chatgpt_session_for_proxy(
                shared,
                proxy,
                proxy,
                "token",
                "device",
            )
            rebuilt = pix.chatgpt_session_for_proxy(
                shared,
                proxy,
                "http://br-other.example:8000",
                "token",
                "device",
            )

        self.assertIs(reused, shared)
        self.assertIsNot(rebuilt, shared)
        build.assert_called_once()

    def test_approve_candidates_keep_checkout_sticky_proxy(self) -> None:
        checkout_proxy = "http://user-session-12345678:pass@proxy.example:8000"
        provider_proxy = "http://user-session-87654321:pass@proxy.example:8000"

        self.assertEqual(
            pix.approve_proxy_candidates(checkout_proxy, provider_proxy, [provider_proxy]),
            [checkout_proxy],
        )

    def test_approve_retries_do_not_refresh_sticky_session(self) -> None:
        proxy = "http://user-session-12345678:pass@proxy.example:8000"
        initial_session = object()
        messages: list[str] = []
        with (
            patch.dict(os.environ, {"PIX_APPROVE_RETRY_MAX": "3"}),
            patch.object(
                pix,
                "approve_attempt",
                side_effect=RuntimeError("ChatGPT approve 未通过: blocked"),
            ) as approve_attempt,
            patch.object(pix.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "approve blocked"):
                pix.approve_with_retry(
                    "access-token",
                    "device-id",
                    self.checkout,
                    [proxy],
                    "session-token",
                    initial_session=initial_session,
                    initial_proxy=proxy,
                    progress=messages.append,
                )

        self.assertEqual([call.args[4] for call in approve_attempt.call_args_list], [proxy] * 3)
        self.assertTrue(all(call.args[7] is initial_session for call in approve_attempt.call_args_list))
        self.assertTrue(any("approve 1/3" in message for message in messages))
        self.assertTrue(any("approve 3/3" in message for message in messages))

    def test_reference_approve_reports_each_retry_and_backoff(self) -> None:
        messages: list[str] = []
        with (
            patch.dict(os.environ, {"PIX_REFERENCE_APPROVE_RETRY_MAX": "3"}),
            patch.object(
                pix,
                "reference_chatgpt_approve",
                side_effect=[RuntimeError("blocked"), RuntimeError("blocked"), None],
            ) as approve,
            patch.object(pix.time, "sleep") as sleep,
        ):
            pix.reference_approve_with_retry(
                MagicMock(),
                self.checkout,
                progress=messages.append,
            )

        self.assertEqual(approve.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        self.assertTrue(any("approve 1/3" in message for message in messages))
        self.assertTrue(any("approve 2/3" in message for message in messages))
        self.assertTrue(any("approve 3/3" in message for message in messages))

    def test_reference_poll_reports_first_attempt(self) -> None:
        hosted_url = "https://payments.stripe.com/qr/instructions/pix-reference"
        stripe = RecordingSession([{"hosted_instructions_url": hosted_url}])
        messages: list[str] = []

        with patch.object(pix, "dump_http"):
            details, _payload = pix.reference_poll_pix_result(
                stripe,
                self.checkout["cs_id"],
                "pk_test",
                self.ctx,
                progress=messages.append,
            )

        self.assertEqual(details["hosted_instructions_url"], hosted_url)
        self.assertTrue(any("第 1 次" in message for message in messages))

    def test_chatgpt_session_loads_saved_cookies_into_mutable_jar(self) -> None:
        session = requests.Session()
        with patch.object(pix, "new_session", return_value=session):
            result = pix.build_chatgpt_session(
                "access-token",
                "generated-device",
                "",
                "fallback-session",
                (
                    "oai-did=saved-device; "
                    "__Secure-next-auth.session-token=saved-session; "
                    "__cf_bm=cf-cookie; "
                    "_account=account-id"
                ),
            )

        self.assertIs(result, session)
        self.assertEqual(result.cookies.get("oai-did"), "saved-device")
        self.assertEqual(
            result.cookies.get("__Secure-next-auth.session-token"),
            "saved-session",
        )
        self.assertEqual(result.cookies.get("__cf_bm"), "cf-cookie")
        self.assertNotIn("Cookie", result.headers)
        self.assertEqual(result.headers["oai-device-id"], "saved-device")
        self.assertNotEqual(result.headers["oai-session-id"], "saved-device")
        self.assertEqual(result.headers["oai-client-version"], pix.CHATGPT_CLIENT_VERSION)
        self.assertEqual(result.headers["ChatGPT-Account-Id"], "account-id")

    def test_approve_warmup_builds_checkout_sentinel_cookie_before_ping(self) -> None:
        session = MagicMock()
        session.headers = {
            "User-Agent": "saved-user-agent",
            "sec-ch-ua": "saved-sec-ch-ua",
        }
        session.post.return_value.status_code = 204
        session.post.return_value.text = ""
        session.post.return_value.url = "https://chatgpt.com/backend-api/sentinel/ping"
        session.post.return_value.headers = {}
        fingerprint = {
            "user-agent": "fingerprint-user-agent",
            "sec-ch-ua": "fingerprint-sec-ch-ua",
        }

        with patch.object(
            pix,
            "build_sentinel_token",
            return_value=("sentinel-token", "oai-sc-value"),
        ) as sentinel:
            sentinel_token = pix.warmup_approve_context(
                session,
                "https://chatgpt.com/checkout/openai_llc/cs_pix_test",
                "device-id",
                fingerprint,
            )

        self.assertEqual(sentinel_token, "sentinel-token")

        sentinel.assert_called_once_with(
            session,
            "device-id",
            "checkout_session_approval",
            user_agent="fingerprint-user-agent",
            sec_ch_ua="fingerprint-sec-ch-ua",
        )
        session.cookies.set.assert_called_once_with(
            "oai-sc",
            "oai-sc-value",
            domain=".chatgpt.com",
            path="/",
        )
        ping = session.post.call_args
        self.assertEqual(ping.args[0], "https://chatgpt.com/backend-api/sentinel/ping")
        self.assertNotIn("json", ping.kwargs)
        self.assertEqual(
            ping.kwargs["headers"]["Referer"],
            "https://chatgpt.com/checkout/openai_llc/cs_pix_test",
        )

    def test_chatgpt_approve_sends_browser_sentinel_and_attestation_headers(self) -> None:
        session = MagicMock()
        session.get.return_value.text = (
            '<script>{"webDeploymentAttestation":"deployment.attestation"}</script>'
        )
        session.post.return_value.status_code = 200
        session.post.return_value.text = '{"result":"approved"}'
        session.post.return_value.json.return_value = {"result": "approved"}
        session.post.return_value.headers = {}

        with (
            patch.object(pix, "warmup_approve_context", return_value="sentinel-token"),
            patch.object(pix, "dump_http"),
        ):
            pix.chatgpt_approve(session, self.checkout, "device-id")

        approve_headers = session.post.call_args.kwargs["headers"]
        self.assertEqual(approve_headers["OpenAI-Sentinel-Token"], "sentinel-token")
        self.assertEqual(
            approve_headers["oai-web-deployment-attestation"],
            "deployment.attestation",
        )
        self.assertEqual(
            approve_headers["x-oai-is-pending-updates"],
            '{"v":3,"updates":[]}',
        )

    def test_extracts_escaped_web_deployment_attestation(self) -> None:
        self.assertEqual(
            pix.extract_web_deployment_attestation(
                r'{\"webDeploymentAttestation\":\"deployment.attestation\"}'
            ),
            "deployment.attestation",
        )

    def test_cloudflare_managed_challenge_is_classified(self) -> None:
        response = FakeResponse({}, status_code=403, url="https://chatgpt.com/backend-api/payments/checkout")
        response.text = "<span id='challenge-error-text'>Enable JavaScript and cookies to continue</span>"

        self.assertTrue(pix.is_cloudflare_challenge_response(response))
        with self.assertRaisesRegex(RuntimeError, "cloudflare_challenge"):
            pix.raise_if_cloudflare_challenge(response, "checkout")

    def test_checkout_update_sends_reference_billing_and_custom_ui(self) -> None:
        session = MagicMock()
        session.post.return_value.status_code = 200
        session.post.return_value.text = '{"success":true}'
        session.post.return_value.json.return_value = {"success": True}
        session.post.return_value.url = (
            "https://chatgpt.com/backend-api/payments/checkout/update"
        )
        session.post.return_value.headers = {}

        with patch.object(pix, "dump_http"):
            pix.update_checkout_promotion(session, self.checkout, "BR")

        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body["billing_details"], {"country": "BR", "currency": "BRL"})
        self.assertEqual(body["checkout_ui_mode"], "custom")
        self.assertEqual(
            body["promo_campaign"],
            {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": False,
            },
        )

    def test_stripe_init_and_elements_session_match_browser_sequence(self) -> None:
        session = RecordingSession(
            [
                {
                    "id": "ppage_pix_test",
                    "config_id": "payment-element-config",
                    "init_checksum": "checksum",
                },
                {
                    "session_id": "elements_session_real",
                    "config_id": "elements-config-real",
                },
            ]
        )
        init_payload = pix.stripe_init(
            self.checkout["cs_id"],
            "pk_test",
            "",
            session=session,
        )
        _init_url, init_form = session.posts[0]
        self.assertEqual(
            init_form["elements_options_client[saved_payment_method][enable_save]"],
            "auto",
        )
        self.assertNotIn("elements_session_client[session_id]", init_form)

        ctx = pix.build_ctx(init_payload, self.checkout)
        payload = pix.stripe_elements_session(
            session,
            self.checkout["cs_id"],
            "pk_test",
            ctx,
        )
        self.assertEqual(payload["session_id"], "elements_session_real")
        self.assertEqual(ctx["elements_session_id"], "elements_session_real")
        self.assertEqual(ctx["elements_session_config_id"], "elements-config-real")
        _url, params = session.gets[0]
        self.assertEqual(params["deferred_intent[payment_method_types][1]"], "pix")
        self.assertEqual(params["stripe_js_id"], init_payload["client_stripe_js_id"])

    def test_confirm_uses_inline_pix_protocol_and_dynamic_tokens(self) -> None:
        session = RecordingSession([{"submission_attempt": {"state": "requires_approval"}}])
        self.ctx["elements_session_id"] = "elements-session-real"
        self.ctx["elements_session_config_id"] = "elements-config-real"
        token_config = stripe_token.StripeTokenConfig(
            bundle_hash="bundle-hash",
            shift=11,
            rv_ts="2024-01-01 00:00:00 -0000",
            rv="299e1ea907-runtime",
            sv="build-salt",
        )
        with patch.object(pix, "dump_http"):
            result = pix.stripe_confirm_pix(
                session,
                self.checkout["cs_id"],
                "pk_test",
                self.init_payload,
                self.ctx,
                self.checkout,
                self.init_payload["stripe_hosted_url"],
                self.billing,
                token_config=token_config,
            )

        self.assertEqual(result["submission_attempt"]["state"], "requires_approval")
        _url, form = session.posts[0]
        self.assertNotIn("payment_method", form)
        self.assertEqual(form["payment_method_data[type]"], "pix")
        self.assertEqual(form["payment_method_data[billing_details][tax_id]"], "060.543.916-05")
        self.assertEqual(
            form["payment_method_data[billing_details][address][postal_code]"],
            "01310100",
        )
        self.assertEqual(
            form["payment_method_data[billing_details][phone]"],
            "+5511998765432",
        )
        self.assertEqual(form["expected_payment_method_type"], "pix")
        self.assertEqual(form["version"], "299e1ea907")
        self.assertEqual(form["elements_options_client[saved_payment_method][enable_save]"], "auto")
        self.assertEqual(form["elements_options_client[saved_payment_method][enable_redisplay]"], "auto")
        self.assertTrue(form["js_checksum"])
        self.assertTrue(form["rv_timestamp"])
        self.assertEqual(form["link_brand"], "link")
        self.assertNotIn("passive_captcha_token", form)
        self.assertNotIn("consent[terms_of_service]", form)
        self.assertEqual(
            form["payment_method_data[client_attribution_metadata][checkout_config_id]"],
            "cfg_pix",
        )
        self.assertEqual(
            form["client_attribution_metadata[checkout_config_id]"],
            self.ctx["checkout_config_id"],
        )
        self.assertEqual(
            form["client_attribution_metadata[merchant_integration_additional_elements][0]"],
            "expressCheckout",
        )

        return_url = urlsplit(form["return_url"])
        query = parse_qs(return_url.query)
        self.assertEqual(return_url.hostname, "checkout.stripe.com")
        self.assertEqual(query["returned_from_redirect"], ["true"])
        self.assertEqual(query["ui_mode"], ["custom"])
        self.assertEqual(
            query["return_url"],
            [
                "https://chatgpt.com/checkout/verify?"
                "stripe_session_id=cs_pix_test&processor_entity=openai_ie&plan_type=plus"
            ],
        )
        self.assertEqual(return_url.fragment, "fidkdWxOYHwnPyd1blpxYHZxWjA0")

    def test_reference_mode_creates_pix_pm_then_confirms_by_id(self) -> None:
        session = RecordingSession(
            [
                {"id": "pm_pix_reference"},
                {"submission_attempt": {"state": "requires_approval"}},
            ]
        )
        self.ctx["elements_session_id"] = "elements-session-real"
        self.ctx["elements_session_config_id"] = "elements-config-real"

        with patch.object(pix, "dump_http"):
            pm_id = pix.stripe_create_pix_payment_method(
                session,
                self.checkout["cs_id"],
                "pk_test",
                self.billing,
                self.ctx,
            )
            result = pix.stripe_confirm_pix(
                session,
                self.checkout["cs_id"],
                "pk_test",
                self.init_payload,
                self.ctx,
                self.checkout,
                self.init_payload["stripe_hosted_url"],
                self.billing,
                token_config=None,
                pm_id=pm_id,
                confirm_mode="explicit",
            )

        self.assertEqual(result["submission_attempt"]["state"], "requires_approval")
        pm_url, pm_form = session.posts[0]
        self.assertEqual(pm_url, "https://api.stripe.com/v1/payment_methods")
        self.assertEqual(pm_form["type"], "pix")
        self.assertEqual(pm_form["billing_details[phone]"], "+5511998765432")
        self.assertEqual(pm_form["billing_details[address][postal_code]"], "01310-100")
        self.assertEqual(pm_form["billing_details[tax_id]"], "06054391605")
        self.assertEqual(
            pm_form["client_attribution_metadata[checkout_config_id]"],
            "cfg_pix",
        )
        self.assertEqual(
            pm_form[
                "client_attribution_metadata[merchant_integration_additional_elements][0]"
            ],
            "payment",
        )

        confirm_url, confirm_form = session.posts[1]
        self.assertEqual(
            confirm_url,
            "https://api.stripe.com/v1/payment_pages/cs_pix_test/confirm",
        )
        self.assertEqual(confirm_form["payment_method"], "pm_pix_reference")
        self.assertEqual(confirm_form["consent[terms_of_service]"], "accepted")
        self.assertEqual(
            confirm_form["elements_options_client[saved_payment_method][enable_save]"],
            "never",
        )
        self.assertEqual(
            confirm_form["client_attribution_metadata[checkout_config_id]"],
            "cfg_pix",
        )
        self.assertNotIn("payment_method_data[type]", confirm_form)
        self.assertNotIn("js_checksum", confirm_form)
        self.assertNotIn("rv_timestamp", confirm_form)
        self.assertNotIn("link_brand", confirm_form)

    def test_reference_confirm_uses_the_reference_return_url_and_never_save(self) -> None:
        session = RecordingSession([{"submission_attempt": {"state": "requires_approval"}}])
        init_payload = dict(self.init_payload)
        init_payload["client_stripe_js_id"] = "reference-stripe-js"
        ctx = pix.reference_build_ctx(init_payload)

        with patch.object(pix, "dump_http"):
            pix.reference_stripe_confirm(
                session,
                self.checkout["cs_id"],
                "pm_pix_reference",
                "pk_test",
                init_payload,
                ctx,
                self.init_payload["stripe_hosted_url"],
            )

        _url, form = session.posts[0]
        returned = urlsplit(form["return_url"])
        self.assertEqual(returned.hostname, "pay.openai.com")
        self.assertEqual(form["payment_method"], "pm_pix_reference")
        self.assertEqual(form["expected_amount"], "0")
        self.assertEqual(form["elements_options_client[saved_payment_method][enable_save]"], "never")
        self.assertEqual(form["elements_options_client[saved_payment_method][enable_redisplay]"], "never")
        self.assertNotIn("payment_method_data[type]", form)

    def test_reference_flow_rejects_nonzero_before_creating_payment_method(self) -> None:
        checkout = {"cs_id": "cs_pix_test", "stripe_pk": "pk_test", "currency": "BRL"}
        init_payload = {"stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_pix_test", "total_summary": {"due": 1}}
        stripe = MagicMock()
        stripe.headers = {}
        with (
            patch.object(pix, "chatgpt_session_for_proxy", return_value=MagicMock()),
            patch.object(pix, "update_checkout_promotion"),
            patch.object(pix, "new_session", return_value=stripe),
            patch.object(pix, "reference_stripe_init", return_value=init_payload),
            patch.object(pix, "stripe_create_pix_payment_method") as create_pm,
        ):
            with self.assertRaisesRegex(RuntimeError, "仅接受 0"):
                pix.run_reference_provider_flow(
                    "access-token",
                    "session-token",
                    "http://br-sticky.example:8000",
                    "http://br-sticky.example:8000",
                    "http://br-sticky.example:8000",
                    "device-id",
                    checkout,
                    self.billing,
                )

        create_pm.assert_not_called()

    def test_tax_region_and_snapshot_use_the_same_br_address(self) -> None:
        self.ctx["elements_session_id"] = "elements-session-real"
        self.ctx["elements_session_config_id"] = "elements-config-real"
        stripe = RecordingSession([{}, {}])

        pix.stripe_update_tax_region(
            stripe,
            self.checkout["cs_id"],
            "pk_test",
            self.ctx,
            self.billing,
            country_only=True,
        )
        pix.stripe_update_tax_region(
            stripe,
            self.checkout["cs_id"],
            "pk_test",
            self.ctx,
            self.billing,
        )

        country_form = stripe.posts[0][1]
        full_form = stripe.posts[1][1]
        self.assertEqual(country_form["tax_region[country]"], "BR")
        self.assertNotIn("tax_region[postal_code]", country_form)
        self.assertEqual(full_form["tax_region[postal_code]"], "01310100")
        self.assertEqual(full_form["tax_region[state]"], "SP")
        self.assertEqual(
            full_form[
                "client_attribution_metadata[merchant_integration_additional_elements][0]"
            ],
            "expressCheckout",
        )

        chatgpt = MagicMock()
        chatgpt.post.return_value.status_code = 204
        chatgpt.post.return_value.text = ""
        chatgpt.post.return_value.url = (
            "https://chatgpt.com/backend-api/payments/checkout/snapshot"
        )
        chatgpt.post.return_value.headers = {}
        pix.checkout_snapshot(chatgpt, self.checkout, self.billing)
        snapshot = chatgpt.post.call_args.kwargs["json"]["snapshot"]
        self.assertEqual(snapshot["billing_address"]["name"], self.billing["name"])
        self.assertEqual(
            snapshot["billing_address"]["address"]["postal_code"],
            "01310100",
        )

    def test_explicit_flow_uses_account_email_for_pix_billing(self) -> None:
        messages: list[str] = []
        progress = messages.append
        with (
            patch.object(pix, "pix_billing_profile", return_value=dict(self.billing)),
            patch.object(pix, "build_chatgpt_session", return_value=MagicMock()),
            patch.object(
                pix,
                "create_checkout",
                return_value={
                    "cs_id": "cs_pix_test",
                    "stripe_pk": "pk_test",
                    "currency": "BRL",
                },
            ),
            patch.object(
                pix,
                "run_provider_flow",
                return_value=pix.PixFlowResult(
                    redirect_url="https://payments.stripe.com/qr/instructions/pix-test",
                    qr_urls=[],
                    amount_minor=0,
                    intent_kind="setup_intent",
                ),
            ) as provider_flow,
        ):
            result = pix.generate_pix_link_explicit(
                "access-token",
                "http://br-sticky.example:8000",
                "http://br-sticky.example:8000",
                "http://br-sticky.example:8000",
                account="member@example.test",
                session_token="session-token",
                progress=progress,
            )

        self.assertTrue(result["ok"])
        billing = provider_flow.call_args.args[8]
        self.assertEqual(billing["email"], "member@example.test")
        self.assertIs(provider_flow.call_args.kwargs["progress"], progress)
        self.assertTrue(any("方案1 1/8：创建 BR Checkout" in message for message in messages))
        self.assertEqual(result["checkout_billing_name"], "Gabriel Almeida")
        self.assertEqual(result["checkout_billing_email"], "member@example.test")
        self.assertEqual(
            result["checkout_billing_address"],
            {
                "country": "BR",
                "line1": "Avenida Paulista 1000",
                "line2": "",
                "city": "Sao Paulo",
                "state": "SP",
                "postal_code": "01310100",
            },
        )
        self.assertEqual(result["checkout_billing_tax_id"], "060.543.916-05")

    def test_final_link_preserves_successful_billing_snapshot(self) -> None:
        billing_address = {
            "country": "BR",
            "line1": "Avenida Paulista 1000",
            "line2": "",
            "city": "Sao Paulo",
            "state": "SP",
            "postal_code": "01310100",
        }
        with patch.object(
            pix,
            "generate_pix_link_explicit",
            return_value={
                "ok": True,
                "provider": "pix_br",
                "hosted_instructions_url": "https://payments.stripe.com/qr/instructions/pix-test",
                "checkout_billing_name": "Gabriel Almeida",
                "checkout_billing_email": "member@example.test",
                "checkout_billing_address": billing_address,
                "checkout_billing_tax_id": "060.543.916-05",
            },
        ):
            result = pix.generate_pix_final_link(
                {"email": "member@example.test"},
                "access-token",
                checkout_proxy="http://br-sticky.example:8000",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["checkout_billing_name"], "Gabriel Almeida")
        self.assertEqual(result["checkout_billing_email"], "member@example.test")
        self.assertEqual(result["checkout_billing_address"], billing_address)
        self.assertEqual(result["checkout_billing_tax_id"], "060.543.916-05")

    def test_failed_explicit_flow_does_not_expose_billing_snapshot(self) -> None:
        with (
            patch.object(pix, "pix_billing_profile", return_value=dict(self.billing)),
            patch.object(pix, "build_chatgpt_session", return_value=MagicMock()),
            patch.object(
                pix,
                "create_checkout",
                return_value={"cs_id": "cs_pix_test", "stripe_pk": "pk_test", "currency": "BRL"},
            ),
            patch.object(pix, "run_provider_flow", side_effect=RuntimeError("approve blocked")),
        ):
            result = pix.generate_pix_link_explicit(
                "access-token",
                "http://br-sticky.example:8000",
                "http://br-sticky.example:8000",
                "http://br-sticky.example:8000",
                account="member@example.test",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "approve_blocked")
        self.assertFalse(any(key.startswith("checkout_billing_") for key in result))

    def test_requires_approval_poll_preserves_intent_qr_metadata(self) -> None:
        hosted_url = "https://payments.stripe.com/qr/instructions/pix-approved"
        png_url = "https://qr.stripe.com/pix-approved.png"
        svg_url = "https://qr.stripe.com/pix-approved.svg"
        payment_page = {
            "setup_intent": {
                "id": "seti_pix_test",
                "object": "setup_intent",
                "client_secret": "seti_pix_test_secret",
                "status": "requires_action",
            }
        }
        intent_payload = {
            "id": "seti_pix_test",
            "object": "setup_intent",
            "status": "requires_action",
            "next_action": {
                "type": "pix_display_qr_code",
                "pix_display_qr_code": {
                    "hosted_instructions_url": hosted_url,
                    "image_url_png": png_url,
                    "image_url_svg": svg_url,
                    "expires_at": 1_800_000_000,
                },
            },
        }
        session = RecordingSession([payment_page, intent_payload])
        confirm_payload = {"submission_attempt": {"state": "requires_approval"}}
        provider_proxy = "http://br-sticky.example:8000"
        shared_chatgpt = object()
        messages: list[str] = []
        progress = messages.append

        with (
            patch.object(pix, "approve_proxy_candidates", return_value=[provider_proxy]),
            patch.object(pix, "approve_with_retry", return_value=provider_proxy) as approve,
            patch.object(pix, "dump_http"),
            patch.object(pix.time, "sleep"),
        ):
            redirect_url, qr_urls, approve_proxy, resolved_payload = pix.resolve_confirm_payload_pix(
                session,
                confirm_payload,
                self.checkout,
                "pk_test",
                self.ctx,
                "pm_pix_test",
                "access-token",
                "device-id",
                "session-token",
                provider_proxy,
                provider_proxy,
                [provider_proxy],
                shared_chatgpt,
                progress=progress,
            )

        self.assertEqual(redirect_url, hosted_url)
        self.assertEqual(approve_proxy, provider_proxy)
        self.assertIn(png_url, qr_urls)
        self.assertIn(svg_url, qr_urls)
        self.assertEqual(
            pix.pix_display_qr_details(resolved_payload),
            {
                "hosted_instructions_url": hosted_url,
                "image_url_png": png_url,
                "image_url_svg": svg_url,
                "expires_at": 1_800_000_000,
            },
        )
        self.assertEqual(session.gets[0][0], "https://api.stripe.com/v1/payment_pages/cs_pix_test")
        self.assertEqual(session.gets[1][0], "https://api.stripe.com/v1/setup_intents/seti_pix_test")
        approve.assert_called_once()
        self.assertIs(approve.call_args.kwargs["initial_session"], shared_chatgpt)
        self.assertEqual(approve.call_args.kwargs["initial_proxy"], provider_proxy)
        self.assertIs(approve.call_args.kwargs["progress"], progress)
        self.assertTrue(any("轮询 Pix instructions，第 1 次" in message for message in messages))

    def test_explicit_flow_rejects_mixed_stage_proxies(self) -> None:
        with patch.object(pix, "pix_billing_profile", return_value=self.billing):
            result = pix.generate_pix_link_explicit(
                "token",
                "http://br-one.example:8000",
                "http://br-two.example:8000",
                "http://br-one.example:8000",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "proxy_mismatch")


if __name__ == "__main__":
    unittest.main()
