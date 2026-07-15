"""UPI/Pix final payment-link extraction and account persistence."""
from __future__ import annotations

import hashlib
import json
import random
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable
from urllib import request as urllib_request
from urllib.parse import quote, unquote, urlparse, urlunparse

from services.account_service import account_service
from services.checkout_url import is_final_checkout_url, is_final_pix_url, is_final_upi_url
from services.openai_checkout_service import (
    CheckoutSessionError,
    _normalise_checkout_proxy_list,
    _redact_proxy_credentials,
)


FINAL_LINK_CHANNELS = frozenset({"upi", "pix"})
_PROXY_API_PATHS = frozenset({"gen", "get", "proxy", "proxies", "fetch"})
_STICKY_SESSION_RE = re.compile(
    r"(?i)(?P<prefix>(?:^|[-_])(?:session|sid)[-_])(?P<value>[A-Za-z0-9]+)(?=(?:[-_]|$))"
)


def _safe_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_bool_or_none(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


def _is_proxy_api_url(value: object) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except (TypeError, ValueError):
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.query:
        return False
    path_name = (parsed.path or "").rstrip("/").rsplit("/", 1)[-1].lower()
    return path_name in _PROXY_API_PATHS


def _strings_from_proxy_api_payload(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for entry in value for item in _strings_from_proxy_api_payload(entry)]
    if isinstance(value, dict):
        return [item for entry in value.values() for item in _strings_from_proxy_api_payload(entry)]
    return []


def _fetch_proxy_api_candidates(url: str) -> list[str]:
    try:
        request = urllib_request.Request(url, headers={"Accept": "text/plain, application/json"})
        with urllib_request.urlopen(request, timeout=12) as response:  # nosec B310 - admin-configured URL
            raw = response.read().decode("utf-8", "replace").strip()
    except Exception as exc:
        raise CheckoutSessionError("Checkout 代理 API 获取失败", status_code=502) from exc
    if not raw:
        raise CheckoutSessionError("Checkout 代理 API 未返回代理", status_code=502)

    payload_strings = [raw]
    try:
        payload_strings = _strings_from_proxy_api_payload(json.loads(raw)) or payload_strings
    except (TypeError, ValueError):
        pass
    candidates: list[str] = []
    for text in payload_strings:
        try:
            candidates.extend(_normalise_checkout_proxy_list(str(text)))
        except CheckoutSessionError:
            continue
    if not candidates:
        raise CheckoutSessionError("Checkout 代理 API 未返回有效代理", status_code=502)
    return candidates


def _resolved_proxy_candidates(value: str) -> list[str]:
    candidates: list[str] = []
    for raw in str(value or "").splitlines():
        candidate = raw.strip()
        if not candidate:
            continue
        if _is_proxy_api_url(candidate):
            candidates.extend(_fetch_proxy_api_candidates(candidate))
        else:
            candidates.extend(_normalise_checkout_proxy_list(candidate))
    return candidates


def _with_rotated_sticky_session(proxy: str, *, role: str, rotation: int) -> str:
    """Rotate known sticky-session markers without changing the proxy country."""
    try:
        parsed = urlparse(proxy)
        username = unquote(str(parsed.username or ""))
        password = unquote(str(parsed.password or ""))
        host = str(parsed.hostname or "")
        port = parsed.port
    except (TypeError, ValueError):
        return proxy
    if not host:
        return proxy
    seed = hashlib.sha256(f"{role}:{rotation}:{proxy}".encode("utf-8")).hexdigest()
    fresh_session = str(int(seed[:14], 16) % 100_000_000).zfill(8)

    def rewrite(value: str) -> str:
        return _STICKY_SESSION_RE.sub(rf"\g<prefix>{fresh_session}", value, count=1)

    rewritten_username = rewrite(username)
    rewritten_password = rewrite(password)
    if rewritten_username == username and rewritten_password == password:
        return proxy
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port is not None:
        rendered_host = f"{rendered_host}:{port}"
    auth = quote(rewritten_username, safe="-_.~")
    if parsed.password is not None:
        auth += ":" + quote(rewritten_password, safe="-_.~")
    return urlunparse(parsed._replace(netloc=f"{auth}@{rendered_host}"))


class OpenAIFinalLinkService:
    """Run one selected payment protocol and persist only its final result."""

    def __init__(self) -> None:
        self._account_locks: dict[str, threading.Lock] = {}
        self._account_locks_guard = threading.Lock()

    def _lock_for_account(self, access_token: str) -> threading.Lock:
        with self._account_locks_guard:
            return self._account_locks.setdefault(access_token, threading.Lock())

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _error_from_protocol(result: object) -> CheckoutSessionError:
        payload = result if isinstance(result, dict) else {}
        error = _redact_proxy_credentials(payload.get("error") or "最终支付链接提取失败")[:500]
        code = str(payload.get("error_code") or "").strip().lower()
        if code in {"proxy_empty", "bad_token_format"}:
            status = 400
        elif code in {"token_revoked", "token_invalid"}:
            status = 401
        elif code in {
            "checkout_amount_mismatch",
            "non_zero_amount",
            "upi_unavailable",
            "pix_unavailable",
            "pix_amount_mismatch",
        }:
            status = 422
        else:
            status = 502
        return CheckoutSessionError(error, status_code=status, code=code)

    @staticmethod
    def _final_url(result: dict[str, Any], checkout_channel: str) -> str:
        keys = (
            ("upi_url", "long_url", "final_url", "checkout_final_url")
            if checkout_channel == "upi"
            else ("hosted_instructions_url", "pix_url", "long_url", "final_url", "checkout_final_url")
        )
        for key in keys:
            url = str(result.get(key) or "").strip()
            if is_final_checkout_url(url, checkout_channel):
                return url
        return ""

    @staticmethod
    def _select_role_proxy(
        value: str,
        *,
        role: str,
        proxy_rotation: int | None,
        required_message: str,
    ) -> str:
        candidates = _resolved_proxy_candidates(value)
        if not candidates:
            raise CheckoutSessionError(required_message, status_code=400)
        if proxy_rotation is None:
            return random.choice(candidates)
        try:
            rotation = max(0, int(proxy_rotation))
        except (TypeError, ValueError):
            rotation = 0
        selected = candidates[rotation % len(candidates)]
        return _with_rotated_sticky_session(selected, role=role, rotation=rotation)

    @staticmethod
    def _force_role_country(proxy: str, country: str, *, channel: str = "UPI") -> str:
        """Rewrite configurable country selectors while preserving fixed-country proxies."""
        from services.checkout_protocol.upi import proxy_for_country

        try:
            return proxy_for_country(proxy, country)
        except RuntimeError as exc:
            if "代理未包含可改写的 country/region 选择器" in str(exc):
                return proxy
            raise CheckoutSessionError(
                f"无法为 {channel} {country} 阶段派生代理地区: {_redact_proxy_credentials(exc)}",
                status_code=400,
            ) from exc

    @classmethod
    def _prepare_upi_proxies(
        cls,
        *,
        checkout_proxy: str,
        promotion_proxy: str,
        provider_proxy: str,
        proxy_rotation: int | None = None,
    ) -> tuple[str, str, str]:
        try:
            rotation = (
                max(0, int(proxy_rotation))
                if proxy_rotation is not None
                else random.randrange(0, 2**31)
            )
        except (TypeError, ValueError):
            rotation = 0

        checkout = cls._select_role_proxy(
            checkout_proxy,
            role="upi-in-shared",
            proxy_rotation=rotation,
            required_message="UPI 最终提链需要 IN Checkout 代理",
        )
        checkout = cls._force_role_country(checkout, "IN")
        provider = cls._select_role_proxy(
            provider_proxy,
            role="upi-in-shared",
            proxy_rotation=rotation,
            required_message="UPI 最终提链需要 IN 共享代理",
        )
        provider = cls._force_role_country(provider, "IN")
        if checkout != provider:
            raise CheckoutSessionError(
                "IN Checkout 与 IN Provider 必须使用同一条代理和 sticky session",
                status_code=400,
            )

        promotion = cls._select_role_proxy(
            promotion_proxy,
            role="upi-vn-promotion",
            proxy_rotation=rotation,
            required_message="UPI 最终提链需要 VN Promotion 代理",
        )
        promotion = cls._force_role_country(promotion, "VN")
        return checkout, promotion, provider

    @classmethod
    def _prepare_pix_proxies(
        cls,
        *,
        checkout_proxy: str,
        promotion_proxy: str,
        provider_proxy: str,
        proxy_rotation: int | None = None,
        pix_protocol: str = "enhanced",
    ) -> tuple[str, str, str]:
        """Choose the shared BR chain or scheme 3's BR/VN split chain."""
        source = str(checkout_proxy or provider_proxy or promotion_proxy or "").strip()
        shared = cls._select_role_proxy(
            source,
            role="pix-br-shared",
            proxy_rotation=proxy_rotation,
            required_message="Pix 最终提链需要 BR 共享代理",
        )
        shared = cls._force_role_country(shared, "BR", channel="Pix")
        if str(pix_protocol or "").strip().lower() == "standalone":
            promotion_source = str(promotion_proxy or source).strip()
            promotion = cls._select_role_proxy(
                promotion_source,
                role="pix-vn-promotion",
                proxy_rotation=proxy_rotation,
                required_message="Pix 方案 3 需要 VN Promotion 代理",
            )
            promotion = cls._force_role_country(promotion, "VN", channel="Pix 方案 3")
            return shared, promotion, shared
        return shared, shared, shared

    @staticmethod
    def _run_upi_protocol(
        active_token: str,
        account: dict[str, Any],
        checkout_proxy: str,
        promotion_proxy: str,
        provider_proxy: str,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        from services.checkout_protocol import generate_upi_final_link

        return generate_upi_final_link(
            active_token,
            checkout_proxy=checkout_proxy,
            promotion_proxy=promotion_proxy,
            provider_proxy=provider_proxy,
            account=account,
            progress=progress,
        )

    @staticmethod
    def _run_pix_protocol(
        active_token: str,
        account: dict[str, Any],
        checkout_proxy: str,
        promotion_proxy: str,
        provider_proxy: str,
        pix_protocol: str = "enhanced",
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        from services.checkout_protocol import generate_pix_final_link

        return generate_pix_final_link(
            account,
            active_token,
            checkout_proxy=checkout_proxy,
            promotion_proxy=promotion_proxy,
            provider_proxy=provider_proxy,
            protocol_mode=pix_protocol,
            checkout_channel="pix",
            progress=progress,
        )

    def create_final_link(
        self,
        access_token: str,
        *,
        checkout_channel: str = "upi",
        pix_protocol: str = "enhanced",
        checkout_proxy: str = "",
        promotion_proxy: str = "",
        provider_proxy: str = "",
        proxy_rotation: int | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        requested_token = str(access_token or "").strip()
        if not requested_token:
            raise CheckoutSessionError("access_token is required", status_code=400)
        channel = str(checkout_channel or "upi").strip().lower()
        if channel not in FINAL_LINK_CHANNELS:
            raise CheckoutSessionError("提链渠道仅支持 upi 或 pix", status_code=400)
        resolved_token = account_service.resolve_access_token(requested_token)
        account = account_service.get_account(resolved_token)
        if account is None:
            raise CheckoutSessionError("account not found in the local account pool", status_code=404)

        active_token = account_service.refresh_access_token(resolved_token, event="openai_final_link")
        account = account_service.get_account(active_token) or account
        if channel == "upi":
            checkout_proxy, promotion_proxy, provider_proxy = self._prepare_upi_proxies(
                checkout_proxy=checkout_proxy,
                promotion_proxy=promotion_proxy,
                provider_proxy=provider_proxy,
                proxy_rotation=proxy_rotation,
            )
        else:
            checkout_proxy, promotion_proxy, provider_proxy = self._prepare_pix_proxies(
                checkout_proxy=checkout_proxy,
                promotion_proxy=promotion_proxy,
                provider_proxy=provider_proxy,
                proxy_rotation=proxy_rotation,
                pix_protocol=pix_protocol,
            )
        try:
            if progress is not None:
                progress(f"启动 {channel.upper()} 最终支付链接协议")
            if channel == "pix":
                protocol_result = self._run_pix_protocol(
                    active_token,
                    account,
                    checkout_proxy,
                    promotion_proxy,
                    provider_proxy,
                    pix_protocol,
                    progress,
                )
            else:
                protocol_result = self._run_upi_protocol(
                    active_token,
                    account,
                    checkout_proxy,
                    promotion_proxy,
                    provider_proxy,
                    progress,
                )
        except CheckoutSessionError:
            raise
        except Exception as exc:
            status_code = int(getattr(exc, "status_code", 502) or 502)
            upstream_status = int(getattr(exc, "upstream_status", 0) or 0)
            raise CheckoutSessionError(
                f"{channel.upper()} 最终支付链接协议请求失败: {type(exc).__name__}: {_redact_proxy_credentials(exc)}",
                status_code=status_code if status_code in {400, 401, 403, 409, 422, 429, 502} else 502,
                upstream_status=upstream_status,
                code=str(getattr(exc, "code", "") or "").strip(),
            ) from exc

        if not isinstance(protocol_result, dict) or not protocol_result.get("ok"):
            raise self._error_from_protocol(protocol_result)
        final_url = self._final_url(protocol_result, channel)
        if not final_url:
            raise CheckoutSessionError(f"{channel.upper()} 协议未返回可用的最终支付链接", status_code=502)

        session_id = str(protocol_result.get("checkout_session_id") or "").strip()
        processor_entity = str(protocol_result.get("processor_entity") or "").strip()
        chatgpt_url = str(protocol_result.get("chatgpt_checkout_url") or "").strip()
        if not chatgpt_url and session_id:
            default_entity = "openai_llc" if channel == "upi" else "openai_ie"
            chatgpt_url = f"https://chatgpt.com/checkout/{processor_entity or default_entity}/{session_id}"
        amount_minor = _safe_int(protocol_result.get("checkout_amount_minor", protocol_result.get("pay_amount_minor")))
        is_free_trial = _safe_bool_or_none(protocol_result.get("checkout_is_free_trial", protocol_result.get("is_free_trial")))
        country = "IN" if channel == "upi" else "BR"
        currency = "INR" if channel == "upi" else "BRL"
        result = {
            "checkout_url": final_url,
            "checkout_final_url": final_url,
            "checkout_final_kind": "upi_instructions" if channel == "upi" else "stripe_pix_instructions",
            "checkout_payment_method": channel,
            "checkout_amount_minor": amount_minor,
            "checkout_amount": protocol_result.get("checkout_amount", protocol_result.get("pay_amount")),
            "checkout_is_free_trial": is_free_trial,
            "checkout_session_id": session_id or None,
            "checkout_processor_entity": processor_entity or None,
            "chatgpt_checkout_url": chatgpt_url or None,
            "checkout_channel": channel,
            "country": country,
            "currency": currency,
            "checkout_provider": str(protocol_result.get("provider") or "").strip() or None,
            "checkout_billing_name": protocol_result.get("checkout_billing_name"),
            "checkout_billing_email": protocol_result.get("checkout_billing_email"),
            "checkout_billing_address": protocol_result.get("checkout_billing_address"),
            "checkout_billing_tax_id": protocol_result.get("checkout_billing_tax_id"),
            "checkout_proxy_used": True,
            "checkout_promotion_proxy_used": True,
            "checkout_provider_proxy_used": True,
        }
        if channel == "pix":
            result.update(
                {
                    "checkout_hosted_instructions_url": final_url,
                    "checkout_qr_image_url_png": protocol_result.get("qr_image_url_png"),
                    "checkout_qr_image_url_svg": protocol_result.get("qr_image_url_svg"),
                    "checkout_qr_expires_at": protocol_result.get("qr_expires_at"),
                }
            )
        return result

    def extract_and_store_final_link(
        self,
        access_token: str,
        *,
        checkout_channel: str = "upi",
        pix_protocol: str = "enhanced",
        checkout_proxy: str = "",
        promotion_proxy: str = "",
        provider_proxy: str = "",
        proxy_rotation: int | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        requested_token = str(access_token or "").strip()
        resolved_token = account_service.resolve_access_token(requested_token)
        account = account_service.get_account(resolved_token)
        if account is None:
            raise CheckoutSessionError("account not found in the local account pool", status_code=404)

        with self._lock_for_account(resolved_token):
            account = account_service.get_account(resolved_token) or account
            attempted_at = self._now()
            try:
                attempt_count = max(0, int(account.get("checkout_attempt_count") or 0)) + 1
            except (TypeError, ValueError):
                attempt_count = 1
            kwargs: dict[str, Any] = {
                "checkout_channel": checkout_channel,
                "pix_protocol": pix_protocol,
                "checkout_proxy": checkout_proxy,
                "promotion_proxy": promotion_proxy,
                "provider_proxy": provider_proxy,
            }
            if proxy_rotation is not None:
                kwargs["proxy_rotation"] = proxy_rotation
            if progress is not None:
                kwargs["progress"] = progress
            try:
                checkout = self.create_final_link(resolved_token, **kwargs)
            except CheckoutSessionError as exc:
                existing_final = str(account.get("checkout_final_url") or account.get("checkout_url") or "").strip()
                item = account_service.update_account(
                    resolved_token,
                    {
                        "checkout_link_status": (
                            "ready" if is_final_checkout_url(existing_final, checkout_channel) else "failed"
                        ),
                        "checkout_last_attempt_at": attempted_at,
                        "checkout_attempt_count": attempt_count,
                        "checkout_last_error": _redact_proxy_credentials(exc)[:500],
                        "checkout_last_error_at": attempted_at,
                        "checkout_upstream_status": exc.upstream_status or None,
                    },
                    quiet=True,
                )
                exc.account = item  # type: ignore[attr-defined]
                raise

            created_at = self._now()
            item = account_service.update_account(
                account_service.resolve_access_token(resolved_token),
                {
                    "checkout_link_status": "ready",
                    "checkout_url": checkout["checkout_url"],
                    "checkout_final_url": checkout["checkout_final_url"],
                    "checkout_final_kind": checkout["checkout_final_kind"],
                    "checkout_payment_method": checkout["checkout_payment_method"],
                    "checkout_amount_minor": checkout["checkout_amount_minor"],
                    "checkout_amount": checkout.get("checkout_amount"),
                    "checkout_is_free_trial": checkout["checkout_is_free_trial"],
                    "checkout_chatgpt_url": checkout.get("chatgpt_checkout_url"),
                    "checkout_session_id": checkout.get("checkout_session_id"),
                    "checkout_processor_entity": checkout.get("checkout_processor_entity"),
                    "checkout_country": checkout.get("country", "IN"),
                    "checkout_currency": checkout.get("currency", "INR"),
                    "checkout_channel": checkout.get("checkout_channel", "upi"),
                    "checkout_provider": checkout.get("checkout_provider"),
                    "checkout_hosted_instructions_url": checkout.get("checkout_hosted_instructions_url"),
                    "checkout_qr_image_url_png": checkout.get("checkout_qr_image_url_png"),
                    "checkout_qr_image_url_svg": checkout.get("checkout_qr_image_url_svg"),
                    "checkout_qr_expires_at": checkout.get("checkout_qr_expires_at"),
                    "checkout_billing_name": checkout.get("checkout_billing_name"),
                    "checkout_billing_email": checkout.get("checkout_billing_email"),
                    "checkout_billing_address": checkout.get("checkout_billing_address"),
                    "checkout_billing_tax_id": checkout.get("checkout_billing_tax_id"),
                    "checkout_proxy_used": checkout["checkout_proxy_used"],
                    "checkout_promotion_proxy_used": checkout["checkout_promotion_proxy_used"],
                    "checkout_provider_proxy_used": checkout["checkout_provider_proxy_used"],
                    "checkout_created_at": created_at,
                    "checkout_final_created_at": created_at,
                    "checkout_last_attempt_at": attempted_at,
                    "checkout_attempt_count": attempt_count,
                    "checkout_last_error": None,
                    "checkout_last_error_at": None,
                    "checkout_upstream_status": None,
                },
                quiet=True,
            )
            return {**checkout, "item": item}


openai_final_link_service = OpenAIFinalLinkService()


__all__ = [
    "FINAL_LINK_CHANNELS",
    "OpenAIFinalLinkService",
    "is_final_checkout_url",
    "is_final_pix_url",
    "is_final_upi_url",
    "openai_final_link_service",
]
