from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from .config import MarketGuardSettings
from .exceptions import (
    HypixelAuthenticationError,
    HypixelRateLimitError,
    HypixelSnapshotDriftError,
    HypixelUpstreamError,
    MojangUpstreamError,
)
from .models import AuctionPage, AuctionSnapshot, AuctionSnapshotSummary, BazaarProductSnapshot

logger = logging.getLogger(__name__)


class _HypixelKeyRateLimiter:
    """Enforce Hypixel's API-key quota with a rolling, process-wide window."""

    def __init__(
        self,
        *,
        max_requests: int = 300,
        window_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_requests = max(1, int(max_requests))
        self._window_seconds = max(1, int(window_seconds))
        self._clock = clock
        self._lock = asyncio.Lock()
        self._requests: deque[float] = deque()

    async def acquire(self) -> None:
        now = self._clock()
        async with self._lock:
            cutoff = now - self._window_seconds
            while self._requests and self._requests[0] <= cutoff:
                self._requests.popleft()

            if len(self._requests) >= self._max_requests:
                retry_after = max(1, math.ceil(self._requests[0] + self._window_seconds - now))
                raise HypixelRateLimitError(
                    "Configured Hypixel API key request limit reached.",
                    retry_after_seconds=retry_after,
                )

            # Count attempts, including requests that later time out, because they may still
            # have reached Hypixel and consumed the key's quota.
            self._requests.append(now)


_HYPIXEL_KEY_RATE_LIMITER = _HypixelKeyRateLimiter()


class AuctionPageConsumer(Protocol):
    """Folds auction pages as they arrive so no caller holds the whole house.

    ``reset`` is called before every snapshot attempt, because a drift retry
    has to discard whatever the previous attempt already folded in.
    """

    def reset(self) -> None: ...

    async def add_page(self, auctions: list[dict[str, Any]]) -> None: ...


class _CollectingConsumer:
    """Consumer that keeps every auction, for callers that really want them all."""

    def __init__(self) -> None:
        self.auctions: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.auctions = []

    async def add_page(self, auctions: list[dict[str, Any]]) -> None:
        self.auctions.extend(auctions)


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

    async def stream_snapshot(self, consumer: AuctionPageConsumer) -> AuctionSnapshotSummary:
        """Fetch one consistent snapshot, handing each page to ``consumer``.

        Pages are still fetched concurrently; they are just consumed as they
        land instead of being collected first. Holding all ~100k auctions at
        once cost roughly 700 MiB and got the process OOM-killed on a 2 GiB
        host, so nothing here may keep a page alive past ``add_page``.
        """
        last_error: Exception | None = None
        for attempt in range(1, self._settings.snapshot_retries + 1):
            # A retry must not inherit the pages the failed attempt folded in.
            consumer.reset()
            try:
                return await self._stream_consistent_snapshot(consumer)
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

    async def fetch_snapshot(self) -> AuctionSnapshot:
        """Fetch a snapshot and keep every auction. Prefer ``stream_snapshot``."""
        consumer = _CollectingConsumer()
        summary = await self.stream_snapshot(consumer)
        return AuctionSnapshot(
            total_pages=summary.total_pages,
            last_updated=summary.last_updated,
            auctions=consumer.auctions,
        )

    async def _stream_consistent_snapshot(self, consumer: AuctionPageConsumer) -> AuctionSnapshotSummary:
        first_page = await self._fetch_page(0)
        total_pages = first_page.total_pages
        last_updated = first_page.last_updated
        total_auctions = len(first_page.auctions)
        await consumer.add_page(first_page.auctions)
        # Drop the first page before fetching the rest; the counts above are
        # all that is still needed from it.
        del first_page

        if total_pages > 1:
            # A fixed pool of workers, each holding at most one page: a worker
            # only fetches its next page once it has handed the current one
            # over. Scheduling every page up front instead would bound the
            # concurrent *requests* but not the pages already fetched - those
            # pile up while the consumer is busy decoding, which is exactly the
            # accumulation this streaming path exists to avoid.
            page_numbers = iter(range(1, total_pages))
            # Consumption is serialised: the consumer folds into one shared
            # index, and two folds at once would corrupt it.
            consume_lock = asyncio.Lock()

            async def _worker() -> None:
                nonlocal total_auctions
                # Advancing a plain iterator is safe here: asyncio runs these
                # workers on one thread and never preempts between the next()
                # and the await that follows it.
                for page_number in page_numbers:
                    page = await self._fetch_page(page_number)
                    if page.last_updated != last_updated:
                        raise HypixelSnapshotDriftError(
                            f"Hypixel snapshot drift detected between page 0 and page {page_number}."
                        )
                    async with consume_lock:
                        total_auctions += len(page.auctions)
                        await consumer.add_page(page.auctions)
                    # Drop it before fetching the next one.
                    del page

            worker_count = min(self._settings.max_parallel_pages, total_pages - 1)
            workers = [asyncio.ensure_future(_worker()) for _ in range(worker_count)]
            try:
                await asyncio.gather(*workers)
            finally:
                # On drift (or any failure) the remaining workers would keep
                # fetching pages nobody wants; stop them right away.
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

        return AuctionSnapshotSummary(
            total_pages=total_pages,
            last_updated=last_updated,
            total_auctions=total_auctions,
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


class HypixelPlayerClient:
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
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._close_client:
            await self._client.aclose()
            self._client = None

    async def fetch_player(self, player_uuid: str) -> dict[str, Any] | None:
        payload = await self._fetch_authenticated("/player", params={"uuid": player_uuid})
        player = payload.get("player")
        if player is None:
            return None
        if not isinstance(player, dict):
            raise HypixelUpstreamError("Hypixel API returned an invalid player payload.")
        return player

    async def fetch_profiles(self, player_uuid: str) -> list[dict[str, Any]]:
        payload = await self._fetch_authenticated("/skyblock/profiles", params={"uuid": player_uuid})
        profiles = payload.get("profiles")
        if profiles is None:
            return []
        if not isinstance(profiles, list):
            raise HypixelUpstreamError("Hypixel API returned an invalid SkyBlock profiles payload.")
        return [profile for profile in profiles if isinstance(profile, dict)]

    async def fetch_profile(self, profile_id: str) -> dict[str, Any] | None:
        payload = await self._fetch_authenticated("/skyblock/profile", params={"profile": profile_id})
        profile = payload.get("profile")
        if profile is None:
            return None
        if not isinstance(profile, dict):
            raise HypixelUpstreamError("Hypixel API returned an invalid SkyBlock profile payload.")
        return profile

    async def fetch_museum(self, profile_id: str) -> dict[str, Any]:
        return await self._fetch_authenticated("/skyblock/museum", params={"profile": profile_id})

    async def fetch_skyblock_skills(self) -> dict[str, dict[str, Any]]:
        client = self._get_client()
        try:
            response = await client.get("/resources/skyblock/skills")
        except httpx.TimeoutException as exc:
            raise HypixelUpstreamError("Timed out while fetching Hypixel SkyBlock skill definitions.") from exc
        except httpx.HTTPError as exc:
            raise HypixelUpstreamError("Failed to fetch Hypixel SkyBlock skill definitions.") from exc

        self._raise_for_upstream_status(response, resource="SkyBlock skill definitions")
        payload = self._parse_success_payload(response, resource="SkyBlock skill definitions")
        skills = payload.get("skills")
        if not isinstance(skills, dict):
            raise HypixelUpstreamError("Hypixel API returned invalid SkyBlock skill definitions.")
        return {str(key): value for key, value in skills.items() if isinstance(key, str) and isinstance(value, dict)}

    async def _fetch_authenticated(self, path: str, *, params: dict[str, str]) -> dict[str, Any]:
        api_key = self._settings.hypixel_api_key.strip()
        if not api_key:
            raise HypixelUpstreamError("Hypixel player API is not configured.")

        await _HYPIXEL_KEY_RATE_LIMITER.acquire()
        client = self._get_client()
        try:
            response = await client.get(path, params=params, headers={"API-Key": api_key})
        except httpx.TimeoutException as exc:
            raise HypixelUpstreamError("Timed out while fetching Hypixel player data.") from exc
        except httpx.HTTPError as exc:
            raise HypixelUpstreamError("Failed to fetch Hypixel player data.") from exc

        self._raise_for_upstream_status(response, resource="player data")
        return self._parse_success_payload(response, resource="player data")

    @staticmethod
    def _raise_for_upstream_status(response: httpx.Response, *, resource: str) -> None:
        retry_after_header = str(response.headers.get("Retry-After", "")).strip()
        retry_after = int(retry_after_header) if retry_after_header.isdigit() else None
        if response.status_code == 429:
            raise HypixelRateLimitError("Hypixel API rate limited player data.", retry_after_seconds=retry_after)
        if response.status_code in {401, 403}:
            raise HypixelAuthenticationError("Hypixel API rejected the configured API key.")
        if response.is_error:
            raise HypixelUpstreamError(f"Hypixel API returned HTTP {response.status_code} for {resource}.")

    @staticmethod
    def _parse_success_payload(response: httpx.Response, *, resource: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise HypixelUpstreamError(f"Hypixel API returned invalid JSON for {resource}.") from exc
        if not isinstance(payload, dict):
            raise HypixelUpstreamError(f"Hypixel API returned an invalid {resource} payload.")
        if payload.get("success") is not True:
            cause = str(payload.get("cause", "")).strip().lower()
            if "api key" in cause or "apikey" in cause:
                raise HypixelAuthenticationError("Hypixel API rejected the configured API key.")
            raise HypixelUpstreamError(f"Hypixel API reported an unsuccessful {resource} response.")
        return payload


class MojangNameClient:
    _BASE_URL = "https://api.mojang.com"

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
            base_url=self._BASE_URL,
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

    async def resolve_name(self, player_name: str) -> tuple[str, str] | None:
        client = self._get_client()
        try:
            response = await client.get(f"/users/profiles/minecraft/{player_name}")
        except httpx.TimeoutException as exc:
            raise MojangUpstreamError("Timed out while resolving the Minecraft player name.") from exc
        except httpx.HTTPError as exc:
            raise MojangUpstreamError("Failed to resolve the Minecraft player name.") from exc

        if response.status_code in {204, 404}:
            return None
        if response.status_code == 429:
            raise MojangUpstreamError("Minecraft name resolution is temporarily rate limited.")
        if response.is_error:
            raise MojangUpstreamError("Minecraft name resolution is temporarily unavailable.")

        try:
            payload = response.json()
        except ValueError as exc:
            raise MojangUpstreamError("Minecraft name resolution returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise MojangUpstreamError("Minecraft name resolution returned an invalid payload.")

        player_uuid = _normalize_uuid(payload.get("id"))
        resolved_name = str(payload.get("name") or "").strip()
        if player_uuid is None or not resolved_name:
            raise MojangUpstreamError("Minecraft name resolution returned incomplete data.")
        return player_uuid, resolved_name


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


def _normalize_uuid(value: object) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "")
    if len(normalized) != 32 or not all(character in "0123456789abcdef" for character in normalized):
        return None
    return normalized
