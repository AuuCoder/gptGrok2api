"""xAI Grok CLI OAuth provider.

This provider deliberately does not share credentials with the embedded
``grok.com`` SSO runtime.  The CLI promotion API accepts a renewable OAuth
Bearer token at ``cli-chat-proxy.grok.com/v1`` and currently exposes Grok 4.5
through OpenAI's Responses shape.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import queue
import threading
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import orjson

from app.platform.errors import RateLimitError, UpstreamError, ValidationError
from services.config import config
from services.xai_cli_oauth_protocol import (
    GROK_45_MODEL_ID,
    GROK_45_MODEL_ITEM,
    XAI_CLI_BASE_URL,
    XAI_CLI_HEADERS,
    XAI_DEVICE_CODE_URL,
    XAI_OAUTH_CLIENT_ID,
    XAI_OAUTH_SCOPE,
    XAI_TOKEN_URL,
    anthropic_messages_to_response_input,
    chat_messages_to_response_input,
    jwt_claims,
    normalize_model_ids,
    response_to_anthropic_message,
    response_to_chat_completion,
    response_usage,
    token_email,
    token_expiry_epoch,
)
from services.xai_cli_oauth_store import account_log_identity, XaiCliOAuthAccountStore, xai_cli_oauth_store


_REFRESH_EARLY_SECONDS = 60
_DEVICE_SESSION_MAX_SECONDS = 1_800
_ERROR_BODY_LIMIT = 1_200
_PROTOCOL_JOB_TTL_SECONDS = 3_600
_PROTOCOL_DEFER_POLL_SECONDS = 2.0
_PROTOCOL_QUEUE_WORKERS = 2
_PROTOCOL_QUEUE_MAX_ATTEMPTS = 2
_PROTOCOL_RETRY_STAGES = frozenset(
    {
        "approve",
        "bootstrap",
        "captcha",
        "castle",
        "consent",
        "device_code",
        "pkce",
        "response",
        "session",
        "signin",
        "token",
        "turnstile",
    }
)
AccountSelectedCallback = Callable[[dict[str, str]], None]


def _now_epoch() -> int:
    return int(time.time())


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _is_personal_team_blocked(code: object) -> bool:
    normalized = _clean_text(code).lower()
    return normalized == "personal-team-blocked" or normalized.startswith("personal-team-blocked:")


def _is_permission_pending_probe(probe: object) -> bool:
    if not isinstance(probe, dict):
        return False
    code = _clean_text(probe.get("code"))
    return code == "permission-denied" or (
        _clean_text(probe.get("http_status")) == "402"
        and _is_personal_team_blocked(code)
    )


def _expires_at_epoch(value: object) -> int:
    text = _clean_text(value)
    if not text:
        return 0
    try:
        normalized = text.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return 0


def _safe_error_body(response: httpx.Response) -> str:
    try:
        text = response.text
    except Exception:
        return ""
    return text[:_ERROR_BODY_LIMIT]


def _optional_header_int(value: object) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return max(0, int(text))
    except (TypeError, ValueError, OverflowError):
        return None


def _response_quota(response: httpx.Response) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for key in ("requests", "tokens"):
        limit = _optional_header_int(response.headers.get(f"x-ratelimit-limit-{key}"))
        remaining = _optional_header_int(response.headers.get(f"x-ratelimit-remaining-{key}"))
        if limit is None and remaining is None:
            continue
        window: dict[str, Any] = {}
        if limit is not None:
            window["limit"] = limit
        if remaining is not None:
            window["remaining"] = remaining
        reset = _clean_text(response.headers.get(f"x-ratelimit-reset-{key}"))
        if reset:
            window["reset"] = reset[:100]
        result[key] = window
    return result


def _response_error_fields(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return "", _safe_error_body(response)
    if not isinstance(payload, dict):
        return "", _safe_error_body(response)
    error = payload.get("error")
    if isinstance(error, dict):
        code = _clean_text(error.get("code") or error.get("type"))[:120]
        message = _clean_text(error.get("message") or error.get("detail"))[:500]
        return code, message or _safe_error_body(response)
    return (
        _clean_text(payload.get("code") or payload.get("type"))[:120],
        _clean_text(error or payload.get("message") or payload.get("detail"))[:500],
    )


def _response_probe_usage(response: httpx.Response) -> dict[str, int]:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    usage = payload.get("usage") if isinstance(payload, dict) and isinstance(payload.get("usage"), dict) else {}
    result: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens", "cost_in_usd_ticks"):
        try:
            value = max(0, int(usage.get(key)))
        except (TypeError, ValueError, OverflowError):
            continue
        result[key] = value
    return result


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {orjson.dumps(payload).decode()}\n\n"


class XaiCliOAuthService:
    """Manage OAuth device sessions and dispatch Grok CLI Responses requests."""

    def __init__(self, store: XaiCliOAuthAccountStore = xai_cli_oauth_store) -> None:
        self.store = store
        self._device_sessions: dict[str, dict[str, Any]] = {}
        self._device_lock = asyncio.Lock()
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._refresh_locks_guard = asyncio.Lock()
        self._delivery_locks: dict[str, asyncio.Lock] = {}
        self._delivery_locks_guard = asyncio.Lock()
        self._protocol_jobs: dict[str, dict[str, Any]] = {}
        self._protocol_job_lock = threading.RLock()
        self._protocol_tasks: set[asyncio.Task[Any]] = set()
        self._protocol_threads: set[threading.Thread] = set()
        self._protocol_queue: queue.PriorityQueue[tuple[int, int, str, dict[str, Any]]] = queue.PriorityQueue()
        self._protocol_queue_sequence = itertools.count()
        self._protocol_queue_workers: set[threading.Thread] = set()
        self.protocol_event_sink: Callable[[dict[str, Any]], None] | None = None

    def _client(self, *, proxy: str = "", timeout: float = 60.0) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": httpx.Timeout(timeout, connect=min(timeout, 20.0))}
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def _cli_headers(access_token: str, *, content_type: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            **XAI_CLI_HEADERS,
        }
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def available_models(self) -> list[str]:
        """Return verified models from active CLI OAuth accounts only."""
        return self.store.available_models()

    def supports_model(self, model: object) -> bool:
        model_id = _clean_text(model)
        return model_id == GROK_45_MODEL_ID and model_id in self.available_models()

    def model_items(self) -> list[dict[str, Any]]:
        if not self.supports_model(GROK_45_MODEL_ID):
            return []
        return [dict(GROK_45_MODEL_ITEM)]

    async def _form_post(
        self,
        url: str,
        form: dict[str, str],
        *,
        timeout: float = 30.0,
        proxy: str = "",
    ) -> httpx.Response:
        async with self._client(proxy=proxy, timeout=timeout) as client:
            return await client.post(
                url,
                data=form,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": XAI_CLI_HEADERS["User-Agent"],
                },
            )

    @staticmethod
    def _json_body(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise UpstreamError(
                "xAI OAuth returned an invalid JSON response",
                status=502,
                body=_safe_error_body(response),
            ) from exc
        if not isinstance(payload, dict):
            raise UpstreamError("xAI OAuth returned an invalid response object", status=502)
        return payload

    async def start_device_authorization(self, *, proxy: str = "") -> dict[str, Any]:
        proxy = _clean_text(proxy) or config.get_proxy_settings()
        response = await self._form_post(
            XAI_DEVICE_CODE_URL,
            {"client_id": XAI_OAUTH_CLIENT_ID, "scope": XAI_OAUTH_SCOPE},
            proxy=proxy,
        )
        if response.status_code != 200:
            raise UpstreamError(
                "Unable to start xAI device authorization",
                status=502,
                body=_safe_error_body(response),
            )
        payload = self._json_body(response)
        device_code = _clean_text(payload.get("device_code"))
        user_code = _clean_text(payload.get("user_code"))
        if not device_code or not user_code:
            raise UpstreamError("xAI device authorization response is incomplete", status=502)

        expires_in = max(30, min(int(payload.get("expires_in") or _DEVICE_SESSION_MAX_SECONDS), _DEVICE_SESSION_MAX_SECONDS))
        interval = max(1, min(int(payload.get("interval") or 5), 30))
        verification_uri = _clean_text(payload.get("verification_uri")) or "https://accounts.x.ai/oauth2/device"
        complete_uri = _clean_text(payload.get("verification_uri_complete")) or f"{verification_uri}?user_code={user_code}"
        session_id = f"xai-device-{uuid.uuid4().hex}"
        session = {
            "id": session_id,
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "verification_uri_complete": complete_uri,
            "expires_at": _now_epoch() + expires_in,
            "interval": interval,
            "proxy": _clean_text(proxy),
        }
        async with self._device_lock:
            self._drop_expired_device_sessions_unlocked()
            self._device_sessions[session_id] = session
        return self._public_device_session(session)

    @staticmethod
    def _public_device_session(session: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": _clean_text(session.get("id")),
            "user_code": _clean_text(session.get("user_code")),
            "verification_uri": _clean_text(session.get("verification_uri")),
            "verification_uri_complete": _clean_text(session.get("verification_uri_complete")),
            "expires_at": int(session.get("expires_at") or 0),
            "interval": int(session.get("interval") or 5),
            "status": "pending",
        }

    def _drop_expired_device_sessions_unlocked(self) -> None:
        now = _now_epoch()
        for session_id, session in list(self._device_sessions.items()):
            if int(session.get("expires_at") or 0) <= now:
                self._device_sessions.pop(session_id, None)

    async def poll_device_authorization(self, session_id: str) -> dict[str, Any]:
        clean_id = _clean_text(session_id)
        async with self._device_lock:
            self._drop_expired_device_sessions_unlocked()
            session = self._device_sessions.get(clean_id)
            session = dict(session) if isinstance(session, dict) else None
        if session is None:
            raise ValidationError("OAuth device authorization has expired or does not exist", param="session_id")

        response = await self._form_post(
            XAI_TOKEN_URL,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": _clean_text(session.get("device_code")),
                "client_id": XAI_OAUTH_CLIENT_ID,
            },
            proxy=_clean_text(session.get("proxy")),
        )
        payload = self._json_body(response)
        if response.status_code == 200 and _clean_text(payload.get("access_token")):
            try:
                imported = await self.import_credentials(
                    access_token=_clean_text(payload.get("access_token")),
                    refresh_token=_clean_text(payload.get("refresh_token")),
                    id_token=_clean_text(payload.get("id_token")),
                    expires_in=int(payload.get("expires_in") or 21_600),
                    source_type="device_authorization",
                    proxy=_clean_text(session.get("proxy")),
                )
            finally:
                async with self._device_lock:
                    self._device_sessions.pop(clean_id, None)
            return {"status": "authorized", **imported}

        error = _clean_text(payload.get("error"))
        if error in {"authorization_pending", "slow_down"}:
            interval = int(session.get("interval") or 5)
            if error == "slow_down":
                interval = min(interval + 5, 30)
                async with self._device_lock:
                    current = self._device_sessions.get(clean_id)
                    if isinstance(current, dict):
                        current["interval"] = interval
            return {"status": "pending", "interval": interval, "expires_at": int(session["expires_at"])}

        async with self._device_lock:
            self._device_sessions.pop(clean_id, None)
        description = _clean_text(payload.get("error_description"))
        if error in {"expired_token", "access_denied"}:
            raise ValidationError(f"xAI device authorization failed: {error}{(': ' + description) if description else ''}")
        raise UpstreamError(
            f"xAI device authorization failed: {error or 'unexpected_response'}",
            status=502,
            body=_safe_error_body(response),
        )

    @staticmethod
    def _protocol_job_view(job: dict[str, Any]) -> dict[str, Any]:
        return {
            key: job[key]
            for key in (
                "id",
                "status",
                "stage",
                "message",
                "error",
                "source_account_id",
                "created_at",
                "updated_at",
                "account",
                "models",
                "delivery",
            )
            if key in job
        }

    def _drop_expired_protocol_jobs_unlocked(self) -> None:
        cutoff = _now_epoch() - _PROTOCOL_JOB_TTL_SECONDS
        for job_id, job in list(self._protocol_jobs.items()):
            if int(job.get("updated_at") or 0) < cutoff and _clean_text(job.get("status")) not in {"pending", "running"}:
                self._protocol_jobs.pop(job_id, None)

    def _update_protocol_job(self, job_id: str, **updates: Any) -> None:
        with self._protocol_job_lock:
            job = self._protocol_jobs.get(job_id)
            if not isinstance(job, dict):
                return
            job.update(updates)
            job["updated_at"] = _now_epoch()

    def _select_protocol_source_account(self, account_id: str = "") -> dict[str, Any]:
        from services.register.grok_account_store import grok_account_store

        requested_id = _clean_text(account_id)
        if requested_id:
            candidates = grok_account_store.get_accounts_by_ids([requested_id])
        else:
            candidates = grok_account_store.list_accounts(redacted=False, status="active")
            linked_emails = {
                _clean_text(item.get("email")).lower()
                for item in self.store.list_accounts(redacted=False)
                if _clean_text(item.get("email"))
            }
            candidates = [
                item
                for item in candidates
                if _clean_text(item.get("email")).lower() not in linked_emails
            ]
        account = next(
            (
                item
                for item in candidates
                if _clean_text(item.get("id"))
                and _clean_text(item.get("email"))
                and (
                    _clean_text(item.get("sso") or item.get("sso_token"))
                    or _clean_text(item.get("password"))
                )
            ),
            None,
        )
        if account is None:
            if requested_id:
                raise ValidationError(
                    "Selected Grok account is missing or has no saved SSO/login password",
                    param="account_id",
                )
            raise ValidationError("No unlinked Grok account with a saved SSO/login password is available")
        return account

    def _prepare_protocol_authorization(
        self,
        account_id: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        source = self._select_protocol_source_account(account_id)
        source_account_id = _clean_text(source.get("id"))
        with self._protocol_job_lock:
            self._drop_expired_protocol_jobs_unlocked()
            active = next(
                (
                    job
                    for job in self._protocol_jobs.values()
                    if _clean_text(job.get("status")) in {"pending", "running"}
                    and _clean_text(job.get("source_account_id")) == source_account_id
                ),
                None,
            )
            if active is not None:
                return {"reused": True, "job": self._protocol_job_view(active)}, None
            job_id = f"xai-protocol-{uuid.uuid4().hex}"
            now = _now_epoch()
            job = {
                "id": job_id,
                "status": "pending",
                "stage": "queued",
                "message": "等待开始协议授权",
                "error": "",
                "retryable": False,
                "source_account_id": source_account_id,
                "created_at": now,
                "updated_at": now,
                "models": [],
            }
            self._protocol_jobs[job_id] = job
        return {"reused": False, "job": self._protocol_job_view(job)}, source

    async def start_protocol_authorization(self, account_id: str = "") -> dict[str, Any]:
        result, source = self._prepare_protocol_authorization(account_id)
        if source is None:
            return result

        job_id = _clean_text(result["job"].get("id"))
        task = asyncio.create_task(self._run_protocol_authorization(job_id, source))
        self._protocol_tasks.add(task)
        task.add_done_callback(self._protocol_tasks.discard)
        return result

    def start_protocol_authorization_background(
        self,
        account_id: str = "",
        *,
        prioritize: bool = False,
        retry: bool = False,
    ) -> dict[str, Any]:
        """Queue protocol OAuth from synchronous registration workers."""
        result, source = self._prepare_protocol_authorization(account_id)
        if source is None:
            return result

        job_id = _clean_text(result["job"].get("id"))
        priority = 0 if prioritize else (20 if retry else 10)
        self._protocol_queue.put((priority, next(self._protocol_queue_sequence), job_id, source))
        self._ensure_protocol_workers()
        result["queued"] = True
        result["priority"] = "registration" if prioritize else ("retry" if retry else "backfill")
        return result

    def _protocol_worker_limit(self) -> int:
        return _PROTOCOL_QUEUE_WORKERS

    def _ensure_protocol_workers(self) -> None:
        with self._protocol_job_lock:
            self._protocol_queue_workers = {
                worker for worker in self._protocol_queue_workers if worker.is_alive()
            }
            while len(self._protocol_queue_workers) < self._protocol_worker_limit():
                worker = threading.Thread(
                    target=self._run_protocol_queue,
                    daemon=True,
                    name=f"xai-protocol-queue-{len(self._protocol_queue_workers) + 1}",
                )
                self._protocol_queue_workers.add(worker)
                worker.start()

    def protocol_queue_status(self) -> dict[str, int]:
        with self._protocol_queue.mutex:
            pending = list(self._protocol_queue.queue)
        with self._protocol_job_lock:
            workers = sum(1 for worker in self._protocol_queue_workers if worker.is_alive())
            running = sum(
                1
                for job in self._protocol_jobs.values()
                if _clean_text(job.get("status")) == "running"
            )
        return {
            "queued": len(pending),
            "running": running,
            "workers": workers,
            "registration": sum(1 for item in pending if item[0] == 0),
            "backfill": sum(1 for item in pending if item[0] == 10),
            "retry": sum(1 for item in pending if item[0] == 20),
        }

    @staticmethod
    def _oauth_grok_config(runtime: dict[str, Any]) -> dict[str, Any]:
        source = runtime.get("grok") if isinstance(runtime.get("grok"), dict) else {}
        result = dict(source)
        if _clean_text(result.get("provider")).lower() == "local":
            attempt_timeout = max(15, min(120, int(result.get("local_attempt_timeout") or 60)))
            queue_timeout = max(0, min(300, int(result.get("local_queue_timeout") or 60)))
            configured_timeout = max(10, min(900, int(result.get("captcha_timeout") or 180)))
            result["captcha_timeout"] = max(configured_timeout, attempt_timeout + queue_timeout + 15)
        return result

    @staticmethod
    def _resolve_registration_proxy(raw_proxy: object) -> str:
        """Use the same upstream proxy selection as Grok registration workers."""
        from services.proxy_service import proxy_settings

        profile = proxy_settings.get_profile(proxy=_clean_text(raw_proxy), upstream=True)
        return _clean_text(profile.proxy_url) or "direct"

    def _emit_protocol_event(self, payload: dict[str, Any]) -> None:
        sink = self.protocol_event_sink
        if sink is None:
            return
        try:
            sink(dict(payload))
        except Exception:
            pass

    def _run_protocol_queue(self) -> None:
        while True:
            _priority, _sequence, job_id, source = self._protocol_queue.get()
            try:
                runner = threading.current_thread()
                with self._protocol_job_lock:
                    self._protocol_threads.add(runner)
                try:
                    for attempt in range(1, _PROTOCOL_QUEUE_MAX_ATTEMPTS + 1):
                        asyncio.run(
                            self._run_protocol_authorization(
                                job_id,
                                source,
                                notify_failure=False,
                            )
                        )
                        with self._protocol_job_lock:
                            job = dict(self._protocol_jobs.get(job_id) or {})
                        if _clean_text(job.get("status")) != "failed":
                            break
                        stage = _clean_text(job.get("stage"))
                        if (
                            attempt >= _PROTOCOL_QUEUE_MAX_ATTEMPTS
                            or stage not in _PROTOCOL_RETRY_STAGES
                            or not bool(job.get("retryable"))
                        ):
                            self._emit_protocol_event(
                                {
                                    "status": "failed",
                                    "job_id": job_id,
                                    "source_account_id": _clean_text(source.get("id")),
                                    "email": _clean_text(source.get("email")),
                                    "error": _clean_text(job.get("error")),
                                }
                            )
                            break
                        self._update_protocol_job(
                            job_id,
                            status="pending",
                            stage="queued",
                            message=f"协议授权瞬时失败，准备重试（{attempt + 1}/{_PROTOCOL_QUEUE_MAX_ATTEMPTS}）",
                            error="",
                            retryable=False,
                        )
                        time.sleep(_PROTOCOL_DEFER_POLL_SECONDS)
                finally:
                    with self._protocol_job_lock:
                        self._protocol_threads.discard(runner)
            finally:
                self._protocol_queue.task_done()

    async def _run_protocol_authorization(
        self,
        job_id: str,
        source: dict[str, Any],
        *,
        notify_failure: bool = True,
    ) -> None:
        from services.register_service import register_service
        from services.xai_oauth_delivery_service import deliver_xai_oauth_account

        runtime = register_service.get()
        grok_config = self._oauth_grok_config(runtime)
        proxy = self._resolve_registration_proxy(runtime.get("proxy"))
        oauth_flow = _clean_text(grok_config.get("xai_cli_oauth_flow")).lower() or "device"
        if oauth_flow == "pkce_reference":
            from services.xai_reference_pkce_protocol import XaiReferencePkceProtocol

            protocol = XaiReferencePkceProtocol(
                _clean_text(grok_config.get("xai_cli_pkce_reference_dir")),
                proxy=proxy,
                timeout=max(60, int(grok_config.get("captcha_timeout") or 180) + 60),
            )
            initial_stage = "pkce"
            initial_message = "准备 Authorization Code + PKCE 参考授权"
            source_type = "registered_account_pkce_reference"
        else:
            from services.xai_device_oauth_protocol import XaiDeviceOAuthProtocol

            protocol = XaiDeviceOAuthProtocol(grok_config, proxy=proxy)
            initial_stage = "device_code"
            initial_message = "准备 Device Code 协议授权"
            source_type = "registered_account_protocol"
        self._update_protocol_job(job_id, status="running", stage=initial_stage, message=initial_message)

        def progress(stage: str, message: str) -> None:
            self._update_protocol_job(job_id, status="running", stage=stage, message=message)

        try:
            protocol.progress = progress
            credential = await asyncio.to_thread(
                protocol.authorize,
                email=_clean_text(source.get("email")),
                password=_clean_text(source.get("password")),
                sso=_clean_text(source.get("sso") or source.get("sso_token")),
            )
            self._update_protocol_job(job_id, stage="models", message="验证 OAuth 凭据并探测模型")
            imported = await self.import_credentials(
                access_token=_clean_text(credential.get("access_token")),
                refresh_token=_clean_text(credential.get("refresh_token")),
                id_token=_clean_text(credential.get("id_token")),
                email=_clean_text(source.get("email")),
                expires_in=int(credential.get("expires_in") or 21_600),
                source_type=source_type,
                proxy="" if proxy == "direct" else proxy,
            )
            account_id = _clean_text((imported.get("account") or {}).get("id"))
            probe = imported.get("probe") if isinstance(imported.get("probe"), dict) else {}
            if _is_permission_pending_probe(probe):
                self._update_protocol_job(
                    job_id,
                    status="authorized",
                    stage="permission_pending",
                    message="OAuth 授权完成，Grok 4.5 权限待生效",
                    error="",
                    account=imported.get("account"),
                    models=imported.get("models") if isinstance(imported.get("models"), list) else [],
                    delivery={},
                )
                self._emit_protocol_event(
                    {
                        "status": "permission_pending",
                        "job_id": job_id,
                        "oauth_account_id": account_id,
                        "source_account_id": _clean_text(source.get("id")),
                        "email": _clean_text(source.get("email")),
                        "delivery": {},
                    }
                )
                return
            stored_account = self.store.get(account_id) if account_id else None
            delivery: dict[str, Any] = {}
            if isinstance(stored_account, dict):
                self._update_protocol_job(job_id, stage="delivery", message="按配置投递 OAuth 凭据")
                try:
                    delivery_account = dict(stored_account)
                    source_sso = _clean_text(source.get("sso") or source.get("sso_token"))
                    if source_sso:
                        delivery_account["sso_token"] = source_sso
                    delivery = await asyncio.to_thread(
                        deliver_xai_oauth_account,
                        delivery_account,
                        grok_config.get("oauth_delivery"),
                    )
                except Exception as exc:
                    delivery_error = _clean_text(exc) or type(exc).__name__
                    for key in ("access_token", "refresh_token", "id_token", "email", "subject"):
                        secret = _clean_text(stored_account.get(key))
                        if secret:
                            delivery_error = delivery_error.replace(secret, "[redacted]")
                    delivery = {
                        "system": {
                            "status": "failed",
                            "target_id": "",
                            "at": datetime.now(timezone.utc).isoformat(),
                            "error": delivery_error[:500],
                        }
                    }
                updated_account = self.store.update_metadata(account_id, {"oauth_delivery": delivery})
                if updated_account is not None:
                    imported["account"] = updated_account
            delivery_failed = any(
                isinstance(item, dict) and item.get("status") == "failed"
                for item in delivery.values()
            )
            self._update_protocol_job(
                job_id,
                status="authorized",
                stage="completed",
                message="协议授权完成，外部投递部分失败" if delivery_failed else "协议授权完成",
                error="",
                account=imported.get("account"),
                models=imported.get("models") if isinstance(imported.get("models"), list) else [],
                delivery=delivery,
            )
            self._emit_protocol_event(
                {
                    "status": "authorized",
                    "job_id": job_id,
                    "oauth_account_id": account_id,
                    "source_account_id": _clean_text(source.get("id")),
                    "email": _clean_text(source.get("email")),
                    "delivery": delivery,
                }
            )
        except Exception as exc:
            error = _clean_text(exc) or type(exc).__name__
            for secret in (_clean_text(source.get("email")), _clean_text(source.get("password"))):
                if secret:
                    error = error.replace(secret, "[redacted]")
            self._update_protocol_job(
                job_id,
                status="failed",
                stage=_clean_text(getattr(exc, "stage", "failed")) or "failed",
                message="协议授权失败",
                error=error[:500],
                retryable=bool(getattr(exc, "retryable", False)),
            )
            if notify_failure:
                self._emit_protocol_event(
                    {
                        "status": "failed",
                        "job_id": job_id,
                        "source_account_id": _clean_text(source.get("id")),
                        "email": _clean_text(source.get("email")),
                        "error": error[:500],
                    }
                )

    def get_protocol_authorization_job(self, job_id: str) -> dict[str, Any] | None:
        clean_id = _clean_text(job_id)
        with self._protocol_job_lock:
            self._drop_expired_protocol_jobs_unlocked()
            job = self._protocol_jobs.get(clean_id)
            return self._protocol_job_view(job) if isinstance(job, dict) else None

    async def import_credentials(
        self,
        *,
        access_token: str,
        refresh_token: str,
        email: str = "",
        subject: str = "",
        id_token: str = "",
        expires_in: int | None = None,
        source_type: str = "oauth_import",
        proxy: str = "",
    ) -> dict[str, Any]:
        """Validate model discovery and a real ``grok-4.5`` call, then persist it."""
        access = _clean_text(access_token)
        refresh = _clean_text(refresh_token)
        if not access:
            raise ValidationError("access_token is required", param="access_token")
        if not refresh:
            raise ValidationError("refresh_token is required", param="refresh_token")
        model_ids = await self._fetch_models(access, proxy=_clean_text(proxy) or config.get_proxy_settings())
        if GROK_45_MODEL_ID not in model_ids:
            raise ValidationError(
                "This xAI CLI OAuth account does not expose grok-4.5",
                param="access_token",
                code="model_not_available",
            )

        claims = jwt_claims(access)
        identity_claims = claims or jwt_claims(id_token)
        expires = token_expiry_epoch(access, fallback_seconds=expires_in or 21_600)
        identity_email = _clean_text(email) or token_email(access) or token_email(id_token)
        identity_subject = _clean_text(subject) or _clean_text(
            identity_claims.get("sub") or identity_claims.get("principal_id")
        )
        if not identity_email and not identity_subject:
            raise ValidationError(
                "OAuth token did not contain an email or subject; provide one when importing.",
                param="email",
                code="identity_missing",
            )
        account = self.store.upsert(
            {
                "email": identity_email,
                "subject": identity_subject,
                "access_token": access,
                "refresh_token": refresh,
                "id_token": _clean_text(id_token),
                "expires_at": datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(),
                "source_type": _clean_text(source_type) or "oauth_import",
                "models": model_ids,
                "metadata": {
                    "last_model_sync_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        )
        probe = await self.probe_account(_clean_text(account["item"].get("id")))
        if probe.get("status") == "invalid" and not _is_permission_pending_probe(probe):
            raise UpstreamError(
                "xAI CLI OAuth account cannot call grok-4.5",
                status=int(probe.get("http_status") or 403),
                body=_clean_text(probe.get("error")),
            )
        return {"account": probe.get("account") or account["item"], "models": model_ids, "probe": probe}

    async def _fetch_models(self, access_token: str, *, proxy: str = "") -> list[str]:
        async with self._client(proxy=proxy) as client:
            response = await client.get(
                f"{XAI_CLI_BASE_URL}/models",
                headers=self._cli_headers(access_token),
            )
        if response.status_code in {401, 403}:
            raise ValidationError("xAI CLI OAuth credential was rejected", param="access_token", code="invalid_credentials")
        if response.status_code != 200:
            raise UpstreamError(
                "xAI CLI model discovery failed",
                status=502,
                body=_safe_error_body(response),
            )
        model_ids = normalize_model_ids(self._json_body(response))
        if not model_ids:
            raise UpstreamError("xAI CLI model discovery returned no models", status=502)
        return model_ids

    async def sync_models(self, account_id: str) -> dict[str, Any]:
        account = self._get_account(account_id)
        account = await self._ensure_access_token(account)
        models = await self._fetch_models(_clean_text(account.get("access_token")), proxy=self._proxy_for(account))
        saved = self.store.set_available_models(_clean_text(account.get("id")), models)
        probe = await self.probe_account(_clean_text(account.get("id")))
        delivery = {}
        if _clean_text(probe.get("status")).lower() == "valid":
            delivery = await self._deliver_oauth_if_needed(_clean_text(account.get("id")))
        return {
            "account": self.store.get(_clean_text(account.get("id")), redacted=True) or probe.get("account") or saved,
            "models": models,
            "probe": probe,
            "delivery": delivery,
        }

    def _get_account(self, account_id: str) -> dict[str, Any]:
        items = self.store.get_accounts_by_ids([account_id])
        if not items:
            raise ValidationError("xAI CLI OAuth account does not exist", param="account_id")
        return items[0]

    @staticmethod
    def _proxy_for(account: dict[str, Any]) -> str:
        # OAuth records must not expose proxy credentials through their
        # redacted metadata.  Reuse the host's existing outbound proxy setting
        # instead of persisting a per-account proxy URL alongside OAuth data.
        return config.get_proxy_settings()

    async def _refresh_lock(self, account_id: str) -> asyncio.Lock:
        async with self._refresh_locks_guard:
            return self._refresh_locks.setdefault(account_id, asyncio.Lock())

    async def _delivery_lock(self, account_id: str) -> asyncio.Lock:
        async with self._delivery_locks_guard:
            return self._delivery_locks.setdefault(account_id, asyncio.Lock())

    @staticmethod
    def _oauth_delivery_config() -> object:
        from services.register_service import register_service

        runtime = register_service.get()
        grok = runtime.get("grok") if isinstance(runtime.get("grok"), dict) else {}
        return grok.get("oauth_delivery")

    async def _deliver_oauth_if_needed(self, account_id: str) -> dict[str, Any]:
        from services.xai_oauth_delivery_service import (
            deliver_xai_oauth_account,
            normalize_xai_oauth_delivery_config,
        )

        clean_id = _clean_text(account_id)
        if not clean_id:
            return {}
        lock = await self._delivery_lock(clean_id)
        async with lock:
            account = self.store.get(clean_id)
            if not isinstance(account, dict):
                return {}
            raw_config = self._oauth_delivery_config()
            settings = normalize_xai_oauth_delivery_config(raw_config)
            metadata = account.get("metadata") if isinstance(account.get("metadata"), dict) else {}
            existing = metadata.get("oauth_delivery") if isinstance(metadata.get("oauth_delivery"), dict) else {}
            pending = {
                name
                for name, target in settings.items()
                if bool(target.get("enabled"))
                and _clean_text((existing.get(name) or {}).get("status")) != "success"
            }
            if not pending:
                return existing

            effective = {
                name: {**target, "enabled": name in pending}
                for name, target in settings.items()
            }
            delivered = await asyncio.to_thread(
                deliver_xai_oauth_account,
                account,
                effective,
            )
            merged = dict(existing)
            for name, result in delivered.items():
                if (
                    _clean_text(result.get("status") if isinstance(result, dict) else "") == "skipped"
                    and _clean_text((existing.get(name) or {}).get("status")) == "success"
                ):
                    continue
                merged[name] = result
            self.store.update_metadata(clean_id, {"oauth_delivery": merged})
            return merged

    async def _ensure_access_token(self, account: dict[str, Any], *, force_refresh: bool = False) -> dict[str, Any]:
        account_id = _clean_text(account.get("id"))
        if not account_id:
            raise ValidationError("xAI CLI OAuth account is missing its id")
        expires_at = _expires_at_epoch(account.get("expires_at"))
        has_access = bool(_clean_text(account.get("access_token")))
        if not force_refresh and has_access and expires_at > _now_epoch() + _REFRESH_EARLY_SECONDS:
            return account

        lock = await self._refresh_lock(account_id)
        async with lock:
            current = self._get_account(account_id)
            current_expiry = _expires_at_epoch(current.get("expires_at"))
            if (
                not force_refresh
                and _clean_text(current.get("access_token"))
                and current_expiry > _now_epoch() + _REFRESH_EARLY_SECONDS
            ):
                return current
            return await self._refresh_account(current)

    async def _refresh_account(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = _clean_text(account.get("id"))
        refresh_token = _clean_text(account.get("refresh_token"))
        if not refresh_token:
            self.store.set_status([account_id], "invalid")
            raise ValidationError("xAI CLI OAuth account needs reauthorization", code="invalid_credentials")
        response = await self._form_post(
            XAI_TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": XAI_OAUTH_CLIENT_ID,
                "refresh_token": refresh_token,
            },
            proxy=self._proxy_for(account),
        )
        payload = self._json_body(response)
        if response.status_code != 200 or not _clean_text(payload.get("access_token")):
            error = _clean_text(payload.get("error"))
            if error in {"invalid_grant", "invalid_token", "unauthorized_client"} or response.status_code in {400, 401, 403}:
                self.store.set_status([account_id], "invalid")
                raise ValidationError("xAI CLI OAuth account needs reauthorization", code="invalid_credentials")
            raise UpstreamError("xAI CLI OAuth token refresh failed", status=502, body=_safe_error_body(response))

        access = _clean_text(payload.get("access_token"))
        expiry = token_expiry_epoch(access, fallback_seconds=int(payload.get("expires_in") or 21_600))
        self.store.update_tokens(
            account_id,
            access_token=access,
            refresh_token=_clean_text(payload.get("refresh_token")) or None,
            id_token=_clean_text(payload.get("id_token")) or None,
            expires_at=datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat(),
        )
        return self._get_account(account_id)

    async def refresh_account(self, account_id: str) -> dict[str, Any]:
        account = self._get_account(account_id)
        refreshed = await self._ensure_access_token(account, force_refresh=True)
        records = self.store.list_accounts(redacted=True)
        redacted = next((item for item in records if _clean_text(item.get("id")) == _clean_text(refreshed.get("id"))), None)
        return {"account": redacted}

    async def test_account(self, account_id: str, *, model: str, prompt: str) -> dict[str, Any]:
        """Run one non-streaming probe through exactly the requested OAuth account."""
        model_id = _clean_text(model)
        prompt_text = _clean_text(prompt)
        if model_id != GROK_45_MODEL_ID:
            raise ValidationError(f"Unsupported xAI CLI OAuth model: {model_id!r}", param="model", code="model_not_found")
        if not prompt_text:
            raise ValidationError("prompt is required", param="prompt")

        account = self._get_account(account_id)
        account_id = _clean_text(account.get("id"))
        started_at = time.monotonic()
        failure_recorded = False
        try:
            account = await self._ensure_access_token(account)
            payload = {
                "model": model_id,
                "input": chat_messages_to_response_input([{"role": "user", "content": prompt_text}]),
                "stream": False,
                "max_output_tokens": 128,
            }
            response = await self._post_response(account, payload)
            if response.status_code in {401, 403}:
                account = await self._ensure_access_token(account, force_refresh=True)
                response = await self._post_response(account, payload)
            if response.status_code >= 400:
                self._mark_response_failure(account, response)
                error = f"HTTP {response.status_code}: {_safe_error_body(response)}"
                self.store.record_result(account_id, False, error)
                failure_recorded = True
                raise UpstreamError(
                    "xAI CLI account test failed",
                    status=response.status_code,
                    body=_safe_error_body(response),
                )

            data = self._json_body(response)
            completion = response_to_chat_completion(data, model=model_id)
            choices = completion.get("choices") if isinstance(completion.get("choices"), list) else []
            message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
            content = _clean_text(message.get("content") if isinstance(message, dict) else "")
            if not content:
                raise UpstreamError("xAI CLI account test returned no text", status=502)

            self.store.record_result(account_id, True)
            if _clean_text(account.get("status")).lower() not in {"active", "disabled"}:
                self.store.set_status([account_id], "active")
            return {
                "account_id": account_id,
                "account": self.store.get(account_id, redacted=True),
                "model": model_id,
                "content": content,
                "elapsed_ms": max(0, round((time.monotonic() - started_at) * 1000)),
            }
        except Exception as exc:
            if not failure_recorded:
                self.store.record_result(account_id, False, _clean_text(exc) or type(exc).__name__)
            raise

    async def probe_account(self, account_id: str, *, persist: bool = True) -> dict[str, Any]:
        """Probe one OAuth account with the real model and persist safe quota headers."""
        account = self._get_account(account_id)
        account_id = _clean_text(account.get("id"))
        probed_at = datetime.now(timezone.utc).isoformat()
        response: httpx.Response | None = None

        def finish(result: dict[str, Any]) -> dict[str, Any]:
            result["probed_at"] = probed_at
            result["account"] = (
                self.store.update_probe_result(
                    account_id,
                    status=_clean_text(result.get("status")),
                    model=GROK_45_MODEL_ID,
                    http_status=int(result.get("http_status") or 0),
                    code=_clean_text(result.get("code")),
                    error=_clean_text(result.get("error")),
                    quota=result.get("quota") if isinstance(result.get("quota"), dict) else {},
                    usage=result.get("usage") if isinstance(result.get("usage"), dict) else {},
                    probed_at=probed_at,
                )
                if persist
                else None
            )
            return result

        try:
            account = await self._ensure_access_token(account)
            payload = {
                "model": GROK_45_MODEL_ID,
                "input": chat_messages_to_response_input([{"role": "user", "content": "Reply only OK."}]),
                "stream": False,
                "max_output_tokens": 8,
            }
            response = await self._post_response(account, payload)
            if response.status_code in {401, 403}:
                account = await self._ensure_access_token(account, force_refresh=True)
                response = await self._post_response(account, payload)

            code, error = _response_error_fields(response)
            if 200 <= response.status_code < 300:
                status = "valid"
            elif response.status_code == 429:
                status = "limited"
            elif response.status_code in {401, 403} or (
                response.status_code == 402 and _is_personal_team_blocked(code)
            ):
                status = "invalid"
            else:
                status = "unknown"
            quota = _response_quota(response)
            usage = _response_probe_usage(response)
            return finish({
                "account_id": account_id,
                "status": status,
                "model": GROK_45_MODEL_ID,
                "http_status": response.status_code,
                "code": code,
                "error": error,
                "quota": quota,
                "usage": usage,
            })
        except ValidationError as exc:
            error = _clean_text(exc) or "OAuth credential is invalid"
            return finish({
                "account_id": account_id,
                "status": "invalid",
                "model": GROK_45_MODEL_ID,
                "http_status": 0,
                "code": "invalid_credentials",
                "error": error,
                "quota": {},
                "usage": {},
            })
        except Exception as exc:
            error = _clean_text(exc) or type(exc).__name__
            return finish({
                "account_id": account_id,
                "status": "unknown",
                "model": GROK_45_MODEL_ID,
                "http_status": response.status_code if response is not None else 0,
                "code": "",
                "error": error,
                "quota": {},
                "usage": {},
            })

    async def probe_accounts(self, account_ids: list[str], *, concurrency: int = 10) -> dict[str, Any]:
        ordered_ids = list(dict.fromkeys(_clean_text(value) for value in account_ids if _clean_text(value)))
        semaphore = asyncio.Semaphore(max(1, min(25, int(concurrency or 1))))

        async def run(account_id: str) -> dict[str, Any]:
            async with semaphore:
                return await self.probe_account(account_id, persist=False)

        results = await asyncio.gather(*(run(account_id) for account_id in ordered_ids))
        saved_by_id = {
            _clean_text(item.get("id")): item
            for item in self.store.update_probe_results(results)
            if _clean_text(item.get("id"))
        }
        summary = {"total": len(results), "valid": 0, "limited": 0, "invalid": 0, "unknown": 0}
        for result in results:
            result["account"] = saved_by_id.get(_clean_text(result.get("account_id")))
            status = _clean_text(result.get("status")).lower()
            summary[status if status in {"valid", "limited", "invalid", "unknown"} else "unknown"] += 1
        valid_results = [
            result
            for result in results
            if _clean_text(result.get("status")).lower() == "valid"
        ]

        async def deliver_valid(result: dict[str, Any]) -> None:
            account_id = _clean_text(result.get("account_id"))
            result["delivery"] = await self._deliver_oauth_if_needed(account_id)
            result["account"] = self.store.get(account_id, redacted=True)

        await asyncio.gather(*(deliver_valid(result) for result in valid_results))
        summary["delivery_checked"] = len(valid_results)
        summary["delivery_success"] = sum(
            1
            for result in valid_results
            if any(
                isinstance(item, dict) and _clean_text(item.get("status")) == "success"
                for item in (result.get("delivery") or {}).values()
            )
        )
        return {"results": results, "summary": summary}

    async def create_response(
        self,
        payload: dict[str, Any],
        *,
        on_account_selected: AccountSelectedCallback | None = None,
    ) -> dict[str, Any] | AsyncGenerator[str, None]:
        model = _clean_text(payload.get("model"))
        if model != GROK_45_MODEL_ID:
            raise ValidationError(f"Unsupported xAI CLI OAuth model: {model!r}", param="model", code="model_not_found")
        if not self.supports_model(model):
            raise RateLimitError("No active xAI CLI OAuth account exposes grok-4.5")
        if bool(payload.get("stream")):
            return self._stream_response(dict(payload), on_account_selected=on_account_selected)
        return await self._nonstream_response(dict(payload), on_account_selected=on_account_selected)

    async def _select_ready_account(self, *, exclude_ids: list[str] | None = None, force_refresh: bool = False) -> dict[str, Any]:
        account = self.store.select_next_account(exclude_ids=exclude_ids)
        if account is None:
            raise RateLimitError("No active xAI CLI OAuth account is available")
        return await self._ensure_access_token(account, force_refresh=force_refresh)

    @staticmethod
    def _report_selected_account(account: dict[str, Any], callback: AccountSelectedCallback | None) -> None:
        if callback is None:
            return
        callback(account_log_identity(account))

    async def _nonstream_response(
        self,
        payload: dict[str, Any],
        *,
        on_account_selected: AccountSelectedCallback | None = None,
    ) -> dict[str, Any]:
        account = await self._select_ready_account()
        self._report_selected_account(account, on_account_selected)
        response = await self._post_response(account, payload)
        if response.status_code in {401, 403}:
            account = await self._ensure_access_token(account, force_refresh=True)
            response = await self._post_response(account, payload)
        if response.status_code >= 400:
            self._mark_response_failure(account, response)
            self.store.record_result(
                _clean_text(account.get("id")),
                False,
                f"HTTP {response.status_code}: {_safe_error_body(response)}",
            )
            raise UpstreamError("xAI CLI response request failed", status=response.status_code, body=_safe_error_body(response))
        data = self._json_body(response)
        self.store.record_result(_clean_text(account.get("id")), True)
        return data

    async def _post_response(self, account: dict[str, Any], payload: dict[str, Any]) -> httpx.Response:
        async with self._client(proxy=self._proxy_for(account), timeout=120.0) as client:
            return await client.post(
                f"{XAI_CLI_BASE_URL}/responses",
                json=payload,
                headers=self._cli_headers(_clean_text(account.get("access_token")), content_type=True),
            )

    def _mark_response_failure(self, account: dict[str, Any], response: httpx.Response) -> None:
        code, _error = _response_error_fields(response)
        if response.status_code in {401, 403} or (
            response.status_code == 402 and _is_personal_team_blocked(code)
        ):
            self.store.set_status([_clean_text(account.get("id"))], "invalid")

    async def _stream_response(
        self,
        payload: dict[str, Any],
        *,
        on_account_selected: AccountSelectedCallback | None = None,
    ) -> AsyncGenerator[str, None]:
        account = await self._select_ready_account()
        self._report_selected_account(account, on_account_selected)
        for attempt in range(2):
            async with self._client(proxy=self._proxy_for(account), timeout=180.0) as client:
                async with client.stream(
                    "POST",
                    f"{XAI_CLI_BASE_URL}/responses",
                    json=payload,
                    headers=self._cli_headers(_clean_text(account.get("access_token")), content_type=True),
                ) as response:
                    if response.status_code in {401, 403} and attempt == 0:
                        account = await self._ensure_access_token(account, force_refresh=True)
                        continue
                    if response.status_code >= 400:
                        self._mark_response_failure(account, response)
                        error_body = (await response.aread()).decode("utf-8", errors="replace")[:_ERROR_BODY_LIMIT]
                        self.store.record_result(
                            _clean_text(account.get("id")),
                            False,
                            f"HTTP {response.status_code}: {error_body}",
                        )
                        raise UpstreamError(
                            "xAI CLI streaming response request failed",
                            status=response.status_code,
                            body=error_body,
                        )
                    async for chunk in response.aiter_text():
                        if chunk:
                            yield chunk
                    self.store.record_result(_clean_text(account.get("id")), True)
                    return

    @staticmethod
    async def _iter_sse_events(stream: AsyncIterable[str]) -> AsyncGenerator[tuple[str, dict[str, Any] | None], None]:
        """Parse line-framed SSE while tolerating proxy chunk boundaries."""
        buffer = ""
        event = ""
        data_lines: list[str] = []
        async for chunk in stream:
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.rstrip("\r")
                if not line:
                    if data_lines:
                        raw = "\n".join(data_lines)
                        if raw != "[DONE]":
                            try:
                                payload = orjson.loads(raw)
                            except orjson.JSONDecodeError:
                                payload = None
                            yield event, payload if isinstance(payload, dict) else None
                    event = ""
                    data_lines = []
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
        if data_lines:
            raw = "\n".join(data_lines)
            if raw != "[DONE]":
                try:
                    payload = orjson.loads(raw)
                except orjson.JSONDecodeError:
                    payload = None
                yield event, payload if isinstance(payload, dict) else None

    async def create_chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        on_account_selected: AccountSelectedCallback | None = None,
    ) -> dict[str, Any] | AsyncGenerator[str, None]:
        request: dict[str, Any] = {
            "model": model,
            "input": chat_messages_to_response_input(messages),
            "stream": stream,
        }
        if temperature is not None:
            request["temperature"] = temperature
        if top_p is not None:
            request["top_p"] = top_p
        if max_tokens is not None:
            request["max_output_tokens"] = max_tokens
        if tools:
            request["tools"] = tools
        if tool_choice is not None:
            request["tool_choice"] = tool_choice
        result = await self.create_response(request, on_account_selected=on_account_selected)
        if isinstance(result, dict):
            return response_to_chat_completion(result, model=model)
        return self._chat_stream(model=model, response_stream=result)

    async def _chat_stream(self, *, model: str, response_stream: AsyncIterable[str]) -> AsyncGenerator[str, None]:
        response_id = f"chatcmpl_{uuid.uuid4().hex}"
        created = _now_epoch()
        yielded_role = False
        final_usage: dict[str, int] | None = None
        async for event, payload in self._iter_sse_events(response_stream):
            kind = event or _clean_text(payload.get("type")) if isinstance(payload, dict) else event
            if not isinstance(payload, dict):
                continue
            if kind == "response.output_text.delta":
                delta = _clean_text(payload.get("delta"))
                if not delta:
                    continue
                if not yielded_role:
                    role = {"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
                    yield f"data: {orjson.dumps(role).decode()}\n\n"
                    yielded_role = True
                chunk = {"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]}
                yield f"data: {orjson.dumps(chunk).decode()}\n\n"
            elif kind == "response.completed":
                source = payload.get("response") if isinstance(payload.get("response"), dict) else payload
                final_usage = response_usage(source)
            elif kind == "error":
                message = _clean_text(payload.get("message") or payload.get("error")) or "xAI CLI stream failed"
                raise UpstreamError(message, status=502)
        final = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        if final_usage:
            final["usage"] = final_usage
        yield f"data: {orjson.dumps(final).decode()}\n\n"
        yield "data: [DONE]\n\n"

    async def create_anthropic_message(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        system: object,
        stream: bool,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        on_account_selected: AccountSelectedCallback | None = None,
    ) -> dict[str, Any] | AsyncGenerator[str, None]:
        request: dict[str, Any] = {
            "model": model,
            "input": anthropic_messages_to_response_input(messages, system),
            "stream": stream,
        }
        if temperature is not None:
            request["temperature"] = temperature
        if top_p is not None:
            request["top_p"] = top_p
        if max_tokens is not None:
            request["max_output_tokens"] = max_tokens
        if tools:
            request["tools"] = tools
        if tool_choice is not None:
            request["tool_choice"] = tool_choice
        result = await self.create_response(request, on_account_selected=on_account_selected)
        if isinstance(result, dict):
            return response_to_anthropic_message(result, model=model)
        return self._anthropic_stream(model=model, response_stream=result)

    async def _anthropic_stream(self, *, model: str, response_stream: AsyncIterable[str]) -> AsyncGenerator[str, None]:
        message_id = f"msg_{uuid.uuid4().hex}"
        started = False
        input_tokens = 0
        output_tokens = 0

        async for event, payload in self._iter_sse_events(response_stream):
            kind = event or _clean_text(payload.get("type")) if isinstance(payload, dict) else event
            if not isinstance(payload, dict):
                continue
            if kind == "response.output_text.delta":
                delta = _clean_text(payload.get("delta"))
                if not delta:
                    continue
                if not started:
                    started = True
                    yield _sse("message_start", {"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
                    yield _sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta}})
            elif kind == "response.completed":
                source = payload.get("response") if isinstance(payload.get("response"), dict) else payload
                usage = response_usage(source)
                input_tokens = usage["prompt_tokens"]
                output_tokens = usage["completion_tokens"]
            elif kind == "error":
                message = _clean_text(payload.get("message") or payload.get("error")) or "xAI CLI stream failed"
                raise UpstreamError(message, status=502)

        if not started:
            yield _sse("message_start", {"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
            yield _sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": output_tokens}})
        yield _sse("message_stop", {"type": "message_stop"})


xai_cli_oauth_service = XaiCliOAuthService()


__all__ = ["XaiCliOAuthService", "xai_cli_oauth_service"]
