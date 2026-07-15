"""Legacy Grok2API pages hosted by the chatgpt2api process.

The embedded runtime includes the original Grok2API admin and WebUI assets.
This router deliberately excludes its root redirect so the existing Vue console
remains the host application's default interface.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from app.platform.meta import get_project_version
from app.products.web.static_html import serve_static_html


_STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "statics"


def _static_file(asset_path: str) -> FileResponse:
    candidate = (_STATIC_DIR / asset_path).resolve()
    try:
        candidate.relative_to(_STATIC_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Static asset not found") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Static asset not found")
    return FileResponse(candidate)


def _page(path: str):
    return serve_static_html(_STATIC_DIR / path)


def create_router() -> APIRouter:
    """Expose original Grok pages without replacing the host SPA root route."""
    from app.products.web.webui import router as webui_router

    router = APIRouter(include_in_schema=False)
    router.include_router(webui_router)

    @router.get("/admin")
    async def admin_root():
        return RedirectResponse("/admin/login")

    @router.get("/admin/login")
    async def admin_login():
        return _page("admin/login.html")

    @router.get("/admin/account")
    async def admin_account():
        return _page("admin/account.html")

    @router.get("/admin/config")
    async def admin_config():
        return _page("admin/config.html")

    @router.get("/admin/cache")
    async def admin_cache():
        return _page("admin/cache.html")

    @router.get("/webui")
    async def webui_root():
        return RedirectResponse("/webui/login")

    @router.get("/webui/login")
    async def webui_login():
        return _page("webui/login.html")

    @router.get("/static/{asset_path:path}")
    async def static_asset(asset_path: str):
        return _static_file(asset_path)

    @router.get("/favicon.ico")
    async def favicon():
        return _static_file("favicon.ico")

    @router.get("/meta")
    async def app_meta():
        return {"version": get_project_version()}

    return router


__all__ = ["create_router"]
