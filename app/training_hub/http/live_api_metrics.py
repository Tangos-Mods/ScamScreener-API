from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

_WINDOW_10_SECONDS = 10.0
_WINDOW_60_SECONDS = 60.0
_SUPPORTED_BUCKETS = ("public_api", "client_api", "internal_api")
_SCAMSCREENER_USER_AGENT_RE = re.compile(r"^ScamScreener/(?P<mod>[^+\s]+)\+(?P<mc>[^\s]+)$", re.IGNORECASE)
_ENTRY_LIMIT = 80


@dataclass(frozen=True)
class LiveApiRequestToken:
    bucket: str
    endpoint: str
    agent: str


class LiveApiRequestMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_by_bucket = {bucket: 0 for bucket in _SUPPORTED_BUCKETS}
        self._recent_by_bucket = {bucket: deque() for bucket in _SUPPORTED_BUCKETS}
        self._total_by_bucket = {bucket: 0 for bucket in _SUPPORTED_BUCKETS}
        self._today_by_bucket = {bucket: 0 for bucket in _SUPPORTED_BUCKETS}
        self._entry_recent: dict[tuple[str, str], deque[float]] = {}
        self._entry_today: dict[tuple[str, str], int] = {}
        self._entry_total: dict[tuple[str, str], int] = {}
        self._current_day_key = self._utc_day_key()

    def start_request(self, path: str, user_agent: str = "") -> LiveApiRequestToken | None:
        bucket = _bucket_for_path(path)
        if bucket is None:
            return None
        endpoint = _normalize_endpoint(path)
        agent = _classify_agent(user_agent)
        entry_key = (endpoint, agent)

        now = time.monotonic()
        with self._lock:
            self._rollover_day_unlocked(self._utc_day_key())
            self._prune_unlocked(now)
            self._active_by_bucket[bucket] += 1
            self._recent_by_bucket[bucket].append(now)
            self._total_by_bucket[bucket] += 1
            self._today_by_bucket[bucket] += 1
            self._entry_recent.setdefault(entry_key, deque()).append(now)
            self._entry_today[entry_key] = int(self._entry_today.get(entry_key, 0)) + 1
            self._entry_total[entry_key] = int(self._entry_total.get(entry_key, 0)) + 1
        return LiveApiRequestToken(bucket=bucket, endpoint=endpoint, agent=agent)

    def finish_request(self, token: LiveApiRequestToken | None) -> None:
        if token is None:
            return

        with self._lock:
            current_active = int(self._active_by_bucket.get(token.bucket, 0))
            self._active_by_bucket[token.bucket] = max(0, current_active - 1)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            self._rollover_day_unlocked(self._utc_day_key())
            self._prune_unlocked(now)
            buckets = {bucket: self._bucket_snapshot_unlocked(bucket, now) for bucket in _SUPPORTED_BUCKETS}

        return {
            "totalActive": sum(int(bucket["active"]) for bucket in buckets.values()),
            "totalToday": sum(int(bucket["requestsToday"]) for bucket in buckets.values()),
            "totalLast10s": sum(int(bucket["requestsLast10s"]) for bucket in buckets.values()),
            "totalLast60s": sum(int(bucket["requestsLast60s"]) for bucket in buckets.values()),
            "totalSinceStart": sum(int(bucket["totalSinceStart"]) for bucket in buckets.values()),
            "publicApi": buckets["public_api"],
            "clientApi": buckets["client_api"],
            "internalApi": buckets["internal_api"],
            "entries": self._entries_snapshot_unlocked(now),
        }

    def _bucket_snapshot_unlocked(self, bucket: str, now: float) -> dict[str, int | float | str]:
        recent = self._recent_by_bucket[bucket]
        requests_last_60s = len(recent)
        requests_last_10s = 0
        threshold_10s = now - _WINDOW_10_SECONDS
        for timestamp in reversed(recent):
            if timestamp < threshold_10s:
                break
            requests_last_10s += 1
        return {
            "active": int(self._active_by_bucket[bucket]),
            "requestsToday": int(self._today_by_bucket[bucket]),
            "requestsLast10s": requests_last_10s,
            "requestsLast60s": requests_last_60s,
            "requestsPerSecond10s": requests_last_10s / _WINDOW_10_SECONDS,
            "totalSinceStart": int(self._total_by_bucket[bucket]),
        }

    def _prune_unlocked(self, now: float) -> None:
        cutoff_60s = now - _WINDOW_60_SECONDS
        for bucket in _SUPPORTED_BUCKETS:
            recent = self._recent_by_bucket[bucket]
            while recent and recent[0] < cutoff_60s:
                recent.popleft()
        stale_keys: list[tuple[str, str]] = []
        for entry_key, recent in self._entry_recent.items():
            while recent and recent[0] < cutoff_60s:
                recent.popleft()
            if not recent and int(self._entry_today.get(entry_key, 0)) == 0 and int(self._entry_total.get(entry_key, 0)) == 0:
                stale_keys.append(entry_key)
        for entry_key in stale_keys:
            self._entry_recent.pop(entry_key, None)
            self._entry_today.pop(entry_key, None)
            self._entry_total.pop(entry_key, None)

    def _rollover_day_unlocked(self, day_key: str) -> None:
        if day_key == self._current_day_key:
            return
        self._current_day_key = day_key
        self._today_by_bucket = {bucket: 0 for bucket in _SUPPORTED_BUCKETS}
        self._entry_today = {}

    @staticmethod
    def _utc_day_key() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _entries_snapshot_unlocked(self, now: float) -> list[dict[str, Any]]:
        threshold_10s = now - _WINDOW_10_SECONDS
        rows: list[dict[str, Any]] = []
        for entry_key, total in self._entry_total.items():
            endpoint, agent = entry_key
            recent = self._entry_recent.get(entry_key, deque())
            requests_last_60s = len(recent)
            requests_last_10s = 0
            for timestamp in reversed(recent):
                if timestamp < threshold_10s:
                    break
                requests_last_10s += 1
            rows.append(
                {
                    "endpoint": endpoint,
                    "agent": agent,
                    "requestsLast10s": requests_last_10s,
                    "requestsLast60s": requests_last_60s,
                    "requestsToday": int(self._entry_today.get(entry_key, 0)),
                    "totalSinceStart": int(total),
                }
            )
        rows.sort(
            key=lambda item: (
                int(item["requestsLast10s"]),
                int(item["requestsLast60s"]),
                int(item["requestsToday"]),
                int(item["totalSinceStart"]),
                str(item["endpoint"]),
                str(item["agent"]),
            ),
            reverse=True,
        )
        return rows[:_ENTRY_LIMIT]


