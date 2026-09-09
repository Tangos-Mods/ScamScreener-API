from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Any

from .client import HypixelAuctionClient, HypixelBazaarClient
from .config import MarketGuardSettings
from .exceptions import HypixelUpstreamError, MarketGuardStorageError
from .item_keys import resolve_auction_item
from .models import BazaarSnapshot, LowestBinSnapshot, LowestBinV2Entry, LowestBinV2Snapshot
from .storage import MarketGuardStorage, StoredLowestBinSnapshot, snapshot_day_from_last_updated

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LowestBinSnapshotMetadata:
    """Per-item metadata belonging to exactly one Lowest BIN snapshot."""

    auctioneer_uuids: dict[str, str]
    item_names: dict[str, str]


@dataclass(frozen=True, slots=True)
class LowestBinIndex:
    lowest_bins: dict[str, float]
    metadata: LowestBinSnapshotMetadata
    total_bin_auctions: int


class LowestBinIndexBuilder:
    """Accumulates the Lowest BIN index one auction page at a time.

    The index itself is tiny - a few hundred kilobytes of item key to price.
    What used to be expensive was the input: collecting all ~100k auction
    payloads before indexing them pushed the process past 700 MiB and got it
    OOM-killed on a 2 GiB host. Folding page by page keeps only the pages
    currently in flight.
    """

    __slots__ = ("lowest_bins", "auctioneer_uuids", "item_names", "total_bin_auctions")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.lowest_bins: dict[str, float] = {}
        self.auctioneer_uuids: dict[str, str] = {}
        self.item_names: dict[str, str] = {}
        self.total_bin_auctions = 0

    def add_page(self, auctions: list[dict[str, Any]]) -> None:
        fold_auctions_into_index(auctions, self)

    def build(self) -> LowestBinIndex:
        return LowestBinIndex(
            lowest_bins=dict(sorted(self.lowest_bins.items(), key=lambda item: item[0].lower())),
            metadata=LowestBinSnapshotMetadata(
                auctioneer_uuids=self.auctioneer_uuids,
                item_names=self.item_names,
            ),
            total_bin_auctions=self.total_bin_auctions,
        )


def fold_auctions_into_index(auctions: list[dict[str, Any]], accumulator: LowestBinIndexBuilder) -> None:
    """Decode one page of auctions and fold it into ``accumulator``.

    This is pure CPU work: a full Hypixel auction house is ~100k auctions, and
    each one costs a base64 decode, a gzip inflate and an NBT parse. Measured at
    5-10 seconds for a full house, so it must never run on the event loop -
    doing so stalls every other request, including the health and readiness
    probes. Callers run it through ``asyncio.to_thread``.
    """
    lowest_bins = accumulator.lowest_bins
    auctioneer_uuids = accumulator.auctioneer_uuids
    item_names = accumulator.item_names
    page_bin_auctions = 0

    for auction in auctions:
        if auction.get("bin") is not True:
            continue

        resolved_item = resolve_auction_item(auction)
        if resolved_item is None:
            continue

        auctioneer_uuid = _parse_auctioneer_uuid(auction.get("auctioneer"))
        if auctioneer_uuid is None:
            continue

        page_bin_auctions += 1
        for item_key in resolved_item.keys:
            current_lowest = lowest_bins.get(item_key)
            if current_lowest is None or resolved_item.unit_price < current_lowest:
                lowest_bins[item_key] = resolved_item.unit_price
                auctioneer_uuids[item_key] = auctioneer_uuid
                item_names[item_key] = _parse_item_name(auction.get("item_name"), item_key)

    accumulator.total_bin_auctions += page_bin_auctions


def build_lowestbin_index(auctions: list[dict[str, Any]]) -> LowestBinIndex:
    """Index a complete auction list in one call.

    Kept for callers that already hold every auction; the refresh path streams
    instead, via :class:`LowestBinIndexBuilder`.
    """
    builder = LowestBinIndexBuilder()
    builder.add_page(auctions)
    return builder.build()


