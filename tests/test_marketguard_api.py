from __future__ import annotations

import asyncio
import base64
import gzip
import json
import struct
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.marketguard_api.client import HypixelAuctionClient, HypixelBazaarClient
from app.marketguard_api.config import MarketGuardSettings
from app.marketguard_api.item_keys import resolve_auction_item
from app.marketguard_api.main import create_marketguard_app
from app.marketguard_api.service import BazaarService, LowestBinService
from app.training_hub.config.settings import TrainingHubSettings


def test_lowestbin_returns_moulberry_style_lowest_bin_mapping(tmp_path: Path) -> None:
    requests: list[int] = []
    legendary_enderman = {"petInfo": json.dumps({"type": "ENDERMAN", "tier": "LEGENDARY"})}

    async def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(int(request.url.params.get("page", "0")))
        page = int(request.url.params["page"])
        if page == 0:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "totalPages": 2,
                    "lastUpdated": 1_700_000_000_000,
                    "auctions": [
                        _auction("HYPERION", 100_000_000),
                        _auction("TRUE_ESSENCE", 1_500_000, count=64),
                        _auction(
                            "PET",
                            12_000_000,
                            item_name="[Lvl 100] Enderman",
                            extra_attributes=legendary_enderman,
                        ),
                        _auction("HYPERION", 1, bin=False),
                    ],
                },
            )

        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 2,
                "lastUpdated": 1_700_000_000_000,
                "auctions": [
                    _auction("HYPERION", 98_000_000),
                    _auction(
                        "PET",
                        5_000_000,
                        item_name="[Lvl 1] Enderman",
                        extra_attributes=legendary_enderman,
                    ),
                    _auction("RUNE", 250_000, extra_attributes={"runes": {"ICE": 3}}),
                    _auction(
                        "CRIMSON_BOOTS",
                        7_000_000,
                        extra_attributes={"attributes": {"veteran": 2, "mana_pool": 1}},
                    ),
                ],
            },
        )

    settings = _marketguard_settings()
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=_marketguard_service(settings, _handler),
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/lowestbin")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60, stale-if-error=300"
    assert response.headers["x-data-stale"] == "false"
    assert "set-cookie" not in response.headers
    assert response.json() == {
        "CRIMSON_BOOTS": 7_000_000.0,
        "CRIMSON_BOOTS+ATTRIBUTE_MANA_POOL+ATTRIBUTE_VETERAN": 7_000_000.0,
        "CRIMSON_BOOTS+ATTRIBUTE_MANA_POOL;1": 7_000_000.0,
        "CRIMSON_BOOTS+ATTRIBUTE_VETERAN;2": 7_000_000.0,
        "ENDERMAN;4": 5_000_000.0,
        "ENDERMAN;4+100": 12_000_000.0,
        "HYPERION": 98_000_000.0,
        "ICE_RUNE;3": 250_000.0,
        "TRUE_ESSENCE": 23_437.5,
    }
    assert requests == [0, 1]


def test_bazaar_returns_transformed_quick_status_snapshot(tmp_path: Path) -> None:
    request_count = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "lastUpdated": 1_715_478_978_620,
                "products": {
                    "CORRUPTED_BAIT": {
                        "product_id": "CORRUPTED_BAIT",
                        "quick_status": {
                            "productId": "CORRUPTED_BAIT",
                            "sellPrice": 2,
                            "sellVolume": 718212,
                            "sellMovingWeek": 257881,
                            "sellOrders": 17,
                            "buyPrice": 101.950378482847,
                            "buyVolume": 308384,
                            "buyMovingWeek": 429197,
                            "buyOrders": 95,
                        },
                    },
                    "BROKEN_PRODUCT": {
                        "product_id": "BROKEN_PRODUCT",
                        "quick_status": {
                            "buyPrice": "nan",
                            "sellPrice": 1,
                            "buyVolume": 5,
                            "sellVolume": 4,
                        },
                    },
                },
            },
        )

    settings = _marketguard_settings()
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_bazaar_service=_marketguard_bazaar_service(settings, _handler),
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/bazaar")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60, stale-if-error=300"
    assert response.headers["x-data-stale"] == "false"
    assert response.json() == {
        "lastUpdated": 1_715_478_978_620,
        "products": {
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
        },
    }
    assert request_count == 1


