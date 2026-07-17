"""XAI rate-limits API protocol — fetch live quota data per mode."""

import asyncio
from dataclasses import dataclass
from typing import Literal

import orjson

from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_ms


# ---------------------------------------------------------------------------
# Mode → request model name
# ---------------------------------------------------------------------------

# Each mode requires a separate POST request.
# The API returns a flat single-mode quota object per call.
_MODE_NAMES: dict[int, str] = {
    0: "auto",
    1: "fast",
    2: "expert",
    3: "heavy",
    4: "grok-420-computer-use-sa",
}

# Default window durations used as fallback when API call fails.
_DEFAULT_WINDOW_SECS: dict[int, int] = {
    0: 7_200,  # auto   — 2 h  (super/heavy only)
    1: 86_400,  # fast   — 24 h (basic; real value overrides for super/heavy)
    2: 7_200,  # expert — 2 h  (super/heavy only)
    3: 7_200,  # heavy  — 2 h  (heavy-pool only)
    4: 7_200,  # grok_4_3 — 2 h  (super/heavy only)
}


@dataclass(frozen=True)
class FastQuotaProbeResult:
    """Result of one read-only ``fast`` rate-limits request.

    ``exception`` is intentionally kept for the control plane only.  It lets
    callers mark a token expired when the upstream response proves that its
    credentials are invalid, while the public admin response can use the
    stable, token-free ``status`` and ``error`` fields.
    """

    status: Literal["valid", "invalid", "unknown"]
    quota: object | None = None
    error: str | None = None
    exception: BaseException | None = None


def _build_payload(mode_name: str) -> bytes:
    """Build rate-limits request payload: {"modelName": "fast"}"""
    return orjson.dumps({"modelName": mode_name})


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def parse_rate_limits(
    body: dict, *, default_window_seconds: int = 72_000
) -> dict | None:
    """Parse flat rate-limits response.

    Expected format::

        {
            "windowSizeSeconds": 72000,
            "remainingQueries":  20,
            "totalQueries":      20,
            "lowEffortRateLimits":  null,
            "highEffortRateLimits": null
        }

    Returns a dict with keys ``remaining``, ``total``, ``window_seconds``
    or ``None`` if the required fields are absent.
    """
    remaining = body.get("remainingQueries")
    if remaining is None:
        return None
    total = body.get("totalQueries")
    window_secs = body.get("windowSizeSeconds")
    return {
        "remaining": int(remaining),
        "total": int(total) if total is not None else int(remaining),
        "window_seconds": int(window_secs) if window_secs else default_window_seconds,
    }


# ---------------------------------------------------------------------------
# QuotaWindow builder
# ---------------------------------------------------------------------------


def _to_quota_window(data: dict, synced_at: int) -> object:
    from app.control.account.models import QuotaWindow
    from app.control.account.enums import QuotaSource

    ws = data["window_seconds"]
    return QuotaWindow(
        remaining=data["remaining"],
        total=data["total"],
        window_seconds=ws,
        reset_at=synced_at + ws * 1000,  # estimated end of current window
        synced_at=synced_at,
        source=QuotaSource.REAL,
    )


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------


async def _do_fetch(token: str, mode_name: str) -> dict:
    """POST the rate-limits endpoint for one mode and return parsed JSON body."""
    from app.dataplane.reverse.transport.http import post_json
    from app.dataplane.proxy import get_proxy_runtime
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    try:
        body = await post_json(
            "https://grok.com/rest/rate-limits",
            token,
            _build_payload(mode_name),
            lease=lease,
            timeout_s=20.0,
        )
        await proxy.feedback(
            lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200)
        )
        return body
    except Exception as exc:
        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        kind = _proxy_feedback_kind_for_error(exc, status=status)
        await proxy.feedback(lease, ProxyFeedback(kind=kind, status_code=status))
        raise


async def _fetch_one(token: str, mode_id: int) -> object | None:
    """Fetch quota window for a single mode. Returns QuotaWindow or None."""
    mode_name = _MODE_NAMES.get(mode_id)
    if mode_name is None:
        # Console (mode 5) is tracked from Console calls locally.  It has no
        # grok.com rate-limits API model name, so it must never fall back to
        # the auto probe and overwrite the local Console quota window.
        logger.debug("rate-limits fetch skipped: local-only or unknown mode={}", mode_id)
        return None
    try:
        body = await asyncio.wait_for(_do_fetch(token, mode_name), timeout=25.0)
    except asyncio.TimeoutError:
        logger.debug(
            "rate-limits fetch timed out: token={}... mode={}", token[:10], mode_name
        )
        return None
    except Exception as exc:
        if is_invalid_credentials_error(exc):
            raise
        logger.debug(
            "rate-limits fetch failed: token={}... mode={} error={}",
            token[:10],
            mode_name,
            exc,
        )
        return None

    data = parse_rate_limits(
        body,
        default_window_seconds=_DEFAULT_WINDOW_SECS.get(mode_id, 72_000),
    )
    if data is None:
        logger.debug(
            "rate-limits response missing quota fields: token={}... mode={} body={}",
            token[:10],
            mode_name,
            body,
        )
        return None

    return _to_quota_window(data, now_ms())


