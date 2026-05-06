from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.training_hub.infra import db

from .exceptions import MarketGuardStorageError
from .models import BazaarSnapshot, LowestBinSnapshot

logger = logging.getLogger(__name__)

_ROW_BATCH_SIZE = 250


@dataclass(frozen=True, slots=True)
class LowestBinAverageWindow:
    avg_7d: float | None
    avg_30d: float | None


@dataclass(frozen=True, slots=True)
class StoredLowestBinSnapshot:
    snapshot: LowestBinSnapshot
    auctioneer_uuids: dict[str, str]
    item_names: dict[str, str]


def snapshot_day_from_last_updated(snapshot_last_updated: int) -> date:
    try:
        normalized = int(snapshot_last_updated)
    except (TypeError, ValueError) as exc:
        raise MarketGuardStorageError("Lowest BIN snapshot timestamp is invalid.") from exc
    if normalized <= 0:
        raise MarketGuardStorageError("Lowest BIN snapshot timestamp must be positive.")
    try:
        return datetime.fromtimestamp(normalized / 1000, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError) as exc:
        raise MarketGuardStorageError("Lowest BIN snapshot timestamp is out of range.") from exc


class MarketGuardStorage:
    def __init__(self, database_url: str, retention_days: int) -> None:
        self._database_url = str(database_url or "").strip()
        self._retention_days = max(31, int(retention_days))
        if not db.is_mariadb_target(self._database_url):
            raise MarketGuardStorageError("MarketGuard storage requires a MariaDB database URL.")
        self._init_database()

    def write_lowestbin_snapshot(
        self,
        snapshot: LowestBinSnapshot,
        *,
        auctioneer_uuids: Mapping[str, str],
        item_names: Mapping[str, str],
    ) -> None:
        item_rows: list[tuple[str, float, str, str]] = []
        for item_key, price in snapshot.items.items():
            normalized_item_key = str(item_key or "").strip()
            auctioneer_uuid = str(auctioneer_uuids.get(item_key, "") or "").strip().lower()
            item_name = str(item_names.get(item_key, "") or "").strip() or normalized_item_key
            if not normalized_item_key:
                logger.warning("Skipping MarketGuard Lowest BIN row with blank item key.")
                continue
            if len(auctioneer_uuid) != 32 or not all(character in "0123456789abcdef" for character in auctioneer_uuid):
                logger.warning("Skipping MarketGuard Lowest BIN row for %s with invalid auctioneer UUID.", item_key)
                continue
            try:
                normalized_price = float(price)
            except (TypeError, ValueError):
                logger.warning("Skipping MarketGuard Lowest BIN row for %s with invalid price %r.", item_key, price)
                continue
            if not math.isfinite(normalized_price) or normalized_price < 0:
                logger.warning("Skipping MarketGuard Lowest BIN row for %s with non-finite price %r.", item_key, price)
                continue
            item_rows.append((normalized_item_key, normalized_price, auctioneer_uuid, item_name[:255]))

        snapshot_day = snapshot_day_from_last_updated(snapshot.snapshot_last_updated).isoformat()
        prune_before = (date.fromisoformat(snapshot_day) - timedelta(days=self._retention_days - 1)).isoformat()
        generated_at_epoch_ms = _datetime_to_epoch_millis(snapshot.generated_at)
        processed_at_epoch_ms = _utc_now_epoch_millis()

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO marketguard_lowestbin_current_snapshot (
                        singleton_id,
                        generated_at_epoch_ms,
                        snapshot_last_updated,
                        total_pages,
                        total_auctions,
                        total_bin_auctions,
                        updated_at_epoch_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        generated_at_epoch_ms = VALUES(generated_at_epoch_ms),
                        snapshot_last_updated = VALUES(snapshot_last_updated),
                        total_pages = VALUES(total_pages),
                        total_auctions = VALUES(total_auctions),
                        total_bin_auctions = VALUES(total_bin_auctions),
                        updated_at_epoch_ms = VALUES(updated_at_epoch_ms)
                    """,
                    (
                        1,
                        generated_at_epoch_ms,
                        int(snapshot.snapshot_last_updated),
                        int(snapshot.total_pages),
                        int(snapshot.total_auctions),
                        int(snapshot.total_bin_auctions),
                        processed_at_epoch_ms,
                    ),
                )
                connection.execute("DELETE FROM marketguard_lowestbin_current_items")
                if item_rows:
                    _insert_many(
                        connection,
                        """
                        INSERT INTO marketguard_lowestbin_current_items (
                            item_key,
                            price,
                            auctioneer_uuid,
                            item_name
                        ) VALUES
                        """,
                        "(?, ?, ?, ?)",
                        item_rows,
                    )

                inserted = int(
                    connection.execute(
                        """
                        INSERT IGNORE INTO marketguard_processed_lowestbin_snapshots (
                            snapshot_last_updated,
                            snapshot_day,
                            processed_at_epoch_ms
                        ) VALUES (?, ?, ?)
                        """,
                        (int(snapshot.snapshot_last_updated), snapshot_day, processed_at_epoch_ms),
                    ).rowcount
                    or 0
                ) > 0
                if inserted:
                    aggregate_rows = list(_aggregate_rows_for_snapshot(snapshot_day, snapshot.items))
                    if aggregate_rows:
                        _insert_many(
                            connection,
                            """
                            INSERT INTO marketguard_lowestbin_daily_aggregates (
                                item_key,
                                snapshot_day,
                                price_sum,
                                sample_count
                            ) VALUES
                            """,
                            "(?, ?, ?, ?)",
                            aggregate_rows,
                            tail_sql="""
                            ON DUPLICATE KEY UPDATE
                                price_sum = price_sum + VALUES(price_sum),
                                sample_count = sample_count + VALUES(sample_count)
                            """,
                        )
                    connection.execute(
                        "DELETE FROM marketguard_processed_lowestbin_snapshots WHERE snapshot_day < ?",
                        (prune_before,),
                    )
                    connection.execute(
                        "DELETE FROM marketguard_lowestbin_daily_aggregates WHERE snapshot_day < ?",
                        (prune_before,),
                    )
                connection.commit()
        except Exception as exc:
            raise MarketGuardStorageError("Failed to persist Lowest BIN snapshot data.") from exc

    def read_lowestbin_snapshot(self) -> StoredLowestBinSnapshot | None:
        try:
            with self._connect() as connection:
                meta_row = connection.execute(
                    """
                    SELECT
                        generated_at_epoch_ms,
                        snapshot_last_updated,
                        total_pages,
                        total_auctions,
                        total_bin_auctions
                    FROM marketguard_lowestbin_current_snapshot
                    WHERE singleton_id = 1
                    """
                ).fetchone()
                if meta_row is None:
                    return None

                item_rows = connection.execute(
                    """
                    SELECT item_key, price, auctioneer_uuid, item_name
                    FROM marketguard_lowestbin_current_items
                    ORDER BY item_key ASC
                    """
                ).fetchall()
        except Exception as exc:
            raise MarketGuardStorageError("Failed to load Lowest BIN snapshot data.") from exc

        items: dict[str, float] = {}
        auctioneer_uuids: dict[str, str] = {}
        item_names: dict[str, str] = {}
        for row in item_rows:
            item_key = str(row["item_key"])
            items[item_key] = float(row["price"])
            auctioneer_uuids[item_key] = str(row["auctioneer_uuid"])
            item_names[item_key] = str(row["item_name"])

        snapshot = LowestBinSnapshot(
            generated_at=_epoch_millis_to_datetime(int(meta_row["generated_at_epoch_ms"])),
            snapshot_last_updated=int(meta_row["snapshot_last_updated"]),
            total_pages=int(meta_row["total_pages"]),
            total_auctions=int(meta_row["total_auctions"]),
            total_bin_auctions=int(meta_row["total_bin_auctions"]),
            items=items,
            is_stale=False,
        )
        return StoredLowestBinSnapshot(
            snapshot=snapshot,
            auctioneer_uuids=auctioneer_uuids,
            item_names=item_names,
        )

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
                for batch in _iter_batches(normalized_keys, _ROW_BATCH_SIZE):
                    placeholders = ", ".join("?" for _ in batch)
                    rows = connection.execute(
                        f"""
                        SELECT item_key, snapshot_day, price_sum, sample_count
                        FROM marketguard_lowestbin_daily_aggregates
                        WHERE snapshot_day >= ?
                          AND snapshot_day <= ?
                          AND item_key IN ({placeholders})
                        """,
                        (day_30d_iso, anchor_day_iso, *batch),
                    ).fetchall()
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
        except Exception as exc:
            raise MarketGuardStorageError("Failed to query Lowest BIN averages.") from exc

        return {
            item_key: LowestBinAverageWindow(
                avg_7d=_average_or_none(sums_7d[item_key], counts_7d[item_key]),
                avg_30d=_average_or_none(sums_30d[item_key], counts_30d[item_key]),
            )
            for item_key in normalized_keys
        }

    def write_bazaar_snapshot(self, snapshot: BazaarSnapshot) -> None:
        generated_at_epoch_ms = _datetime_to_epoch_millis(snapshot.generated_at)
        updated_at_epoch_ms = _utc_now_epoch_millis()
        product_rows: list[tuple[str, float, float, float, float, int, int, int, int]] = []
        for product_id, product_data in snapshot.products.items():
            normalized_product_id = str(product_id or "").strip()
            if not normalized_product_id:
                continue
            try:
                product_rows.append(
                    (
                        normalized_product_id,
                        float(product_data["buy"]),
                        float(product_data["sell"]),
                        float(product_data["spread"]),
                        float(product_data["spreadPercentage"]),
                        int(product_data["buyVolume"]),
                        int(product_data["sellVolume"]),
                        int(product_data["buyMovingWeek"]),
                        int(product_data["sellMovingWeek"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping invalid bazaar product row for %s.", normalized_product_id)

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO marketguard_bazaar_current_snapshot (
                        singleton_id,
                        generated_at_epoch_ms,
                        snapshot_last_updated,
                        updated_at_epoch_ms
                    ) VALUES (?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                        generated_at_epoch_ms = VALUES(generated_at_epoch_ms),
                        snapshot_last_updated = VALUES(snapshot_last_updated),
                        updated_at_epoch_ms = VALUES(updated_at_epoch_ms)
                    """,
                    (1, generated_at_epoch_ms, int(snapshot.snapshot_last_updated), updated_at_epoch_ms),
                )
                connection.execute("DELETE FROM marketguard_bazaar_current_products")
                if product_rows:
                    _insert_many(
                        connection,
                        """
                        INSERT INTO marketguard_bazaar_current_products (
                            product_id,
                            buy_price,
                            sell_price,
                            spread,
                            spread_percentage,
                            buy_volume,
                            sell_volume,
                            buy_moving_week,
                            sell_moving_week
                        ) VALUES
                        """,
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        product_rows,
                    )
                connection.commit()
        except Exception as exc:
            raise MarketGuardStorageError("Failed to persist bazaar snapshot data.") from exc

    def read_bazaar_snapshot(self) -> BazaarSnapshot | None:
        try:
            with self._connect() as connection:
                meta_row = connection.execute(
                    """
                    SELECT generated_at_epoch_ms, snapshot_last_updated
                    FROM marketguard_bazaar_current_snapshot
                    WHERE singleton_id = 1
                    """
                ).fetchone()
                if meta_row is None:
                    return None
                product_rows = connection.execute(
                    """
                    SELECT
                        product_id,
                        buy_price,
                        sell_price,
                        spread,
                        spread_percentage,
                        buy_volume,
                        sell_volume,
                        buy_moving_week,
                        sell_moving_week
                    FROM marketguard_bazaar_current_products
                    ORDER BY product_id ASC
                    """
                ).fetchall()
        except Exception as exc:
            raise MarketGuardStorageError("Failed to load bazaar snapshot data.") from exc

        products: dict[str, dict[str, float | int]] = {}
        for row in product_rows:
            products[str(row["product_id"])] = {
                "buy": float(row["buy_price"]),
                "sell": float(row["sell_price"]),
                "spread": float(row["spread"]),
                "spreadPercentage": float(row["spread_percentage"]),
                "buyVolume": int(row["buy_volume"]),
                "sellVolume": int(row["sell_volume"]),
                "buyMovingWeek": int(row["buy_moving_week"]),
                "sellMovingWeek": int(row["sell_moving_week"]),
            }

        return BazaarSnapshot(
            generated_at=_epoch_millis_to_datetime(int(meta_row["generated_at_epoch_ms"])),
            snapshot_last_updated=int(meta_row["snapshot_last_updated"]),
            products=products,
            is_stale=False,
        )

    def _connect(self):
        connection = db.connect(self._database_url)
        connection.row_factory = db.Row
        return connection

    def _init_database(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS marketguard_lowestbin_current_snapshot (
                singleton_id TINYINT NOT NULL PRIMARY KEY,
                generated_at_epoch_ms BIGINT NOT NULL,
                snapshot_last_updated BIGINT NOT NULL,
                total_pages INT NOT NULL,
                total_auctions BIGINT NOT NULL,
                total_bin_auctions BIGINT NOT NULL,
                updated_at_epoch_ms BIGINT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS marketguard_lowestbin_current_items (
                item_key VARCHAR(191) NOT NULL PRIMARY KEY,
                price DOUBLE NOT NULL,
                auctioneer_uuid CHAR(32) NOT NULL,
                item_name VARCHAR(255) NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS marketguard_processed_lowestbin_snapshots (
                snapshot_last_updated BIGINT NOT NULL PRIMARY KEY,
                snapshot_day VARCHAR(10) NOT NULL,
                processed_at_epoch_ms BIGINT NOT NULL,
                KEY idx_marketguard_processed_snapshot_day (snapshot_day)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS marketguard_lowestbin_daily_aggregates (
                item_key VARCHAR(191) NOT NULL,
                snapshot_day VARCHAR(10) NOT NULL,
                price_sum DOUBLE NOT NULL,
                sample_count BIGINT NOT NULL,
                PRIMARY KEY (item_key, snapshot_day),
                KEY idx_marketguard_lowestbin_aggregate_snapshot_day (snapshot_day)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS marketguard_bazaar_current_snapshot (
                singleton_id TINYINT NOT NULL PRIMARY KEY,
                generated_at_epoch_ms BIGINT NOT NULL,
                snapshot_last_updated BIGINT NOT NULL,
                updated_at_epoch_ms BIGINT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS marketguard_bazaar_current_products (
                product_id VARCHAR(191) NOT NULL PRIMARY KEY,
                buy_price DOUBLE NOT NULL,
                sell_price DOUBLE NOT NULL,
                spread DOUBLE NOT NULL,
                spread_percentage DOUBLE NOT NULL,
                buy_volume BIGINT NOT NULL,
                sell_volume BIGINT NOT NULL,
                buy_moving_week BIGINT NOT NULL,
                sell_moving_week BIGINT NOT NULL
            )
            """,
        )
        try:
            with self._connect() as connection:
                for statement in statements:
                    connection.execute(statement)
                connection.commit()
        except Exception as exc:
            raise MarketGuardStorageError("Failed to initialize MarketGuard MariaDB schema.") from exc


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


def _insert_many(connection, sql_prefix: str, row_template: str, rows: Sequence[Sequence[object]], *, tail_sql: str = "") -> None:
    for batch in _iter_batches(list(rows), _ROW_BATCH_SIZE):
        values_sql = ", ".join(row_template for _ in batch)
        flattened_params: list[object] = []
        for row in batch:
            flattened_params.extend(row)
        connection.execute(f"{sql_prefix} {values_sql} {tail_sql}".strip(), tuple(flattened_params))


def _iter_batches(values: Sequence[Sequence[object]] | Sequence[str], batch_size: int):
    for start in range(0, len(values), batch_size):
        yield values[start : start + batch_size]


def _average_or_none(price_sum: float, sample_count: int) -> float | None:
    if sample_count <= 0:
        return None
    return price_sum / float(sample_count)


def _utc_now_epoch_millis() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _datetime_to_epoch_millis(value: datetime) -> int:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return int(normalized.timestamp() * 1000)


def _epoch_millis_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
