from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from .client import HypixelAuctionClient, HypixelBazaarClient
from .config import MarketGuardSettings
from .exceptions import HypixelUpstreamError
from .item_keys import resolve_auction_item
from .models import BazaarSnapshot, LowestBinSnapshot

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _CachedLowestBinSnapshot:
    snapshot: LowestBinSnapshot
    fetched_at_monotonic: float

    def is_fresh(self, now: float, ttl_seconds: int) -> bool:
        return (now - self.fetched_at_monotonic) < ttl_seconds

    def can_serve_stale(self, now: float, stale_if_error_seconds: int) -> bool:
        return (now - self.fetched_at_monotonic) < stale_if_error_seconds


@dataclass(slots=True)
class _CachedBazaarSnapshot:
    snapshot: BazaarSnapshot
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


class BazaarService:
    def __init__(
        self,
        settings: MarketGuardSettings,
        client: HypixelBazaarClient | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or HypixelBazaarClient(settings)
        self._clock = clock or time.monotonic
        self._cache: _CachedBazaarSnapshot | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_bazaar(self) -> BazaarSnapshot:
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
                    logger.warning("Serving stale MarketGuard bazaar cache after Hypixel refresh failure.")
                    return replace(cached.snapshot, is_stale=True)
                raise

            self._cache = _CachedBazaarSnapshot(snapshot=snapshot, fetched_at_monotonic=now)
            return snapshot

    async def _refresh_snapshot(self) -> BazaarSnapshot:
        bazaar_snapshot = await self._client.fetch_snapshot()
        products: dict[str, dict[str, float | int]] = {}

        for product_id, quick_status in bazaar_snapshot.products.items():
            buy_price = float(quick_status["buyPrice"])
            sell_price = float(quick_status["sellPrice"])
            buy_volume = int(quick_status["buyVolume"])
            sell_volume = int(quick_status["sellVolume"])
            buy_moving_week = int(quick_status["buyMovingWeek"])
            sell_moving_week = int(quick_status["sellMovingWeek"])
            spread = _decimal_difference(buy_price, sell_price)
            spread_percentage = _spread_percentage(spread, sell_price)

            products[product_id] = {
                "buy": buy_price,
                "sell": sell_price,
                "spread": spread,
                "spreadPercentage": spread_percentage,
                "buyVolume": buy_volume,
                "sellVolume": sell_volume,
                "buyMovingWeek": buy_moving_week,
                "sellMovingWeek": sell_moving_week,
            }

        return BazaarSnapshot(
            generated_at=datetime.now(timezone.utc),
            snapshot_last_updated=bazaar_snapshot.last_updated,
            products=dict(sorted(products.items(), key=lambda item: item[0].lower())),
            is_stale=False,
        )


def _decimal_difference(left: float, right: float) -> float:
    try:
        difference = Decimal(str(left)) - Decimal(str(right))
    except (InvalidOperation, ValueError) as exc:
        raise HypixelUpstreamError("Hypixel API returned an invalid bazaar price.") from exc
    return float(difference)


def _spread_percentage(spread: float, sell_price: float) -> float:
    if sell_price <= 0:
        return 0.0

    try:
        percentage = (Decimal(str(spread)) / Decimal(str(sell_price))) * Decimal("100")
    except (InvalidOperation, ValueError) as exc:
        raise HypixelUpstreamError("Hypixel API returned an invalid bazaar price.") from exc
    return float(percentage)
