"""Reverse pipeline executor — the 7-step request lifecycle.

Pipeline:  plan → account → proxy → serialize → execute → classify → feedback

This executor is opt-in.  Existing products-layer code that calls transport
directly continues to work; the executor wraps the same pattern with
structured feedback and classification.
"""

import asyncio
from typing import Any

from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_ms
from app.platform.errors import UpstreamError
from app.control.model.spec import ModelSpec
from app.dataplane.account import AccountDirectory, get_account_directory
from app.dataplane.account.lease import AccountLease
from app.dataplane.proxy import get_proxy_runtime

from .types import ReversePlan, ReverseLeaseSet, ReverseResult, ResultCategory
from .planner import build_plan
from .classifier import classify_result
from .feedback import build_proxy_feedback


async def execute(
    spec: ModelSpec,
    request: dict[str, Any],
    *,
    payload_builder: Any | None = None,
) -> ReverseResult:
    """Execute the full reverse pipeline for one request.

    Parameters
    ----------
    spec : ModelSpec
        The resolved model specification.
    request : dict
        The raw API request body.
    payload_builder : callable, optional
        ``(plan, token, request) → bytes`` — serializes the request into the
        upstream payload format.  If None, the request dict is JSON-encoded.

    Returns
    -------
    ReverseResult
        Classified outcome of the upstream call.
    """
    t0 = now_ms()

    # Step 1: Plan
    plan = build_plan(spec, request)

    # Step 2: Acquire account
    directory = await get_account_directory()

    lease = await directory.reserve(plan.pool_candidates, plan.mode_id)
    if lease is None:
        return ReverseResult(
            category=ResultCategory.RATE_LIMITED,
            error="No available accounts",
            latency_ms=int(now_ms() - t0),
        )

    # Step 3: Acquire proxy
    proxy_runtime = await get_proxy_runtime()
    proxy_lease = await proxy_runtime.acquire()

    leases = ReverseLeaseSet(
        account_idx=lease.idx,
        account_token=lease.token,
        proxy_lease=proxy_lease,
    )

    # Step 4-5: Serialize + Execute
    result = await _execute_transport(plan, leases, request, payload_builder)
    result.latency_ms = int(now_ms() - t0)

    # Step 6: Classify (done inside _execute_transport)

    # Step 7: Feedback + release (fire-and-forget)
    asyncio.create_task(
        _apply_feedback_and_release(plan, leases, result, directory, lease),
    )

    return result


async def _execute_transport(
    plan: ReversePlan,
    leases: ReverseLeaseSet,
    request: dict[str, Any],
    payload_builder: Any | None,
) -> ReverseResult:
    """Execute the transport call and classify the result."""
    try:
        import orjson

        if payload_builder:
            payload = payload_builder(plan, leases.account_token, request)
        else:
            payload = orjson.dumps(request)

        from app.dataplane.reverse.transport.http import post_json

        raw = await post_json(
            plan.endpoint,
            leases.account_token,
            payload,
            lease=leases.proxy_lease,
            timeout_s=plan.timeout_s,
            content_type=plan.content_type,
            origin=plan.origin,
            referer=plan.referer,
        )
        category = classify_result(200)
        return ReverseResult(
            category=category,
            status_code=200,
            payload=raw,
        )
    except UpstreamError as exc:
        category = classify_result(exc.status, exc.details.get("body", ""))
        return ReverseResult(
            category=category,
            status_code=exc.status,
            body=exc.details.get("body", ""),
            error=str(exc),
        )
    except Exception as exc:
        logger.error(
            "reverse transport execution failed: error_type={} error={}",
            type(exc).__name__,
            exc,
        )
        return ReverseResult(
            category=ResultCategory.TRANSPORT_ERR,
            error=str(exc),
        )


async def _apply_feedback_and_release(
    plan: ReversePlan,
    leases: ReverseLeaseSet,
    result: ReverseResult,
    directory: AccountDirectory,
    account_lease: AccountLease,
) -> None:
    """Apply account and proxy feedback, then release the lease (best-effort)."""
    try:
        # Release inflight counter.
        await directory.release(account_lease)

        # Account feedback via the directory's feedback API.
        from app.control.account.enums import FeedbackKind

        _CATEGORY_TO_FEEDBACK = {
            ResultCategory.SUCCESS: FeedbackKind.SUCCESS,
            ResultCategory.RATE_LIMITED: FeedbackKind.RATE_LIMITED,
            ResultCategory.AUTH_FAILURE: FeedbackKind.UNAUTHORIZED,
            ResultCategory.FORBIDDEN: FeedbackKind.FORBIDDEN,
            ResultCategory.UPSTREAM_5XX: FeedbackKind.SERVER_ERROR,
            ResultCategory.TRANSPORT_ERR: FeedbackKind.SERVER_ERROR,
            ResultCategory.UNKNOWN: FeedbackKind.SERVER_ERROR,
        }
        fb_kind = _CATEGORY_TO_FEEDBACK.get(result.category)
        if fb_kind is not None:
            await directory.feedback(
                leases.account_token,
                fb_kind,
                plan.mode_id,
            )

        # Proxy feedback.
        if leases.proxy_lease:
            proxy_fb = build_proxy_feedback(result)
            proxy_runtime = await get_proxy_runtime()
            await proxy_runtime.feedback(leases.proxy_lease, proxy_fb)
    except Exception as exc:
        logger.debug("reverse feedback update failed (non-fatal): error={}", exc)


__all__ = ["execute"]
