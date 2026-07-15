"""Apply feedback to the runtime table columns (in-place, lock-free inner ops).

The caller (AccountDirectory) is responsible for holding the state lock before
calling these functions.

Strategy-split functions
------------------------
``apply_success`` / ``apply_rate_limited`` are split into ``*_quota`` and
``*_random`` variants. The caller picks exactly one based on the active
selector strategy. No runtime ``if`` inside the apply helpers themselves.
"""

from app.platform.runtime.clock import now_s
from ..shared.enums import ALL_MODE_IDS, StatusId
from .table import AccountRuntimeTable

# Health adjustment constants.
_SUCCESS_STEP        = 0.12
_AUTH_FACTOR         = 0.55
_FORBIDDEN_FACTOR    = 0.25
_RATE_LIMIT_FACTOR   = 0.45
_SERVER_ERROR_FACTOR = 0.75
_MIN_HEALTH = 0.05
_MAX_HEALTH = 1.0


# ---------------------------------------------------------------------------
# apply_success — two independent implementations
# ---------------------------------------------------------------------------


def apply_success_quota(table: AccountRuntimeTable, idx: int, mode_id: int) -> None:
    """Quota strategy: decrement per-mode quota and improve health."""
    quota_col = table._quota_col(mode_id)
    quota_col[idx] = max(0, int(quota_col[idx]) - 1)
    _bump_health(table, idx)


def apply_success_random(table: AccountRuntimeTable, idx: int) -> None:
    """Random strategy: only improve health; quota columns are ignored."""
    _bump_health(table, idx)


# ---------------------------------------------------------------------------
# apply_rate_limited — two independent implementations
# ---------------------------------------------------------------------------


def apply_rate_limited_quota(
    table: AccountRuntimeTable, idx: int, mode_id: int
) -> None:
    """Quota strategy: zero the mode quota and reduce health."""
    table._quota_col(mode_id)[idx] = 0
    _adjust_health(table, idx, _RATE_LIMIT_FACTOR)


def apply_rate_limited_random(
    table: AccountRuntimeTable, idx: int, *, cooling_sec: int
) -> None:
    """Random strategy: set per-account cooldown timestamp; do not touch quota.

    The account stays in ``mode_available``; ``_random_select`` filters it out
    until ``cooling_until_s_by_idx`` has elapsed.
    """
    ts = now_s() + max(0, cooling_sec)
    cooling_col = table.cooling_until_s_by_idx
    cooling_col[idx] = max(int(cooling_col[idx]), ts)
    _adjust_health(table, idx, _RATE_LIMIT_FACTOR)


# ---------------------------------------------------------------------------
# Strategy-agnostic feedback helpers (shared)
# ---------------------------------------------------------------------------


def apply_auth_failure(table: AccountRuntimeTable, idx: int) -> None:
    """Reduce health on 401; caller may mark account expired."""
    _adjust_health(table, idx, _AUTH_FACTOR)


def apply_forbidden(table: AccountRuntimeTable, idx: int) -> None:
    """Reduce health heavily on 403."""
    _adjust_health(table, idx, _FORBIDDEN_FACTOR)


def apply_server_error(table: AccountRuntimeTable, idx: int) -> None:
    """Mildly reduce health on 5xx / transport errors."""
    _adjust_health(table, idx, _SERVER_ERROR_FACTOR)


def apply_status_change(
    table: AccountRuntimeTable, idx: int, new_status_id: int
) -> None:
    """Update status column and refresh availability indexes."""
    pool_id = int(table.pool_by_idx[idx])
    old_status = int(table.status_by_idx[idx])

    if old_status == new_status_id:
        return

    table.status_by_idx[idx] = new_status_id

    if new_status_id != int(StatusId.ACTIVE):
        for mode_id in ALL_MODE_IDS:
            bucket = table.mode_available.get((pool_id, mode_id))
            if bucket:
                bucket.discard(idx)
    else:
        for mode_id in ALL_MODE_IDS:
            if int(table._quota_col(mode_id)[idx]) > 0:
                table.mode_available.setdefault((pool_id, mode_id), set()).add(idx)


def apply_quota_update(
    table: AccountRuntimeTable,
    idx: int,
    mode_id: int,
    remaining: int,
    reset_s: int,
) -> None:
    """Update quota and reset timestamp from upstream API data.

    Used exclusively by the quota strategy (refresh service). The random
    strategy never calls this.
    """
    quota_col = table._quota_col(mode_id)
    reset_col = table._reset_col(mode_id)
    quota_col[idx] = max(0, min(remaining, 32767))
    reset_col[idx] = reset_s

    pool_id = int(table.pool_by_idx[idx])
    if int(table.status_by_idx[idx]) == int(StatusId.ACTIVE):
        bucket = table.mode_available.setdefault((pool_id, mode_id), set())
        if int(table._window_col(mode_id)[idx]) > 0:
            bucket.add(idx)


def increment_inflight(table: AccountRuntimeTable, idx: int) -> None:
    table.inflight_by_idx[idx] = min(int(table.inflight_by_idx[idx]) + 1, 65535)


def decrement_inflight(table: AccountRuntimeTable, idx: int) -> None:
    table.inflight_by_idx[idx] = max(0, int(table.inflight_by_idx[idx]) - 1)


def update_last_use(table: AccountRuntimeTable, idx: int, now_s: int) -> None:
    table.last_use_at_by_idx[idx] = now_s


def update_last_fail(table: AccountRuntimeTable, idx: int, now_s: int) -> None:
    table.last_fail_at_by_idx[idx] = now_s
    table.fail_count_by_idx[idx] = min(int(table.fail_count_by_idx[idx]) + 1, 65535)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _bump_health(table: AccountRuntimeTable, idx: int) -> None:
    table.health_by_idx[idx] = min(
        _MAX_HEALTH, float(table.health_by_idx[idx]) + _SUCCESS_STEP
    )


def _adjust_health(table: AccountRuntimeTable, idx: int, factor: float) -> None:
    table.health_by_idx[idx] = max(
        _MIN_HEALTH, float(table.health_by_idx[idx]) * factor
    )


__all__ = [
    "apply_success_quota",
    "apply_success_random",
    "apply_rate_limited_quota",
    "apply_rate_limited_random",
    "apply_auth_failure",
    "apply_forbidden",
    "apply_server_error",
    "apply_status_change",
    "apply_quota_update",
    "increment_inflight",
    "decrement_inflight",
    "update_last_use",
    "update_last_fail",
]
