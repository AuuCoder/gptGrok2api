"""Host-authenticated administration endpoints for the embedded Grok runtime.

The Grok runtime ships its own admin router under ``/admin/api`` and protects it
with its separate ``app.app_key``.  Reusing that router directly would create a
second administrator credential in chatgpt2api.  This adapter republishes the
same handlers under a host-owned API namespace and replaces only that legacy
router dependency with chatgpt2api's ``require_admin`` check.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from fastapi.params import Depends as DependsParam
from fastapi.routing import APIRoute

from api.support import require_admin

_HOST_PREFIX = "/api/grok/runtime/admin"
_UPSTREAM_PREFIX = "/admin/api"


async def require_grok_runtime_admin(
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """Authorize embedded-runtime administration with the host admin session."""
    return require_admin(authorization)


def _rebased_path(source_route: APIRoute) -> str:
    """Translate the upstream admin path without exposing its old namespace."""
    if not source_route.path.startswith(_UPSTREAM_PREFIX):
        raise RuntimeError(f"Unexpected embedded Grok admin route: {source_route.path}")
    suffix = source_route.path.removeprefix(_UPSTREAM_PREFIX)
    return suffix or "/"


def _host_dependencies(source_route: APIRoute) -> list[DependsParam]:
    """Keep future upstream route dependencies except the legacy app-key guard."""
    from app.platform.auth.middleware import verify_admin_key

    dependencies: list[DependsParam] = [Depends(require_grok_runtime_admin)]
    dependencies.extend(
        dependency
        for dependency in source_route.dependencies
        if getattr(dependency, "dependency", None) is not verify_admin_key
    )
    return dependencies


def _add_rebased_route(router: APIRouter, source_route: APIRoute) -> None:
    """Copy FastAPI route metadata while regenerating its dependant graph."""
    router.add_api_route(
        _rebased_path(source_route),
        source_route.endpoint,
        response_model=source_route.response_model,
        status_code=source_route.status_code,
        tags=source_route.tags,
        dependencies=_host_dependencies(source_route),
        summary=source_route.summary,
        description=source_route.description,
        response_description=source_route.response_description,
        responses=source_route.responses,
        deprecated=source_route.deprecated,
        methods=source_route.methods,
        operation_id=source_route.operation_id,
        response_model_include=source_route.response_model_include,
        response_model_exclude=source_route.response_model_exclude,
        response_model_by_alias=source_route.response_model_by_alias,
        response_model_exclude_unset=source_route.response_model_exclude_unset,
        response_model_exclude_defaults=source_route.response_model_exclude_defaults,
        response_model_exclude_none=source_route.response_model_exclude_none,
        include_in_schema=source_route.include_in_schema,
        response_class=source_route.response_class,
        name=source_route.name,
        route_class_override=type(source_route),
        callbacks=source_route.callbacks,
        openapi_extra=source_route.openapi_extra,
        generate_unique_id_function=source_route.generate_unique_id_function,
        strict_content_type=source_route.strict_content_type,
    )


def create_router(
    *,
    prefix: str = _HOST_PREFIX,
    include_in_schema: bool = True,
) -> APIRouter:
    """Create the isolated Grok runtime admin router.

    Runtime repository and refresh-service injection stays in the copied Grok
    handlers, which resolve them from the host FastAPI application's state.
    ``EmbeddedGrokRuntime.lifespan`` initializes that state before requests are
    accepted.
    """
    from app.products.web.admin import router as upstream_admin_router

    router = APIRouter(
        prefix=prefix.rstrip("/"),
        tags=["Grok Runtime Admin"],
        include_in_schema=include_in_schema,
    )
    for source_route in upstream_admin_router.routes:
        if isinstance(source_route, APIRoute):
            _add_rebased_route(router, source_route)
    return router


__all__ = ["create_router", "require_grok_runtime_admin"]
