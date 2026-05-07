from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from .client import HypixelAuctionClient, HypixelBazaarClient
from .config import MarketGuardSettings
from .exceptions import HypixelUpstreamError, MarketGuardStorageError
from .item_keys import resolve_auction_item
from .models import BazaarSnapshot, LowestBinSnapshot, LowestBinV2Entry, LowestBinV2Snapshot
from .storage import MarketGuardStorage, StoredLowestBinSnapshot, snapshot_day_from_last_updated

logger = logging.getLogger(__name__)


class LowestBinService:
    def __init__(
        self,
        settings: MarketGuardSettings,
        client: HypixelAuctionClient | None = None,
        clock: Callable[[], float] | None = None,
        storage: MarketGuardStorage | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or HypixelAuctionClient(settings)
        self._clock = clock or _utc_epoch_seconds
        self._storage = storage or MarketGuardStorage(settings.database_url, settings.history_retention_days)
        self._last_auctioneer_uuids: dict[str, str] = {}
        self._last_item_names: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_lowest_bins(self) -> LowestBinSnapshot:
        async with self._lock:
            now = self._clock()
            stored = await asyncio.to_thread(self._storage.read_lowestbin_snapshot)
            if stored is not None:
                self._remember_snapshot_metadata(stored)
                if _is_snapshot_fresh(stored.snapshot, now, self._settings.cache_ttl_seconds):
                    return stored.snapshot

            try:
                snapshot = await self._refresh_snapshot()
            except HypixelUpstreamError:
                if stored is not None and _can_serve_snapshot_stale(
                    stored.snapshot,
                    now,
                    self._settings.stale_if_error_seconds,
                ):
                    logger.warning("Serving stale MarketGuard Lowest BIN data from MariaDB after Hypixel refresh failure.")
                    return replace(stored.snapshot, is_stale=True)
                raise

            await self._persist_snapshot(snapshot)
            return snapshot

    async def get_lowest_bins_v2(self) -> LowestBinV2Snapshot:
        snapshot = await self.get_lowest_bins()
        anchor_day = snapshot_day_from_last_updated(snapshot.snapshot_last_updated)
        averages = await asyncio.to_thread(
            self._storage.get_averages,
            list(snapshot.items.keys()),
            anchor_day=anchor_day,
        )
        items: dict[str, LowestBinV2Entry] = {}

        for item_key, price in snapshot.items.items():
            auctioneer_uuid = self._find_auctioneer_uuid_for_price(item_key, price)
            if auctioneer_uuid is None:
                continue
            average_window = averages.get(item_key)
            items[item_key] = LowestBinV2Entry(
                price=price,
                auctioneer_uuid=auctioneer_uuid,
                item_name=self._find_item_name_for_key(item_key),
                avg_7d=None if average_window is None else average_window.avg_7d,
                avg_30d=None if average_window is None else average_window.avg_30d,
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
            generated_at=_clock_datetime(self._clock()),
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

    async def _persist_snapshot(self, snapshot: LowestBinSnapshot) -> None:
        try:
            await asyncio.to_thread(
                self._storage.write_lowestbin_snapshot,
                snapshot,
                auctioneer_uuids=self._last_auctioneer_uuids,
                item_names=self._last_item_names,
            )
        except MarketGuardStorageError:
            logger.exception(
                "Failed to persist Lowest BIN snapshot %s.",
                snapshot.snapshot_last_updated,
            )
            raise

    def _remember_snapshot_metadata(self, stored: StoredLowestBinSnapshot) -> None:
        self._last_auctioneer_uuids = dict(stored.auctioneer_uuids)
        self._last_item_names = dict(stored.item_names)

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
        storage: MarketGuardStorage | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or HypixelBazaarClient(settings)
        self._clock = clock or _utc_epoch_seconds
        self._storage = storage or MarketGuardStorage(settings.database_url, settings.history_retention_days)
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_bazaar(self) -> BazaarSnapshot:
        async with self._lock:
            now = self._clock()
            cached = await asyncio.to_thread(self._storage.read_bazaar_snapshot)
            if cached is not None and _is_snapshot_fresh(cached, now, self._settings.cache_ttl_seconds):
                return cached

            try:
                snapshot = await self._refresh_snapshot()
            except HypixelUpstreamError:
                if cached is not None and _can_serve_snapshot_stale(cached, now, self._settings.stale_if_error_seconds):
                    logger.warning("Serving stale MarketGuard bazaar data from MariaDB after Hypixel refresh failure.")
                    return replace(cached, is_stale=True)
                raise

            try:
                await asyncio.to_thread(self._storage.write_bazaar_snapshot, snapshot)
            except MarketGuardStorageError:
                logger.exception("Failed to persist MarketGuard bazaar snapshot %s.", snapshot.snapshot_last_updated)
                raise
            return snapshot

    async def _refresh_snapshot(self) -> BazaarSnapshot:
        bazaar_snapshot = await self._client.fetch_snapshot()
        products: dict[str, dict[str, float | int | str]] = {}

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
                "item_name": _bazaar_item_name(product_id),
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
            generated_at=_clock_datetime(self._clock()),
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


def _bazaar_item_name(product_id: object) -> str:
    normalized_product_id = str(product_id or "").strip()
    if not normalized_product_id:
        return ""
    words = [part for part in normalized_product_id.split("_") if part]
    if not words:
        return normalized_product_id
    return " ".join(words).title()


def _utc_epoch_seconds() -> float:
    return datetime.now(timezone.utc).timestamp()


def _clock_datetime(clock_value: float) -> datetime:
    return datetime.fromtimestamp(float(clock_value), tz=timezone.utc)


def _is_snapshot_fresh(snapshot: LowestBinSnapshot | BazaarSnapshot, now_epoch_seconds: float, ttl_seconds: int) -> bool:
    return (now_epoch_seconds - snapshot.generated_at.timestamp()) < ttl_seconds


def _can_serve_snapshot_stale(
    snapshot: LowestBinSnapshot | BazaarSnapshot,
    now_epoch_seconds: float,
    stale_if_error_seconds: int,
) -> bool:
    return (now_epoch_seconds - snapshot.generated_at.timestamp()) < stale_if_error_seconds
