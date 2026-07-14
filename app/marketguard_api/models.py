from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, constr


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
    item_name: str
    avg_7d: float | None
    avg_30d: float | None


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
    products: dict[str, dict[str, float | int | str]]
    is_stale: bool = False


class ApiErrorResponse(BaseModel):
    detail: str = Field(..., examples=["Lowest BIN data is temporarily unavailable."])


class LowestBinV2Product(BaseModel):
    price: float = Field(..., examples=[98000000.0])
    auctioneerUuid: str = Field(..., examples=["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"])
    item_name: str = Field(..., examples=["Hyperion"])
    avg7d: int | None = Field(None, examples=[97500000])
    avg30d: int | None = Field(None, examples=[96000000])


class LowestBinV2Response(BaseModel):
    status: str = Field(..., examples=["ok"])
    lastUpdated: int = Field(..., examples=[1700000000000])
    products: dict[str, LowestBinV2Product] = Field(
        ...,
        examples=[
            {
                "HYPERION": {
                    "price": 98000000.0,
                    "auctioneerUuid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "item_name": "Hyperion",
                    "avg7d": 97500000,
                    "avg30d": 96000000,
                },
                "TRUE_ESSENCE": {
                    "price": 23437.5,
                    "auctioneerUuid": "cccccccccccccccccccccccccccccccc",
                    "item_name": "True Essence",
                    "avg7d": 22850,
                    "avg30d": 22120,
                },
            }
        ],
    )


class LowestBinQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    products: list[constr(strip_whitespace=True, min_length=1, max_length=64)] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Exact product identifiers to return.",
        examples=[["HYPERION", "TRUE_ESSENCE"]],
    )


class BazaarProductResponse(BaseModel):
    item_name: str = Field(..., examples=["Corrupted Bait"])
    buy: float = Field(..., examples=[101.950378482847])
    sell: float = Field(..., examples=[2.0])
    spread: float = Field(..., examples=[99.950378482847])
    spreadPercentage: float = Field(..., examples=[4997.51892414235])
    buyVolume: int = Field(..., examples=[308384])
    sellVolume: int = Field(..., examples=[718212])
    buyMovingWeek: int = Field(..., examples=[429197])
    sellMovingWeek: int = Field(..., examples=[257881])


class BazaarResponse(BaseModel):
    status: str = Field(..., examples=["ok"])
    lastUpdated: int = Field(..., examples=[1715478978620])
    products: dict[str, BazaarProductResponse] = Field(
        ...,
        examples=[
            {
                "CORRUPTED_BAIT": {
                    "item_name": "Corrupted Bait",
                    "buy": 101.950378482847,
                    "sell": 2.0,
                    "spread": 99.950378482847,
                    "spreadPercentage": 4997.51892414235,
                    "buyVolume": 308384,
                    "sellVolume": 718212,
                    "buyMovingWeek": 429197,
                    "sellMovingWeek": 257881,
                }
            }
        ],
    )


class ReadinessComponentResponse(BaseModel):
    status: str = Field(..., examples=["ok"])
    lastUpdated: int | None = Field(None, examples=[1700000000000])


class ReadinessResponse(BaseModel):
    status: str = Field(..., examples=["ok"])
    checkedAt: str = Field(..., examples=["2026-06-12T09:30:00Z"])
    lowestbinV2: ReadinessComponentResponse
    bazaar: ReadinessComponentResponse
