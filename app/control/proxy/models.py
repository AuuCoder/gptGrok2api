"""Control-plane proxy domain models."""

from enum import IntEnum, StrEnum
from typing import Self

from pydantic import BaseModel


class ProxyScope(StrEnum):
    APP   = "app"    # grok.com API calls
    ASSET = "asset"  # static asset / CDN fetches


class RequestKind(StrEnum):
    HTTP      = "http"
    WEBSOCKET = "websocket"
    GRPC      = "grpc"


class EgressMode(StrEnum):
    DIRECT       = "direct"        # no proxy
    SINGLE_PROXY = "single_proxy"  # one fixed proxy URL
    PROXY_POOL   = "proxy_pool"    # rotate from a pool


class ClearanceMode(StrEnum):
    NONE         = "none"         # no CF clearance required
    MANUAL       = "manual"       # operator-supplied cf_cookies
    FLARESOLVERR = "flaresolverr" # maintained by FlareSolverr

    @classmethod
    def parse(cls, value: str | Self) -> Self:
        if isinstance(value, cls):
            return value
        normalized = str(value or "").strip().lower()
        if not normalized:
            return cls.NONE
        return cls(normalized)


class EgressNodeState(IntEnum):
    HEALTHY   = 0
    DEGRADED  = 1
    UNHEALTHY = 2


class ClearanceBundleState(IntEnum):
    VALID    = 0
    STALE    = 1
    INVALID  = 2


class ProxyFeedbackKind(StrEnum):
    SUCCESS         = "success"
    CHALLENGE       = "challenge"    # CF JS challenge / captcha
    UNAUTHORIZED    = "unauthorized" # 401 on proxy auth
    FORBIDDEN       = "forbidden"    # 403 not CF-related
    RATE_LIMITED    = "rate_limited" # 429
    UPSTREAM_5XX    = "upstream_5xx"
    TRANSPORT_ERROR = "transport_error"


class EgressNode(BaseModel):
    node_id:    str
    proxy_url:  str | None       = None  # None → direct
    scope:      ProxyScope       = ProxyScope.APP
    state:      EgressNodeState  = EgressNodeState.HEALTHY
    health:     float            = 1.0
    inflight:   int              = 0
    last_used:  int | None       = None  # ms


class ClearanceBundle(BaseModel):
    bundle_id:       str
    cf_cookies:      str            = ""
    user_agent:      str            = ""
    state:           ClearanceBundleState = ClearanceBundleState.VALID
    affinity_key:    str            = ""  # associates bundle with an egress node
    clearance_host:  str            = "grok.com"
    last_refresh_at: int | None     = None  # ms


class ProxyLease(BaseModel):
    lease_id:    str
    proxy_url:   str | None    = None
    cf_cookies:  str           = ""
    user_agent:  str           = ""
    clearance_host: str        = "grok.com"
    scope:       ProxyScope    = ProxyScope.APP
    kind:        RequestKind   = RequestKind.HTTP
    acquired_at: int           = 0   # ms

    @property
    def has_proxy(self) -> bool:
        return bool(self.proxy_url)


class ProxyFeedback(BaseModel):
    kind:           ProxyFeedbackKind
    status_code:    int | None = None
    reason:         str        = ""
    retry_after_ms: int | None = None


__all__ = [
    "ProxyScope", "RequestKind", "EgressMode", "ClearanceMode",
    "EgressNodeState", "ClearanceBundleState", "ProxyFeedbackKind",
    "EgressNode", "ClearanceBundle", "ProxyLease", "ProxyFeedback",
]
