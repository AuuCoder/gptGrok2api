"""Proxy clearance refresh scheduler.

Periodically refreshes ClearanceBundles for managed (FlareSolverr) mode.
Previously inline in ProxyDirectory; extracted for separation of concerns.
"""

import asyncio

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.control.proxy import ProxyDirectory


class ProxyClearanceScheduler:
    """Periodically refreshes proxy clearance bundles."""

    def __init__(self, directory: ProxyDirectory) -> None:
        self._directory = directory
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("proxy clearance scheduler started")

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("proxy clearance scheduler stopped")

    async def _loop(self) -> None:
        # Warm up immediately on start so the first request doesn't block.
        await self._warm_up()
        while self._running:
            try:
                interval = self._get_interval()
                await asyncio.sleep(interval)
                if not self._running:
                    break
                await self._refresh()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "proxy clearance scheduler loop failed: error_type={} error={}",
                    type(exc).__name__,
                    exc,
                )
                await asyncio.sleep(60)

    async def _warm_up(self) -> None:
        """Pre-fetch clearance bundles without invalidating existing ones."""
        try:
            await self._directory.load()
            await self._directory.warm_up()
            logger.debug("proxy clearance warm-up completed")
        except Exception as exc:
            logger.warning("proxy clearance warm-up failed: error={}", exc)

    async def _refresh(self) -> None:
        """Build fresh clearance bundles and swap atomically (build-then-swap).

        Old bundles are kept if FlareSolverr is unavailable, so a transient
        refresh failure never leaves requests without clearance.
        """
        try:
            await self._directory.load()
            await self._directory.refresh_clearance_safe()
            logger.debug("proxy clearance refresh completed")
        except Exception as exc:
            logger.warning("proxy clearance refresh failed: error={}", exc)

    def _get_interval(self) -> int:
        """Return refresh interval in seconds from config."""
        cfg = get_config()
        return cfg.get_int("proxy.clearance.refresh_interval", 600)


__all__ = ["ProxyClearanceScheduler"]
