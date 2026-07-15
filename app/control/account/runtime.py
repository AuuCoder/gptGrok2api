"""Account runtime singletons and hot-apply helpers.

These helpers expose the process-local account refresh runtime without making
callers import ``app.main``. Admin handlers use them to reconcile strategy and
scheduler state after hot config updates.
"""

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .refresh import AccountRefreshService
    from .scheduler import AccountRefreshScheduler

_refresh_service: "AccountRefreshService | None" = None
_refresh_scheduler: "AccountRefreshScheduler | None" = None
_refresh_scheduler_leader = False


def set_refresh_service(service: "AccountRefreshService | None") -> None:
    """Register the process-global account refresh service."""
    global _refresh_service
    _refresh_service = service


def get_refresh_service() -> "AccountRefreshService | None":
    """Return the registered account refresh service, if any."""
    return _refresh_service


def set_refresh_scheduler(scheduler: "AccountRefreshScheduler | None") -> None:
    """Register the process-global account refresh scheduler."""
    global _refresh_scheduler
    _refresh_scheduler = scheduler


def get_refresh_scheduler() -> "AccountRefreshScheduler | None":
    """Return the registered account refresh scheduler, if any."""
    return _refresh_scheduler


def set_refresh_scheduler_leader(is_leader: bool) -> None:
    """Record whether this worker currently owns the refresh scheduler lock."""
    global _refresh_scheduler_leader
    _refresh_scheduler_leader = bool(is_leader)


def is_refresh_scheduler_leader() -> bool:
    """Return True when this worker is the active refresh-scheduler leader."""
    return _refresh_scheduler_leader


def reconcile_refresh_runtime(
    enabled: bool | None = None,
) -> Literal["quota", "random"]:
    """Hot-apply refresh strategy and scheduler state for the current worker."""
    from app.dataplane.account.selector import current_strategy, set_strategy
    from app.platform.config.snapshot import config
    from app.platform.logging.logger import logger

    refresh_enabled = (
        config.get_bool("account.refresh.enabled", False)
        if enabled is None
        else bool(enabled)
    )
    target_strategy: Literal["quota", "random"] = (
        "quota" if refresh_enabled else "random"
    )
    previous_strategy = current_strategy()
    if previous_strategy != target_strategy:
        set_strategy(target_strategy)

    scheduler_action = "unchanged"
    scheduler = _refresh_scheduler
    if scheduler is not None and _refresh_scheduler_leader:
        if refresh_enabled:
            if not scheduler.is_running():
                scheduler.start()
                scheduler_action = "started"
        elif scheduler.is_running():
            scheduler.stop()
            scheduler_action = "stopped"

    if previous_strategy != target_strategy or scheduler_action != "unchanged":
        logger.info(
            "account refresh runtime reconciled: previous_strategy={} strategy={} leader={} scheduler_action={}",
            previous_strategy,
            target_strategy,
            _refresh_scheduler_leader,
            scheduler_action,
        )
    return target_strategy


__all__ = [
    "get_refresh_service",
    "set_refresh_service",
    "get_refresh_scheduler",
    "set_refresh_scheduler",
    "is_refresh_scheduler_leader",
    "set_refresh_scheduler_leader",
    "reconcile_refresh_runtime",
]
