from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from ..infra import db

logger = logging.getLogger(__name__)

_SUPPORTED_BUCKETS = ("public_api", "client_api", "internal_api")
_ENTRY_LIMIT = 80
_PERSISTENT_QUERY_LIMIT = 400


class PersistentApiMetricsRecorder:
    def __init__(self, database_target: str | Any) -> None:
        self._database_target = database_target
        self._lock = threading.Lock()
        self._bucket_pending: dict[tuple[str, str], int] = {}
        self._entry_pending: dict[tuple[str, str, str, str], int] = {}
        self._schema_ready = False

    def record_request(self, bucket: str, endpoint: str, agent: str) -> None:
        normalized_bucket = str(bucket or "").strip()
        normalized_endpoint = str(endpoint or "").strip()
        normalized_agent = str(agent or "").strip()
        if normalized_bucket not in _SUPPORTED_BUCKETS or not normalized_endpoint or not normalized_agent:
            return
        metric_day = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            bucket_key = (normalized_bucket, metric_day)
            entry_key = (normalized_bucket, normalized_endpoint, normalized_agent, metric_day)
            self._bucket_pending[bucket_key] = int(self._bucket_pending.get(bucket_key, 0)) + 1
            self._entry_pending[entry_key] = int(self._entry_pending.get(entry_key, 0)) + 1

    def flush(self) -> bool:
        with self._lock:
            bucket_pending = self._bucket_pending
            entry_pending = self._entry_pending
            self._bucket_pending = {}
            self._entry_pending = {}
        if not bucket_pending and not entry_pending:
            return True

        updated_at = datetime.now(timezone.utc).isoformat()
        current_day = datetime.now(timezone.utc).date().isoformat()
        try:
            self._ensure_schema()
            with db.connect(self._database_target) as connection:
                connection.execute("BEGIN IMMEDIATE")
                for (bucket, metric_day), delta in bucket_pending.items():
                    _upsert_bucket_total(connection, self._database_target, bucket, int(delta), updated_at)
                    _upsert_bucket_daily(connection, self._database_target, bucket, metric_day, int(delta), updated_at)
                for (bucket, endpoint, agent, metric_day), delta in entry_pending.items():
                    _upsert_entry_total(
                        connection,
                        self._database_target,
                        bucket,
                        endpoint,
                        agent,
                        int(delta),
                        updated_at,
                    )
                    _upsert_entry_daily(
                        connection,
                        self._database_target,
                        bucket,
                        endpoint,
                        agent,
                        metric_day,
                        int(delta),
                        updated_at,
                    )
                connection.execute("DELETE FROM api_request_metric_bucket_daily WHERE metric_day < ?", (current_day,))
                connection.execute("DELETE FROM api_request_metric_endpoint_daily WHERE metric_day < ?", (current_day,))
                connection.commit()
            return True
        except Exception:
            logger.exception("Failed to flush persistent API request metrics.")
            with self._lock:
                for key, delta in bucket_pending.items():
                    self._bucket_pending[key] = int(self._bucket_pending.get(key, 0)) + int(delta)
                for key, delta in entry_pending.items():
                    self._entry_pending[key] = int(self._entry_pending.get(key, 0)) + int(delta)
            return False

    def snapshot(self, *, focus_entries: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
        self._ensure_schema()
        metric_day = datetime.now(timezone.utc).date().isoformat()
        bucket_rows: dict[str, dict[str, int]] = {
            bucket: {"requestsToday": 0, "totalSinceStart": 0}
            for bucket in _SUPPORTED_BUCKETS
        }
        entry_rows: dict[tuple[str, str], dict[str, Any]] = {}
        focus_keys = {(str(endpoint or "").strip(), str(agent or "").strip()) for endpoint, agent in focus_entries}
        focus_keys = {key for key in focus_keys if key[0] and key[1]}

        with db.connect(self._database_target) as connection:
            connection.row_factory = db.Row
            totals_rows = connection.execute(
                """
                SELECT bucket, total_count
                FROM api_request_metric_bucket_totals
                """
            ).fetchall()
            daily_rows = connection.execute(
                """
                SELECT bucket, request_count
                FROM api_request_metric_bucket_daily
                WHERE metric_day = ?
                """,
                (metric_day,),
            ).fetchall()
            endpoint_totals_rows = connection.execute(
                """
                SELECT
                    t.bucket,
                    t.endpoint,
                    t.agent,
                    COALESCE(d.request_count, 0) AS requests_today,
                    t.total_count
                FROM api_request_metric_endpoint_totals t
                LEFT JOIN api_request_metric_endpoint_daily d
                  ON d.bucket = t.bucket
                 AND d.endpoint = t.endpoint
                 AND d.agent = t.agent
                 AND d.metric_day = ?
                ORDER BY t.total_count DESC, requests_today DESC, t.endpoint ASC, t.agent ASC
                LIMIT ?
                """,
                (metric_day, _PERSISTENT_QUERY_LIMIT),
            ).fetchall()
            for endpoint, agent in focus_keys:
                if any(str(row["endpoint"]) == endpoint and str(row["agent"]) == agent for row in endpoint_totals_rows):
                    continue
                focus_row = connection.execute(
                    """
                    SELECT
                        t.bucket,
                        t.endpoint,
                        t.agent,
                        COALESCE(d.request_count, 0) AS requests_today,
                        t.total_count
                    FROM api_request_metric_endpoint_totals t
                    LEFT JOIN api_request_metric_endpoint_daily d
                      ON d.bucket = t.bucket
                     AND d.endpoint = t.endpoint
                     AND d.agent = t.agent
                     AND d.metric_day = ?
                    WHERE t.endpoint = ? AND t.agent = ?
                    """,
                    (metric_day, endpoint, agent),
                ).fetchone()
                if focus_row is not None:
                    endpoint_totals_rows.append(focus_row)

        for row in totals_rows:
            bucket = str(row["bucket"])
            if bucket in bucket_rows:
                bucket_rows[bucket]["totalSinceStart"] = int(row["total_count"])
        for row in daily_rows:
            bucket = str(row["bucket"])
            if bucket in bucket_rows:
                bucket_rows[bucket]["requestsToday"] = int(row["request_count"])
        for row in endpoint_totals_rows:
            endpoint = str(row["endpoint"])
            agent = str(row["agent"])
            if not endpoint or not agent:
                continue
            entry_rows[(endpoint, agent)] = {
                "bucket": str(row["bucket"]),
                "endpoint": endpoint,
                "agent": agent,
                "requestsToday": int(row["requests_today"]),
                "totalSinceStart": int(row["total_count"]),
            }

        entries = list(entry_rows.values())
        entries.sort(
            key=lambda item: (
                int(item["requestsToday"]),
                int(item["totalSinceStart"]),
                str(item["endpoint"]),
                str(item["agent"]),
            ),
            reverse=True,
        )
        return {
            "totalToday": sum(values["requestsToday"] for values in bucket_rows.values()),
            "totalSinceStart": sum(values["totalSinceStart"] for values in bucket_rows.values()),
            "publicApi": bucket_rows["public_api"],
            "clientApi": bucket_rows["client_api"],
            "internalApi": bucket_rows["internal_api"],
            "entries": entries[:_ENTRY_LIMIT],
        }

    def _ensure_schema(self) -> None:
        with self._lock:
            if self._schema_ready:
                return
        _ensure_persistent_api_metrics_tables(self._database_target)
        with self._lock:
            self._schema_ready = True


def _ensure_persistent_api_metrics_tables(database_target: str | Any) -> None:
    bucket_totals_sql, bucket_daily_sql, entry_totals_sql, entry_daily_sql, index_statements = _schema_sql(database_target)
    with db.connect(database_target) as connection:
        connection.execute(bucket_totals_sql)
        connection.execute(bucket_daily_sql)
        connection.execute(entry_totals_sql)
        connection.execute(entry_daily_sql)
        for statement in index_statements:
            connection.execute(statement)
        connection.commit()


def combine_live_and_persistent_api_metrics(
    live_snapshot: dict[str, Any],
    persistent_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(persistent_snapshot, dict):
        return live_snapshot

    combined = {
        "totalActive": int(live_snapshot.get("totalActive", 0)),
        "totalToday": int(persistent_snapshot.get("totalToday", live_snapshot.get("totalToday", 0))),
        "totalLast10s": int(live_snapshot.get("totalLast10s", 0)),
        "totalLast60s": int(live_snapshot.get("totalLast60s", 0)),
        "totalSinceStart": int(persistent_snapshot.get("totalSinceStart", live_snapshot.get("totalSinceStart", 0))),
        "publicApi": dict(live_snapshot.get("publicApi", {}) or {}),
        "clientApi": dict(live_snapshot.get("clientApi", {}) or {}),
        "internalApi": dict(live_snapshot.get("internalApi", {}) or {}),
        "entries": [],
    }

    for live_key, persistent_key in (
        ("publicApi", "publicApi"),
        ("clientApi", "clientApi"),
        ("internalApi", "internalApi"),
    ):
        persistent_bucket = dict(persistent_snapshot.get(persistent_key, {}) or {})
        combined[live_key]["requestsToday"] = int(
            persistent_bucket.get("requestsToday", combined[live_key].get("requestsToday", 0))
        )
        combined[live_key]["totalSinceStart"] = int(
            persistent_bucket.get("totalSinceStart", combined[live_key].get("totalSinceStart", 0))
        )

    live_entries = {
        (str(entry.get("endpoint", "")), str(entry.get("agent", ""))): {
            "requestsLast10s": int(entry.get("requestsLast10s", 0)),
            "requestsLast60s": int(entry.get("requestsLast60s", 0)),
        }
        for entry in (live_snapshot.get("entries", []) or [])
        if str(entry.get("endpoint", "")) and str(entry.get("agent", ""))
    }
    persistent_entries = {
        (str(entry.get("endpoint", "")), str(entry.get("agent", ""))): {
            "requestsToday": int(entry.get("requestsToday", 0)),
            "totalSinceStart": int(entry.get("totalSinceStart", 0)),
        }
        for entry in (persistent_snapshot.get("entries", []) or [])
        if str(entry.get("endpoint", "")) and str(entry.get("agent", ""))
    }

    all_keys = set(live_entries) | set(persistent_entries)
    rows: list[dict[str, Any]] = []
    for endpoint, agent in all_keys:
        live_row = live_entries.get((endpoint, agent), {})
        persistent_row = persistent_entries.get((endpoint, agent), {})
        rows.append(
            {
                "endpoint": endpoint,
                "agent": agent,
                "requestsLast10s": int(live_row.get("requestsLast10s", 0)),
                "requestsLast60s": int(live_row.get("requestsLast60s", 0)),
                "requestsToday": int(persistent_row.get("requestsToday", 0)),
                "totalSinceStart": int(persistent_row.get("totalSinceStart", 0)),
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
    combined["entries"] = rows[:_ENTRY_LIMIT]
    return combined


def _schema_sql(database_target: str | Any) -> tuple[str, str, str, str, tuple[str, ...]]:
    if db.is_mariadb_target(database_target):
        return (
            """
            CREATE TABLE IF NOT EXISTS api_request_metric_bucket_totals (
                bucket VARCHAR(32) NOT NULL PRIMARY KEY,
                total_count BIGINT NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS api_request_metric_bucket_daily (
                bucket VARCHAR(32) NOT NULL,
                metric_day VARCHAR(10) NOT NULL,
                request_count BIGINT NOT NULL,
                updated_at VARCHAR(40) NOT NULL,
                PRIMARY KEY (bucket, metric_day),
                KEY idx_api_request_metric_bucket_daily_day (metric_day)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS api_request_metric_endpoint_totals (
                bucket VARCHAR(32) NOT NULL,
                endpoint VARCHAR(255) NOT NULL,
                agent VARCHAR(190) NOT NULL,
                total_count BIGINT NOT NULL,
                updated_at VARCHAR(40) NOT NULL,
                PRIMARY KEY (bucket, endpoint, agent),
                KEY idx_api_request_metric_endpoint_totals_total (total_count),
                KEY idx_api_request_metric_endpoint_totals_updated (updated_at)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS api_request_metric_endpoint_daily (
                bucket VARCHAR(32) NOT NULL,
                endpoint VARCHAR(255) NOT NULL,
                agent VARCHAR(190) NOT NULL,
                metric_day VARCHAR(10) NOT NULL,
                request_count BIGINT NOT NULL,
                updated_at VARCHAR(40) NOT NULL,
                PRIMARY KEY (bucket, endpoint, agent, metric_day),
                KEY idx_api_request_metric_endpoint_daily_day (metric_day)
            )
            """,
            (),
        )
    return (
        """
        CREATE TABLE IF NOT EXISTS api_request_metric_bucket_totals (
            bucket TEXT PRIMARY KEY,
            total_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS api_request_metric_bucket_daily (
            bucket TEXT NOT NULL,
            metric_day TEXT NOT NULL,
            request_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (bucket, metric_day)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS api_request_metric_endpoint_totals (
            bucket TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            agent TEXT NOT NULL,
            total_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (bucket, endpoint, agent)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS api_request_metric_endpoint_daily (
            bucket TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            agent TEXT NOT NULL,
            metric_day TEXT NOT NULL,
            request_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (bucket, endpoint, agent, metric_day)
        )
        """,
        (
            "CREATE INDEX IF NOT EXISTS idx_api_request_metric_bucket_daily_day ON api_request_metric_bucket_daily(metric_day)",
            "CREATE INDEX IF NOT EXISTS idx_api_request_metric_endpoint_totals_total ON api_request_metric_endpoint_totals(total_count)",
            "CREATE INDEX IF NOT EXISTS idx_api_request_metric_endpoint_totals_updated ON api_request_metric_endpoint_totals(updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_api_request_metric_endpoint_daily_day ON api_request_metric_endpoint_daily(metric_day)",
        ),
    )


def _upsert_bucket_total(connection, database_target: str | Any, bucket: str, delta: int, updated_at: str) -> None:
    if db.is_mariadb_target(database_target):
        connection.execute(
            """
            INSERT INTO api_request_metric_bucket_totals (bucket, total_count, updated_at)
            VALUES (?, ?, ?)
            ON DUPLICATE KEY UPDATE
                total_count = total_count + VALUES(total_count),
                updated_at = VALUES(updated_at)
            """,
            (bucket, delta, updated_at),
        )
        return
    connection.execute(
        """
        INSERT INTO api_request_metric_bucket_totals (bucket, total_count, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(bucket) DO UPDATE SET
            total_count = api_request_metric_bucket_totals.total_count + excluded.total_count,
            updated_at = excluded.updated_at
        """,
        (bucket, delta, updated_at),
    )


def _upsert_bucket_daily(connection, database_target: str | Any, bucket: str, metric_day: str, delta: int, updated_at: str) -> None:
    if db.is_mariadb_target(database_target):
        connection.execute(
            """
            INSERT INTO api_request_metric_bucket_daily (bucket, metric_day, request_count, updated_at)
            VALUES (?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                request_count = request_count + VALUES(request_count),
                updated_at = VALUES(updated_at)
            """,
            (bucket, metric_day, delta, updated_at),
        )
        return
    connection.execute(
        """
        INSERT INTO api_request_metric_bucket_daily (bucket, metric_day, request_count, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(bucket, metric_day) DO UPDATE SET
            request_count = api_request_metric_bucket_daily.request_count + excluded.request_count,
            updated_at = excluded.updated_at
        """,
        (bucket, metric_day, delta, updated_at),
    )


def _upsert_entry_total(
    connection,
    database_target: str | Any,
    bucket: str,
    endpoint: str,
    agent: str,
    delta: int,
    updated_at: str,
) -> None:
    if db.is_mariadb_target(database_target):
        connection.execute(
            """
            INSERT INTO api_request_metric_endpoint_totals (bucket, endpoint, agent, total_count, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                total_count = total_count + VALUES(total_count),
                updated_at = VALUES(updated_at)
            """,
            (bucket, endpoint, agent, delta, updated_at),
        )
        return
    connection.execute(
        """
        INSERT INTO api_request_metric_endpoint_totals (bucket, endpoint, agent, total_count, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(bucket, endpoint, agent) DO UPDATE SET
            total_count = api_request_metric_endpoint_totals.total_count + excluded.total_count,
            updated_at = excluded.updated_at
        """,
        (bucket, endpoint, agent, delta, updated_at),
    )


def _upsert_entry_daily(
    connection,
    database_target: str | Any,
    bucket: str,
    endpoint: str,
    agent: str,
    metric_day: str,
    delta: int,
    updated_at: str,
) -> None:
    if db.is_mariadb_target(database_target):
        connection.execute(
            """
            INSERT INTO api_request_metric_endpoint_daily (bucket, endpoint, agent, metric_day, request_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                request_count = request_count + VALUES(request_count),
                updated_at = VALUES(updated_at)
            """,
            (bucket, endpoint, agent, metric_day, delta, updated_at),
        )
        return
    connection.execute(
        """
        INSERT INTO api_request_metric_endpoint_daily (bucket, endpoint, agent, metric_day, request_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(bucket, endpoint, agent, metric_day) DO UPDATE SET
            request_count = api_request_metric_endpoint_daily.request_count + excluded.request_count,
            updated_at = excluded.updated_at
        """,
        (bucket, endpoint, agent, metric_day, delta, updated_at),
    )


__all__ = (
    "PersistentApiMetricsRecorder",
    "combine_live_and_persistent_api_metrics",
)
