"""gRPC-Web POST transport.

Sends a pre-framed gRPC-Web request to *url* and returns the parsed
``(messages, trailers)`` tuple.  Proxy lease is accepted from outside so
the caller controls acquisition and feedback.
"""

from typing import Dict, List, Tuple

from app.platform.logging.logger import logger
from app.platform.errors import UpstreamError
from app.platform.net.grpc import GrpcClient
from app.control.proxy.models import ProxyLease
from app.dataplane.proxy.adapters.headers import build_http_headers
from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs

# Headers required by every gRPC-Web call.
_GRPC_WEB_HEADERS: Dict[str, str] = {
    "Content-Type": "application/grpc-web+proto",
    "Accept":       "*/*",
    "x-grpc-web":   "1",
    "x-user-agent": "connect-es/2.1.1",
    "Cache-Control": "no-cache",
    "Pragma":       "no-cache",
    "Sec-Fetch-Dest": "empty",
}


async def post_grpc_web(
    url:       str,
    token:     str,
    payload:   bytes,
    *,
    lease:     ProxyLease | None      = None,
    timeout_s: float                  = 30.0,
    origin:    str                    = "https://grok.com",
    referer:   str                    = "https://grok.com/",
    session:   "ResettableSession | None" = None,
) -> Tuple[List[bytes], Dict[str, str]]:
    """POST a gRPC-Web frame to *url*.

    Pass *session* to reuse an existing connection (avoids a new TLS handshake).
    When *session* is ``None`` a fresh session is created and closed automatically.

    Returns:
        ``(messages, trailers)`` — raw protobuf message payloads and the
        gRPC trailer map (includes ``grpc-status``, ``grpc-message``).

    Raises:
        ``UpstreamError`` if the HTTP response status is not 200.
    """
    headers = build_http_headers(
        token,
        content_type = "application/grpc-web+proto",
        origin       = origin,
        referer      = referer,
        lease        = lease,
    )
    headers.update(_GRPC_WEB_HEADERS)

    async def _do(s: "ResettableSession") -> Tuple[List[bytes], Dict[str, str]]:
        response = await s.post(url, headers=headers, data=payload, timeout=timeout_s)
        body_bytes = response.content
        if response.status_code != 200:
            body_text = body_bytes.decode("utf-8", "replace")[:300]
            logger.error("grpc-web post failed: url={} status={} body={}", url, response.status_code, body_text)
            raise UpstreamError(f"Upstream returned {response.status_code}", status=response.status_code, body=body_text)
        return GrpcClient.parse_response(body_bytes, content_type=response.headers.get("content-type"), headers=response.headers)

    if session is not None:
        return await _do(session)

    async with ResettableSession(**build_session_kwargs(lease=lease)) as s:
        return await _do(s)


__all__ = ["post_grpc_web"]
