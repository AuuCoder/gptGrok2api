"""AccountLease — minimal hot-path rental object."""

from dataclasses import dataclass

from app.platform.runtime.ids import next_id


@dataclass(slots=True)
class AccountLease:
    """Represents a reserved account slot for a single upstream request.

    Holds only the minimum fields needed on the hot path — no AccountRecord.
    The slot *idx* allows O(1) array access for feedback updates.
    """

    lease_id:    int   # monotonic counter — cheaper than uuid
    idx:         int   # slot in AccountRuntimeTable
    token:       str   # cached for header building
    pool_id:     int   # PoolId integer
    mode_id:     int   # ModeId integer
    selected_at: int   # ms timestamp


def new_lease(idx: int, token: str, pool_id: int, mode_id: int, selected_at: int) -> AccountLease:
    """Construct an AccountLease with an auto-incremented id."""
    return AccountLease(
        lease_id    = next_id(),
        idx         = idx,
        token       = token,
        pool_id     = pool_id,
        mode_id     = mode_id,
        selected_at = selected_at,
    )


__all__ = ["AccountLease", "new_lease"]
