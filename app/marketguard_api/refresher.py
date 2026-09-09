"""Background refresh of the MarketGuard market snapshots.

Without this, a Hypixel refresh only ever happened inside a client request: the
first caller after the cache TTL expired paid the full cost of paginating the
auction house and decoding every item payload, and - with a single Uvicorn
worker - every other caller, plus the container health probe and the public
readiness probe, waited behind it. That is what an uptime monitor sees as the
service going offline several times an hour.

Refreshing on a timer instead keeps the stored snapshot inside its TTL, so the
request path is a plain MariaDB read and readiness stays green.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Protocol

from .config import MarketGuardSettings
from .exceptions import HypixelUpstreamError, MarketGuardStorageError

logger = logging.getLogger(__name__)

# Spread the very first refresh of each dataset so a restart does not fire every
# upstream fetch in the same instant.
_INITIAL_JITTER_SECONDS = 2.0
# Applied to every scheduled interval so multiple instances drift apart instead
# of hammering Hypixel in lockstep.
_INTERVAL_JITTER_RATIO = 0.1


class RefreshableService(Protocol):
    async def refresh(self) -> object: ...

    def disable_inline_refresh(self) -> None: ...

    def enable_inline_refresh(self) -> None: ...


class MarketSnapshotRefresher:
    """Keeps one dataset's stored snapshot fresh on a fixed cadence."""

    def __init__(
        self,
        name: str,
        service: RefreshableService,
        settings: MarketGuardSettings,
        *,
        sleep=asyncio.sleep,
    ) -> None:
        self._name = str(name)
        self._service = service
        self._settings = settings
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._first_refresh_done = asyncio.Event()

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._first_refresh_done.clear()
        self._task = asyncio.create_task(self._run(), name=f"marketguard-refresh-{self._name}")
        logger.info(
            "Started MarketGuard %s background refresher (interval %ss).",
            self._name,
            self._settings.effective_background_refresh_interval_seconds,
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        # Requests must be able to refresh again once nothing else does it.
        self._service.enable_inline_refresh()
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("MarketGuard %s background refresher failed during shutdown.", self._name)
        logger.info("Stopped MarketGuard %s background refresher.", self._name)

    async def wait_for_first_refresh(self, timeout: float | None = None) -> bool:
        """Block until the first refresh attempt settled. Test and startup hook."""
        if timeout is None:
            await self._first_refresh_done.wait()
            return True
        try:
            await asyncio.wait_for(self._first_refresh_done.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run(self) -> None:
        await self._sleep(random.uniform(0, _INITIAL_JITTER_SECONDS))
        while True:
            succeeded = await self._refresh_once()
            self._first_refresh_done.set()
            await self._sleep(self._next_delay_seconds(succeeded))

    async def _refresh_once(self) -> bool:
        try:
            await self._service.refresh()
        except asyncio.CancelledError:
            raise
        except (HypixelUpstreamError, MarketGuardStorageError) as exc:
            # Expected upstream/storage trouble: the stored snapshot is still
            # served (as stale) and the next tick retries sooner.
            logger.warning("MarketGuard %s background refresh failed: %s", self._name, exc)
            return False
        except Exception:
            logger.exception("Unexpected error during MarketGuard %s background refresh.", self._name)
            return False

        # Only now is a request guaranteed to find a snapshot it can serve
        # without going upstream itself.
        self._service.disable_inline_refresh()
        logger.debug("Refreshed MarketGuard %s snapshot.", self._name)
        return True

    def _next_delay_seconds(self, succeeded: bool) -> float:
        if not succeeded:
            return float(self._settings.background_refresh_retry_seconds)
        interval = float(self._settings.effective_background_refresh_interval_seconds)
        return interval + random.uniform(0, interval * _INTERVAL_JITTER_RATIO)


class MarketSnapshotRefreshSupervisor:
    """Owns the refreshers for every dataset served by the public API."""

    def __init__(self, refreshers: list[MarketSnapshotRefresher]) -> None:
        self._refreshers = list(refreshers)

    @property
    def refreshers(self) -> list[MarketSnapshotRefresher]:
        return list(self._refreshers)

    async def start(self) -> None:
        for refresher in self._refreshers:
            await refresher.start()

    async def stop(self) -> None:
        for refresher in self._refreshers:
            await refresher.stop()


def build_refresh_supervisor(
    settings: MarketGuardSettings | None,
    *,
    lowestbin_service: RefreshableService | None,
    bazaar_service: RefreshableService | None,
) -> MarketSnapshotRefreshSupervisor | None:
    """Build the supervisor, or ``None`` when background refresh is disabled."""
    if settings is None:
        return None
    if not settings.background_refresh_enabled:
        logger.warning(
            "MarketGuard background refresh is disabled; snapshot refreshes fall back to the request path."
        )
        return None

    refreshers: list[MarketSnapshotRefresher] = []
    for name, service in (("lowestbin", lowestbin_service), ("bazaar", bazaar_service)):
        if service is None or not _is_refreshable(service):
            continue
        refreshers.append(MarketSnapshotRefresher(name, service, settings))

    if not refreshers:
        return None
    return MarketSnapshotRefreshSupervisor(refreshers)


def _is_refreshable(service: object) -> bool:
    return all(
        callable(getattr(service, attribute, None))
        for attribute in ("refresh", "disable_inline_refresh", "enable_inline_refresh")
    )
