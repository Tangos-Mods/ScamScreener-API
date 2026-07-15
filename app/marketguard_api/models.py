from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, constr, field_validator


_COMPACT_UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
_DASHED_UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_PLAYER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,16}$")


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
    products: list[constr(strip_whitespace=True, min_length=1, max_length=64)] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Exact product identifiers to return.",
        examples=[["HYPERION", "TRUE_ESSENCE"]],
    )


class PlayerProfileQuery(BaseModel):
    player: str = Field(..., examples=["Pankraz01", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])
    profileId: str = Field(..., examples=["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"])

    @field_validator("player")
    @classmethod
    def validate_player(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if _PLAYER_NAME_PATTERN.fullmatch(normalized):
            return normalized
        if _COMPACT_UUID_PATTERN.fullmatch(normalized) or _DASHED_UUID_PATTERN.fullmatch(normalized):
            return normalized.lower()
        raise ValueError("player must be a Minecraft username or UUID.")

    @field_validator("profileId")
    @classmethod
    def validate_profile_id(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if _COMPACT_UUID_PATTERN.fullmatch(normalized) or _DASHED_UUID_PATTERN.fullmatch(normalized):
            return normalized
        raise ValueError("profileId must be a SkyBlock profile UUID.")


class PlayersQueryRequest(BaseModel):
    players: list[PlayerProfileQuery] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Players and the SkyBlock profile to read for each player.",
        examples=[
            [
                {
                    "player": "Pankraz01",
                    "profileId": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                }
            ]
        ],
    )


class PlayerInventoryItemResponse(BaseModel):
    slot: int = Field(..., ge=0, examples=[0])
    id: str = Field(..., examples=["NECRON_HELMET"])
    name: str = Field(..., examples=["Necron's Helmet"])
    count: int = Field(..., ge=1, examples=[1])


class PlayerWealthResponse(BaseModel):
    bank: float | None = Field(None, examples=[125000000.0])
    purse: float | None = Field(None, examples=[4250000.5])
    equipment: list[PlayerInventoryItemResponse] | None = None
    armor: list[PlayerInventoryItemResponse] | None = None


class PlayerSkillResponse(BaseModel):
    level: int = Field(..., ge=0, examples=[60])
    xp: float = Field(..., ge=0, examples=[111234567.0])


class SkyBlockProfileResponse(BaseModel):
    id: str = Field(..., examples=["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"])
    name: str | None = Field(None, examples=["Apple"])
    selected: bool = Field(..., examples=[True])
    wealth: PlayerWealthResponse
    skills: dict[str, PlayerSkillResponse] | None = None


class PlayerQueryResult(BaseModel):
    status: Literal["ok", "partial", "not_found", "profile_not_found", "profile_unavailable", "unavailable"] = Field(
        ...,
        examples=["ok"],
    )
    uuid: str | None = Field(None, examples=["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])
    name: str | None = Field(None, examples=["Pankraz01"])
    firstJoin: int | None = Field(None, examples=[1587483921000])
    profile: SkyBlockProfileResponse | None = None
    unavailableFields: list[str] = Field(default_factory=list, examples=[[]])


class PlayersQueryResponse(BaseModel):
    status: Literal["ok", "stale"] = Field(..., examples=["ok"])
    players: list[PlayerQueryResult]


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