def _bucket_for_path(path: str) -> str | None:
    normalized_path = str(path or "").strip()
    if not normalized_path.startswith("/api/"):
        return None
    if normalized_path in {"/api/internal/live-metrics", "/api/v1/ready"}:
        return None
    if normalized_path.startswith("/api/v1/client/"):
        return "client_api"
    if normalized_path.startswith("/api/internal/") or normalized_path in {"/api/v1/health", "/api/v1/metrics"}:
        return "internal_api"
    return "public_api"


def _normalize_endpoint(path: str) -> str:
    normalized_path = str(path or "").strip()
    return normalized_path or "/"


def _classify_agent(user_agent: str) -> str:
    normalized = str(user_agent or "").strip()
    if not normalized:
        return "Unknown"

    scamscreener_match = _SCAMSCREENER_USER_AGENT_RE.match(normalized)
    if scamscreener_match is not None:
        return f"{scamscreener_match.group('mod')} {scamscreener_match.group('mc')}"

    lowered = normalized.lower()
    if lowered.startswith("scamscreener-marketguard/"):
        return "ScamScreener MarketGuard"
    if "mozilla/" in lowered:
        return "Browser (HTTPS)"
    if lowered.startswith("curl/"):
        return "curl"
    if lowered.startswith("python-httpx/"):
        return "python-httpx"
    if lowered.startswith("python-requests/"):
        return "python-requests"
    if lowered.startswith("go-http-client/"):
        return "go-http-client"
    if lowered.startswith("java/"):
        return "Java client"

    return "Other"


def live_api_metrics_snapshot(app_state: Any) -> dict[str, Any]:
    tracker = getattr(app_state, "live_api_metrics", None)
    if tracker is None:
        snapshot = _empty_snapshot()
    else:
        snapshot = tracker.snapshot()
    snapshot["marketguardPlayers"] = _marketguard_players_snapshot(app_state)
    return snapshot


def _empty_snapshot() -> dict[str, Any]:
    def _empty_bucket() -> dict[str, int | float]:
        return {
            "active": 0,
            "requestsToday": 0,
            "requestsLast10s": 0,
            "requestsLast60s": 0,
            "requestsPerSecond10s": 0.0,
            "totalSinceStart": 0,
        }

    return {
        "totalActive": 0,
        "totalToday": 0,
        "totalLast10s": 0,
        "totalLast60s": 0,
        "totalSinceStart": 0,
        "publicApi": _empty_bucket(),
        "clientApi": _empty_bucket(),
        "internalApi": _empty_bucket(),
        "entries": [],
        "marketguardPlayers": _empty_marketguard_players_snapshot(),
    }


def _empty_marketguard_players_snapshot() -> dict[str, int | float]:
    return {
        "completedRequests": 0,
        "cacheHits": 0,
        "cacheMisses": 0,
        "cacheHitRate": 0.0,
        "coalescedWaiters": 0,
        "upstreamFailures": 0,
        "activeUpstreamLoads": 0,
        "peakActiveUpstreamLoads": 0,
        "totalResponseMilliseconds": 0.0,
        "averageResponseMilliseconds": 0.0,
        "maxResponseMilliseconds": 0.0,
    }


