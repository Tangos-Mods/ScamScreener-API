from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .client import HypixelAuctionClient
from .config import MarketGuardSettings
from .exceptions import HypixelUpstreamError
from .item_keys import resolve_auction_item
from .models import LowestBinSnapshot

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _CachedLowestBinSnapshot:
    snapshot: LowestBinSnapshot
    fetched_at_monotonic: float

    def is_fresh(self, now: float, ttl_seconds: int) -> bool:
        return (now - self.fetched_at_monotonic) < ttl_seconds

    def can_serve_stale(self, now: float, stale_if_error_seconds: int) -> bool:
        return (now - self.fetched_at_monotonic) < stale_if_error_seconds


class LowestBinService:
    def __init__(
        self,
        settings: MarketGuardSettings,
        client: HypixelAuctionClient | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or HypixelAuctionClient(settings)
        self._clock = clock or time.monotonic
        self._cache: _CachedLowestBinSnapshot | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_lowest_bins(self) -> LowestBinSnapshot:
        now = self._clock()
        cached = self._cache
        if cached is not None and cached.is_fresh(now, self._settings.cache_ttl_seconds):
            return cached.snapshot

        async with self._lock:
            now = self._clock()
            cached = self._cache
            if cached is not None and cached.is_fresh(now, self._settings.cache_ttl_seconds):
                return cached.snapshot

            try:
                snapshot = await self._refresh_snapshot()
            except HypixelUpstreamError:
                if cached is not None and cached.can_serve_stale(now, self._settings.stale_if_error_seconds):
                    logger.warning("Serving stale MarketGuard Lowest BIN cache after Hypixel refresh failure.")
                    return replace(cached.snapshot, is_stale=True)
                raise

            self._cache = _CachedLowestBinSnapshot(snapshot=snapshot, fetched_at_monotonic=now)
            return snapshot

    async def _refresh_snapshot(self) -> LowestBinSnapshot:
        auction_snapshot = await self._client.fetch_snapshot()
        lowest_bins: dict[str, float] = {}
        total_bin_auctions = 0

        for auction in auction_snapshot.auctions:
            if auction.get("bin") is not True:
                continue

            resolved_item = resolve_auction_item(auction)
            if resolved_item is None:
                continue

            total_bin_auctions += 1
            for item_key in resolved_item.keys:
                current_lowest = lowest_bins.get(item_key)
                if current_lowest is None or resolved_item.unit_price < current_lowest:
                    lowest_bins[item_key] = resolved_item.unit_price

        return LowestBinSnapshot(
            generated_at=datetime.now(timezone.utc),
            snapshot_last_updated=auction_snapshot.last_updated,
            total_pages=auction_snapshot.total_pages,
            total_auctions=len(auction_snapshot.auctions),
            total_bin_auctions=total_bin_auctions,
            items=dict(sorted(lowest_bins.items(), key=lambda item: item[0].lower())),
            is_stale=False,
        )
