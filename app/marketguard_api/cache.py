from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

from redis import asyncio as redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CachedResponse:
    payload: dict[str, Any]
    is_stale: bool


class LocalResponseCache:
    def __init__(self, *, ttl_seconds: int, max_entries: int) -> None:
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._lock = Lock()
        self._entries: OrderedDict[str, tuple[float, CachedResponse]] = OrderedDict()

    async def get(self, key: str) -> CachedResponse | None:
        now = time.monotonic()
        with self._lock:
            self._prune_expired(now)
            entry = self._entries.get(str(key))
            if entry is None:
                return None
            expires_at, cached = entry
            if expires_at <= now:
                self._entries.pop(str(key), None)
                return None
            self._entries.move_to_end(str(key))
            return cached

    async def set(self, key: str, entry: CachedResponse) -> None:
        now = time.monotonic()
        cache_key = str(key)
        with self._lock:
            self._prune_expired(now)
            self._entries[cache_key] = (now + self._ttl_seconds, entry)
            self._entries.move_to_end(cache_key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    async def aclose(self) -> None:
        return None

    def _prune_expired(self, now: float) -> None:
        expired_keys = [key for key, (expires_at, _entry) in self._entries.items() if expires_at <= now]
        for key in expired_keys:
            self._entries.pop(key, None)


class RedisResponseCache:
    def __init__(self, *, redis_url: str, key_prefix: str, ttl_seconds: int) -> None:
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._key_prefix = str(key_prefix or "marketguard:response").strip() or "marketguard:response"
        self._client = redis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5.0,
            socket_timeout=5.0,
            health_check_interval=30,
        )

    async def get(self, key: str) -> CachedResponse | None:
        try:
            payload = await self._client.get(self._qualified_key(key))
        except RedisError:
            logger.exception("Failed to read MarketGuard response cache from Redis.")
            return None
        if not payload:
            return None
        try:
            decoded = json.loads(payload)
            return CachedResponse(
                payload=dict(decoded["payload"]),
                is_stale=bool(decoded.get("is_stale", False)),
            )
        except (TypeError, ValueError, KeyError):
            logger.warning("Discarding invalid MarketGuard Redis cache entry for %s.", key)
            return None

    async def set(self, key: str, entry: CachedResponse) -> None:
        try:
            await self._client.set(
                self._qualified_key(key),
                json.dumps({"payload": entry.payload, "is_stale": entry.is_stale}, separators=(",", ":")),
                ex=self._ttl_seconds,
            )
        except RedisError:
            logger.exception("Failed to write MarketGuard response cache to Redis.")

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except RedisError:
            logger.exception("Failed to close MarketGuard Redis client cleanly.")

    def _qualified_key(self, key: str) -> str:
        return f"{self._key_prefix}:{key}"


class ResponseCacheChain:
    def __init__(self, *backends) -> None:
        self._backends = [backend for backend in backends if backend is not None]
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> CachedResponse | None:
        if not self._backends:
            return None
        populated_backends: list[object] = []
        for backend in self._backends:
            entry = await backend.get(key)
            if entry is None:
                populated_backends.append(backend)
                continue
            for warm_backend in populated_backends:
                await warm_backend.set(key, entry)
            return entry
        return None

    async def set(self, key: str, entry: CachedResponse) -> None:
        if not self._backends:
            return
        async with self._lock:
            for backend in self._backends:
                await backend.set(key, entry)

    async def aclose(self) -> None:
        for backend in self._backends:
            await backend.aclose()


def build_response_cache(
    *,
    local_cache_enabled: bool,
    local_cache_ttl_seconds: int,
    local_cache_max_entries: int,
    redis_enabled: bool,
    redis_url: str,
    redis_key_prefix: str,
    redis_cache_ttl_seconds: int,
) -> ResponseCacheChain | None:
    local_backend = (
        LocalResponseCache(ttl_seconds=local_cache_ttl_seconds, max_entries=local_cache_max_entries)
        if local_cache_enabled
        else None
    )
    redis_backend = (
        RedisResponseCache(
            redis_url=redis_url,
            key_prefix=redis_key_prefix,
            ttl_seconds=redis_cache_ttl_seconds,
        )
        if redis_enabled and redis_url
        else None
    )
    if local_backend is None and redis_backend is None:
        return None
    return ResponseCacheChain(local_backend, redis_backend)
