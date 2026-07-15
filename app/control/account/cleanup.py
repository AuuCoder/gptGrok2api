"""Deleted account physical cleanup helpers."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_ms

_DAY_MS = 86_400_000
_DAY_SECONDS = 86_400
_DEFAULT_RUN_AT = "03:30"


@dataclass(frozen=True)
class CleanupResult:
    purged: int = 0
    skipped: bool = False
    reason: str = ""


def cleanup_threshold_ms(now_ms_value: int, retention_days: int) -> int:
    return now_ms_value - max(0, int(retention_days)) * _DAY_MS


def _parse_run_at(run_at: str) -> tuple[int, int]:
    try:
        hour_s, minute_s = str(run_at or _DEFAULT_RUN_AT).split(":", 1)
        hour = int(hour_s)
        minute = int(minute_s)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (TypeError, ValueError):
        pass
    return 3, 30


def seconds_until_next_daily_run(*, now_ms_value: int, run_at: str) -> int:
    hour, minute = _parse_run_at(run_at)
    now_dt = datetime.fromtimestamp(now_ms_value / 1000)
    target = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_dt:
        target += timedelta(days=1)
    return max(1, min(_DAY_SECONDS, int((target - now_dt).total_seconds())))


async def purge_deleted_accounts_once(
    repo,
    *,
    retention_days: int,
    batch_size: int,
    vacuum: bool,
) -> CleanupResult:
    purge = getattr(repo, "purge_deleted_accounts", None)
    if not callable(purge):
        return CleanupResult(skipped=True, reason="repository_does_not_support_purge")

    threshold = cleanup_threshold_ms(now_ms(), retention_days)
    purged = await purge(
        deleted_before_ms=threshold,
        batch_size=max(1, int(batch_size)),
        vacuum=vacuum,
    )
    return CleanupResult(purged=purged)


async def run_daily_deleted_account_cleanup(
    repo,
    get_settings,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    while True:
        settings = get_settings()
        delay = seconds_until_next_daily_run(
            now_ms_value=now_ms(),
            run_at=settings["run_at"],
        )
        try:
            if stop_event is None:
                await asyncio.sleep(delay)
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    pass
            settings = get_settings()
            if settings["retention_days"] < 0:
                continue
            result = await purge_deleted_accounts_once(
                repo,
                retention_days=settings["retention_days"],
                batch_size=settings["batch_size"],
                vacuum=settings["vacuum"],
            )
            if result.skipped:
                logger.debug(
                    "deleted account cleanup skipped: reason={}",
                    result.reason,
                )
            elif result.purged:
                logger.info("deleted account cleanup completed: purged={}", result.purged)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "deleted account cleanup failed: error_type={} error={}",
                type(exc).__name__,
                exc,
            )
