"""Reverse pipeline feedback — translate ReverseResult into account + proxy feedback.

Called after every upstream call to update account health/quota and proxy state.
"""

from app.control.account.commands import AccountPatch
from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
from app.platform.runtime.clock import now_ms
from .types import ResultCategory, ReverseResult


# ---------------------------------------------------------------------------
# Account feedback
# ---------------------------------------------------------------------------

def build_account_feedback(
    token: str,
    result: ReverseResult,
    *,
    mode_id: int = 0,
) -> AccountPatch:
    """Build an AccountPatch reflecting the outcome of a request.

    SUCCESS  → increment use_count, update last_use_at
    RATE_LIMITED → decrement quota for the mode
    AUTH_FAILURE → increment fail_count, set last_fail_reason
    Others → increment fail_count
    """
    ts = now_ms()

    if result.category == ResultCategory.SUCCESS:
        return AccountPatch(
            token=token,
            usage_use_delta=1,
            last_use_at=ts,
        )

    if result.category == ResultCategory.RATE_LIMITED:
        # Signal quota exhaustion for the specific mode.
        quota_update: dict[str, dict] = {}
        mode_key = {0: "quota_auto", 1: "quota_fast", 2: "quota_expert"}.get(mode_id)
        if mode_key:
            quota_update[mode_key] = {"remaining": 0}
        return AccountPatch(
            token=token,
            usage_fail_delta=1,
            last_fail_at=ts,
            last_fail_reason=f"rate_limited (mode={mode_id})",
            **({mode_key: quota_update[mode_key]} if mode_key and quota_update else {}),
        )

    return AccountPatch(
        token=token,
        usage_fail_delta=1,
        last_fail_at=ts,
        last_fail_reason=result.error or result.category.name,
    )


# ---------------------------------------------------------------------------
# Proxy feedback
# ---------------------------------------------------------------------------

_CATEGORY_TO_PROXY: dict[ResultCategory, ProxyFeedbackKind] = {
    ResultCategory.SUCCESS:      ProxyFeedbackKind.SUCCESS,
    ResultCategory.RATE_LIMITED:  ProxyFeedbackKind.RATE_LIMITED,
    ResultCategory.AUTH_FAILURE:  ProxyFeedbackKind.UNAUTHORIZED,
    ResultCategory.FORBIDDEN:     ProxyFeedbackKind.CHALLENGE,
    ResultCategory.UPSTREAM_5XX:  ProxyFeedbackKind.UPSTREAM_5XX,
    ResultCategory.TRANSPORT_ERR: ProxyFeedbackKind.TRANSPORT_ERROR,
}


def build_proxy_feedback(result: ReverseResult) -> ProxyFeedback:
    """Build a ProxyFeedback from a ReverseResult."""
    kind = _CATEGORY_TO_PROXY.get(result.category, ProxyFeedbackKind.TRANSPORT_ERROR)
    return ProxyFeedback(
        kind=kind,
        status_code=result.status_code,
        reason=result.error,
    )


__all__ = ["build_account_feedback", "build_proxy_feedback"]