async def probe_fast_quota(token: str) -> FastQuotaProbeResult:
    """Probe one account with exactly one read-only ``fast`` quota request.

    A successful rate-limits response proves that the SSO session is usable.
    Only the explicit upstream invalid-credentials markers are authoritative
    enough to report ``invalid``.  Timeouts, challenge pages, generic 401/403
    responses, rate limits, and 5xx failures remain ``unknown`` so transient
    transport conditions never expire a healthy account.
    """
    mode_id = 1
    mode_name = _MODE_NAMES[mode_id]
    try:
        body = await asyncio.wait_for(_do_fetch(token, mode_name), timeout=25.0)
    except asyncio.TimeoutError:
        return FastQuotaProbeResult(
            status="unknown",
            error="fast 配额探针超时，未确认登录态",
        )
    except Exception as exc:
        if is_invalid_credentials_error(exc):
            return FastQuotaProbeResult(
                status="invalid",
                error="登录态已失效或账号不可用",
                exception=exc,
            )

        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        if status == 429:
            error = "fast 配额请求被限流，未确认登录态"
        elif status == 403:
            error = "上游访问被挑战或拒绝，未确认登录态"
        elif isinstance(status, int) and status >= 500:
            error = "上游服务暂不可用，未确认登录态"
        else:
            error = "fast 配额探针失败，未确认登录态"
        return FastQuotaProbeResult(status="unknown", error=error)

    data = parse_rate_limits(
        body if isinstance(body, dict) else {},
        default_window_seconds=_DEFAULT_WINDOW_SECS[mode_id],
    )
    if data is None:
        return FastQuotaProbeResult(
            status="unknown",
            error="fast 配额响应缺少有效额度数据，未确认登录态",
        )
    return FastQuotaProbeResult(status="valid", quota=_to_quota_window(data, now_ms()))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_all_quotas(
    token: str, mode_ids: tuple[int, ...] | None = None
) -> dict[int, object] | None:
    """Fetch quota windows for the requested modes concurrently.

    ``mode_ids`` defaults to ``(0, 1, 2, 3, 4)``. Returns ``{mode_id: QuotaWindow}``
    for every mode that responded successfully, or ``None`` if every requested
    mode failed.
    """
    import asyncio

    requested = mode_ids or (0, 1, 2, 3, 4)
    results = await asyncio.gather(
        *(_fetch_one(token, mode_id) for mode_id in requested), return_exceptions=True
    )
    for result in results:
        if isinstance(result, Exception) and is_invalid_credentials_error(result):
            raise result
    windows = {
        mode_id: win
        for mode_id, win in zip(requested, results, strict=False)
        if win is not None and not isinstance(win, Exception)
    }
    return windows if windows else None


async def fetch_mode_quota(token: str, mode_id: int) -> object | None:
    """Fetch the quota window for a single mode. Returns QuotaWindow or None."""
    return await _fetch_one(token, mode_id)


def invalid_credentials_body_kind(body: str) -> Literal["blocked", "invalid", ""]:
    """Classify explicit Grok blocked-account and invalid-session markers."""
    text = str(body or "").lower()
    if any(marker in text for marker in ("blocked-user", "email-domain-rejected", "account suspended")):
        return "blocked"
    if any(
        marker in text
        for marker in (
            "invalid-credentials",
            "bad-credentials",
            "failed to look up session id",
            "session not found",
            "token revoked",
            "token expired",
        )
    ):
        return "invalid"
    return ""


def is_invalid_credentials_body(body: str) -> bool:
    """Return whether *body* contains a Grok invalid/blocked account marker."""
    return bool(invalid_credentials_body_kind(body))


def invalid_credentials_error_kind(exc: BaseException) -> Literal["blocked", "invalid", ""]:
    """Return a credential classification only when the upstream body proves it."""
    if not isinstance(exc, UpstreamError) or exc.status not in (400, 401, 403):
        return ""
    return invalid_credentials_body_kind(str(exc.details.get("body", "") or ""))


def is_invalid_credentials_error(exc: BaseException) -> bool:
    """Return whether *exc* indicates the account is invalid or blocked."""
    return bool(invalid_credentials_error_kind(exc))


def _proxy_feedback_kind_for_error(
    exc: BaseException,
    *,
    status: int | None,
):
    """Map quota-fetch failures to proxy feedback without burning healthy clearance."""
    from app.control.proxy.models import ProxyFeedbackKind

    # Invalid or blocked accounts are account problems, not proxy problems.
    if is_invalid_credentials_error(exc):
        return ProxyFeedbackKind.FORBIDDEN

    if status == 429:
        return ProxyFeedbackKind.RATE_LIMITED
    if status == 403:
        return ProxyFeedbackKind.CHALLENGE
    if status == 401:
        return ProxyFeedbackKind.UNAUTHORIZED
    if status and status >= 500:
        return ProxyFeedbackKind.UPSTREAM_5XX
    return ProxyFeedbackKind.TRANSPORT_ERROR


__all__ = [
    "FastQuotaProbeResult",
    "invalid_credentials_body_kind",
    "invalid_credentials_error_kind",
    "parse_rate_limits",
    "fetch_all_quotas",
    "fetch_mode_quota",
    "probe_fast_quota",
    "is_invalid_credentials_body",
    "is_invalid_credentials_error",
]
