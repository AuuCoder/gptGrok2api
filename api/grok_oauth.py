"""Host-admin API for Grok CLI OAuth accounts.

This API is intentionally separate from `/api/register/grok/accounts`: those
routes own grok.com SSO registration records and embedded-runtime operations.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from api.support import require_admin
from services.xai_cli_oauth_service import xai_cli_oauth_service
from services.xai_cli_oauth_store import xai_cli_oauth_store


class OAuthCredentialImportRequest(BaseModel):
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    email: str = ""
    subject: str = ""
    expires_in: int | None = Field(default=None, ge=60, le=172_800)
    credential: dict[str, Any] | None = None


class OAuthDevicePollRequest(BaseModel):
    session_id: str = ""


class OAuthProtocolStartRequest(BaseModel):
    account_id: str = ""


class OAuthAccountStatusRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    disabled: bool = False


class OAuthAccountDeleteRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


class OAuthAccountTestRequest(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=1_200)


async def require_grok_oauth_admin(
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    return require_admin(authorization)


def create_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/grok/oauth",
        tags=["Grok CLI OAuth"],
        dependencies=[Depends(require_grok_oauth_admin)],
    )

    @router.get("/accounts")
    async def list_accounts(keyword: str = "", status: str = "all") -> dict[str, Any]:
        items = xai_cli_oauth_store.list_accounts(keyword=keyword, status=status, redacted=True)
        return {
            "provider": "xai_cli_oauth",
            "items": items,
            "total": len(items),
            "available_models": xai_cli_oauth_service.available_models(),
        }

    @router.post("/accounts/import")
    async def import_account(body: OAuthCredentialImportRequest) -> dict[str, Any]:
        source = body.credential if isinstance(body.credential, dict) else {}
        auth_kind = str(source.get("auth_kind") or "").strip().lower()
        provider_type = str(source.get("type") or "").strip().lower()
        if source and (auth_kind not in {"", "oauth"} or provider_type not in {"", "xai"}):
            from app.platform.errors import ValidationError

            raise ValidationError("Only xAI OAuth CPA credentials can be imported", param="credential")
        access_token = body.access_token or str(source.get("access_token") or "")
        refresh_token = body.refresh_token or str(source.get("refresh_token") or "")
        id_token = body.id_token or str(source.get("id_token") or "")
        email = body.email or str(source.get("email") or "")
        subject = body.subject or str(source.get("subject") or source.get("sub") or "")
        expires_in = body.expires_in
        if expires_in is None:
            try:
                expires_in = int(source.get("expires_in") or 0) or None
            except (TypeError, ValueError):
                expires_in = None
        return await xai_cli_oauth_service.import_credentials(
            access_token=access_token,
            refresh_token=refresh_token,
            id_token=id_token,
            email=email,
            subject=subject,
            expires_in=expires_in,
        )

    @router.post("/device/start")
    async def start_device_authorization() -> dict[str, Any]:
        return await xai_cli_oauth_service.start_device_authorization()

    @router.post("/device/poll")
    async def poll_device_authorization(body: OAuthDevicePollRequest) -> dict[str, Any]:
        return await xai_cli_oauth_service.poll_device_authorization(body.session_id)

    @router.post("/protocol/start")
    async def start_protocol_authorization(body: OAuthProtocolStartRequest) -> dict[str, Any]:
        return await xai_cli_oauth_service.start_protocol_authorization(body.account_id)

    @router.get("/protocol/jobs/{job_id}")
    async def get_protocol_authorization_job(job_id: str) -> dict[str, Any]:
        job = xai_cli_oauth_service.get_protocol_authorization_job(job_id)
        if job is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail={"error": "协议授权任务不存在或已过期"})
        return {"job": job}

    @router.post("/accounts/{account_id}/refresh")
    async def refresh_account(account_id: str) -> dict[str, Any]:
        return await xai_cli_oauth_service.refresh_account(account_id)

    @router.post("/accounts/{account_id}/models/sync")
    async def sync_models(account_id: str) -> dict[str, Any]:
        return await xai_cli_oauth_service.sync_models(account_id)

    @router.post("/accounts/{account_id}/test")
    async def test_account(account_id: str, body: OAuthAccountTestRequest) -> dict[str, Any]:
        return await xai_cli_oauth_service.test_account(
            account_id,
            model=body.model,
            prompt=body.prompt,
        )

    @router.post("/accounts/status")
    async def set_status(body: OAuthAccountStatusRequest) -> dict[str, Any]:
        result = xai_cli_oauth_store.set_disabled(body.ids, body.disabled)
        return {**result, "disabled": body.disabled}

    @router.delete("/accounts")
    async def delete_accounts(body: OAuthAccountDeleteRequest) -> dict[str, Any]:
        return xai_cli_oauth_store.delete_accounts(body.ids)

    return router


__all__ = ["create_router"]
