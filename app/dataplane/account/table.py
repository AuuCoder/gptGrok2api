"""Columnar runtime account table for high-throughput hot-path selection.

Memory layout (1 000 accounts):
  Object-list design  : ~800 KB  (Python object overhead per entry)
  Columnar array design:  ~80 KB  (packed C arrays)

All quota, status, health, and counter fields are stored as typed
``array.array`` columns indexed by a compact integer slot index.
"""

import array
from dataclasses import dataclass, field
from typing import Iterator

from ..shared.enums import ALL_MODE_IDS, StatusId

# ---------------------------------------------------------------------------
# Column type codes
#   'B'  uint8   — pool_id, status_id
#   'h'  int16   — quota remaining / total (max 32767; sufficient for all known limits)
#   'H'  uint16  — inflight, fail_count
#   'f'  float32 — health score
#   'L'  uint32  — epoch-second timestamps / window lengths (valid until year 2106)
# ---------------------------------------------------------------------------

_QUOTA_COLS = (
    "quota_auto",
    "quota_fast",
    "quota_expert",
    "quota_heavy",
    "quota_grok_4_3",
    "quota_console",
)
_RESET_COLS = (
    "reset_auto",
    "reset_fast",
    "reset_expert",
    "reset_heavy",
    "reset_grok_4_3",
    "reset_console",
)
_INFLIGHT_CAP = 32_767  # avoid int16 overflow on quota

# ---------------------------------------------------------------------------
# AccountRuntimeTable
# ---------------------------------------------------------------------------


