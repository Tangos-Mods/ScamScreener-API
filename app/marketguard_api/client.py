from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

import httpx

from .config import MarketGuardSettings
from .exceptions import HypixelRateLimitError, HypixelSnapshotDriftError, HypixelUpstreamError
from .models import AuctionPage, AuctionSnapshot, BazaarProductSnapshot

logger = logging.getLogger(__name__)


class HypixelAuctionClient:
    def __init__(
        self,
        settings: MarketGuardSettings,
        client: httpx.AsyncClient | None = None,
        close_client: bool | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._close_client = (client is None) if close_client is None else close_client

    def _build_client(self) -> httpx.AsyncClient:
        connection_pool_size = max(4, self._settings.max_parallel_pages + 2)
        return httpx.AsyncClient(
            base_url=self._settings.hypixel_api_base_url,
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "User-Agent": self._settings.http_user_agent,
            },
            timeout=httpx.Timeout(self._settings.request_timeout_seconds),
            limits=httpx.Limits(
                max_connections=connection_pool_size,
                max_keepalive_connections=connection_pool_size,
            ),
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._close_client:
            await self._client.aclose()
            self._client = None

    async def fetch_snapshot(self) -> AuctionSnapshot:
        last_error: Exception | None = None
        for attempt in range(1, self._settings.snapshot_retries + 1):
            try:
                return await self._fetch_consistent_snapshot()
            except HypixelSnapshotDriftError as exc:
                last_error = exc
                if attempt >= self._settings.snapshot_retries:
                    break
                logger.warning(
                    "Hypixel auction snapshot changed during pagination on attempt %s/%s; retrying.",
                    attempt,
                    self._settings.snapshot_retries,
                )
        raise HypixelUpstreamError("Unable to obtain a consistent Hypixel auction snapshot.") from last_error

    async def _fetch_consistent_snapshot(self) -> AuctionSnapshot:
        first_page = await self._fetch_page(0)
        auctions = list(first_page.auctions)

        if first_page.total_pages > 1:
            semaphore = asyncio.Semaphore(self._settings.max_parallel_pages)

            async def _fetch_followup(page_number: int) -> AuctionPage:
                async with semaphore:
                    page = await self._fetch_page(page_number)
                if page.last_updated != first_page.last_updated:
                    raise HypixelSnapshotDriftError(
                        f"Hypixel snapshot drift detected between page 0 and page {page_number}."
                    )
                return page

            pages = await asyncio.gather(
                *(_fetch_followup(page_number) for page_number in range(1, first_page.total_pages))
            )
            for page in pages:
                auctions.extend(page.auctions)

        return AuctionSnapshot(
            total_pages=first_page.total_pages,
            last_updated=first_page.last_updated,
            auctions=auctions,
        )

    async def _fetch_page(self, page_number: int) -> AuctionPage:
        client = self._get_client()
        try:
            response = await client.get("/skyblock/auctions", params={"page": page_number})
        except httpx.TimeoutException as exc:
            raise HypixelUpstreamError("Timed out while fetching Hypixel auctions.") from exc
        except httpx.HTTPError as exc:
            raise HypixelUpstreamError("Failed to fetch Hypixel auctions.") from exc

        retry_after_header = str(response.headers.get("Retry-After", "")).strip()
        retry_after = int(retry_after_header) if retry_after_header.isdigit() else None
        if response.status_code == 429:
            raise HypixelRateLimitError("Hypixel API rate limited the request.", retry_after_seconds=retry_after)
        if response.status_code == 404 and page_number > 0:
            raise HypixelSnapshotDriftError(f"Hypixel auction page {page_number} no longer exists.")
        if response.is_error:
            raise HypixelUpstreamError(
                f"Hypixel API returned HTTP {response.status_code} for auctions page {page_number}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise HypixelUpstreamError("Hypixel API returned invalid JSON.") from exc

        return self._parse_page_payload(payload, page_number)

    def _parse_page_payload(self, payload: dict[str, Any], page_number: int) -> AuctionPage:
        if payload.get("success") is not True:
            cause = str(payload.get("cause", "unknown upstream error")).strip() or "unknown upstream error"
            raise HypixelUpstreamError(f"Hypixel API reported an unsuccessful response: {cause}.")

        auctions_raw = payload.get("auctions", [])
        if not isinstance(auctions_raw, list):
            raise HypixelUpstreamError("Hypixel API returned an invalid auctions payload.")

        try:
            total_pages = max(1, int(payload.get("totalPages", 1)))
            last_updated = int(payload.get("lastUpdated"))
        except (TypeError, ValueError) as exc:
            raise HypixelUpstreamError("Hypixel API returned invalid pagination metadata.") from exc

        auctions: list[dict[str, Any]] = []
        for auction in auctions_raw:
            if isinstance(auction, dict):
                auctions.append(auction)

        return AuctionPage(
            page_number=page_number,
            total_pages=total_pages,
            last_updated=last_updated,
            auctions=auctions,
        )


class HypixelBazaarClient:
    def __init__(
        self,
        settings: MarketGuardSettings,
        client: httpx.AsyncClient | None = None,
        close_client: bool | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._close_client = (client is None) if close_client is None else close_client

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._settings.hypixel_api_base_url,
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "User-Agent": self._settings.http_user_agent,
            },
            timeout=httpx.Timeout(self._settings.request_timeout_seconds),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._close_client:
            await self._client.aclose()
            self._client = None

    async def fetch_snapshot(self) -> BazaarProductSnapshot:
        client = self._get_client()
        try:
            response = await client.get("/skyblock/bazaar")
        except httpx.TimeoutException as exc:
            raise HypixelUpstreamError("Timed out while fetching Hypixel bazaar data.") from exc
        except httpx.HTTPError as exc:
            raise HypixelUpstreamError("Failed to fetch Hypixel bazaar data.") from exc

        retry_after_header = str(response.headers.get("Retry-After", "")).strip()
        retry_after = int(retry_after_header) if retry_after_header.isdigit() else None
        if response.status_code == 429:
            raise HypixelRateLimitError("Hypixel API rate limited the request.", retry_after_seconds=retry_after)
        if response.is_error:
            raise HypixelUpstreamError(f"Hypixel API returned HTTP {response.status_code} for bazaar data.")

        try:
            payload = response.json()
        except ValueError as exc:
            raise HypixelUpstreamError("Hypixel API returned invalid JSON.") from exc

        return self._parse_payload(payload)

    def _parse_payload(self, payload: dict[str, Any]) -> BazaarProductSnapshot:
        if payload.get("success") is not True:
            cause = str(payload.get("cause", "unknown upstream error")).strip() or "unknown upstream error"
            raise HypixelUpstreamError(f"Hypixel API reported an unsuccessful response: {cause}.")

        try:
            last_updated = int(payload.get("lastUpdated"))
        except (TypeError, ValueError) as exc:
            raise HypixelUpstreamError("Hypixel API returned invalid bazaar metadata.") from exc

        products_raw = payload.get("products")
        if not isinstance(products_raw, dict):
            raise HypixelUpstreamError("Hypixel API returned an invalid bazaar payload.")

        products: dict[str, dict[str, Any]] = {}
        for product_id, product_payload in products_raw.items():
            if not isinstance(product_id, str) or not product_id:
                continue
            if not isinstance(product_payload, dict):
                continue
            quick_status = product_payload.get("quick_status")
            if not isinstance(quick_status, dict):
                continue

            buy_price = _parse_finite_number(quick_status.get("buyPrice"))
            sell_price = _parse_finite_number(quick_status.get("sellPrice"))
            buy_volume = _parse_non_negative_int(quick_status.get("buyVolume"))
            sell_volume = _parse_non_negative_int(quick_status.get("sellVolume"))
            buy_moving_week = _parse_non_negative_int(quick_status.get("buyMovingWeek"))
            sell_moving_week = _parse_non_negative_int(quick_status.get("sellMovingWeek"))
            if None in {buy_price, sell_price, buy_volume, sell_volume, buy_moving_week, sell_moving_week}:
                continue

            products[product_id] = {
                "buyPrice": buy_price,
                "sellPrice": sell_price,
                "buyVolume": buy_volume,
                "sellVolume": sell_volume,
                "buyMovingWeek": buy_moving_week,
                "sellMovingWeek": sell_moving_week,
            }

        return BazaarProductSnapshot(last_updated=last_updated, products=products)


def _parse_finite_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _parse_non_negative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed
