from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from api.support import require_admin, require_identity
from services.prompt_library_service import prompt_library_service


class PromptSourceRequest(BaseModel):
    enabled: bool | None = None


def _prompt_response(
    items: list[dict[str, Any]],
    sources: list[dict[str, Any]] | None = None,
    **meta: Any,
) -> dict[str, Any]:
    return {
        "items": items,
        "prompt_count": len(items),
        "sources": sources or [],
        "source_count": len(sources or []),
        **meta,
    }


def _source_response(
    sources: list[dict[str, Any]],
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sources": sources,
        "source_count": len(sources),
    }
    if source is not None:
        payload["source"] = source
    return payload


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/prompts")
    async def list_prompts(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        result = await run_in_threadpool(prompt_library_service.list_cached)
        sources = await run_in_threadpool(prompt_library_service.list_sources)
        return _prompt_response(
            result["items"],
            sources,
            synced=result["synced"],
            cached_source_count=result["cached_source_count"],
            enabled_source_count=result["enabled_source_count"],
        )

    @router.get("/api/admin/prompt-sources")
    async def admin_list_prompt_sources(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        sources = await run_in_threadpool(prompt_library_service.list_sources)
        return _source_response(sources)

    @router.post("/api/admin/prompt-sources/refresh")
    async def admin_refresh_prompt_sources(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        result = await run_in_threadpool(prompt_library_service.refresh)
        return _prompt_response(
            result["items"],
            result["sources"],
            source_error_count=result.get("source_error_count", 0),
            source_errors=result.get("source_errors", []),
        )

    @router.post("/api/admin/prompt-sources/{source_id}/refresh")
    async def admin_refresh_prompt_source(source_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        result = await run_in_threadpool(prompt_library_service.refresh, source_id)
        return _prompt_response(
            result["items"],
            result["sources"],
            source_error_count=result.get("source_error_count", 0),
            source_errors=result.get("source_errors", []),
        )

    @router.post("/api/admin/prompt-sources/{source_id}")
    async def admin_update_prompt_source(
        source_id: str,
        body: PromptSourceRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            source = await run_in_threadpool(
                prompt_library_service.update_source,
                source_id,
                body.model_dump(mode="python", exclude_unset=True),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if source is None:
            raise HTTPException(status_code=404, detail={"error": "prompt source not found"})
        sources = await run_in_threadpool(prompt_library_service.list_sources)
        return _source_response(sources, source)

    return router