def _marketguard_players_snapshot(app_state: Any) -> dict[str, int | float]:
    metrics = getattr(app_state, "marketguard_player_query_metrics", None)
    snapshot = getattr(metrics, "snapshot", None)
    if not callable(snapshot):
        return _empty_marketguard_players_snapshot()
    try:
        payload = snapshot()
    except Exception:
        return _empty_marketguard_players_snapshot()
    if not isinstance(payload, dict):
        return _empty_marketguard_players_snapshot()
    defaults = _empty_marketguard_players_snapshot()
    for key, value in defaults.items():
        candidate = payload.get(key, value)
        try:
            defaults[key] = float(candidate) if isinstance(value, float) else int(candidate)
        except (TypeError, ValueError):
            continue
    return defaults


def merge_live_api_metrics_snapshots(*snapshots: dict[str, Any]) -> dict[str, Any]:
    merged = _empty_snapshot()
    entry_index: dict[tuple[str, str], dict[str, Any]] = {}

    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        merged["totalActive"] += int(snapshot.get("totalActive", 0))
        merged["totalToday"] += int(snapshot.get("totalToday", 0))
        merged["totalLast10s"] += int(snapshot.get("totalLast10s", 0))
        merged["totalLast60s"] += int(snapshot.get("totalLast60s", 0))
        merged["totalSinceStart"] += int(snapshot.get("totalSinceStart", 0))

        source_player_metrics = snapshot.get("marketguardPlayers", {}) or {}
        if not isinstance(source_player_metrics, dict):
            source_player_metrics = {}
        target_player_metrics = merged["marketguardPlayers"]
        for metric_key in (
            "completedRequests",
            "cacheHits",
            "cacheMisses",
            "coalescedWaiters",
            "upstreamFailures",
            "activeUpstreamLoads",
            "peakActiveUpstreamLoads",
            "totalResponseMilliseconds",
        ):
            try:
                value = source_player_metrics.get(metric_key, 0)
                normalized = float(value) if isinstance(target_player_metrics[metric_key], float) else int(value)
            except (TypeError, ValueError):
                continue
            target_player_metrics[metric_key] += normalized
        try:
            source_maximum = float(source_player_metrics.get("maxResponseMilliseconds", 0))
        except (TypeError, ValueError):
            source_maximum = 0.0
        target_player_metrics["maxResponseMilliseconds"] = max(
            float(target_player_metrics["maxResponseMilliseconds"]),
            source_maximum,
        )

        for bucket_key, snapshot_key in (
            ("publicApi", "publicApi"),
            ("clientApi", "clientApi"),
            ("internalApi", "internalApi"),
        ):
            source_bucket = snapshot.get(snapshot_key, {}) or {}
            target_bucket = merged[bucket_key]
            target_bucket["active"] += int(source_bucket.get("active", 0))
            target_bucket["requestsToday"] += int(source_bucket.get("requestsToday", 0))
            target_bucket["requestsLast10s"] += int(source_bucket.get("requestsLast10s", 0))
            target_bucket["requestsLast60s"] += int(source_bucket.get("requestsLast60s", 0))
            target_bucket["totalSinceStart"] += int(source_bucket.get("totalSinceStart", 0))

        for entry in snapshot.get("entries", []) or []:
            endpoint = str(entry.get("endpoint", "") or "").strip()
            agent = str(entry.get("agent", "") or "").strip()
            if not endpoint or not agent:
                continue
            entry_key = (endpoint, agent)
            target_entry = entry_index.setdefault(
                entry_key,
                {
                    "endpoint": endpoint,
                    "agent": agent,
                    "requestsLast10s": 0,
                    "requestsLast60s": 0,
                    "requestsToday": 0,
                    "totalSinceStart": 0,
                },
            )
            target_entry["requestsLast10s"] += int(entry.get("requestsLast10s", 0))
            target_entry["requestsLast60s"] += int(entry.get("requestsLast60s", 0))
            target_entry["requestsToday"] += int(entry.get("requestsToday", 0))
            target_entry["totalSinceStart"] += int(entry.get("totalSinceStart", 0))

    for bucket_key in ("publicApi", "clientApi", "internalApi"):
        bucket = merged[bucket_key]
        bucket["requestsPerSecond10s"] = float(bucket["requestsLast10s"]) / _WINDOW_10_SECONDS

    player_metrics = merged["marketguardPlayers"]
    cache_total = int(player_metrics["cacheHits"]) + int(player_metrics["cacheMisses"])
    player_metrics["cacheHitRate"] = float(player_metrics["cacheHits"]) / cache_total if cache_total else 0.0
    completed_requests = int(player_metrics["completedRequests"])
    player_metrics["averageResponseMilliseconds"] = (
        float(player_metrics["totalResponseMilliseconds"]) / completed_requests if completed_requests else 0.0
    )

    entries = list(entry_index.values())
    entries.sort(
        key=lambda item: (
            int(item["requestsLast10s"]),
            int(item["requestsLast60s"]),
            int(item["requestsToday"]),
            int(item["totalSinceStart"]),
            str(item["endpoint"]),
            str(item["agent"]),
        ),
        reverse=True,
    )
    merged["entries"] = entries[:_ENTRY_LIMIT]
    return merged


__all__ = (
    "LiveApiRequestMetrics",
    "LiveApiRequestToken",
    "live_api_metrics_snapshot",
    "merge_live_api_metrics_snapshots",
)
