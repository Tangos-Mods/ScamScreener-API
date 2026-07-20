from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

import httpx

from .config import MarketGuardSettings
from .exceptions import (
    HypixelAuthenticationError,
    HypixelRateLimitError,
    HypixelSnapshotDriftError,
    HypixelUpstreamError,
    MojangUpstreamError,
)
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
