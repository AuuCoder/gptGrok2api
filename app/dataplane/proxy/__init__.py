"""ProxyRuntime — thin hot-path wrapper around ProxyDirectory.

Delegates acquisition and feedback to the control-plane ProxyDirectory.
Kept as a thin shim so callers in the dataplane need not import control
modules directly.
"""

from app.control.proxy import ProxyDirectory, get_proxy_directory
from app.control.proxy.models import (
    ProxyLease, ProxyFeedback, ProxyScope, RequestKind,
)


class ProxyRuntime:
    """Hot-path facade around ProxyDirectory."""

    def __init__(self, directory: ProxyDirectory) -> None:
        self._dir = directory

    async def acquire(
        self,
        *,
        scope:    ProxyScope  = ProxyScope.APP,
        kind:     RequestKind = RequestKind.HTTP,
        resource: bool        = False,
        clearance_origin: str | None = None,
    ) -> ProxyLease:
        return await self._dir.acquire(
            scope=scope,
            kind=kind,
            resource=resource,
            clearance_origin=clearance_origin,
        )

    async def feedback(self, lease: ProxyLease, result: ProxyFeedback) -> None:
        await self._dir.feedback(lease, result)

    @property
    def has_proxy(self) -> bool:
        from app.control.proxy.models import EgressMode
        return self._dir.egress_mode != EgressMode.DIRECT


_runtime: ProxyRuntime | None = None


async def get_proxy_runtime() -> ProxyRuntime:
    global _runtime
    directory = await get_proxy_directory()
    if _runtime is None:
        _runtime  = ProxyRuntime(directory)
    return _runtime


__all__ = ["ProxyRuntime", "get_proxy_runtime"]
