from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, RootModel


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


class ApiErrorResponse(BaseModel):
    detail: str = Field(..., examples=["Lowest BIN data is temporarily unavailable."])


class LowestBinV1Response(RootModel[dict[str, float]]):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "CRIMSON_BOOTS": 7000000.0,
                "CRIMSON_BOOTS+ATTRIBUTE_MANA_POOL+ATTRIBUTE_VETERAN": 7000000.0,
                "CRIMSON_BOOTS+ATTRIBUTE_MANA_POOL;1": 7000000.0,
                "CRIMSON_BOOTS+ATTRIBUTE_VETERAN;2": 7000000.0,
                "ENDERMAN;4": 5000000.0,
                "ENDERMAN;4+100": 12000000.0,
                "HYPERION": 98000000.0,
                "ICE_RUNE;3": 250000.0,
                "TRUE_ESSENCE": 23437.5,
            }
        }
    )


class LowestBinV2Product(BaseModel):
    price: float = Field(..., examples=[98000000.0])
    auctioneerUuid: str = Field(..., examples=["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"])
    item_name: str = Field(..., examples=["Hyperion"])


class LowestBinV2Response(BaseModel):
    lastUpdated: int = Field(..., examples=[1700000000000])
    products: dict[str, LowestBinV2Product] = Field(
        ...,
        examples=[
            {
                "HYPERION": {
                    "price": 98000000.0,
                    "auctioneerUuid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "item_name": "Hyperion",
                },
                "TRUE_ESSENCE": {
                    "price": 23437.5,
                    "auctioneerUuid": "cccccccccccccccccccccccccccccccc",
                    "item_name": "True Essence",
                },
            }
        ],
    )


class BazaarProductResponse(BaseModel):
    buy: float = Field(..., examples=[101.950378482847])
    sell: float = Field(..., examples=[2.0])
    spread: float = Field(..., examples=[99.950378482847])
    spreadPercentage: float = Field(..., examples=[4997.51892414235])
    buyVolume: int = Field(..., examples=[308384])
    sellVolume: int = Field(..., examples=[718212])
    buyMovingWeek: int = Field(..., examples=[429197])
    sellMovingWeek: int = Field(..., examples=[257881])


class BazaarResponse(BaseModel):
    lastUpdated: int = Field(..., examples=[1715478978620])
    products: dict[str, BazaarProductResponse] = Field(
        ...,
        examples=[
            {
                "CORRUPTED_BAIT": {
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
