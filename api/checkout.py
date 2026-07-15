"""Admin endpoint for extracting an OpenAI checkout/final payment link."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import require_admin
from services.openai_checkout_service import CheckoutSessionError, openai_checkout_service
from services.register_service import register_service


class CheckoutSessionCreateRequest(BaseModel):
    access_token: str = Field(min_length=1, max_length=20_000)
    checkout_channel: Literal["upi", "pix"] | None = None
    pix_protocol: Literal["enhanced", "reference", "standalone"] | None = None


class CheckoutRetryRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list, max_length=500)


def _checkout_response(result: dict) -> dict:
    payload = dict(result)
    # Protocol adapters should already return a safe envelope, but keep the
    # HTTP boundary defensive in case a future adapter accidentally includes
    # account/session material in its top-level result.
    for key in ("cookie_header", "session_token", "refresh_token", "id_token", "publishable_key", "payment_method_id"):
        payload.pop(key, None)
    item = payload.get("item")
    if isinstance(item, dict):
        safe_item = dict(item)
        for key in ("cookie_header", "session_token", "refresh_token", "id_token"):
            safe_item.pop(key, None)
        payload["item"] = safe_item
    return payload


def _configured_stage_proxy(checkout: dict, stage: str) -> str:
    if stage == "checkout":
        enabled = (
            bool(checkout.get("checkout_proxy_enabled"))
            if "checkout_proxy_enabled" in checkout
            else bool(checkout.get("residential_proxy_enabled"))
        )
        raw = (
            checkout.get("checkout_proxy_url")
            if "checkout_proxy_url" in checkout
            else checkout.get("residential_proxy_url")
        )
        label = "Checkout"
    else:
        enabled = bool(checkout.get(f"{stage}_proxy_enabled"))
        raw = checkout.get(f"{stage}_proxy_url")
        label = "Promotion" if stage == "promotion" else "Provider"
    if not enabled:
        return ""
    proxy = str(raw or "").strip()
    if not proxy:
        raise CheckoutSessionError(f"已启用 {label} 代理，但未填写代理 URL", status_code=400)
    return proxy


def _configured_checkout_settings() -> dict[str, str]:
    register = register_service.get()
    checkout = register.get("checkout") if isinstance(register.get("checkout"), dict) else {}
    checkout_proxy = _configured_stage_proxy(checkout, "checkout")
    channel = str(checkout.get("channel") or "upi").strip().lower()
    if channel not in {"upi", "pix"}:
        channel = "upi"
    configured_protocol = str(checkout.get("pix_protocol") or "").strip().lower()
    pix_protocol = configured_protocol if configured_protocol in {"enhanced", "reference", "standalone"} else "enhanced"
    uses_promotion_proxy = channel == "upi" or (channel == "pix" and pix_protocol == "standalone")
    promotion_proxy = _configured_stage_proxy(checkout, "promotion") if uses_promotion_proxy else ""
    promotion_proxy = promotion_proxy or checkout_proxy
    return {
        "checkout_channel": channel,
        "pix_protocol": pix_protocol,
        "checkout_proxy": checkout_proxy,
        "promotion_proxy": promotion_proxy,
        "provider_proxy": checkout_proxy,
    }


def _configured_checkout_proxy() -> str:
    """Compatibility helper retained for callers/tests of the former API."""
    return _configured_checkout_settings()["checkout_proxy"]


def create_router() -> APIRouter:
    router = APIRouter(tags=["OpenAI Checkout"])

    @router.post("/api/accounts/checkout-session")
    async def create_checkout_session(
        body: CheckoutSessionCreateRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            configured = _configured_checkout_settings()
            channel = body.checkout_channel or configured["checkout_channel"]
            result = await run_in_threadpool(
                openai_checkout_service.extract_and_store_checkout_link,
                body.access_token,
                checkout_channel=channel,
                **(
                    {"pix_protocol": body.pix_protocol or configured["pix_protocol"]}
                    if channel == "pix"
                    else {}
                ),
                checkout_proxy=configured["checkout_proxy"],
                promotion_proxy=configured["promotion_proxy"],
                provider_proxy=configured["provider_proxy"],
            )
            return _checkout_response(result)
        except CheckoutSessionError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"error": str(exc), "upstream_status": exc.upstream_status or None},
            ) from exc

    @router.post("/api/accounts/checkout-retries")
    async def enqueue_checkout_retries(
        body: CheckoutRetryRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            result = await run_in_threadpool(
                register_service.enqueue_checkout_retries_for_accounts,
                body.access_tokens,
            )
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    return router


__all__ = ["create_router"]
