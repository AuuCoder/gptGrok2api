"""Admin token CRUD — list, import, delete, replace pool.

Performance notes:
  - DI-injected repo (no try/except per call)
  - orjson direct output (bypasses stdlib json)
  - Quota dict: zero deserialization — reads r.quota directly
  - Import refresh: reuses app.state.refresh_service singleton
"""

import asyncio
import re
from typing import TYPE_CHECKING

import orjson
from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, RootModel

from app.platform.errors import AppError, ErrorKind, ValidationError
from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_ms
from app.control.account.commands import (
    AccountPatch,
    AccountUpsert,
    BulkReplacePoolCommand,
    ListAccountsQuery,
)
from app.control.account.enums import AccountStatus
from app.control.account.state_machine import is_manageable

if TYPE_CHECKING:
    from app.control.account.refresh import AccountRefreshService
    from app.control.account.repository import AccountRepository

from . import get_refresh_svc, get_repo

router = APIRouter(tags=["Admin - Tokens"])
_background_tasks: set[asyncio.Task] = set()

# ---------------------------------------------------------------------------
# Token sanitisation
# ---------------------------------------------------------------------------

_TOKEN_TRANS = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-",
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ",
    "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
})
_STRIP_RE = re.compile(r"\s+")


def _sanitize(value: str) -> str:
    tok = str(value or "").translate(_TOKEN_TRANS)
    tok = _STRIP_RE.sub("", tok)
    if tok.startswith("sso="):
        tok = tok[4:]
    return tok.encode("ascii", errors="ignore").decode("ascii")


def _mask(token: str) -> str:
    return f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ReplacePoolRequest(BaseModel):
    pool: str
    tokens: list[str]
    tags: list[str] = []


class AddTokensRequest(BaseModel):
    tokens: list[str]
    pool: str = "basic"
    tags: list[str] = []


class EditTokenRequest(BaseModel):
    old_token: str
    token: str
    pool: str = "basic"


class ToggleTokenDisabledRequest(BaseModel):
    token: str
    disabled: bool


class ToggleTokensDisabledRequest(BaseModel):
    tokens: list[str]
    disabled: bool


class VerifyTokensRequest(BaseModel):
    tokens: list[str]


class TokenImportItem(BaseModel):
    token: str
    tags: list[str] = []


class SaveTokensRequest(RootModel[dict[str, list[str | TokenImportItem]]]):
    """Bulk-save payload keyed by pool name."""


# ---------------------------------------------------------------------------
# Serialisation — zero-copy quota extraction
# ---------------------------------------------------------------------------

def _quota_brief(q: dict) -> dict:
    """Extract safe quota fields needed by the host account-management UI."""
    out = {}
    for mode in ("auto", "fast", "expert", "heavy", "console"):
        v = q.get(mode)
        if isinstance(v, dict):
            item = {
                "remaining": int(v.get("remaining", 0) or 0),
                "total": int(v.get("total", 0) or 0),
            }
            if mode == "console":
                try:
                    reset_at = int(v.get("reset_at") or 0)
                    if reset_at > 0:
                        item["reset_at"] = reset_at
                except (TypeError, ValueError):
                    pass
                try:
                    source = int(v.get("source"))
                    if source in (0, 1, 2):
                        item["source"] = source
                except (TypeError, ValueError):
                    pass
            out[mode] = item
    return out


def _serialize_record(r) -> dict:
    return {
        "token":       r.token,
        "pool":        r.pool or "basic",
        "status":      r.status,
        "quota":       _quota_brief(r.quota) if isinstance(r.quota, dict) else {},
        "use_count":   r.usage_use_count or 0,
        "fail_count":  r.usage_fail_count or 0,
        "last_used_at": r.last_use_at,
        "tags":        r.tags or [],
        "refresh_status": str(r.ext.get("refresh_status") or ""),
        "refresh_at": r.ext.get("refresh_at"),
        "refresh_error": str(r.ext.get("refresh_error") or "")[:300],
    }


def _json(data) -> Response:
    """orjson fast-path response."""
    return Response(content=orjson.dumps(data), media_type="application/json")


def _fire_and_forget(coro) -> asyncio.Task:
    # Keep a strong reference so import maintenance tasks cannot disappear before completion.
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _cleanup(done: asyncio.Task) -> None:
        _background_tasks.discard(done)
        if done.cancelled():
            return
        if exc := done.exception():
            logger.warning("admin background task failed: error_type={}", type(exc).__name__)

    task.add_done_callback(_cleanup)
    return task


def _schedule_auto_nsfw(
    repo: "AccountRepository",
    tokens: list[str],
    *,
    enabled: bool,
) -> None:
    if not tokens or not enabled:
        return
    unique_tokens = list(dict.fromkeys(tokens))
    _fire_and_forget(_enable_nsfw_imported(repo, unique_tokens))


