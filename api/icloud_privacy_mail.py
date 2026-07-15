from __future__ import annotations

from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response

from api.support import require_admin
from services.account_service import account_service
from services.register.grok_account_store import grok_account_store


_DEFAULT_BASE_URL = "http://127.0.0.1:8787"
_TIMEOUT = httpx.Timeout(connect=8.0, read=120.0, write=30.0, pool=8.0)
_INTERNAL_PROXY_HEADER = "X-ChatGPT2API-Internal"
_INTERNAL_PROXY_VALUE = "icloud-privacy-mail"


def _base_url() -> str:
    import os

    return str(os.getenv("ICLOUD_PRIVACY_MAIL_BASE_URL") or _DEFAULT_BASE_URL).strip().rstrip("/")


def _upstream_path(path: str) -> str | None:
    """Translate the app-scoped API into the sidecar API surface.

    Keeping this mapping explicit prevents the module from becoming an arbitrary
    HTTP proxy while still exposing the complete management workflow.
    """
    path = "/" + path.strip("/")
    if path == "/bridge-status":
        return None
    if path == "/status":
        return "/api/status"
    if path == "/health":
        return "/api/v1/health"
    if path == "/session":
        return "/api/icloud/session"
    if path == "/create-settings":
        return "/api/create-settings"
    if path == "/accounts" or path.startswith("/accounts/"):
        return "/api" + path
    if path == "/mailboxes" or path.startswith("/mailboxes/"):
        return "/api" + path
    if path.startswith(("/icloud/", "/apple-account/", "/runtime/", "/update/")):
        return "/api" + path
    return None


def _response_headers(upstream: httpx.Response) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for name, value in upstream.headers.multi_items():
        lower = name.lower()
        if lower in {"content-type", "content-disposition", "cache-control", "x-content-type-options"}:
            headers.append((name, value))
    return headers


def _existing_account_claims() -> dict[str, list[str]]:
    sources = {
        "openai": account_service.list_accounts(),
        "grok": grok_account_store.list_accounts(redacted=False),
    }
    claims: dict[str, list[str]] = {}
    for project, accounts in sources.items():
        claims[project] = list(
            dict.fromkeys(
                str(item.get("email") or "").strip().lower()
                for item in accounts
                if isinstance(item, dict) and str(item.get("email") or "").strip()
            )
        )
    return claims


async def _sync_existing_account_claims() -> dict[str, object]:
    results: dict[str, dict[str, object]] = {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            for project, emails in _existing_account_claims().items():
                updated = 0
                missing: list[str] = []
                for offset in range(0, len(emails), 500):
                    upstream = await client.post(
                        urljoin(_base_url() + "/", "api/v1/mailboxes/claim-status"),
                        headers={_INTERNAL_PROXY_HEADER: _INTERNAL_PROXY_VALUE},
                        json={"project": project, "emails": emails[offset:offset + 500], "claimed": True},
                    )
                    upstream.raise_for_status()
                    payload = upstream.json()
                    if not isinstance(payload, dict) or payload.get("success") is not True:
                        raise httpx.HTTPError(f"invalid claim-status response for {project}")
                    updated += int(payload.get("updated") or 0)
                    missing.extend(str(item) for item in (payload.get("missing") or []))
                results[project] = {"emails": len(emails), "updated": updated, "missing": missing}
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "icloud_claim_sync_failed",
                "message": f"同步已有账号邮箱标签失败：{type(exc).__name__}",
            },
        ) from exc
    return {"success": True, "projects": results}


async def _proxy_request(request: Request, upstream_path: str) -> Response:
    base_url = _base_url()
    url = urljoin(base_url + "/", upstream_path.lstrip("/"))
    body = await request.body()
    headers: dict[str, str] = {}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    headers[_INTERNAL_PROXY_HEADER] = _INTERNAL_PROXY_VALUE
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            upstream = await client.request(
                request.method,
                url,
                params=request.query_params,
                headers=headers,
                content=body or None,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "icloud_privacy_mail_unavailable",
                "message": f"iCloud Privacy Mail 模块不可用：{type(exc).__name__}",
            },
        ) from exc
    response = Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=None,
    )
    for name, value in _response_headers(upstream):
        response.headers.append(name, value)
    response.headers["X-ICloud-Privacy-Mail-Proxy"] = "1"
    return response


def create_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/icloud/claim-status/sync")
    async def sync_existing_claims(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await _sync_existing_account_claims()

    @router.api_route(
        "/api/icloud/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def proxy_icloud(
        path: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        if path.strip("/") == "bridge-status":
            base_url = _base_url()
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                    upstream = await client.get(urljoin(base_url + "/", "login"))
                return {
                    "enabled": True,
                    "reachable": upstream.status_code < 500,
                    "base_url": base_url,
                    "status_code": upstream.status_code,
                }
            except httpx.HTTPError as exc:
                return {
                    "enabled": True,
                    "reachable": False,
                    "base_url": base_url,
                    "error": type(exc).__name__,
                }
        upstream_path = _upstream_path(path)
        if upstream_path is None:
            raise HTTPException(status_code=404, detail={"error": "icloud_endpoint_not_found"})
        return await _proxy_request(request, upstream_path)

    return router
