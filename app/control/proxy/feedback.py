"""Classify upstream HTTP responses into proxy feedback categories."""

from .models import ProxyFeedback, ProxyFeedbackKind


def classify_status_code(status_code: int) -> ProxyFeedbackKind:
    if status_code == 200:
        return ProxyFeedbackKind.SUCCESS
    if status_code == 401:
        return ProxyFeedbackKind.UNAUTHORIZED
    if status_code == 403:
        return ProxyFeedbackKind.CHALLENGE
    if status_code == 429:
        return ProxyFeedbackKind.RATE_LIMITED
    if status_code >= 500:
        return ProxyFeedbackKind.UPSTREAM_5XX
    return ProxyFeedbackKind.FORBIDDEN


def build_feedback(
    status_code: int,
    *,
    is_cloudflare: bool = False,
    reason: str = "",
    retry_after_ms: int | None = None,
) -> ProxyFeedback:
    """Build a ``ProxyFeedback`` from an HTTP response status code."""
    kind = classify_status_code(status_code)
    if is_cloudflare and status_code == 403:
        kind = ProxyFeedbackKind.CHALLENGE
    return ProxyFeedback(
        kind           = kind,
        status_code    = status_code,
        reason         = reason,
        retry_after_ms = retry_after_ms,
    )


__all__ = ["classify_status_code", "build_feedback"]
