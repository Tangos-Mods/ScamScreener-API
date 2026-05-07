from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_CACHE_TTL_SECONDS = 6 * 60 * 60
_DEFAULT_NEGATIVE_CACHE_TTL_SECONDS = 30 * 60
_DEFAULT_LOOKUP_CONCURRENCY = 4
_PROFILE_LOOKUP_BASE_URL = "https://api.minecraftservices.com"


@dataclass(slots=True)
class _CacheEntry:
    player_name: str | None
    expires_at: float


class MojangProfileResolver:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        clock: callable | None = None,
        cache_ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
        negative_cache_ttl_seconds: int = _DEFAULT_NEGATIVE_CACHE_TTL_SECONDS,
        lookup_concurrency: int = _DEFAULT_LOOKUP_CONCURRENCY,
    ) -> None:
        self._client = client
        self._close_client = client is None
        self._clock = clock or time.monotonic
        self._cache_ttl_seconds = max(60, int(cache_ttl_seconds))
        self._negative_cache_ttl_seconds = max(60, int(negative_cache_ttl_seconds))
        self._lookup_concurrency = max(1, min(16, int(lookup_concurrency)))
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_PROFILE_LOOKUP_BASE_URL,
                follow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "ScamScreener-MarketGuardHub/1.0",
                },
                timeout=httpx.Timeout(_DEFAULT_TIMEOUT_SECONDS),
                limits=httpx.Limits(
                    max_connections=self._lookup_concurrency + 2,
                    max_keepalive_connections=self._lookup_concurrency + 2,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._close_client:
            await self._client.aclose()
            self._client = None

    async def resolve_many(self, uuids: list[str]) -> dict[str, str]:
        normalized = []
        for raw_uuid in uuids:
            parsed = _normalize_uuid(raw_uuid)
            if parsed is not None:
                normalized.append(parsed)

        if not normalized:
            return {}

        unique_uuids = list(dict.fromkeys(normalized))
        now = float(self._clock())
        resolved: dict[str, str] = {}
        to_lookup: list[str] = []

        async with self._lock:
            for player_uuid in unique_uuids:
                cached = self._cache.get(player_uuid)
                if cached is not None and cached.expires_at > now:
                    if cached.player_name:
                        resolved[player_uuid] = cached.player_name
                    continue
                to_lookup.append(player_uuid)

        if not to_lookup:
            return resolved

        semaphore = asyncio.Semaphore(self._lookup_concurrency)

        async def _lookup(player_uuid: str) -> tuple[str, str | None]:
            async with semaphore:
                return player_uuid, await self._resolve_one(player_uuid)

        fetched = await asyncio.gather(*(_lookup(player_uuid) for player_uuid in to_lookup))
        now = float(self._clock())

        async with self._lock:
            for player_uuid, player_name in fetched:
                ttl = self._cache_ttl_seconds if player_name else self._negative_cache_ttl_seconds
                self._cache[player_uuid] = _CacheEntry(player_name=player_name, expires_at=now + ttl)
                if player_name:
                    resolved[player_uuid] = player_name

        return resolved

    async def _resolve_one(self, player_uuid: str) -> str | None:
        client = self._get_client()
        try:
            response = await client.get(f"/minecraft/profile/lookup/{player_uuid}")
        except httpx.TimeoutException:
            logger.warning("Timed out while resolving Mojang profile for UUID %s.", player_uuid)
            return None
        except httpx.HTTPError:
            logger.warning("Failed to resolve Mojang profile for UUID %s.", player_uuid)
            return None

        if response.status_code in {204, 404}:
            return None
        if response.status_code == 429:
            logger.warning("Minecraft Services rate limited UUID lookup for %s.", player_uuid)
            return None
        if response.is_error:
            logger.warning(
                "Minecraft Services returned HTTP %s for UUID %s.",
                response.status_code,
                player_uuid,
            )
            return None

        try:
            payload = response.json()
        except ValueError:
            logger.warning("Minecraft Services returned invalid JSON for UUID %s.", player_uuid)
            return None

        player_name = str(payload.get("name") or "").strip()
        return player_name or None


def _normalize_uuid(value: object) -> str | None:
    parsed = str(value or "").strip().lower().replace("-", "")
    if len(parsed) != 32 or not all(character in "0123456789abcdef" for character in parsed):
        return None
    return parsed