async def _list_all_records(repo: "AccountRepository") -> list:
    items: list = []
    page_num = 1
    while True:
        page = await repo.list_accounts(ListAccountsQuery(page=page_num, page_size=2000))
        items.extend(page.items)
        if page_num >= page.total_pages or not page.items:
            break
        page_num += 1
    return items


async def _list_token_payloads(repo: "AccountRepository") -> list[dict]:
    fast_list = getattr(repo, "list_token_payloads", None)
    if callable(fast_list):
        return await fast_list()
    return [_serialize_record(r) for r in await _list_all_records(repo)]


async def _list_invalid_tokens(repo: "AccountRepository") -> list[str]:
    fast_list = getattr(repo, "list_invalid_tokens", None)
    if callable(fast_list):
        return await fast_list()
    return [
        item["token"]
        for item in await _list_token_payloads(repo)
        if item.get("status") not in (
            AccountStatus.ACTIVE.value,
            AccountStatus.COOLING.value,
            AccountStatus.DISABLED.value,
        )
    ]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/tokens")
async def list_tokens(repo: "AccountRepository" = Depends(get_repo)):
    """Return flat token list."""
    return _json({"tokens": await _list_token_payloads(repo)})


@router.post("/tokens")
async def save_tokens(
    req: SaveTokensRequest,
    auto_nsfw: bool = Query(False),
    repo: "AccountRepository" = Depends(get_repo),
    refresh_svc: "AccountRefreshService" = Depends(get_refresh_svc),
):
    """Full pool replace — accepts {pool_name: [token_objects]} dict."""
    total_upserted = 0
    all_tokens: list[str] = []

    for pool_name, items in req.root.items():
        upserts = []
        for item in items:
            td = {"token": item} if isinstance(item, str) else item.model_dump()
            token_val = _sanitize(td.get("token", ""))
            if not token_val:
                continue
            upserts.append(AccountUpsert(token=token_val, pool=pool_name, tags=td.get("tags") or []))
        if upserts:
            await repo.replace_pool(BulkReplacePoolCommand(pool=pool_name, upserts=upserts))
            all_tokens.extend(u.token for u in upserts)
            total_upserted += len(upserts)

    logger.info("admin tokens saved across pools: saved_count={}", total_upserted)
    if all_tokens:
        _fire_and_forget(_refresh_then_auto_nsfw(
            refresh_svc,
            repo,
            all_tokens,
            auto_nsfw_enabled=auto_nsfw,
        ))
    return _json({"status": "success", "count": total_upserted})


@router.post("/tokens/add")
async def add_tokens(
    req: AddTokensRequest,
    auto_nsfw: bool = Query(False),
    repo: "AccountRepository" = Depends(get_repo),
    refresh_svc: "AccountRefreshService" = Depends(get_refresh_svc),
):
    requested_pool = (req.pool or "basic").strip().lower()

    # Deduplicate and sanitize input
    cleaned: list[str] = []
    seen: set[str] = set()
    for token in req.tokens:
        tok = _sanitize(token)
        if tok and tok not in seen:
            seen.add(tok)
            cleaned.append(tok)
    if not cleaned:
        raise ValidationError("No valid tokens provided", param="tokens")

    # Only upsert tokens that are not already active — avoids overwriting quota/status.
    # Soft-deleted tokens are treated as non-existing so they can be restored.
    existing = {r.token for r in await repo.get_accounts(cleaned) if not r.is_deleted()}
    new_tokens = [t for t in cleaned if t not in existing]

    if not new_tokens:
        return _json({"status": "success", "count": 0, "skipped": len(cleaned)})

    upserts = [AccountUpsert(token=t, pool=requested_pool, tags=req.tags) for t in new_tokens]
    result = await repo.upsert_accounts(upserts)
    logger.info(
        "admin tokens added: pool={} added_count={} skipped_count={}",
        requested_pool,
        len(new_tokens),
        len(existing),
    )

    _fire_and_forget(_refresh_then_auto_nsfw(
        refresh_svc,
        repo,
        new_tokens,
        auto_nsfw_enabled=auto_nsfw,
    ))

    return _json({
        "status": "success",
        "count": result.upserted or len(new_tokens),
        "skipped": len(existing),
    })


