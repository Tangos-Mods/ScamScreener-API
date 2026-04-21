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
from .models import BazaarSnapshot, LowestBinSnapshot, LowestBinV2Entry, LowestBinV2Snapshot

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
        self._last_auctioneer_uuids: dict[str, str] = {}
        self._last_item_names: dict[str, str] = {}
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

    async def get_lowest_bins_v2(self) -> LowestBinV2Snapshot:
        snapshot = await self.get_lowest_bins()
        items: dict[str, LowestBinV2Entry] = {}

        for item_key, price in snapshot.items.items():
            auctioneer_uuid = self._find_auctioneer_uuid_for_price(item_key, price)
            if auctioneer_uuid is None:
                continue
            items[item_key] = LowestBinV2Entry(
                price=price,
                auctioneer_uuid=auctioneer_uuid,
                item_name=self._find_item_name_for_key(item_key),
            )

        return LowestBinV2Snapshot(
            generated_at=snapshot.generated_at,
            snapshot_last_updated=snapshot.snapshot_last_updated,
            total_pages=snapshot.total_pages,
            total_auctions=snapshot.total_auctions,
            total_bin_auctions=snapshot.total_bin_auctions,
            items=items,
            is_stale=snapshot.is_stale,
        )

    async def _refresh_snapshot(self) -> LowestBinSnapshot:
        auction_snapshot = await self._client.fetch_snapshot()
        lowest_bins: dict[str, float] = {}
        auctioneer_uuids: dict[str, str] = {}
        item_names: dict[str, str] = {}
        total_bin_auctions = 0

        for auction in auction_snapshot.auctions:
            if auction.get("bin") is not True:
                continue

            resolved_item = resolve_auction_item(auction)
            if resolved_item is None:
                continue

            auctioneer_uuid = _parse_auctioneer_uuid(auction.get("auctioneer"))
            if auctioneer_uuid is None:
                continue

            total_bin_auctions += 1
            for item_key in resolved_item.keys:
                current_lowest = lowest_bins.get(item_key)
                if current_lowest is None or resolved_item.unit_price < current_lowest:
                    lowest_bins[item_key] = resolved_item.unit_price
                    auctioneer_uuids[item_key] = auctioneer_uuid
                    item_names[item_key] = _parse_item_name(auction.get("item_name"), item_key)

        snapshot = LowestBinSnapshot(
            generated_at=datetime.now(timezone.utc),
            snapshot_last_updated=auction_snapshot.last_updated,
            total_pages=auction_snapshot.total_pages,
            total_auctions=len(auction_snapshot.auctions),
            total_bin_auctions=total_bin_auctions,
            items=dict(sorted(lowest_bins.items(), key=lambda item: item[0].lower())),
            is_stale=False,
        )
        self._last_auctioneer_uuids = auctioneer_uuids
        self._last_item_names = item_names
        return snapshot

    def _find_auctioneer_uuid_for_price(self, item_key: str, price: float) -> str | None:
        auctioneer_uuids = getattr(self, "_last_auctioneer_uuids", {})
        auctioneer_uuid = auctioneer_uuids.get(item_key)
        if not auctioneer_uuid:
            logger.warning("Missing auctioneer UUID for Lowest BIN key %s at price %s.", item_key, price)
            return None
        return auctioneer_uuid

    def _find_item_name_for_key(self, item_key: str) -> str:
        item_names = getattr(self, "_last_item_names", {})
        item_name = item_names.get(item_key)
        if item_name:
            return item_name
        logger.warning("Missing item_name for Lowest BIN key %s.", item_key)
        return item_key


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


def _parse_auctioneer_uuid(value: object) -> str | None:
    parsed = str(value or "").strip().lower()
    if len(parsed) != 32 or not all(character in "0123456789abcdef" for character in parsed):
        return None
    return parsed


def _parse_item_name(value: object, fallback: str) -> str:
    parsed = str(value or "").strip()
    return parsed or fallback
