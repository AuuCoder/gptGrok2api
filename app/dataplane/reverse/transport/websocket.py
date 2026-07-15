"""WebSocket transport with proxy and SOCKS support."""

import ssl
from typing import Any, Awaitable, Callable, Mapping, Optional
from urllib.parse import urlparse

import aiohttp
import certifi
from aiohttp_socks import ProxyConnector

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.control.proxy.models import ProxyLease


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(certifi.where())
    return ctx


def _normalize_socks(proxy_url: str) -> tuple[str, Optional[bool]]:
    scheme = urlparse(proxy_url).scheme.lower()
    rdns: Optional[bool] = None
    base = scheme
    if scheme == "socks":
        base, rdns = "socks5", True
    elif scheme == "socks5h":
        base, rdns = "socks5", True
    elif scheme == "socks4a":
        base, rdns = "socks4", True
    if base != scheme:
        proxy_url = proxy_url.replace(f"{scheme}://", f"{base}://", 1)
    return proxy_url, rdns


def _build_connector(
    proxy_url: str,
    ssl_ctx:   ssl.SSLContext,
) -> tuple[aiohttp.BaseConnector, str | None]:
    if not proxy_url:
        return aiohttp.TCPConnector(ssl=ssl_ctx), None
    scheme = urlparse(proxy_url).scheme.lower()
    if scheme.startswith("socks"):
        normalized, rdns = _normalize_socks(proxy_url)
        kwargs: dict = {"ssl": ssl_ctx}
        if rdns is not None:
            kwargs["rdns"] = rdns
        logger.debug("websocket connector selected: proxy_type=socks proxy_url={}", proxy_url)
        return ProxyConnector.from_url(normalized, **kwargs), None
    logger.debug("websocket connector selected: proxy_type=http proxy_url={}", proxy_url)
    return aiohttp.TCPConnector(ssl=ssl_ctx), proxy_url


class WebSocketConnection:
    """Wraps aiohttp WebSocketResponse with lifecycle cleanup."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        ws:      aiohttp.ClientWebSocketResponse,
        *,
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.session  = session
        self.ws       = ws
        self._on_close = on_close

    async def close(self) -> None:
        if not self.ws.closed:
            await self.ws.close()
        await self.session.close()
        if self._on_close:
            await self._on_close()
            self._on_close = None

    async def __aenter__(self) -> aiohttp.ClientWebSocketResponse:
        return self.ws

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


class WebSocketClient:
    """Establish WebSocket connections through optional proxy."""

    def __init__(self, proxy: str | None = None) -> None:
        self._proxy_override = proxy
        self._ssl = _ssl_ctx()

    async def connect(
        self,
        url:       str,
        headers:   Optional[Mapping[str, str]] = None,
        timeout:   Optional[float]             = None,
        ws_kwargs: Optional[Mapping[str, Any]] = None,
        *,
        lease:    ProxyLease | None                      = None,
        on_close: Callable[[], Awaitable[None]] | None   = None,
    ) -> WebSocketConnection:
        proxy_url  = self._proxy_override or (lease.proxy_url if lease else "")
        connector, http_proxy = _build_connector(proxy_url, self._ssl)

        cfg           = get_config()
        total_s       = float(timeout) if timeout is not None else cfg.get_float("voice.timeout", 120.0)
        client_timeout = aiohttp.ClientTimeout(total=total_s)
        session        = aiohttp.ClientSession(connector=connector, timeout=client_timeout)

        try:
            extra: dict[str, Any] = dict(ws_kwargs or {})
            skip_ssl = cfg.get_bool("proxy.egress.skip_ssl_verify", False) and bool(proxy_url)

            if skip_ssl and urlparse(proxy_url).scheme.lower() == "https":
                proxy_ssl = ssl.create_default_context()
                proxy_ssl.check_hostname = False
                proxy_ssl.verify_mode    = ssl.CERT_NONE
                try:
                    ws = await session.ws_connect(
                        url, headers=headers, proxy=http_proxy,
                        ssl=self._ssl, proxy_ssl=proxy_ssl, **extra,
                    )
                except TypeError:
                    ws = await session.ws_connect(
                        url, headers=headers, proxy=http_proxy,
                        ssl=self._ssl, **extra,
                    )
            else:
                ws = await session.ws_connect(
                    url, headers=headers, proxy=http_proxy,
                    ssl=self._ssl, **extra,
                )

            return WebSocketConnection(session, ws, on_close=on_close)
        except Exception:
            await session.close()
            raise


__all__ = ["WebSocketClient", "WebSocketConnection"]