@router.post("/tokens/verify")
async def verify_tokens(
    req: VerifyTokensRequest,
    refresh_svc: "AccountRefreshService" = Depends(get_refresh_svc),
):
    """Probe supplied tokens once via the read-only ``fast`` quota endpoint."""
    cleaned = list(dict.fromkeys(token for value in req.tokens if (token := _sanitize(value))))
    if not cleaned:
        raise ValidationError("No valid tokens provided", param="tokens")

    semaphore = asyncio.Semaphore(8)

    async def verify_one(token: str) -> dict:
        async with semaphore:
            try:
                result = await refresh_svc.verify_fast_token(token)
            except Exception:
                result = {
                    "status": "unknown",
                    "error": "fast 配额探针失败，未确认登录态",
                }
        status = str(result.get("status") or "unknown").lower()
        if status not in {"valid", "invalid", "unknown"}:
            status = "unknown"
        item: dict = {"token": token, "status": status}
        quota = result.get("quota")
        if status == "valid" and isinstance(quota, dict):
            try:
                item["quota"] = {
                    "remaining": max(0, int(quota.get("remaining", 0) or 0)),
                    "total": max(0, int(quota.get("total", 0) or 0)),
                }
            except (TypeError, ValueError):
                pass
        error = str(result.get("error") or "").strip()
        if error:
            item["error"] = error[:300]
        return item

    results = await asyncio.gather(*(verify_one(token) for token in cleaned))
    return _json({"results": results})


@router.delete("/tokens")
async def delete_tokens(
    tokens: list[str] = Body(...),
    repo: "AccountRepository" = Depends(get_repo),
):
    cleaned = [t for t in (_sanitize(t) for t in tokens) if t]
    if not cleaned:
        raise ValidationError("No valid tokens provided", param="tokens")
    await repo.delete_accounts(cleaned)
    logger.info("admin tokens deleted: deleted_count={}", len(cleaned))
    return _json({"deleted": len(cleaned)})


@router.delete("/tokens/invalid")
async def delete_invalid_tokens(repo: "AccountRepository" = Depends(get_repo)):
    tokens = await _list_invalid_tokens(repo)

    if not tokens:
        return _json({"deleted": 0})

    await repo.delete_accounts(tokens)
    logger.info("admin invalid tokens deleted: deleted_count={}", len(tokens))
    return _json({"deleted": len(tokens)})