@dataclass
class AccountRuntimeTable:
    """Columnar in-memory account store.

    Columns are indexed by a compact integer *slot index* (idx).
    The index space is dense: deleted slots are reused on next bootstrap.

    Thread-safety: the caller (AccountDirectory) owns all locking.
    """

    # --- Identity ---
    token_by_idx: list[str]      = field(default_factory=list)
    idx_by_token: dict[str, int] = field(default_factory=dict)

    # --- Pool / status (uint8) ---
    pool_by_idx:   "array.array[int]" = field(default_factory=lambda: array.array("B"))
    status_by_idx: "array.array[int]" = field(default_factory=lambda: array.array("B"))

    # --- Quota remaining per mode (int16; -1 = unknown) ---
    quota_auto_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("h")
    )
    quota_fast_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("h")
    )
    quota_expert_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("h")
    )
    quota_heavy_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("h")
    )
    quota_grok_4_3_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("h")
    )

    # --- Quota remaining per mode (int16; -1 = unknown) --- console
    quota_console_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("h")
    )

    # --- Quota total per mode (int16; 0 = unsupported / unknown) ---
    total_auto_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("h")
    )
    total_fast_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("h")
    )
    total_expert_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("h")
    )
    total_heavy_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("h")
    )
    total_grok_4_3_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("h")
    )

    # --- Quota total per mode (int16; 0 = unsupported / unknown) --- console
    total_console_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("h")
    )

    # --- Window size per mode (uint32 seconds; 0 = unsupported / unknown) ---
    window_auto_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )
    window_fast_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )
    window_expert_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )
    window_heavy_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )
    window_grok_4_3_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )

    # --- Window size per mode (uint32 seconds; 0 = unsupported / unknown) --- console
    window_console_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )

    # --- Window reset timestamps (uint32 epoch-seconds; 0 = unknown) ---
    reset_auto_at_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )
    reset_fast_at_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )
    reset_expert_at_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )
    reset_heavy_at_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )
    reset_grok_4_3_at_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )

    # --- Window reset timestamps (uint32 epoch-seconds; 0 = unknown) --- console
    reset_console_at_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )

    # --- Runtime counters (uint16) ---
    inflight_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("H")
    )
    fail_count_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("H")
    )

    # --- Health score (float32; [0.05, 1.0]) ---
    health_by_idx: "array.array[float]" = field(
        default_factory=lambda: array.array("f")
    )

    # --- Last-activity timestamps (uint32 epoch-seconds; 0 = never) ---
    last_use_at_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )
    last_fail_at_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )

    # --- Per-account cooldown (random strategy only; uint32 epoch-seconds; 0 = not cooling) ---
    cooling_until_s_by_idx: "array.array[int]" = field(
        default_factory=lambda: array.array("L")
    )

    # --- Pre-computed selection indexes ---
    # (pool_id, mode_id) → set of idx with a supported quota window and status == ACTIVE
    mode_available: dict[tuple[int, int], set[int]] = field(default_factory=dict)
    # tag string → set of idx
    tag_idx: dict[str, set[int]] = field(default_factory=dict)

    # --- Metadata ---
    revision: int = 0
    size:     int = 0   # number of live (non-deleted) slots

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _quota_col(self, mode_id: int) -> "array.array[int]":
        if mode_id == 0:
            return self.quota_auto_by_idx
        if mode_id == 1:
            return self.quota_fast_by_idx
        if mode_id == 2:
            return self.quota_expert_by_idx
        if mode_id == 3:
            return self.quota_heavy_by_idx
        if mode_id == 4:
            return self.quota_grok_4_3_by_idx
        return self.quota_console_by_idx

    def _reset_col(self, mode_id: int) -> "array.array[int]":
        if mode_id == 0:
            return self.reset_auto_at_by_idx
        if mode_id == 1:
            return self.reset_fast_at_by_idx
        if mode_id == 2:
            return self.reset_expert_at_by_idx
        if mode_id == 3:
            return self.reset_heavy_at_by_idx
        if mode_id == 4:
            return self.reset_grok_4_3_at_by_idx
        return self.reset_console_at_by_idx

    def _total_col(self, mode_id: int) -> "array.array[int]":
        if mode_id == 0:
            return self.total_auto_by_idx
        if mode_id == 1:
            return self.total_fast_by_idx
        if mode_id == 2:
            return self.total_expert_by_idx
        if mode_id == 3:
            return self.total_heavy_by_idx
        if mode_id == 4:
            return self.total_grok_4_3_by_idx
        return self.total_console_by_idx

    def _window_col(self, mode_id: int) -> "array.array[int]":
        if mode_id == 0:
            return self.window_auto_by_idx
        if mode_id == 1:
            return self.window_fast_by_idx
        if mode_id == 2:
            return self.window_expert_by_idx
        if mode_id == 3:
            return self.window_heavy_by_idx
        if mode_id == 4:
            return self.window_grok_4_3_by_idx
        return self.window_console_by_idx

    def _add_to_indexes(self, idx: int) -> None:
        pool_id   = int(self.pool_by_idx[idx])
        status_id = int(self.status_by_idx[idx])
        if status_id != int(StatusId.ACTIVE):
            return
        for mode_id in ALL_MODE_IDS:
            if self._window_col(mode_id)[idx] > 0:
                self.mode_available.setdefault((pool_id, mode_id), set()).add(idx)

    def _remove_from_indexes(self, idx: int) -> None:
        pool_id = int(self.pool_by_idx[idx])
        for mode_id in ALL_MODE_IDS:
            bucket = self.mode_available.get((pool_id, mode_id))
            if bucket:
                bucket.discard(idx)

    def _remove_from_tag_idx(self, idx: int, tags: list[str]) -> None:
        for tag in tags:
            bucket = self.tag_idx.get(tag)
            if bucket:
                bucket.discard(idx)

    def _add_to_tag_idx(self, idx: int, tags: list[str]) -> None:
        for tag in tags:
            self.tag_idx.setdefault(tag, set()).add(idx)

    # ---------------------------------------------------------------------------
    # Slot append (used during bootstrap)
    # ---------------------------------------------------------------------------

    def _append_slot(
        self,
        token:           str,
        pool_id:         int,
        status_id:       int,
        quota_auto:      int,
        quota_fast:      int,
        quota_expert:    int,
        quota_heavy:     int,
        quota_grok_4_3:  int,
        quota_console:   int,
        total_auto:      int,
        total_fast:      int,
        total_expert:    int,
        total_heavy:     int,
        total_grok_4_3:  int,
        total_console:   int,
        window_auto:     int,
        window_fast:     int,
        window_expert:   int,
        window_heavy:    int,
        window_grok_4_3: int,
        window_console:  int,
        reset_auto:      int,
        reset_fast:      int,
        reset_expert:    int,
        reset_heavy:     int,
        reset_grok_4_3:  int,
        reset_console:   int,
        health:          float,
        last_use_s:      int,
        last_fail_s:     int,
        fail_count:      int,
        tags:            list[str],
    ) -> int:
        idx = len(self.token_by_idx)
        self.token_by_idx.append(token)
        self.idx_by_token[token] = idx
        self.pool_by_idx.append(pool_id)
        self.status_by_idx.append(status_id)
        self.quota_auto_by_idx.append(max(-1, min(quota_auto, 32767)))
        self.quota_fast_by_idx.append(max(-1, min(quota_fast, 32767)))
        self.quota_expert_by_idx.append(max(-1, min(quota_expert, 32767)))
        self.quota_heavy_by_idx.append(max(-1, min(quota_heavy, 32767)))
        self.quota_grok_4_3_by_idx.append(max(-1, min(quota_grok_4_3, 32767)))
        self.quota_console_by_idx.append(max(-1, min(quota_console, 32767)))
        self.total_auto_by_idx.append(max(0, min(total_auto, 32767)))
        self.total_fast_by_idx.append(max(0, min(total_fast, 32767)))
        self.total_expert_by_idx.append(max(0, min(total_expert, 32767)))
        self.total_heavy_by_idx.append(max(0, min(total_heavy, 32767)))
        self.total_grok_4_3_by_idx.append(max(0, min(total_grok_4_3, 32767)))
        self.total_console_by_idx.append(max(0, min(total_console, 32767)))
        self.window_auto_by_idx.append(max(0, window_auto))
        self.window_fast_by_idx.append(max(0, window_fast))
        self.window_expert_by_idx.append(max(0, window_expert))
        self.window_heavy_by_idx.append(max(0, window_heavy))
        self.window_grok_4_3_by_idx.append(max(0, window_grok_4_3))
        self.window_console_by_idx.append(max(0, window_console))
        self.reset_auto_at_by_idx.append(reset_auto)
        self.reset_fast_at_by_idx.append(reset_fast)
        self.reset_expert_at_by_idx.append(reset_expert)
        self.reset_heavy_at_by_idx.append(reset_heavy)
        self.reset_grok_4_3_at_by_idx.append(reset_grok_4_3)
        self.reset_console_at_by_idx.append(reset_console)
        self.inflight_by_idx.append(0)
        self.fail_count_by_idx.append(min(fail_count, 65535))
        self.health_by_idx.append(health)
        self.last_use_at_by_idx.append(last_use_s)
        self.last_fail_at_by_idx.append(last_fail_s)
        self.cooling_until_s_by_idx.append(0)
        self.size += 1
        self._add_to_indexes(idx)
        self._add_to_tag_idx(idx, tags)
        return idx

    # ---------------------------------------------------------------------------
    # Slot update (used during incremental sync)
    # ---------------------------------------------------------------------------

    def _update_slot(
        self,
        idx: int,
        pool_id: int,
        status_id: int,
        quota_auto: int,
        quota_fast: int,
        quota_expert: int,
        quota_heavy: int,
        quota_grok_4_3: int,
        quota_console: int,
        total_auto: int,
        total_fast: int,
        total_expert: int,
        total_heavy: int,
        total_grok_4_3: int,
        total_console: int,
        window_auto: int,
        window_fast: int,
        window_expert: int,
        window_heavy: int,
        window_grok_4_3: int,
        window_console: int,
        reset_auto: int,
        reset_fast: int,
        reset_expert: int,
        reset_heavy: int,
        reset_grok_4_3: int,
        reset_console: int,
        health: float,
        last_use_s: int,
        last_fail_s: int,
        fail_count: int,
        old_tags: list[str],
        new_tags: list[str],
    ) -> None:
        self._remove_from_indexes(idx)
        self._remove_from_tag_idx(idx, old_tags)

        self.pool_by_idx[idx] = pool_id
        self.status_by_idx[idx] = status_id
        self.quota_auto_by_idx[idx] = max(-1, min(quota_auto, 32767))
        self.quota_fast_by_idx[idx] = max(-1, min(quota_fast, 32767))
        self.quota_expert_by_idx[idx] = max(-1, min(quota_expert, 32767))
        self.quota_heavy_by_idx[idx] = max(-1, min(quota_heavy, 32767))
        self.quota_grok_4_3_by_idx[idx] = max(-1, min(quota_grok_4_3, 32767))
        self.quota_console_by_idx[idx] = max(-1, min(quota_console, 32767))
        self.total_auto_by_idx[idx] = max(0, min(total_auto, 32767))
        self.total_fast_by_idx[idx] = max(0, min(total_fast, 32767))
        self.total_expert_by_idx[idx] = max(0, min(total_expert, 32767))
        self.total_heavy_by_idx[idx] = max(0, min(total_heavy, 32767))
        self.total_grok_4_3_by_idx[idx] = max(0, min(total_grok_4_3, 32767))
        self.total_console_by_idx[idx] = max(0, min(total_console, 32767))
        self.window_auto_by_idx[idx] = max(0, window_auto)
        self.window_fast_by_idx[idx] = max(0, window_fast)
        self.window_expert_by_idx[idx] = max(0, window_expert)
        self.window_heavy_by_idx[idx] = max(0, window_heavy)
        self.window_grok_4_3_by_idx[idx] = max(0, window_grok_4_3)
        self.window_console_by_idx[idx] = max(0, window_console)
        self.reset_auto_at_by_idx[idx] = reset_auto
        self.reset_fast_at_by_idx[idx] = reset_fast
        self.reset_expert_at_by_idx[idx] = reset_expert
        self.reset_heavy_at_by_idx[idx] = reset_heavy
        self.reset_grok_4_3_at_by_idx[idx] = reset_grok_4_3
        self.reset_console_at_by_idx[idx] = reset_console
        self.fail_count_by_idx[idx] = min(fail_count, 65535)
        self.last_use_at_by_idx[idx] = last_use_s
        self.last_fail_at_by_idx[idx] = last_fail_s
        # health is not reset on update

        self._add_to_indexes(idx)
        self._add_to_tag_idx(idx, new_tags)

    # ---------------------------------------------------------------------------
    # Public read accessors
    # ---------------------------------------------------------------------------

    def get_token(self, idx: int) -> str:
        return self.token_by_idx[idx]

    def get_pool_id(self, idx: int) -> int:
        return int(self.pool_by_idx[idx])

    def quota_for(self, idx: int, mode_id: int) -> int:
        return int(self._quota_col(mode_id)[idx])

    def is_active(self, idx: int) -> bool:
        return int(self.status_by_idx[idx]) == int(StatusId.ACTIVE)

    def iter_live_indices(self) -> Iterator[int]:
        for idx in range(len(self.token_by_idx)):
            if int(self.status_by_idx[idx]) != int(StatusId.DELETED):
                yield idx


def make_empty_table() -> AccountRuntimeTable:
    """Return a freshly initialised (empty) runtime table."""
    return AccountRuntimeTable()


__all__ = ["AccountRuntimeTable", "make_empty_table"]
