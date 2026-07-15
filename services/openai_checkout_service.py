"""OpenAI UPI/Pix final payment-link entrypoint and shared validation."""
from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urlparse

from services.proxy_service import normalize_proxy_url


_CHECKOUT_PROXY_SCHEMES = {"http", "https", "socks", "socks5", "socks5h"}
_PROXY_CREDENTIALS_RE = re.compile(
    r"((?:https?|socks5h?|socks)://)([^\s/@:]+):([^\s/@]+)@",
    re.I,
)


class CheckoutSessionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        upstream_status: int = 0,
        code: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.upstream_status = upstream_status
        self.code = str(code or "").strip()


def _normalise_checkout_proxy_list(value: str) -> list[str]:
    """Validate a multi-line proxy field without discarding its order."""
    candidates: list[str] = []
    for line_number, raw in enumerate(str(value or "").splitlines(), start=1):
        proxy = normalize_proxy_url(raw)
        if not proxy:
            continue
        try:
            parsed = urlparse(proxy)
            parsed.port
        except ValueError:
            parsed = urlparse("")
        if parsed.scheme.lower() not in _CHECKOUT_PROXY_SCHEMES or not parsed.hostname:
            raise CheckoutSessionError(
                f"Checkout 代理第 {line_number} 行必须是有效的 http(s) 或 socks5(h) 地址",
                status_code=400,
            )
        candidates.append(proxy)
    return candidates


def _redact_proxy_credentials(value: object) -> str:
    return _PROXY_CREDENTIALS_RE.sub(r"\1[REDACTED]@", str(value or ""))


class OpenAICheckoutService:
    """Delegate checkout requests to one explicitly selected payment flow."""

    def extract_and_store_checkout_link(
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
        from services.openai_final_link_service import openai_final_link_service

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
        return openai_final_link_service.extract_and_store_final_link(access_token, **kwargs)


openai_checkout_service = OpenAICheckoutService()


__all__ = [
    "CheckoutSessionError",
    "OpenAICheckoutService",
    "openai_checkout_service",
]
