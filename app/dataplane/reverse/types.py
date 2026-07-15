"""Reverse pipeline data types — ReversePlan, ReverseLeaseSet, ReverseResult."""

from dataclasses import dataclass, field
from enum import IntEnum, auto
from typing import Any

from app.control.proxy.models import ProxyLease


# ---------------------------------------------------------------------------
# Result classification
# ---------------------------------------------------------------------------

class ResultCategory(IntEnum):
    """Outcome category for upstream responses."""
    SUCCESS      = 0
    RATE_LIMITED  = auto()
    AUTH_FAILURE  = auto()
    FORBIDDEN     = auto()
    NOT_FOUND     = auto()
    UPSTREAM_5XX  = auto()
    TRANSPORT_ERR = auto()
    UNKNOWN       = auto()


# ---------------------------------------------------------------------------
# Transport type hint
# ---------------------------------------------------------------------------

class TransportKind(IntEnum):
    HTTP_SSE   = 0   # streaming POST (SSE lines)
    HTTP_JSON  = 1   # single-shot POST → JSON
    WEBSOCKET  = 2   # WebSocket-based protocol
    GRPC_WEB   = 3   # gRPC-Web over HTTP


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ReversePlan:
    """Describes how a single upstream call should be executed."""
    endpoint:        str                  # full URL
    transport_kind:  TransportKind
    pool_candidates: tuple[int, ...]      # ordered pool IDs to try (e.g. (0,1,2))
    mode_id:         int                  # 0=auto, 1=fast, 2=expert, 3=heavy
    timeout_s:       float    = 120.0
    content_type:    str      = "application/json"
    origin:          str      = "https://grok.com"
    referer:         str      = "https://grok.com/"
    extra:           dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReverseLeaseSet:
    """Holds both account and proxy leases for one request cycle."""
    account_idx:  int                   # slot index in AccountRuntimeTable
    account_token: str
    proxy_lease:  ProxyLease | None = None


@dataclass(slots=True)
class ReverseResult:
    """Outcome of a single upstream request."""
    category:    ResultCategory
    status_code: int          = 0
    body:        str          = ""
    payload:     Any          = None    # parsed response data (dict / list / None)
    error:       str          = ""
    latency_ms:  int          = 0


__all__ = [
    "ResultCategory", "TransportKind",
    "ReversePlan", "ReverseLeaseSet", "ReverseResult",
]
