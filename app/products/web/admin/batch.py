"""Admin batch operations + SSE progress streaming.

Performance notes:
  - Uses ``run_batch`` for bounded-concurrency parallel execution
    (replaces old sequential for-loop)
  - Async mode: background task with SSE fan-out via AsyncTask
  - Sync mode: concurrent execution, single JSON response
"""

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Awaitable

import orjson
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.platform.config.snapshot import get_config
from app.platform.errors import AppError, ErrorKind, UpstreamError, ValidationError
from app.platform.logging.logger import logger
from app.platform.runtime.batch import run_batch
from app.platform.runtime.task import create_task, expire_task, get_task
from app.control.account.commands import AccountPatch, ListAccountsQuery
from app.control.account.state_machine import is_manageable

if TYPE_CHECKING:
    from app.control.account.refresh import AccountRefreshService
    from app.control.account.repository import AccountRepository

from . import get_refresh_svc, get_repo

router = APIRouter(prefix="/batch", tags=["Admin - Batch"])
_MAX_BATCH_CONCURRENCY = 80

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _concurrency(override: int | None, config_key: str, fallback: int = 50) -> int:
    """Resolve effective concurrency: query-param → config → fallback."""
    if override is not None:
        return min(max(1, override), _MAX_BATCH_CONCURRENCY)
    v = get_config(config_key, fallback)
    try:
        resolved = int(v)
    except (TypeError, ValueError):
        resolved = fallback
    return min(max(1, resolved), _MAX_BATCH_CONCURRENCY)


def _mask(token: str) -> str:
    return f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token


async def _list_all_tokens(repo: "AccountRepository") -> list[str]:
    page_num, tokens = 1, []
    while True:
        page = await repo.list_accounts(ListAccountsQuery(page=page_num, page_size=2000))
        tokens.extend(r.token for r in page.items if is_manageable(r))
        if page_num >= page.total_pages or not page.items:
            break
        page_num += 1
    return tokens


async def _filter_manageable_tokens(repo: "AccountRepository", tokens: list[str]) -> list[str]:
    unique_tokens = list(dict.fromkeys(tokens))
    records = await repo.get_accounts(unique_tokens)
    by_token = {r.token: r for r in records}
    return [token for token in unique_tokens if (record := by_token.get(token)) and is_manageable(record)]


def _json(data: Any, status_code: int = 200) -> Response:
    return Response(content=orjson.dumps(data), media_type="application/json", status_code=status_code)


class BatchRequest(BaseModel):
    tokens: list[str] = []


# ---------------------------------------------------------------------------
# Dispatch engine — sync (run_batch) or async (background task + SSE)
# ---------------------------------------------------------------------------

async def _dispatch(
    tokens: list[str],
    handler: Callable[[str], Awaitable[dict]],
    *,
    use_async: bool,
    concurrency: int = 10,
    repo: "AccountRepository | None" = None,
) -> Response:
    if use_async:
        return await _dispatch_async(tokens, handler, concurrency)
    return await _dispatch_sync(tokens, handler, concurrency, repo)


async def _dispatch_sync(
    tokens: list[str],
    handler: Callable[[str], Awaitable[dict]],
    concurrency: int,
    repo: "AccountRepository | None" = None,
) -> Response:
    """Concurrent execution, collect all results, return at once."""
    results: dict[str, Any] = {}
    ok_c = fail_c = 0
    failed_tokens: list[str] = []

    async def _wrapped(token: str) -> tuple[str, dict | None, str | None]:
        try:
            data = await handler(token)
            return token, data, None
        except Exception as exc:
            return token, None, str(exc)

    raw = await run_batch(tokens, _wrapped, concurrency=concurrency)
    for token, data, err in raw:
        key = _mask(token)
        if err is None:
            ok_c += 1
            results[key] = data
        else:
            fail_c += 1
            failed_tokens.append(token)
            results[key] = {"error": err}

    # 批量查询失败 token 的状态，统计凭证失效数量
    expired_c = 0
    if repo and failed_tokens:
        from app.control.account.enums import AccountStatus
        records = await repo.get_accounts(failed_tokens)
        expired_c = sum(1 for r in records if r.status == AccountStatus.EXPIRED)

    return _json({
        "status": "success",
        "summary": {
            "total": len(tokens),
            "ok": ok_c,
            "fail": fail_c,
            "expired": expired_c,
            "transient": fail_c - expired_c,
        },
        "results": results,
    })