class _IndexingPageConsumer:
    """Feeds streamed auction pages into a :class:`LowestBinIndexBuilder`."""

    __slots__ = ("_builder",)

    def __init__(self) -> None:
        self._builder = LowestBinIndexBuilder()

    def reset(self) -> None:
        self._builder.reset()

    async def add_page(self, auctions: list[dict[str, Any]]) -> None:
        # Decoding ~100k NBT item payloads is seconds of CPU; keep it off the
        # event loop so health, readiness and every other route stay responsive.
        await asyncio.to_thread(self._builder.add_page, auctions)

    def build(self) -> LowestBinIndex:
        return self._builder.build()


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
        self._lock = asyncio.Lock()
        # Flipped on by the background refresher. While it owns the upstream
        # fetch, a request must never trigger one itself: a Hypixel refresh
        # costs tens of seconds, and paying it inside a request is what turned
        # a slow refresh into an outage for every other caller.
        self._inline_refresh_enabled = True

    @property
    def inline_refresh_enabled(self) -> bool:
        return self._inline_refresh_enabled

    def disable_inline_refresh(self) -> None:
        self._inline_refresh_enabled = False

    def enable_inline_refresh(self) -> None:
        self._inline_refresh_enabled = True

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_lowest_bins(self) -> LowestBinSnapshot:
        snapshot, _metadata = await self._load_snapshot()
        return snapshot

    async def get_lowest_bins_v2(self) -> LowestBinV2Snapshot:
        snapshot, metadata = await self._load_snapshot()
        anchor_day = snapshot_day_from_last_updated(snapshot.snapshot_last_updated)
        averages = await asyncio.to_thread(
            self._storage.get_averages,
            list(snapshot.items.keys()),
            anchor_day=anchor_day,
        )
        items: dict[str, LowestBinV2Entry] = {}

        for item_key, price in snapshot.items.items():
            auctioneer_uuid = _lookup_auctioneer_uuid(metadata, item_key, price)
            if auctioneer_uuid is None:
                continue
            average_window = averages.get(item_key)
            items[item_key] = LowestBinV2Entry(
                price=price,
                auctioneer_uuid=auctioneer_uuid,
                item_name=_lookup_item_name(metadata, item_key),
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

    async def refresh(self) -> LowestBinSnapshot:
        """Fetch a fresh snapshot from Hypixel and persist it.

        Entry point for the background refresher. Serialized against inline
        refreshes by the same lock so only one upstream fetch is ever in flight.
        """
        async with self._lock:
            snapshot, metadata = await self._refresh_snapshot()
            await self._persist_snapshot(snapshot, metadata)
            return snapshot

    async def _load_snapshot(self) -> tuple[LowestBinSnapshot, LowestBinSnapshotMetadata]:
        """Serve the current snapshot, refreshing inline only when unavoidable.

        The read path deliberately takes no lock: a stored snapshot is served
        straight from MariaDB even while a refresh is running, so a slow Hypixel
        fetch can never queue up client requests behind it.
        """
        now = self._clock()
        stored = await asyncio.to_thread(self._storage.read_lowestbin_snapshot)
        if stored is not None:
            metadata = _metadata_from_stored(stored)
            if _is_snapshot_fresh(stored.snapshot, now, self._settings.cache_ttl_seconds):
                return stored.snapshot, metadata
            if not self._inline_refresh_enabled and _can_serve_snapshot_stale(
                stored.snapshot,
                now,
                self._settings.stale_if_error_seconds,
            ):
                # The refresher is behind (Hypixel slow or down). Serving the
                # last good snapshot beats making the caller wait for upstream.
                return replace(stored.snapshot, is_stale=True), metadata

        async with self._lock:
            # Another caller may have refreshed while we waited for the lock.
            recheck_now = self._clock()
            recheck = await asyncio.to_thread(self._storage.read_lowestbin_snapshot)
            if recheck is not None and _is_snapshot_fresh(
                recheck.snapshot,
                recheck_now,
                self._settings.cache_ttl_seconds,
            ):
                return recheck.snapshot, _metadata_from_stored(recheck)

            fallback = recheck if recheck is not None else stored
            try:
                snapshot, metadata = await self._refresh_snapshot()
            except HypixelUpstreamError:
                if fallback is not None and _can_serve_snapshot_stale(
                    fallback.snapshot,
                    recheck_now,
                    self._settings.stale_if_error_seconds,
                ):
                    logger.warning("Serving stale MarketGuard Lowest BIN data from MariaDB after Hypixel refresh failure.")
                    return replace(fallback.snapshot, is_stale=True), _metadata_from_stored(fallback)
                raise

            await self._persist_snapshot(snapshot, metadata)
            return snapshot, metadata

    async def _refresh_snapshot(self) -> tuple[LowestBinSnapshot, LowestBinSnapshotMetadata]:
        # Stream the auction house instead of collecting it: the payloads only
        # matter while they are being decoded, and holding all of them at once
        # is what made the host OOM-kill this process.
        consumer = _IndexingPageConsumer()
        summary = await self._client.stream_snapshot(consumer)
        index = consumer.build()

        snapshot = LowestBinSnapshot(
            generated_at=_clock_datetime(self._clock()),
            snapshot_last_updated=summary.last_updated,
            total_pages=summary.total_pages,
            total_auctions=summary.total_auctions,
            total_bin_auctions=index.total_bin_auctions,
            items=index.lowest_bins,
            is_stale=False,
        )
        return snapshot, index.metadata

    async def _persist_snapshot(
        self,
        snapshot: LowestBinSnapshot,
        metadata: LowestBinSnapshotMetadata,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._storage.write_lowestbin_snapshot,
                snapshot,
                auctioneer_uuids=metadata.auctioneer_uuids,
                item_names=metadata.item_names,
            )
        except MarketGuardStorageError:
            logger.exception(
                "Failed to persist Lowest BIN snapshot %s.",
                snapshot.snapshot_last_updated,
            )
            raise


def _metadata_from_stored(stored: StoredLowestBinSnapshot) -> LowestBinSnapshotMetadata:
    return LowestBinSnapshotMetadata(
        auctioneer_uuids=dict(stored.auctioneer_uuids),
        item_names=dict(stored.item_names),
    )


def _lookup_auctioneer_uuid(metadata: LowestBinSnapshotMetadata, item_key: str, price: float) -> str | None:
    auctioneer_uuid = metadata.auctioneer_uuids.get(item_key)
    if not auctioneer_uuid:
        logger.warning("Missing auctioneer UUID for Lowest BIN key %s at price %s.", item_key, price)
        return None
    return auctioneer_uuid


def _lookup_item_name(metadata: LowestBinSnapshotMetadata, item_key: str) -> str:
    item_name = metadata.item_names.get(item_key)
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
        self._inline_refresh_enabled = True

    @property
    def inline_refresh_enabled(self) -> bool:
        return self._inline_refresh_enabled

    def disable_inline_refresh(self) -> None:
        self._inline_refresh_enabled = False

    def enable_inline_refresh(self) -> None:
        self._inline_refresh_enabled = True

    async def aclose(self) -> None:
        await self._client.aclose()

    async def refresh(self) -> BazaarSnapshot:
        """Fetch fresh bazaar data from Hypixel and persist it."""
        async with self._lock:
            snapshot = await self._refresh_snapshot()
            await self._persist_snapshot(snapshot)
            return snapshot

    async def get_bazaar(self) -> BazaarSnapshot:
        now = self._clock()
        cached = await asyncio.to_thread(self._storage.read_bazaar_snapshot)
        if cached is not None:
            if _is_snapshot_fresh(cached, now, self._settings.cache_ttl_seconds):
                return cached
            if not self._inline_refresh_enabled and _can_serve_snapshot_stale(
                cached,
                now,
                self._settings.stale_if_error_seconds,
            ):
                return replace(cached, is_stale=True)

        async with self._lock:
            recheck_now = self._clock()
            recheck = await asyncio.to_thread(self._storage.read_bazaar_snapshot)
            if recheck is not None and _is_snapshot_fresh(recheck, recheck_now, self._settings.cache_ttl_seconds):
                return recheck

            fallback = recheck if recheck is not None else cached
            try:
                snapshot = await self._refresh_snapshot()
            except HypixelUpstreamError:
                if fallback is not None and _can_serve_snapshot_stale(
                    fallback,
                    recheck_now,
                    self._settings.stale_if_error_seconds,
                ):
                    logger.warning("Serving stale MarketGuard bazaar data from MariaDB after Hypixel refresh failure.")
                    return replace(fallback, is_stale=True)
                raise

            await self._persist_snapshot(snapshot)
            return snapshot

    async def _persist_snapshot(self, snapshot: BazaarSnapshot) -> None:
        try:
            await asyncio.to_thread(self._storage.write_bazaar_snapshot, snapshot)
        except MarketGuardStorageError:
            logger.exception("Failed to persist MarketGuard bazaar snapshot %s.", snapshot.snapshot_last_updated)
            raise

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
