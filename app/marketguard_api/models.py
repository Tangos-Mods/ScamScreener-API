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
class LowestBinSnapshot:
    generated_at: datetime
    snapshot_last_updated: int
    total_pages: int
    total_auctions: int
    total_bin_auctions: int
    items: dict[str, float]
    is_stale: bool = False
