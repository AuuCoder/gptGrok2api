from __future__ import annotations

from contextlib import asynccontextmanager
import os
from threading import Event

from anyio.to_thread import current_default_thread_limiter
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api import (
    accounts,
    ai,
    checkout,
    grok,
    grok_admin,
    grok_oauth,
    grok_web,
    icloud_privacy_mail,
    image_tasks,
    prompts,
    register,
    system,
)
from api.errors import install_exception_handlers
from api.support import resolve_image_base_url, resolve_web_asset, start_limited_account_watcher
from app.platform.request_context import (
    reset_media_base_url,
    set_media_base_url,
)
from services.backup_service import backup_service
from services.config import config
from services.dashboard_metrics_service import dashboard_metrics_service
from services.image_service import start_image_cleanup_scheduler
from services.log_service import cleanup_old_logs, start_log_cleanup_scheduler
from services.grok_runtime import grok_runtime
from services.realtime_monitor_service import realtime_monitor_service
from utils.log import logger


def _env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(str(os.getenv(name, "") or default).strip())
    except (TypeError, ValueError):
        value = default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _configure_threadpool() -> None:
    tokens = _env_int("CHATGPT2API_THREAD_TOKENS", 80, 1)
    limiter = current_default_thread_limiter()
    previous = int(getattr(limiter, "total_tokens", 0) or 0)
    if previous != tokens:
        limiter.total_tokens = tokens
    realtime_monitor_service.set_threadpool(tokens=tokens, previous_tokens=previous)
    logger.info({
        "event": "runtime_threadpool_configured",
        "previous_tokens": previous,
        "tokens": tokens,
    })


def create_app() -> FastAPI:
    app_version = config.app_version

    @asynccontextmanager
    async def lifespan(host_app: FastAPI):
        _configure_threadpool()
        async with grok_runtime.lifespan(host_app):
            stop_event = Event()
            thread = start_limited_account_watcher(stop_event)
            cleanup_thread = start_image_cleanup_scheduler(stop_event)
            log_cleanup_thread = start_log_cleanup_scheduler(stop_event)
            backup_service.start()
            config.cleanup_old_images()
            cleanup_old_logs()
            try:
                yield
            finally:
                stop_event.set()
                thread.join(timeout=1)
                cleanup_thread.join(timeout=1)
                log_cleanup_thread.join(timeout=1)
                try:
                    dashboard_metrics_service.flush()
                except Exception as exc:
                    logger.error({"event": "dashboard_metrics_shutdown_flush_failed", "error": str(exc)})
                backup_service.stop()

    app = FastAPI(title="GPTGrok2API", version=app_version, lifespan=lifespan)
    install_exception_handlers(app)
    grok.install_exception_handlers(app)

    @app.middleware("http")
    async def bind_embedded_media_base_url(request, call_next):
        """Expose the public host to embedded media URL resolvers.

        A streaming body starts after the route handler returns, so its iterator
        is wrapped as well. This keeps local image/video URLs bound to the
        request that created them without changing global runtime config.
        """
        base_url = resolve_image_base_url(request)
        token = set_media_base_url(base_url)
        try:
            response = await call_next(request)
        finally:
            reset_media_base_url(token)

        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is not None:
            async def scoped_body():
                stream_token = set_media_base_url(base_url)
                try:
                    async for chunk in body_iterator:
                        yield chunk
                finally:
                    reset_media_base_url(stream_token)

            response.body_iterator = scoped_body()
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(ai.create_router())
    app.include_router(grok.create_router())
    app.include_router(grok_admin.create_router())
    app.include_router(grok_oauth.create_router())
    # Keep Grok2API's original admin pages usable without creating a second
    # admin credential; the same handlers are protected by host admin auth.
    app.include_router(grok_admin.create_router(prefix="/admin/api", include_in_schema=False))
    app.include_router(grok_web.create_router())
    app.include_router(accounts.create_router())
    app.include_router(checkout.create_router())
    app.include_router(image_tasks.create_router())
    app.include_router(prompts.create_router())
    app.include_router(register.create_router())
    app.include_router(icloud_privacy_mail.create_router())
    app.include_router(system.create_router(app_version))

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_web(full_path: str):
        asset = resolve_web_asset(full_path)
        if asset is None:
            raise HTTPException(status_code=404, detail="Not Found")
        response = FileResponse(asset)
        if asset.name == "index.html":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        elif full_path.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    return app
