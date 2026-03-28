from __future__ import annotations

import threading
import time


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, int], int] = {}

    def allow(self, key: str, max_requests: int, window_seconds: int) -> tuple[bool, int]:
        now = int(time.time())
        safe_window = max(1, int(window_seconds))
        bucket_start = now - (now % safe_window)
        retry_after = max(1, (bucket_start + safe_window) - now)
        stale_before = bucket_start - (safe_window * 12)

        with self._lock:
            for bucket_key in [bucket for bucket in self._buckets if bucket[1] < stale_before]:
                self._buckets.pop(bucket_key, None)

            composite_key = (str(key), bucket_start)
            current_count = int(self._buckets.get(composite_key, 0))
            if current_count >= int(max_requests):
                return False, retry_after

            self._buckets[composite_key] = current_count + 1
            return True, 0