async def _dispatch_async(
    tokens: list[str],
    handler: Callable[[str], Awaitable[dict]],
    concurrency: int,
) -> Response:
    """Background task with per-item progress via AsyncTask SSE."""
    task = create_task(len(tokens))

    async def _run() -> None:
        try:
            results: dict[str, Any] = {}
            ok_c = fail_c = 0

            async def _one(token: str) -> None:
                nonlocal ok_c, fail_c
                if task.cancelled:
                    return
                masked = _mask(token)
                try:
                    if task.cancelled:
                        return
                    data = await handler(token)
                    ok_c += 1
                    results[masked] = data
                    task.record(True, item=masked, detail=data)
                except Exception as exc:
                    fail_c += 1
                    results[masked] = {"error": str(exc)}
                    task.record(False, item=masked, error=str(exc))

            await run_batch(tokens, _one, concurrency=concurrency)

            if task.cancelled:
                task.finish_cancelled()
            else:
                task.finish({
                    "status": "success",
                    "summary": {"total": len(tokens), "ok": ok_c, "fail": fail_c},
                    "results": results,
                })
        except Exception as exc:
            task.fail_task(str(exc))
        finally:
            asyncio.create_task(expire_task(task.id, 300))

    asyncio.create_task(_run())
    return _json({"status": "success", "task_id": task.id, "total": len(tokens)})


# ---------------------------------------------------------------------------
# Per-token handlers
# ---------------------------------------------------------------------------

async def _nsfw_one(repo: "AccountRepository", token: str, enabled: bool) -> dict:
    from app.dataplane.reverse.protocol.xai_auth import nsfw_sequence, set_nsfw
    if enabled:
        await nsfw_sequence(token)
    else:
        await set_nsfw(token, enabled)
    patch = AccountPatch(token=token, add_tags=["nsfw"]) if enabled else AccountPatch(token=token, remove_tags=["nsfw"])
    await repo.patch_accounts([patch])
    return {"success": True, "tagged": enabled}


async def _cache_clear_one(repo: "AccountRepository", token: str) -> dict:
    from app.control.account.invalid_credentials import mark_account_invalid_credentials
    from app.dataplane.reverse.transport.assets import list_assets, delete_asset
    try:
        resp = await list_assets(token)
        items = resp.get("assets", resp.get("items", []))

        async def _delete_one(item: dict) -> int:
            asset_id = item.get("id") or item.get("assetId")
            if not asset_id:
                return 0
            await delete_asset(token, asset_id)
            return 1

        results = await asyncio.gather(*[_delete_one(item) for item in items], return_exceptions=True)
        for result in results:
            if not isinstance(result, Exception):
                continue
            if await mark_account_invalid_credentials(repo, token, result, source="asset batch clear"):
                raise result
        return {"deleted": sum(r for r in results if isinstance(r, int))}
    except Exception as exc:
        await mark_account_invalid_credentials(repo, token, exc, source="asset batch clear")
        raise


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/nsfw")
async def batch_nsfw(
    req: BatchRequest,
    async_mode: bool = Query(False, alias="async"),
    all_manageable: bool = Query(False),
    concurrency: int | None = Query(None, ge=1),
    enabled: bool = Query(True),
    repo: "AccountRepository" = Depends(get_repo),
):
    tokens = [t.strip() for t in req.tokens if t.strip()]
    if all_manageable and tokens:
        raise ValidationError("tokens must be empty when all_manageable=true", param="tokens")
    if all_manageable:
        tokens = await _list_all_tokens(repo)
    else:
        if not tokens:
            raise ValidationError("No tokens provided", param="tokens")
        # Explicit refresh follows NSFW maintenance: only manageable accounts are operator-maintainable.
        requested_count = len(tokens)
        tokens = await _filter_manageable_tokens(repo, tokens)
        skipped_count = requested_count - len(tokens)
        if skipped_count:
            logger.info("admin batch nsfw skipped non-manageable tokens: skipped_count={}", skipped_count)
    if not tokens:
        raise ValidationError("No manageable tokens available", param="tokens")

    async def _nsfw_and_tag(token: str) -> dict:
        return await _nsfw_one(repo, token, enabled)

    c = _concurrency(concurrency, "batch.nsfw_concurrency")
    if all_manageable:
        logger.info("admin batch nsfw all manageable: token_count={} concurrency={}", len(tokens), c)
    return await _dispatch(tokens, _nsfw_and_tag, use_async=async_mode, concurrency=c)


