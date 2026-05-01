from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .exceptions import LowestBinHistoryError

logger = logging.getLogger(__name__)

_HISTORY_DATABASE_FILE = "marketguard_history.db"
_SQLITE_TIMEOUT_SECONDS = 30
_SQLITE_BUSY_TIMEOUT_MILLISECONDS = 30_000
_SQLITE_BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class LowestBinAverageWindow:
    avg_7d: float | None
    avg_30d: float | None


def snapshot_day_from_last_updated(snapshot_last_updated: int) -> date:
    try:
        normalized = int(snapshot_last_updated)
    except (TypeError, ValueError) as exc:
        raise LowestBinHistoryError("Lowest BIN snapshot timestamp is invalid.") from exc
    if normalized <= 0:
        raise LowestBinHistoryError("Lowest BIN snapshot timestamp must be positive.")
    try:
        return datetime.fromtimestamp(normalized / 1000, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError) as exc:
        raise LowestBinHistoryError("Lowest BIN snapshot timestamp is out of range.") from exc


class LowestBinHistoryStore:
    def __init__(self, storage_dir: Path, retention_days: int) -> None:
        self._storage_dir = Path(storage_dir).expanduser().resolve()
        self._retention_days = max(31, int(retention_days))
        self._database_path = self._storage_dir / _HISTORY_DATABASE_FILE
        _ensure_storage_dir(self._storage_dir)
        self._init_database()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def record_snapshot(self, snapshot_last_updated: int, item_prices: Mapping[str, float]) -> bool:
        snapshot_day = snapshot_day_from_last_updated(snapshot_last_updated).isoformat()
        processed_at = _utc_now_iso()
        rows = list(_aggregate_rows_for_snapshot(snapshot_day, item_prices))
        prune_before = (date.fromisoformat(snapshot_day) - timedelta(days=self._retention_days - 1)).isoformat()

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO processed_snapshots (
                        snapshot_last_updated,
                        snapshot_day,
                        processed_at
                    ) VALUES (?, ?, ?)
                    """,
                    (int(snapshot_last_updated), snapshot_day, processed_at),
                )
                inserted = int(cursor.rowcount or 0) > 0
                if inserted and rows:
                    connection.executemany(
                        """
                        INSERT INTO lowestbin_daily_aggregates (
                            item_key,
                            snapshot_day,
                            price_sum,
                            sample_count
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(item_key, snapshot_day) DO UPDATE SET
                            price_sum = price_sum + excluded.price_sum,
                            sample_count = sample_count + excluded.sample_count
                        """,
                        rows,
                    )
                if inserted:
                    connection.execute(
                        "DELETE FROM processed_snapshots WHERE snapshot_day < ?",
                        (prune_before,),
                    )
                    connection.execute(
                        "DELETE FROM lowestbin_daily_aggregates WHERE snapshot_day < ?",
                        (prune_before,),
                    )
                return inserted
        except sqlite3.Error as exc:
            raise LowestBinHistoryError("Failed to update Lowest BIN history store.") from exc

    def get_averages(
        self,
        item_keys: Sequence[str],
        *,
        anchor_day: date,
    ) -> dict[str, LowestBinAverageWindow]:
        normalized_keys = [item_key for item_key in dict.fromkeys(str(key).strip() for key in item_keys) if item_key]
        if not normalized_keys:
            return {}

        anchor_day_iso = anchor_day.isoformat()
        day_7d_iso = (anchor_day - timedelta(days=6)).isoformat()
        day_30d_iso = (anchor_day - timedelta(days=29)).isoformat()
        sums_7d: dict[str, float] = {item_key: 0.0 for item_key in normalized_keys}
        sums_30d: dict[str, float] = {item_key: 0.0 for item_key in normalized_keys}
        counts_7d: dict[str, int] = {item_key: 0 for item_key in normalized_keys}
        counts_30d: dict[str, int] = {item_key: 0 for item_key in normalized_keys}

        try:
            with self._connect() as connection:
                for batch in _iter_batches(normalized_keys, _SQLITE_BATCH_SIZE):
                    placeholders = ", ".join("?" for _ in batch)
                    query = f"""
                        SELECT item_key, snapshot_day, price_sum, sample_count
                        FROM lowestbin_daily_aggregates
                        WHERE snapshot_day >= ?
                          AND snapshot_day <= ?
                          AND item_key IN ({placeholders})
                    """
                    rows = connection.execute(query, (day_30d_iso, anchor_day_iso, *batch)).fetchall()
                    for row in rows:
                        item_key = str(row["item_key"])
                        snapshot_day = str(row["snapshot_day"])
                        price_sum = float(row["price_sum"])
                        sample_count = int(row["sample_count"])
                        sums_30d[item_key] += price_sum
                        counts_30d[item_key] += sample_count
                        if snapshot_day >= day_7d_iso:
                            sums_7d[item_key] += price_sum
                            counts_7d[item_key] += sample_count
        except sqlite3.Error as exc:
            raise LowestBinHistoryError("Failed to query Lowest BIN history store.") from exc

        return {
            item_key: LowestBinAverageWindow(
                avg_7d=_average_or_none(sums_7d[item_key], counts_7d[item_key]),
                avg_30d=_average_or_none(sums_30d[item_key], counts_30d[item_key]),
            )
            for item_key in normalized_keys
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=_SQLITE_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MILLISECONDS}")
        return connection

    def _init_database(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS processed_snapshots (
                        snapshot_last_updated INTEGER PRIMARY KEY,
                        snapshot_day TEXT NOT NULL,
                        processed_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS lowestbin_daily_aggregates (
                        item_key TEXT NOT NULL,
                        snapshot_day TEXT NOT NULL,
                        price_sum REAL NOT NULL CHECK (price_sum >= 0),
                        sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
                        PRIMARY KEY (item_key, snapshot_day)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_processed_snapshots_snapshot_day
                    ON processed_snapshots (snapshot_day)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_lowestbin_daily_aggregates_snapshot_day
                    ON lowestbin_daily_aggregates (snapshot_day)
                    """
                )
        except sqlite3.Error as exc:
            raise LowestBinHistoryError("Failed to initialize Lowest BIN history store.") from exc
        with suppress(OSError, PermissionError):
            self._database_path.chmod(0o600)


def _aggregate_rows_for_snapshot(
    snapshot_day: str,
    item_prices: Mapping[str, float],
) -> Iterable[tuple[str, str, float, int]]:
    for raw_item_key, raw_price in item_prices.items():
        item_key = str(raw_item_key or "").strip()
        if not item_key:
            logger.warning("Skipping Lowest BIN history row with blank item key.")
            continue
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            logger.warning("Skipping Lowest BIN history row for %s with invalid price %r.", item_key, raw_price)
            continue
        if not math.isfinite(price) or price < 0:
            logger.warning("Skipping Lowest BIN history row for %s with non-finite price %r.", item_key, raw_price)
            continue
        yield (item_key, snapshot_day, price, 1)


def _average_or_none(price_sum: float, sample_count: int) -> float | None:
    if sample_count <= 0:
        return None
    return price_sum / float(sample_count)


def _ensure_storage_dir(storage_dir: Path) -> None:
    storage_dir.mkdir(parents=True, exist_ok=True)
    with suppress(OSError, PermissionError):
        storage_dir.chmod(0o700)


def _iter_batches(values: Sequence[str], batch_size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), batch_size):
        yield values[start : start + batch_size]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
