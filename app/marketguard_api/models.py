from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class AuctionPage:
    page_number: int
    total_pages: int
    last_updated: int
    auctions: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class AuctionSnapshot:
    total_pages: int
    last_updated: int
    auctions: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class BazaarProductSnapshot:
    last_updated: int
    products: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class LowestBinSnapshot:
    generated_at: datetime
    snapshot_last_updated: int
    total_pages: int
    total_auctions: int
    total_bin_auctions: int
    items: dict[str, float]
    is_stale: bool = False


@dataclass(frozen=True, slots=True)
class LowestBinV2Entry:
    price: float
    auctioneer_uuid: str


@dataclass(frozen=True, slots=True)
class LowestBinV2Snapshot:
    generated_at: datetime
    snapshot_last_updated: int
    total_pages: int
    total_auctions: int
    total_bin_auctions: int
    items: dict[str, LowestBinV2Entry]
    is_stale: bool = False


@dataclass(frozen=True, slots=True)
class BazaarSnapshot:
    generated_at: datetime
    snapshot_last_updated: int
    products: dict[str, dict[str, float | int]]
    is_stale: bool = False