@router.put("/tokens/edit")
async def edit_token(
    req: EditTokenRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    old_token = _sanitize(req.old_token)
    new_token = _sanitize(req.token)
    pool = (req.pool or "basic").strip().lower()

    if not old_token or not new_token:
        raise ValidationError("Token is required", param="token")

    records = await repo.get_accounts([old_token])
    if not records:
        raise AppError(
            "Account not found",
            kind=ErrorKind.VALIDATION,
            code="account_not_found",
            status=404,
        )
    record = records[0]

    if old_token != new_token:
        existing = await repo.get_accounts([new_token])
        if existing:
            raise AppError(
                "Target token already exists",
                kind=ErrorKind.VALIDATION,
                code="token_conflict",
                status=409,
            )

    await repo.upsert_accounts([AccountUpsert(
        token=new_token,
        pool=pool,
        tags=record.tags,
        ext=record.ext,
    )])

    if old_token == new_token:
        logger.info("admin token updated: token={} pool={}", _mask(new_token), pool)
        return _json({"status": "success", "token": new_token, "pool": pool})

    qs = record.quota_set()
    await repo.patch_accounts([AccountPatch(
        token=new_token,
        status=record.status,
        tags=record.tags,
        quota_auto=qs.auto.to_dict(),
        quota_fast=qs.fast.to_dict(),
        quota_expert=qs.expert.to_dict(),
        usage_use_delta=record.usage_use_count,
        usage_fail_delta=record.usage_fail_count,
        usage_sync_delta=record.usage_sync_count,
        last_use_at=record.last_use_at,
        last_fail_at=record.last_fail_at,
        last_fail_reason=record.last_fail_reason,
        last_sync_at=record.last_sync_at,
        last_clear_at=record.last_clear_at,
        state_reason=record.state_reason,
        ext_merge=record.ext,
    )])
    await repo.delete_accounts([old_token])

    logger.info("admin token replaced: previous_token={} current_token={} pool={}", _mask(old_token), _mask(new_token), pool)
    return _json({"status": "success", "token": new_token, "pool": pool})


@router.post("/tokens/disabled")
async def toggle_token_disabled(
    req: ToggleTokenDisabledRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    token = _sanitize(req.token)
    if not token:
        raise ValidationError("Token is required", param="token")

    records = await repo.get_accounts([token])
    if not records:
        raise AppError(
            "Account not found",
            kind=ErrorKind.VALIDATION,
            code="account_not_found",
            status=404,
        )
    record = records[0]

    if req.disabled:
        await repo.patch_accounts([AccountPatch(
            token=token,
            status=AccountStatus.DISABLED,
            state_reason="operator_disabled",
            ext_merge={
                **record.ext,
                "disabled_at": now_ms(),
                "disabled_reason": "operator_disabled",
            },
        )])
        logger.info("admin token disabled: token={}", _mask(token))
        return _json({"status": "success", "token": token, "disabled": True})

    await repo.patch_accounts([AccountPatch(
        token=token,
        status=AccountStatus.ACTIVE,
        clear_failures=True,
    )])
    logger.info("admin token restored: token={}", _mask(token))
    return _json({"status": "success", "token": token, "disabled": False})


@router.post("/tokens/disabled/batch")
async def toggle_tokens_disabled(
    req: ToggleTokensDisabledRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in req.tokens:
        token = _sanitize(raw)
        if token and token not in seen:
            seen.add(token)
            cleaned.append(token)
    if not cleaned:
        raise ValidationError("No valid tokens provided", param="tokens")

    records = await repo.get_accounts(cleaned)
    if not records:
        raise AppError(
            "No matching accounts found",
            kind=ErrorKind.VALIDATION,
            code="account_not_found",
            status=404,
        )

    ts = now_ms()
    patches: list[AccountPatch] = []
    for record in records:
        if req.disabled:
            patches.append(AccountPatch(
                token=record.token,
                status=AccountStatus.DISABLED,
                state_reason="operator_disabled",
                ext_merge={
                    **record.ext,
                    "disabled_at": ts,
                    "disabled_reason": "operator_disabled",
                },
            ))
        else:
            patches.append(AccountPatch(
                token=record.token,
                status=AccountStatus.ACTIVE,
                clear_failures=True,
            ))

    result = await repo.patch_accounts(patches)
    logger.info(
        "admin tokens disabled batch updated: disabled={} requested_count={} patched_count={}",
        req.disabled,
        len(cleaned),
        result.patched,
    )
    return _json({
        "status": "success",
        "disabled": req.disabled,
        "summary": {
            "total": len(cleaned),
            "ok": result.patched,
            "fail": max(0, len(cleaned) - result.patched),
        },
    })


@router.put("/tokens/pool")
async def replace_pool(
    req: ReplacePoolRequest,
    auto_nsfw: bool = Query(False),
    repo: "AccountRepository" = Depends(get_repo),
    refresh_svc: "AccountRefreshService" = Depends(get_refresh_svc),
):
    cleaned = [t for t in (_sanitize(t) for t in req.tokens) if t]
    upserts = [AccountUpsert(token=t, pool=req.pool, tags=req.tags) for t in cleaned]
    await repo.replace_pool(BulkReplacePoolCommand(pool=req.pool, upserts=upserts))
    logger.info("admin pool replaced: pool={} token_count={}", req.pool, len(cleaned))
    if cleaned:
        _fire_and_forget(_refresh_then_auto_nsfw(
            refresh_svc,
            repo,
            cleaned,
            auto_nsfw_enabled=auto_nsfw,
        ))
    return _json({"pool": req.pool, "count": len(cleaned)})


# ---------------------------------------------------------------------------
# Fire-and-forget import refresh
# ---------------------------------------------------------------------------

async def _refresh_imported(svc: "AccountRefreshService", tokens: list[str]) -> bool:
    try:
        await svc.refresh_on_import(tokens)
        logger.info("admin import quota sync completed: token_count={}", len(tokens))
        return True
    except Exception as exc:
        logger.warning("admin import quota sync failed: token_count={} error={}", len(tokens), exc)
        return False


async def _refresh_then_auto_nsfw(
    svc: "AccountRefreshService",
    repo: "AccountRepository",
    tokens: list[str],
    *,
    auto_nsfw_enabled: bool,
) -> None:
    unique_tokens = list(dict.fromkeys(tokens))
    if await _refresh_imported(svc, unique_tokens):
        _schedule_auto_nsfw(repo, unique_tokens, enabled=auto_nsfw_enabled)


async def _enable_nsfw_imported(repo: "AccountRepository", tokens: list[str]) -> None:
    from app.products.web.admin.batch import _concurrency, _nsfw_one
    from app.platform.runtime.batch import run_batch

    records = await repo.get_accounts(tokens)
    by_token = {r.token: r for r in records}
    manageable_tokens = [token for token in tokens if (record := by_token.get(token)) and is_manageable(record)]
    skipped_c = len(tokens) - len(manageable_tokens)
    if not manageable_tokens:
        logger.info("admin import auto nsfw skipped: token_count={} skipped_non_manageable={}", len(tokens), skipped_c)
        return

    ok_c = fail_c = 0

    async def _one(token: str) -> None:
        nonlocal ok_c, fail_c
        try:
            await _nsfw_one(repo, token, True)
            ok_c += 1
        except Exception as exc:
            fail_c += 1
            logger.warning("admin import auto nsfw failed: token={} error={}", _mask(token), exc)

    await run_batch(manageable_tokens, _one, concurrency=_concurrency(None, "batch.nsfw_concurrency"))
    logger.info(
        "admin import auto nsfw completed: token_count={} skipped_non_manageable={} ok={} failed={}",
        len(manageable_tokens),
        skipped_c,
        ok_c,
        fail_c,
    )
