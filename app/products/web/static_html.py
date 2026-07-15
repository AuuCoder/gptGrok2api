"""Helpers for serving static HTML with lightweight version injection."""

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from app.platform.meta import get_project_version


_VERSION_TOKEN = "{{APP_VERSION}}"


def serve_static_html(path: Path) -> HTMLResponse:
    """Serve an HTML file, replacing the version token if present."""
    if not path.exists():
        raise HTTPException(status_code=404, detail="Page not found")

    body = path.read_text(encoding="utf-8")
    if _VERSION_TOKEN in body:
        body = body.replace(_VERSION_TOKEN, get_project_version())

    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


__all__ = ["serve_static_html"]
