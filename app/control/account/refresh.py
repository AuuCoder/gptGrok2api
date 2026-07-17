"""Account refresh service — mode-aware usage synchronisation."""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.platform.errors import UpstreamError
from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_ms
from app.platform.runtime.batch import run_batch
from app.control.model.enums import ALL_MODES_FULL
from .enums import AccountStatus, QuotaSource
from .models import AccountRecord, QuotaWindow
from .quota_defaults import (
    default_quota_window,
    infer_pool,
    normalize_quota_window,
    supported_mode_ids,
    supports_mode,
)
from .state_machine import is_manageable

if TYPE_CHECKING:
    from .repository import AccountRepository


@dataclass
class RefreshResult:
    checked: int = 0
    refreshed: int = 0
    recovered: int = 0
    expired: int = 0
    disabled: int = 0
    rate_limited: int = 0
    failed: int = 0

    def merge(self, other: "RefreshResult") -> None:
        self.checked += other.checked
        self.refreshed += other.refreshed
        self.recovered += other.recovered
        self.expired += other.expired
        self.disabled += other.disabled
        self.rate_limited += other.rate_limited
        self.failed += other.failed


_MODE_KEYS = {
    0: "quota_auto",
    1: "quota_fast",
    2: "quota_expert",
    3: "quota_heavy",
    4: "quota_grok_4_3",
    5: "quota_console",  # console.x.ai 独立配额
}


def _infer_pool_from_live_windows(windows: dict[int, QuotaWindow]) -> str | None:
    """Infer pool only from quota totals that identify an entitlement tier."""
    auto_win = windows.get(0)
    if auto_win is not None:
        inferred = infer_pool(windows)  # type: ignore[arg-type]
        if inferred != "basic" or auto_win.total == 20:
            return inferred

    for mode_id in (2, 4):
        win = windows.get(mode_id)
        if win is None:
            continue
        if win.total == 150:
            return "heavy"
        if win.total == 50:
            return "super"
    return None