def test_lowestbin_uses_cached_snapshot_between_requests(tmp_path: Path) -> None:
    request_count = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": 1_700_000_000_000,
                "auctions": [
                    _auction("HYPERION", 99_000_000),
                ],
            },
        )

    settings = _marketguard_settings()
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=_marketguard_service(settings, _handler),
    )

    with TestClient(app) as client:
        first = client.get("/api/v1/lowestbin")
        second = client.get("/api/v1/lowestbin")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {"HYPERION": 99_000_000.0}
    assert second.json() == {"HYPERION": 99_000_000.0}
    assert request_count == 1


def test_bazaar_uses_cached_snapshot_between_requests(tmp_path: Path) -> None:
    request_count = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "lastUpdated": 1_700_000_000_000,
                "products": {
                    "ENCHANTED_GOLD": {
                        "quick_status": {
                            "buyPrice": 123.4,
                            "sellPrice": 120.1,
                            "buyVolume": 123456,
                            "sellVolume": 120000,
                            "buyMovingWeek": 543210,
                            "sellMovingWeek": 432100,
                        }
                    }
                },
            },
        )

    settings = _marketguard_settings()
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_bazaar_service=_marketguard_bazaar_service(settings, _handler),
    )

    with TestClient(app) as client:
        first = client.get("/api/v1/bazaar")
        second = client.get("/api/v1/bazaar")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert request_count == 1


def test_lowestbin_returns_stale_cache_when_refresh_fails() -> None:
    clock = [0.0]
    request_count = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "totalPages": 1,
                    "lastUpdated": 1_700_000_000_000,
                    "auctions": [
                        _auction("HYPERION", 99_000_000),
                    ],
                },
            )
        return httpx.Response(503, json={"success": False, "cause": "maintenance"})

    settings = _marketguard_settings(cache_ttl_seconds=5, stale_if_error_seconds=30)
    service = _marketguard_service(settings, _handler, clock=lambda: clock[0])

    async def _exercise_service() -> tuple[Any, Any]:
        first_snapshot = await service.get_lowest_bins()
        clock[0] = 6.0
        second_snapshot = await service.get_lowest_bins()
        await service.aclose()
        return first_snapshot, second_snapshot

    first, second = asyncio.run(_exercise_service())

    assert first.is_stale is False
    assert second.is_stale is True
    assert second.items == {"HYPERION": 99_000_000.0}
    assert request_count == 2


def test_bazaar_returns_stale_cache_when_refresh_fails() -> None:
    clock = [0.0]
    request_count = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "lastUpdated": 1_700_000_000_000,
                    "products": {
                        "ENCHANTED_GOLD": {
                            "quick_status": {
                                "buyPrice": 123.4,
                                "sellPrice": 120.1,
                                "buyVolume": 123456,
                                "sellVolume": 120000,
                                "buyMovingWeek": 543210,
                                "sellMovingWeek": 432100,
                            }
                        }
                    },
                },
            )
        return httpx.Response(503, json={"success": False, "cause": "maintenance"})

    settings = _marketguard_settings(cache_ttl_seconds=5, stale_if_error_seconds=30)
    service = _marketguard_bazaar_service(settings, _handler, clock=lambda: clock[0])

    async def _exercise_service() -> tuple[Any, Any]:
        first_snapshot = await service.get_bazaar()
        clock[0] = 6.0
        second_snapshot = await service.get_bazaar()
        await service.aclose()
        return first_snapshot, second_snapshot

    first, second = asyncio.run(_exercise_service())

    assert first.is_stale is False
    assert second.is_stale is True
    assert second.products == {
        "ENCHANTED_GOLD": {
            "buy": 123.4,
            "sell": 120.1,
            "spread": 3.3,
            "spreadPercentage": 2.7477102414654456,
            "buyVolume": 123456,
            "sellVolume": 120000,
            "buyMovingWeek": 543210,
            "sellMovingWeek": 432100,
        }
    }
    assert request_count == 2


def test_lowestbin_rate_limit_uses_platform_limiter(tmp_path: Path) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": 1_700_000_000_000,
                "auctions": [
                    _auction("HYPERION", 99_000_000),
                ],
            },
        )

    settings = _marketguard_settings(lowestbin_rate_limit_per_minute=1)
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=_marketguard_service(settings, _handler),
    )

    with TestClient(app) as client:
        first = client.get("/api/v1/lowestbin")
        second = client.get("/api/v1/lowestbin")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"].isdigit()


