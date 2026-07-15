"""Validation helpers for user-actionable Stripe instruction URLs."""
from __future__ import annotations

from urllib.parse import urlparse


def is_final_upi_url(value: object) -> bool:
    """Return whether value is a user-actionable Stripe UPI instructions URL."""
    url = str(value or "").strip()
    if not url or any(char.isspace() for char in url):
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    prefix = "/upi/instructions/"
    path = parsed.path or ""
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname == "payments.stripe.com"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and path.startswith(prefix)
        and bool(path[len(prefix) :].strip("/"))
        and not parsed.fragment
    )


def is_final_pix_url(value: object) -> bool:
    """Return whether value is a user-actionable Stripe Pix instructions URL."""
    url = str(value or "").strip()
    if not url or any(char.isspace() for char in url):
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    path = parsed.path or ""
    valid_path = any(
        path.startswith(prefix) and bool(path[len(prefix) :].strip("/"))
        for prefix in ("/qr/instructions/", "/pix/instructions/")
    )
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname == "payments.stripe.com"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and valid_path
        and not parsed.fragment
    )


def is_final_checkout_url(value: object, checkout_channel: str = "") -> bool:
    """Validate a final URL against one channel, or against both when omitted."""
    channel = str(checkout_channel or "").strip().lower()
    if channel == "upi":
        return is_final_upi_url(value)
    if channel == "pix":
        return is_final_pix_url(value)
    return is_final_upi_url(value) or is_final_pix_url(value)


__all__ = ["is_final_checkout_url", "is_final_pix_url", "is_final_upi_url"]