class AccountRefreshService:
    """Fetches real quota data from the upstream usage API and persists it.

    Triggers:
      1. Import   — fetch all modes supported by the account's pool.
      2. Call     — fetch the called mode only (async, non-blocking).
      3. Schedule — refresh one pool per loop using that pool's supported modes.
    """

    def __init__(self, repository: "AccountRepository") -> None:
        self._repo = repository
        self._lock = asyncio.Lock()
        self._od_lock = asyncio.Lock()
        self._od_last = 0.0

    # ------------------------------------------------------------------
    # Usage API fetch (delegates to dataplane reverse protocol)
    # ------------------------------------------------------------------

    async def _fetch_all_quotas(
        self, token: str, pool: str, *, bootstrap: bool = False
    ) -> dict[int, QuotaWindow] | None:
        """Fetch quota windows for every mode supported by *pool*.

        Examples:
          - basic -> fast
          - super -> auto / fast / expert / grok_4_3
          - heavy -> auto / fast / expert / heavy / grok_4_3
        """
        try:
            from app.dataplane.reverse.protocol.xai_usage import fetch_all_quotas

            mode_ids = supported_mode_ids(pool)
            if bootstrap:
                # Bootstrap refreshes need entitlement probes even when the
                # current local image is basic. If auto is flaky, expert/heavy
                # windows still provide enough signal to avoid a sticky
                # misclassification.
                mode_ids = tuple(dict.fromkeys((0, 2, 3, 4, *mode_ids)))
            # Console quota is maintained from console.x.ai call feedback.
            # It is not exposed by grok.com's rate-limits endpoint.
            mode_ids = tuple(mode_id for mode_id in mode_ids if mode_id != 5)
            return await fetch_all_quotas(token, mode_ids)
        except UpstreamError:
            raise
        except Exception as exc:
            logger.debug(
                "account quota fetch failed: token={}... pool={} error={}",
                token[:10],
                pool,
                exc,
            )
            return None

    async def _fetch_mode_quota(
        self, token: str, pool: str, mode_id: int
    ) -> QuotaWindow | None:
        """Fetch a single mode quota window."""
        if not supports_mode(pool, mode_id):
            logger.debug(
                "account mode quota fetch skipped: token={}... pool={} mode_id={} reason=unsupported_mode",
                token[:10],
                pool,
                mode_id,
            )
            return None
        try:
            from app.dataplane.reverse.protocol.xai_usage import fetch_mode_quota

            return await fetch_mode_quota(token, mode_id)
        except UpstreamError:
            raise
        except Exception as exc:
            logger.debug(
                "account mode quota fetch failed: token={}... pool={} mode_id={} error={}",
                token[:10],
                pool,
                mode_id,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Core refresh logic
    # ------------------------------------------------------------------

    async def refresh_on_import(self, tokens: list[str]) -> RefreshResult:
        """Called after bulk import — sync real quotas for all accounts."""
        records = await self._repo.get_accounts(tokens)
        active = [r for r in records if is_manageable(r)]
        if not active:
            return RefreshResult(checked=len(records))

        concurrency = get_config("account.refresh.usage_concurrency", 15)
        results = await run_batch(
            active,
            lambda r: self._refresh_one(r, apply_fallback=True, bootstrap=True),
            concurrency=concurrency,
        )
        agg = RefreshResult(checked=len(records))
        for r in results:
            agg.merge(r)
        return agg

    async def refresh_call_async(self, token: str, mode_id: int) -> None:
        """Fire-and-forget single-mode quota sync after a successful call."""
        record = (await self._repo.get_accounts([token]) or [None])[0]
        if record is None or record.is_deleted():
            return

        # mode_id=5 (CONSOLE) 是本地管理的配额，不需要请求 xai usage API
        # 直接做本地扣减并更新 usage_use_count
        if mode_id == 5:
            await self._apply_single_mode(
                record, mode_id, window=None, is_use=True, use_at_ms=now_ms()
            )
            return

        try:
            window = await self._fetch_mode_quota(token, record.pool, mode_id)
        except UpstreamError as exc:
            if await self._expire_invalid_credentials(record, exc):
                return
            raise
        await self._apply_single_mode(
            record, mode_id, window, is_use=True, use_at_ms=now_ms()
        )

    async def refresh_scheduled(self, pool: str | None = None) -> RefreshResult:
        """Periodic refresh — fetch real quotas for all (or one pool's) accounts.

        Args:
            pool: When set, only refreshes accounts belonging to that pool.
                  When ``None``, refreshes all pools.
        """
        snapshot = await self._repo.runtime_snapshot()
        records = [r for r in snapshot.items if is_manageable(r)]
        if pool is not None:
            records = [r for r in records if r.pool == pool]

        concurrency = get_config("account.refresh.usage_concurrency", 15)
        results = await run_batch(
            records,
            lambda r: self._refresh_one(r, apply_fallback=True),
            concurrency=concurrency,
        )
        agg = RefreshResult()
        for r in results:
            agg.merge(r)
        return agg

    async def refresh_on_demand(self) -> RefreshResult:
        """Throttled on-demand refresh triggered by request path."""
        min_interval = float(
            get_config("account.refresh.on_demand_min_interval_sec", 300)
        )
        import time

        now = time.monotonic()
        if now - self._od_last < min_interval:
            return RefreshResult()
        if self._od_lock.locked():
            return RefreshResult()
        async with self._od_lock:
            now = time.monotonic()
            if now - self._od_last < min_interval:
                return RefreshResult()
            result = await self.refresh_scheduled()
            self._od_last = time.monotonic()
            return result

    async def refresh_tokens(self, tokens: list[str]) -> RefreshResult:
        """Explicit refresh for a list of tokens (admin / manual trigger)."""
        records = [r for r in await self._repo.get_accounts(tokens) if is_manageable(r)]
        concurrency = get_config("account.refresh.usage_concurrency", 15)
        results = await run_batch(
            records,
            lambda r: self._refresh_one(r, bootstrap=True, track_result=True),
            concurrency=concurrency,
        )
        agg = RefreshResult()
        for r in results:
            agg.merge(r)
        return agg

    async def verify_fast_token(self, token: str) -> dict[str, Any]:
        """Verify one SSO token with a single read-only ``fast`` quota probe.

        This is deliberately narrower than :meth:`refresh_tokens`: the
        registration-account UI needs a fast login-state check, not a complete
        multi-model quota refresh.  Only a confirmed invalid-credentials
        response can expire the runtime account; all other upstream failures
        are returned as ``unknown`` without changing account state.
        """
        value = str(token or "").strip()
        if not value:
            return {"status": "unknown", "error": "缺少 SSO 登录态"}

        records = await self._repo.get_accounts([value])
        record = next((item for item in records if not item.is_deleted()), None)

        from app.dataplane.reverse.protocol.xai_usage import probe_fast_quota

        probe = await probe_fast_quota(value)
        if probe.status == "invalid":
            if record is not None and probe.exception is not None:
                try:
                    await self._expire_invalid_credentials(record, probe.exception)
                except Exception as exc:
                    logger.warning(
                        "account invalid-credential persistence failed: token={}... error_type={}",
                        value[:10],
                        type(exc).__name__,
                    )
            return {
                "status": "invalid",
                "error": probe.error or "登录态已失效或账号不可用",
            }

        if probe.status != "valid" or probe.quota is None:
            return {
                "status": "unknown",
                "error": probe.error or "未确认登录态",
            }

        quota = probe.quota
        if record is not None:
            try:
                # Reuse the normal single-mode persistence path so the
                # displayed fast quota stays current without recording a chat
                # call or changing the account's enabled/disabled state.
                await self._apply_single_mode(record, 1, quota, is_use=False)
            except Exception as exc:
                logger.warning(
                    "account fast-quota verification persistence failed: token={}... error_type={}",
                    value[:10],
                    type(exc).__name__,
                )

        return {
            "status": "valid",
            "quota": {
                "remaining": max(0, int(getattr(quota, "remaining", 0) or 0)),
                "total": max(0, int(getattr(quota, "total", 0) or 0)),
            },
        }

    # ------------------------------------------------------------------
    # Per-account refresh
    # ------------------------------------------------------------------

    async def _refresh_one(
        self,
        record: AccountRecord,
        *,
        apply_fallback: bool = False,
        bootstrap: bool = False,
        track_result: bool = False,
    ) -> RefreshResult:
        """Fetch all pool-supported modes from the usage API and persist them.

        apply_fallback=True  — used by scheduled/import paths: when API fails,
                               decrement REAL quotas or reset expired DEFAULT windows.
        apply_fallback=False — used by manual/on-demand paths: if API fails, return
                               failed=1 immediately without touching stored data.
        """
        if record.is_deleted():
            return RefreshResult()

        try:
            windows = await self._fetch_all_quotas(
                record.token, record.pool, bootstrap=bootstrap
            )
        except UpstreamError as exc:
            if await self._expire_invalid_credentials(record, exc):
                return RefreshResult(checked=1, expired=1, failed=0)
            raise

        # API call completely failed — no real data available.
        if windows is None:
            if not apply_fallback:
                if track_result:
                    await self._record_refresh_result(
                        record,
                        status="failed",
                        error="上游未返回真实额度数据",
                    )
                return RefreshResult(checked=1, failed=1)
            # Scheduled/import path: apply conservative fallback.
            return await self._apply_fallback(record)

        # We got at least a response — apply real data per mode.
        qs = record.quota_set()
        now = now_ms()
        patches: dict[str, dict] = {}
        refreshed = False
        inferred = _infer_pool_from_live_windows(windows)
        effective_pool = inferred if (bootstrap and inferred) else record.pool

        for mode in ALL_MODES_FULL:
            mode_id = int(mode)
            existing = qs.get(mode_id)
            if mode_id == 5:
                if existing is None:
                    continue
                # Older versions accidentally copied the `auto` quota into
                # Console.  A REAL Console window is therefore legacy-corrupt
                # data and can safely be restored to the local default.
                if existing.source == QuotaSource.REAL or existing.is_window_expired(now):
                    default = default_quota_window(effective_pool, mode_id)
                    if default is not None:
                        patches[_MODE_KEYS[mode_id]] = QuotaWindow(
                            remaining=default.total,
                            total=default.total,
                            window_seconds=default.window_seconds,
                            reset_at=None,
                            synced_at=now,
                            source=QuotaSource.DEFAULT,
                        ).to_dict()
                        refreshed = True
                continue
            if mode_id in windows:
                window = normalize_quota_window(
                    effective_pool, mode_id, windows[mode_id]
                )
                if window is None:
                    continue
                patches[_MODE_KEYS[mode_id]] = window.to_dict()
                refreshed = True
            elif apply_fallback:
                existing = qs.get(mode_id)
                if existing is None:
                    continue
                if existing.source == QuotaSource.REAL:
                    patches[_MODE_KEYS[mode_id]] = QuotaWindow(
                        remaining=max(0, existing.remaining - 1),
                        total=existing.total,
                        window_seconds=existing.window_seconds,
                        reset_at=existing.reset_at,
                        synced_at=existing.synced_at,
                        source=QuotaSource.ESTIMATED,
                    ).to_dict()
                elif existing.is_window_expired(now):
                    default = default_quota_window(effective_pool, mode_id)
                    if default is None:
                        continue
                    patches[_MODE_KEYS[mode_id]] = QuotaWindow(
                        remaining=default.total,
                        total=default.total,
                        window_seconds=default.window_seconds,
                        reset_at=now + default.window_seconds * 1000,
                        synced_at=now,
                        source=QuotaSource.DEFAULT,
                    ).to_dict()

        if not patches:
            if track_result:
                await self._record_refresh_result(
                    record,
                    status="failed",
                    error="上游响应未包含可用额度数据",
                )
            return RefreshResult(checked=1, failed=0 if refreshed else 1)

        # Infer pool type from live quota data and patch if it changed.
        pool_patch = inferred if inferred is not None and inferred != record.pool else None
        if pool_patch:
            logger.info(
                "account pool updated from live quota: token={}... previous_pool={} current_pool={}",
                record.token[:10],
                record.pool,
                inferred,
            )

        from .commands import AccountPatch

        await self._repo.patch_accounts(
            [
                AccountPatch(
                    token=record.token,
                    pool=pool_patch,
                    last_sync_at=now_ms() if refreshed else None,
                    usage_sync_delta=1 if refreshed else None,
                    ext_merge=(
                        {
                            "refresh_status": "success",
                            "refresh_at": now,
                            "refresh_error": "",
                        }
                        if track_result and refreshed
                        else None
                    ),
                    **patches,  # type: ignore[arg-type]
                )
            ]
        )
        was_cooling = record.status == AccountStatus.COOLING
        return RefreshResult(
            checked=1,
            refreshed=1 if refreshed else 0,
            failed=0 if refreshed else 1,
            recovered=1 if (was_cooling and refreshed) else 0,
        )

    async def _record_refresh_result(
        self,
        record: AccountRecord,
        *,
        status: str,
        error: str = "",
    ) -> None:
        from .commands import AccountPatch

        await self._repo.patch_accounts(
            [
                AccountPatch(
                    token=record.token,
                    ext_merge={
                        "refresh_status": str(status or "").strip(),
                        "refresh_at": now_ms(),
                        "refresh_error": str(error or "").strip()[:300],
                    },
                )
            ]
        )

    async def _apply_fallback(self, record: AccountRecord) -> RefreshResult:
        """Conservative fallback when API is unreachable (scheduled/import path only)."""
        qs = record.quota_set()
        now = now_ms()
        patches: dict[str, dict] = {}

        for mode in ALL_MODES_FULL:
            mode_id = int(mode)
            existing = qs.get(mode_id)
            if existing is None:
                continue
            if mode_id == 5:
                if existing.source == QuotaSource.REAL or existing.is_window_expired(now):
                    default = default_quota_window(record.pool, mode_id)
                    if default is not None:
                        patches[_MODE_KEYS[mode_id]] = QuotaWindow(
                            remaining=default.total,
                            total=default.total,
                            window_seconds=default.window_seconds,
                            reset_at=None,
                            synced_at=now,
                            source=QuotaSource.DEFAULT,
                        ).to_dict()
                continue
            if existing.source == QuotaSource.REAL:
                patches[_MODE_KEYS[mode_id]] = QuotaWindow(
                    remaining=max(0, existing.remaining - 1),
                    total=existing.total,
                    window_seconds=existing.window_seconds,
                    reset_at=existing.reset_at,
                    synced_at=existing.synced_at,
                    source=QuotaSource.ESTIMATED,
                ).to_dict()
            elif existing.is_window_expired(now):
                default = default_quota_window(record.pool, mode_id)
                if default is None:
                    continue
                patches[_MODE_KEYS[mode_id]] = QuotaWindow(
                    remaining=default.total,
                    total=default.total,
                    window_seconds=default.window_seconds,
                    reset_at=now + default.window_seconds * 1000,
                    synced_at=now,
                    source=QuotaSource.DEFAULT,
                ).to_dict()

        if patches:
            from .commands import AccountPatch

            await self._repo.patch_accounts(
                [AccountPatch(token=record.token, **patches)]
            )  # type: ignore[arg-type]

        return RefreshResult(checked=1, failed=1)

    async def record_failure_async(
        self, token: str, mode_id: int, exc: BaseException | None = None
    ) -> None:
        """Fire-and-forget: persist failure counter and timestamp after a failed call."""
        from .commands import AccountPatch

        try:
            if exc is not None:
                record = next(iter(await self._repo.get_accounts([token])), None)
                if record is not None and await self._expire_invalid_credentials(
                    record, exc
                ):
                    return
                if (
                    record is not None
                    and getattr(exc, "status", None) == 429
                    and mode_id in _MODE_KEYS
                ):
                    now = now_ms()
                    quota_patch: dict[str, dict] = {}
                    window = record.quota_set().get(mode_id)
                    if window is not None:
                        if mode_id == 5:
                            # Console 429: 一次直接清零（扣 20），账号当前窗口不再可用
                            # 立即启动恢复计时器，窗口结束后由巡检任务重置
                            new_remaining = 0
                            reset_at = window.reset_at
                            if reset_at is None and window.window_seconds > 0:
                                reset_at = now + window.window_seconds * 1000
                            quota_patch[_MODE_KEYS[mode_id]] = QuotaWindow(
                                remaining=new_remaining,
                                total=window.total,
                                window_seconds=window.window_seconds,
                                reset_at=reset_at,
                                synced_at=window.synced_at,
                                source=QuotaSource.ESTIMATED,
                            ).to_dict()
                            # Console 429 represents a temporary per-window
                            # limit only.  Never promote it to EXPIRED: an
                            # SSO is invalid only when an explicit credential
                            # marker confirms that separately.
                        else:
                            # 非 console 模式保持原有清零逻辑
                            reset_at = (
                                window.reset_at
                                if window.reset_at is not None and window.reset_at > now
                                else now + max(window.window_seconds, 1) * 1000
                            )
                            quota_patch[_MODE_KEYS[mode_id]] = QuotaWindow(
                                remaining=0,
                                total=window.total,
                                window_seconds=window.window_seconds,
                                reset_at=reset_at,
                                synced_at=window.synced_at,
                                source=QuotaSource.ESTIMATED,
                            ).to_dict()
                    await self._repo.patch_accounts(
                        [
                            AccountPatch(
                                token=token,
                                usage_fail_delta=1,
                                last_fail_at=now,
                                last_fail_reason="rate_limited",
                                **quota_patch,
                            )
                        ]
                    )
                    return
            await self._repo.patch_accounts(
                [
                    AccountPatch(
                        token=token,
                        usage_fail_delta=1,
                        last_fail_at=now_ms(),
                    )
                ]
            )
        except Exception as exc:
            logger.debug(
                "account failure record update failed: token={}... error={}",
                token[:10],
                exc,
            )

    async def _apply_single_mode(
        self,
        record: AccountRecord,
        mode_id: int,
        window: QuotaWindow | None,
        *,
        is_use: bool = False,
        use_at_ms: int | None = None,
    ) -> None:
        qs = record.quota_set()
        mode_key = _MODE_KEYS.get(mode_id)
        if mode_key is None:
            logger.warning(
                "account single-mode sync skipped: token={}... pool={} mode_id={} reason=unknown_mode",
                record.token[:10],
                record.pool,
                mode_id,
            )
            return

        quota_patch: dict[str, dict] = {}
        if window is not None:
            normalized = normalize_quota_window(record.pool, mode_id, window)
            if normalized is None:
                logger.debug(
                    "account single-mode quota patch skipped: token={}... pool={} mode_id={} reason=unsupported_mode",
                    record.token[:10],
                    record.pool,
                    mode_id,
                )
                return
            quota_patch[mode_key] = normalized.to_dict()
        else:
            existing = qs.get(mode_id)
            if existing is not None:
                now = now_ms()
                # 如果窗口已过期，重置为默认值（适用于本地管理的配额，如 console）
                if existing.is_window_expired(now):
                    default = default_quota_window(record.pool, mode_id)
                    if default is not None:
                        new_remaining = max(0, default.total - 1)  # 本次调用消耗1次
                        # console (mode_id=5) 阈值轮换策略：reset_at=None，
                        # 让后续扣减在 remaining<=12 时再启动计时器，与 else 分支一致；
                        # 非 console 模式保持原行为：首次使用即启动计时器
                        if mode_id == 5:
                            reset_at = None
                        else:
                            reset_at = now + default.window_seconds * 1000
                        quota_patch[mode_key] = QuotaWindow(
                            remaining=new_remaining,
                            total=default.total,
                            window_seconds=default.window_seconds,
                            reset_at=reset_at,
                            synced_at=now,
                            source=QuotaSource.DEFAULT,
                        ).to_dict()
                else:
                    # Console 配额轮换策略：remaining 降到阈值时才启动恢复计时器，
                    # 避免同一批账号被反复选中（评分机制会优先选配额充足的账号）。
                    new_remaining = max(0, existing.remaining - 1)
                    reset_at = existing.reset_at
                    if mode_id == 5:
                        # console 配额：remaining <= 12 时才启动恢复计时器
                        if reset_at is None and new_remaining <= 12 and existing.window_seconds > 0:
                            reset_at = now + existing.window_seconds * 1000
                    else:
                        # 非 console 模式保持原有逻辑：首次使用即启动计时器
                        if reset_at is None and existing.window_seconds > 0:
                            reset_at = now + existing.window_seconds * 1000
                    quota_patch[mode_key] = QuotaWindow(
                        remaining=new_remaining,
                        total=existing.total,
                        window_seconds=existing.window_seconds,
                        reset_at=reset_at,
                        synced_at=existing.synced_at,
                        source=QuotaSource.ESTIMATED,
                    ).to_dict()
            else:
                logger.debug(
                    "account single-mode quota patch skipped: token={}... pool={} mode_id={} reason=unsupported_mode",
                    record.token[:10],
                    record.pool,
                    mode_id,
                )

        from .commands import AccountPatch

        await self._repo.patch_accounts(
            [
                AccountPatch(
                    token=record.token,
                    last_sync_at=now_ms() if window is not None else None,
                    usage_sync_delta=1 if window is not None else None,
                    usage_use_delta=1 if is_use else None,
                    last_use_at=use_at_ms if is_use else None,
                    **quota_patch,  # type: ignore[arg-type]
                )
            ]
        )

    async def _expire_invalid_credentials(
        self, record: AccountRecord, exc: UpstreamError
    ) -> bool:
        from .invalid_credentials import mark_account_invalid_credentials

        return await mark_account_invalid_credentials(
            self._repo,
            record.token,
            exc,
            source="usage refresh",
        )

    # ------------------------------------------------------------------
    # Console 配额窗口自动重置（后台定时巡检）
    # ------------------------------------------------------------------

    async def reset_expired_console_windows(self) -> int:
        """批量重置过期/卡死的 console 配额（委托给存储后端的 SQL 优化）。

        Returns:
            重置的账号数量。
        """
        count = await self._repo.reset_expired_console_windows()
        if count > 0:
            logger.debug("console quota windows auto-reset: count={}", count)
        return count

    async def recover_console_expired_accounts(self) -> int:
        """Repair accounts written by the retired Console-429 expiry policy.

        Only records carrying the exact legacy state reason are eligible.  A
        future Console reset remains COOLING; a passed or absent reset restores
        the local Console window and ACTIVE state immediately.  This prevents a
        rate limit from being mistaken for invalid credentials while leaving all
        genuine expired/disabled accounts untouched.
        """
        from .commands import AccountPatch

        legacy_reason = "console_429_threshold_exceeded"
        now = now_ms()
        snapshot = await self._repo.runtime_snapshot()
        clear_patches: list[AccountPatch] = []
        follow_up_patches: list[AccountPatch] = []

        for record in snapshot.items:
            if (
                record.is_deleted()
                or record.state_reason != legacy_reason
                or record.status not in {AccountStatus.EXPIRED, AccountStatus.COOLING}
            ):
                continue

            console_window = record.quota_set().get(5)
            reset_at = console_window.reset_at if console_window is not None else None
            clear_patches.append(AccountPatch(token=record.token, clear_failures=True))

            if reset_at is not None and reset_at > now:
                follow_up_patches.append(
                    AccountPatch(
                        token=record.token,
                        status=AccountStatus.COOLING,
                        # Keep the marker only until its known reset has
                        # passed; the next maintenance pass restores ACTIVE.
                        state_reason=legacy_reason,
                        ext_merge={
                            "cooldown_until": reset_at,
                            "cooldown_reason": "console_rate_limited",
                        },
                    )
                )
                continue

            default = default_quota_window(record.pool, 5)
            if default is not None:
                follow_up_patches.append(
                    AccountPatch(
                        token=record.token,
                        quota_console=QuotaWindow(
                            remaining=default.total,
                            total=default.total,
                            window_seconds=default.window_seconds,
                            reset_at=None,
                            synced_at=now,
                            source=QuotaSource.DEFAULT,
                        ).to_dict(),
                    )
                )

        if not clear_patches:
            return 0
        await self._repo.patch_accounts(clear_patches)
        if follow_up_patches:
            await self._repo.patch_accounts(follow_up_patches)
        logger.info("legacy console 429 account states repaired: count={}", len(clear_patches))
        return len(clear_patches)


__all__ = ["AccountRefreshService", "RefreshResult"]
