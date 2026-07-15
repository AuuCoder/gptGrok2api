"""curl_cffi session builder for reverse-proxy requests."""

import asyncio
from typing import Any
from urllib.parse import urlparse

from curl_cffi.const import CurlOpt

from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError
from app.control.proxy.models import ProxyLease
from app.dataplane.proxy.adapters.profile import resolve_proxy_profile


def _skip_proxy_ssl(proxy_url: str) -> bool:
    if not proxy_url:
        return False
    cfg = get_config()
    return cfg.get_bool("proxy.egress.skip_ssl_verify", False)


def normalize_proxy_url(url: str) -> str:
    """Normalize SOCKS schemes for consistent DNS-over-proxy behaviour."""
    if not url:
        return url
    scheme = urlparse(url).scheme.lower()
    if scheme == "socks":
        return "socks5h://" + url[len("socks://") :]
    if scheme == "socks5":
        return "socks5h://" + url[len("socks5://") :]
    if scheme == "socks4":
        return "socks4a://" + url[len("socks4://") :]
    return url


def build_session_kwargs(
    *,
    lease: ProxyLease | None = None,
    browser_override: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build kwargs suitable for ``curl_cffi.requests.AsyncSession``."""
    kwargs: dict[str, Any] = dict(extra or {})

    # Browser impersonation.
    if not kwargs.get("impersonate"):
        browser = browser_override or resolve_proxy_profile(lease).browser
        if browser:
            kwargs["impersonate"] = browser

    # Proxy URL.
    proxy_url = ""
    if lease is not None and lease.proxy_url:
        proxy_url = normalize_proxy_url(lease.proxy_url)
        scheme = urlparse(proxy_url).scheme.lower()
        if scheme.startswith("socks"):
            kwargs.setdefault("proxy", proxy_url)
        else:
            kwargs.setdefault("proxies", {"http": proxy_url, "https": proxy_url})

    # curl SSL options for proxy.
    if _skip_proxy_ssl(proxy_url):
        opts = dict(kwargs.get("curl_options") or {})
        opts[CurlOpt.PROXY_SSL_VERIFYPEER] = 0
        opts[CurlOpt.PROXY_SSL_VERIFYHOST] = 0
        kwargs["curl_options"] = opts

    return kwargs


def _wrap_transport_error(exc: BaseException) -> UpstreamError:
    if isinstance(exc, UpstreamError):
        return exc
    body = str(exc).replace("\n", "\\n")[:400]
    return UpstreamError(
        f"Transport request failed: {exc}",
        status=502,
        body=body,
    )


class ResettableSession:
    """AsyncSession wrapper that resets connection on configurable status codes.

    Designed for long-lived hot-path use; session is recreated transparently
    when a reset-triggering status code is received.
    """

    def __init__(
        self,
        *,
        lease: ProxyLease | None = None,
        browser_override: str | None = None,
        reset_on_status: set[int] | None = None,
        **session_kwargs: Any,
    ) -> None:
        self._kwargs = build_session_kwargs(
            lease=lease,
            browser_override=browser_override,
            extra=session_kwargs or None,
        )
        if reset_on_status is None:
            codes = get_config().get_list("retry.reset_session_status_codes", [403])
            reset_on_status = {int(c) for c in codes}
        self._reset_on = reset_on_status
        self._reset_pending = False
        self._lock = asyncio.Lock()
        self._session = self._create()

    def _create(self):
        from curl_cffi.requests import AsyncSession

        return AsyncSession(**self._kwargs)

    async def _maybe_reset(self) -> None:
        if not self._reset_pending:
            return
        async with self._lock:
            if not self._reset_pending:
                return
            self._reset_pending = False
            old, self._session = self._session, self._create()
            try:
                await old.close()
            except Exception:
                pass

    async def _request(self, method: str, *args: Any, **kwargs: Any):
        await self._maybe_reset()
        try:
            response = await getattr(self._session, method)(*args, **kwargs)
        except Exception as exc:
            self._reset_pending = True
            raise _wrap_transport_error(exc) from exc
        if self._reset_on and response.status_code in self._reset_on:
            self._reset_pending = True
        return response

    async def get(self, *args: Any, **kwargs: Any):
        return await self._request("get", *args, **kwargs)

    async def post(self, *args: Any, **kwargs: Any):
        return await self._request("post", *args, **kwargs)

    async def delete(self, *args: Any, **kwargs: Any):
        return await self._request("delete", *args, **kwargs)

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            finally:
                self._session = None  # type: ignore[assignment]

    async def __aenter__(self) -> "ResettableSession":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


__all__ = [
    "ResettableSession",
    "build_session_kwargs",
    "normalize_proxy_url",
]