def test_bazaar_rate_limit_uses_platform_limiter(tmp_path: Path) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "lastUpdated": 1_700_000_000_000,
                "products": {
                    "ENCHANTED_GOLD": {
                        "quick_status": {
                            "buyPrice": 123.4,
                            "sellPrice": 120.1,
                            "buyVolume": 123456,
                            "sellVolume": 120000,
                            "buyMovingWeek": 543210,
                            "sellMovingWeek": 432100,
                        }
                    }
                },
            },
        )

    settings = _marketguard_settings(lowestbin_rate_limit_per_minute=1)
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_bazaar_service=_marketguard_bazaar_service(settings, _handler),
    )

    with TestClient(app) as client:
        first = client.get("/api/v1/bazaar")
        second = client.get("/api/v1/bazaar")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"].isdigit()


def test_standalone_marketguard_app_enforces_rate_limit_without_training_hub(tmp_path: Path) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": 1_700_000_000_000,
                "auctions": [
                    _auction("HYPERION", 99_000_000),
                ],
            },
        )

    settings = _marketguard_settings(lowestbin_rate_limit_per_minute=1)
    app = create_marketguard_app(
        settings=settings,
        service=_marketguard_service(settings, _handler),
    )

    with TestClient(app) as client:
        first = client.get("/api/v1/lowestbin")
        second = client.get("/api/v1/lowestbin")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"].isdigit()


def test_standalone_marketguard_app_serves_bazaar(tmp_path: Path) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "lastUpdated": 1_700_000_000_000,
                "products": {
                    "ENCHANTED_GOLD": {
                        "quick_status": {
                            "buyPrice": 123.4,
                            "sellPrice": 120.1,
                            "buyVolume": 123456,
                            "sellVolume": 120000,
                            "buyMovingWeek": 543210,
                            "sellMovingWeek": 432100,
                        }
                    }
                },
            },
        )

    settings = _marketguard_settings()
    app = create_marketguard_app(
        settings=settings,
        bazaar_service=_marketguard_bazaar_service(settings, _handler),
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/bazaar")

    assert response.status_code == 200
    assert response.json() == {
        "lastUpdated": 1_700_000_000_000,
        "products": {
            "ENCHANTED_GOLD": {
                "buy": 123.4,
                "sell": 120.1,
                "spread": 3.3,
                "spreadPercentage": 2.7477102414654456,
                "buyVolume": 123456,
                "sellVolume": 120000,
                "buyMovingWeek": 543210,
                "sellMovingWeek": 432100,
            }
        },
    }


def test_resolve_auction_item_supports_special_moulberry_keys() -> None:
    enchanted_book = _auction(
        "ENCHANTED_BOOK",
        4_200_000,
        extra_attributes={"enchantments": {"sharpness": 7}},
    )
    crab_hat = _auction(
        "PARTY_HAT_CRAB",
        80_000_000,
        extra_attributes={"party_hat_color": "blue", "party_hat_year": 2022},
    )

    resolved_book = resolve_auction_item(enchanted_book)
    resolved_hat = resolve_auction_item(crab_hat)

    assert resolved_book is not None
    assert resolved_book.keys == ("SHARPNESS;7",)
    assert resolved_book.unit_price == 4_200_000.0

    assert resolved_hat is not None
    assert resolved_hat.keys == ("PARTY_HAT_CRAB_BLUE_ANIMATED",)
    assert resolved_hat.unit_price == 80_000_000.0


def test_resolve_auction_item_rejects_ambiguous_special_item_payloads() -> None:
    ambiguous_book = _auction(
        "ENCHANTED_BOOK",
        1_000_000,
        extra_attributes={"enchantments": {"sharpness": 7, "smite": 7}},
    )
    ambiguous_rune = _auction(
        "RUNE",
        1_000_000,
        extra_attributes={"runes": {"ICE": 3, "SPIRIT": 3}},
    )

    assert resolve_auction_item(ambiguous_book) is None
    assert resolve_auction_item(ambiguous_rune) is None


def _marketguard_service(
    settings: MarketGuardSettings,
    handler,
    *,
    clock=None,
) -> LowestBinService:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport,
        base_url=settings.hypixel_api_base_url,
    )
    auction_client = HypixelAuctionClient(settings, client=client, close_client=True)
    return LowestBinService(settings, client=auction_client, clock=clock)


