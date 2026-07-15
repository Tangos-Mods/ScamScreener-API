from __future__ import annotations

import threading


class PlayerQueryMetrics:
    """Process-local aggregate metrics for the public player QUERY route."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._completed_requests = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._coalesced_waiters = 0
        self._upstream_failures = 0
        self._active_upstream_loads = 0
        self._peak_active_upstream_loads = 0
        self._total_response_milliseconds = 0.0
        self._max_response_milliseconds = 0.0

    def record_cache_hit(self) -> None:
        with self._lock:
            self._cache_hits += 1

    def record_cache_miss(self) -> None:
        with self._lock:
            self._cache_misses += 1

    def record_coalesced_waiter(self) -> None:
        with self._lock:
            self._coalesced_waiters += 1

    def start_upstream_load(self) -> None:
        with self._lock:
            self._active_upstream_loads += 1
            self._peak_active_upstream_loads = max(
                self._peak_active_upstream_loads,
                self._active_upstream_loads,
            )

    def finish_upstream_load(self) -> None:
        with self._lock:
            self._active_upstream_loads = max(0, self._active_upstream_loads - 1)

    def record_upstream_failure(self) -> None:
        with self._lock:
            self._upstream_failures += 1

    def record_response(self, elapsed_milliseconds: float) -> None:
        duration = max(0.0, float(elapsed_milliseconds))
        with self._lock:
            self._completed_requests += 1
            self._total_response_milliseconds += duration
            self._max_response_milliseconds = max(self._max_response_milliseconds, duration)

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            completed_requests = self._completed_requests
            cache_total = self._cache_hits + self._cache_misses
            return {
                "completedRequests": completed_requests,
                "cacheHits": self._cache_hits,
                "cacheMisses": self._cache_misses,
                "cacheHitRate": (self._cache_hits / cache_total) if cache_total else 0.0,
                "coalescedWaiters": self._coalesced_waiters,
                "upstreamFailures": self._upstream_failures,
                "activeUpstreamLoads": self._active_upstream_loads,
                "peakActiveUpstreamLoads": self._peak_active_upstream_loads,
                "totalResponseMilliseconds": self._total_response_milliseconds,
                "averageResponseMilliseconds": (
                    self._total_response_milliseconds / completed_requests if completed_requests else 0.0
                ),
                "maxResponseMilliseconds": self._max_response_milliseconds,
            }
__all__ = ("PlayerQueryMetrics",)
