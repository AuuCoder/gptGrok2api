"""Control-plane account enumerations."""

from enum import IntEnum, StrEnum


class AccountStatus(StrEnum):
    """Persistent lifecycle status of an account record."""

    ACTIVE   = "active"
    COOLING  = "cooling"
    EXPIRED  = "expired"
    DISABLED = "disabled"


class QuotaSource(IntEnum):
    """Reliability of a stored QuotaWindow value.

    Integer values are stored in the quota JSON payload.
    """

    DEFAULT   = 0  # Never synced; using built-in default.
    REAL      = 1  # Retrieved from upstream rate-limits API.
    ESTIMATED = 2  # Locally decremented after a failed API call.


class FeedbackKind(StrEnum):
    """Classification of the outcome of a single upstream request."""

    SUCCESS      = "success"
    UNAUTHORIZED = "unauthorized"   # 401 — token invalid / expired
    FORBIDDEN    = "forbidden"      # 403 — account suspended / CF challenge
    RATE_LIMITED = "rate_limited"   # 429 — quota exhausted
    SERVER_ERROR = "server_error"   # 5xx — upstream fault
    DISABLE      = "disable"        # operator-initiated disable
    DELETE       = "delete"         # operator-initiated delete
    RESTORE      = "restore"        # operator-initiated restore


__all__ = ["AccountStatus", "QuotaSource", "FeedbackKind"]
