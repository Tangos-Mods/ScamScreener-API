from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any, Awaitable, Callable

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
        self._lock_prefix = f"{self._key_prefix}:lock"
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

    async def try_acquire_lock(self, key: str, *, ttl_seconds: int) -> str | None:
        token = uuid.uuid4().hex
        try:
            acquired = await self._client.set(
                self._lock_key(key),
                token,
                nx=True,
                ex=max(1, int(ttl_seconds)),
            )
        except RedisError:
            logger.exception("Failed to acquire MarketGuard response cache lock from Redis.")
            return None
        return token if acquired else None

    async def release_lock(self, key: str, token: str) -> None:
        try:
            await self._client.eval(
                """
                if redis.call("GET", KEYS[1]) == ARGV[1] then
                    return redis.call("DEL", KEYS[1])
                end
                return 0
                """,
                1,
                self._lock_key(key),
                token,
            )
        except RedisError:
            logger.exception("Failed to release MarketGuard response cache lock from Redis.")

    def _lock_key(self, key: str) -> str:
        return f"{self._lock_prefix}:{key}"


class ResponseCacheChain:
    def __init__(self, *backends) -> None:
        self._backends = [backend for backend in backends if backend is not None]
        self._lock = asyncio.Lock()
        self._refresh_locks: dict[str, str] = {}
        self._refresh_locks_guard = asyncio.Lock()
        self._redis_backend = next((backend for backend in self._backends if isinstance(backend, RedisResponseCache)), None)

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

    async def get_or_fill(
        self,
        key: str,
        factory: Callable[[], Awaitable[CachedResponse]],
        *,
        wait_timeout_seconds: float = 1.5,
        wait_poll_interval_seconds: float = 0.05,
        lock_ttl_seconds: int = 30,
    ) -> CachedResponse:
        cached = await self.get(key)
        if cached is not None:
            return cached

        lock_token = await self.try_acquire_refresh_lock(key, ttl_seconds=lock_ttl_seconds)
        if lock_token is None:
            waited = await self._wait_for_cached_entry(
                key,
                timeout_seconds=wait_timeout_seconds,
                poll_interval_seconds=wait_poll_interval_seconds,
            )
            if waited is not None:
                return waited
            lock_token = await self.try_acquire_refresh_lock(key, ttl_seconds=lock_ttl_seconds)
            if lock_token is None:
                cached = await self.get(key)
                if cached is not None:
                    return cached
                raise RuntimeError(f"Could not coordinate refresh for cache key: {key}")

        try:
            cached = await self.get(key)
            if cached is not None:
                return cached
            entry = await factory()
            await self.set(key, entry)
            return entry
        finally:
            await self.release_refresh_lock(key, lock_token)

    async def try_acquire_refresh_lock(self, key: str, *, ttl_seconds: int) -> str | None:
        if self._redis_backend is not None:
            return await self._redis_backend.try_acquire_lock(key, ttl_seconds=ttl_seconds)

        lock_key = str(key)
        token = uuid.uuid4().hex
        async with self._refresh_locks_guard:
            if lock_key in self._refresh_locks:
                return None
            self._refresh_locks[lock_key] = token
            return token

    async def release_refresh_lock(self, key: str, token: str) -> None:
        if self._redis_backend is not None:
            await self._redis_backend.release_lock(key, token)
            return

        lock_key = str(key)
        async with self._refresh_locks_guard:
            if self._refresh_locks.get(lock_key) == token:
                self._refresh_locks.pop(lock_key, None)

    async def _wait_for_cached_entry(
        self,
        key: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> CachedResponse | None:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        interval = max(0.01, float(poll_interval_seconds))
        while time.monotonic() < deadline:
            cached = await self.get(key)
            if cached is not None:
                return cached
            await asyncio.sleep(interval)
        return None

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