@router.post("/refresh")
async def batch_refresh(
    req: BatchRequest,
    async_mode: bool = Query(False, alias="async"),
    all_manageable: bool = Query(False),
    concurrency: int | None = Query(None, ge=1),
    repo: "AccountRepository" = Depends(get_repo),
    refresh_svc: "AccountRefreshService" = Depends(get_refresh_svc),
):
    tokens = [t.strip() for t in req.tokens if t.strip()]
    if all_manageable and tokens:
        raise ValidationError("tokens must be empty when all_manageable=true", param="tokens")
    if all_manageable:
        tokens = await _list_all_tokens(repo)
    else:
        if not tokens:
            raise ValidationError("No tokens provided", param="tokens")
        requested_count = len(tokens)
        tokens = await _filter_manageable_tokens(repo, tokens)
        skipped_count = requested_count - len(tokens)
        if skipped_count:
            logger.info("admin batch refresh skipped non-manageable tokens: skipped_count={}", skipped_count)
    if not tokens:
        raise ValidationError("No manageable tokens available", param="tokens")

    async def _refresh_one(token: str) -> dict:
        result = await refresh_svc.refresh_tokens([token])
        if not result.refreshed:
            raise UpstreamError("未获取到真实配额数据")
        return {"refreshed": result.refreshed}

    c = _concurrency(concurrency, "batch.refresh_concurrency")
    if all_manageable:
        logger.info("admin batch refresh all manageable: token_count={} concurrency={}", len(tokens), c)
    return await _dispatch(tokens, _refresh_one, use_async=async_mode, concurrency=c, repo=repo)


@router.post("/cache-clear")
async def batch_cache_clear(
    req: BatchRequest,
    async_mode: bool = Query(False, alias="async"),
    concurrency: int | None = Query(None, ge=1),
    repo: "AccountRepository" = Depends(get_repo),
):
    tokens = [t.strip() for t in req.tokens if t.strip()]
    if not tokens:
        tokens = await _list_all_tokens(repo)
    if not tokens:
        raise ValidationError("No tokens available", param="tokens")

    async def _clear_one(token: str) -> dict:
        return await _cache_clear_one(repo, token)

    c = _concurrency(concurrency, "batch.asset_delete_concurrency")
    return await _dispatch(tokens, _clear_one, use_async=async_mode, concurrency=c)


# ---------------------------------------------------------------------------
# SSE stream + cancel
# ---------------------------------------------------------------------------

@router.get("/{task_id}/stream")
async def batch_stream(task_id: str, request: Request):
    # Auth is handled by the parent router's verify_admin_key dependency,
    # which accepts both Bearer header and ?app_key= query param (for EventSource).
    task = get_task(task_id)
    if not task:
        raise AppError(
            "Task not found",
            kind=ErrorKind.VALIDATION,
            code="task_not_found",
            status=404,
        )

    async def _stream():
        queue = task.attach()
        try:
            yield f"data: {orjson.dumps({'type': 'snapshot', **task.snapshot()}).decode()}\n\n"

            final = task.final_event()
            if final:
                yield f"data: {orjson.dumps(final).decode()}\n\n"
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    final = task.final_event()
                    if final:
                        yield f"data: {orjson.dumps(final).decode()}\n\n"
                        return
                    continue

                yield f"data: {orjson.dumps(event).decode()}\n\n"
                if event.get("type") in ("done", "error", "cancelled"):
                    return
        finally:
            task.detach(queue)

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/{task_id}/cancel")
async def batch_cancel(task_id: str):
    task = get_task(task_id)
    if not task:
        raise AppError(
            "Task not found",
            kind=ErrorKind.VALIDATION,
            code="task_not_found",
            status=404,
        )
    task.cancel()
    return {"status": "success"}