def _marketguard_bazaar_service(
    settings: MarketGuardSettings,
    handler,
    *,
    clock=None,
) -> BazaarService:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport,
        base_url=settings.hypixel_api_base_url,
    )
    bazaar_client = HypixelBazaarClient(settings, client=client, close_client=True)
    return BazaarService(settings, client=bazaar_client, clock=clock)


def _marketguard_settings(
    *,
    cache_ttl_seconds: int = 60,
    stale_if_error_seconds: int = 300,
    lowestbin_rate_limit_per_minute: int = 30,
) -> MarketGuardSettings:
    return MarketGuardSettings(
        hypixel_api_base_url="https://api.hypixel.net/v2",
        cache_ttl_seconds=cache_ttl_seconds,
        stale_if_error_seconds=stale_if_error_seconds,
        lowestbin_rate_limit_per_minute=lowestbin_rate_limit_per_minute,
    )


def _training_hub_settings(tmp_path: Path) -> TrainingHubSettings:
    return TrainingHubSettings(
        host="127.0.0.1",
        port=18080,
        database_url="",
        secret_key="test-secret-key-for-security-check-123456",
        session_ttl_minutes=240,
        max_upload_bytes=1024 * 1024,
        storage_dir=tmp_path / "data",
        pipeline_command="",
        project_root=tmp_path,
        admin_emails=set(),
        admin_usernames={"alice", "dev", "owner"},
        trusted_proxies=set(),
        enable_rate_limit=True,
        enforce_origin_check=True,
        smtp_use_starttls=False,
    )


def _auction(
    item_id: str,
    starting_bid: int,
    *,
    count: int = 1,
    item_name: str | None = None,
    bin: bool = True,
    extra_attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged_extra_attributes = {"id": item_id}
    if extra_attributes:
        merged_extra_attributes.update(extra_attributes)

    return {
        "item_name": item_name or item_id,
        "starting_bid": starting_bid,
        "bin": bin,
        "item_bytes": _encode_item_bytes(count=count, extra_attributes=merged_extra_attributes),
    }


def _encode_item_bytes(*, count: int, extra_attributes: dict[str, Any]) -> str:
    item_compound = _compound_payload(
        _tag_byte("Count", count),
        _tag_compound(
            "tag",
            _compound_payload(
                _tag_compound("ExtraAttributes", _encode_compound_fields(extra_attributes)),
            ),
        ),
    )
    root = bytes([10]) + _string_payload("") + _compound_payload(_tag_list("i", 10, item_compound))
    return base64.b64encode(gzip.compress(root)).decode("ascii")


def _encode_compound_fields(values: dict[str, Any]) -> bytes:
    tags: list[bytes] = []
    for key, value in values.items():
        if isinstance(value, bool):
            raise TypeError("Boolean values are not supported in this minimal NBT encoder.")
        if isinstance(value, str):
            tags.append(_named_tag(8, key, _string_payload(value)))
            continue
        if isinstance(value, int):
            tags.append(_named_tag(3, key, struct.pack(">i", value)))
            continue
        if isinstance(value, dict):
            tags.append(_tag_compound(key, _encode_compound_fields(value)))
            continue
        raise TypeError(f"Unsupported NBT test value for {key!r}: {type(value)!r}")
    return _compound_payload(*tags)


def _tag_byte(name: str, value: int) -> bytes:
    return _named_tag(1, name, struct.pack(">b", value))


def _tag_compound(name: str, payload: bytes) -> bytes:
    return _named_tag(10, name, payload)


def _tag_list(name: str, element_type: int, *elements: bytes) -> bytes:
    payload = bytes([element_type]) + struct.pack(">i", len(elements)) + b"".join(elements)
    return _named_tag(9, name, payload)


def _named_tag(tag_type: int, name: str, payload: bytes) -> bytes:
    return bytes([tag_type]) + _string_payload(name) + payload


def _compound_payload(*children: bytes) -> bytes:
    return b"".join(children) + b"\x00"


def _string_payload(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded
