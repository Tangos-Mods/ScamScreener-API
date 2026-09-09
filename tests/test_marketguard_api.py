from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import struct
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import threading

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.marketguard_api.cache import CachedResponse, LocalResponseCache, ResponseCacheChain
from app.marketguard_api.client import HypixelAuctionClient, HypixelBazaarClient, HypixelPlayerClient, MojangNameClient
from app.marketguard_api.config import MarketGuardSettings
from app.marketguard_api.item_keys import resolve_auction_item
from app.marketguard_api.main import create_marketguard_app
from app.marketguard_api.models import BazaarSnapshot, LowestBinSnapshot
from app.marketguard_api.nbt import parse_inventory_nbt
from app.marketguard_api.player_service import PlayerService
from app.marketguard_api.refresher import MarketSnapshotRefresher, build_refresh_supervisor
from app.marketguard_api import service as service_module
from app.marketguard_api.service import BazaarService, LowestBinService
from app.marketguard_api.storage import LowestBinAverageWindow, StoredLowestBinSnapshot, snapshot_day_from_last_updated
from app.training_hub.config.settings import TrainingHubSettings


def test_lowestbin_v1_returns_not_found(tmp_path: Path) -> None:
    settings = _marketguard_settings()
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/lowestbin")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_lowestbin_v2_returns_price_auctioneer_uuid_and_item_name(tmp_path: Path) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": 1_700_000_000_000,
                "auctions": [
                    _auction(
                        "HYPERION",
                        100_000_000,
                        item_name="Hyperion",
                        auctioneer="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    ),
                    _auction(
                        "HYPERION",
                        98_000_000,
                        item_name="Hyperion",
                        auctioneer="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    ),
                    _auction(
                        "TRUE_ESSENCE",
                        1_500_000,
                        count=64,
                        item_name="True Essence",
                        auctioneer="cccccccccccccccccccccccccccccccc",
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
        response = client.get("/api/v2/lowestbin")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=60, stale-if-error=300"
    assert response.headers["x-data-stale"] == "false"
    assert response.headers["x-api-provider"] == "Pankraz01"
    assert response.json() == {
        "status": "ok",
        "lastUpdated": 1_700_000_000_000,
        "products": {
            "HYPERION": {
                "price": 98_000_000.0,
                "auctioneerUuid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "item_name": "Hyperion",
                "avg7d": 98_000_000,
                "avg30d": 98_000_000,
            },
            "TRUE_ESSENCE": {
                "price": 23_437.5,
                "auctioneerUuid": "cccccccccccccccccccccccccccccccc",
                "item_name": "True Essence",
                "avg7d": 23_438,
                "avg30d": 23_438,
            },
        },
    }


def test_lowestbin_v2_falls_back_to_item_key_when_item_name_is_blank(tmp_path: Path) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": 1_700_000_000_000,
                "auctions": [
                    _auction(
                        "HYPERION",
                        98_000_000,
                        item_name="   ",
                        auctioneer="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
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
        response = client.get("/api/v2/lowestbin")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "lastUpdated": 1_700_000_000_000,
        "products": {
            "HYPERION": {
                "price": 98_000_000.0,
                "auctioneerUuid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "item_name": "HYPERION",
                "avg7d": 98_000_000,
                "avg30d": 98_000_000,
            }
        },
    }


def test_lowestbin_query_returns_only_requested_products(tmp_path: Path) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": 1_700_000_000_000,
                "auctions": [
                    _auction("HYPERION", 98_000_000, item_name="Hyperion"),
                    _auction("TRUE_ESSENCE", 1_500_000, count=64, item_name="True Essence"),
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
        response = client.request(
            "QUERY",
            "/api/v2/lowestbin",
            json={"products": ["TRUE_ESSENCE", "NOT_PRESENT", "TRUE_ESSENCE"]},
        )

    assert response.status_code == 200
    assert set(response.json()["products"]) == {"TRUE_ESSENCE"}


def test_lowestbin_query_rejects_empty_product_selection(tmp_path: Path) -> None:
    settings = _marketguard_settings()
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        response = client.request("QUERY", "/api/v2/lowestbin", json={"products": []})

    assert response.status_code == 422


def test_players_query_resolves_name_and_returns_profile_wealth_inventory_and_skills(tmp_path: Path) -> None:
    player_uuid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    profile_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    async def _hypixel_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/resources/skyblock/skills":
            assert "api-key" not in request.headers
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "skills": {
                        "FARMING": {
                            "maxLevel": 2,
                            "levels": [
                                {"level": 1, "totalExpRequired": 50},
                                {"level": 2, "totalExpRequired": 175},
                            ],
                        }
                    },
                },
            )
        assert request.headers["api-key"] == "test-hypixel-key"
        if request.url.path == "/v2/player":
            assert request.url.params["uuid"] == player_uuid
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "player": {
                        "displayname": "Pankraz01",
                        "firstLogin": 1_587_483_921_000,
                    },
                },
            )
        assert request.url.path == "/v2/skyblock/profiles"
        assert request.url.params["uuid"] == player_uuid
        return httpx.Response(
            200,
            json={
                "success": True,
                "profiles": [
                    {
                        "profile_id": profile_id,
                        "cute_name": "Apple",
                        "selected": True,
                        "banking": {"balance": 125_000_000},
                        "members": {
                            player_uuid: {
                                "coin_purse": 4_250_000.5,
                                "equippment_contents": {
                                    "data": _encode_inventory_bytes(
                                        [("GAUNTLET_OF_CONTAGION", "Gauntlet of Contagion", 1)]
                                    )
                                },
                                "inv_armor": {
                                    "data": _encode_inventory_bytes(
                                        [("NECRON_HELMET", "Necron's Helmet", 1)]
                                    )
                                },
                                "pets_data": {
                                    "pets": [
                                        {
                                            "type": "ENDER_DRAGON",
                                            "tier": "LEGENDARY",
                                            "exp": 25_367_890.0,
                                            "heldItem": "CROCHET_TIGER_PLUSHIE",
                                            "active": True,
                                        }
                                    ]
                                },
                                "player_data": {"experience": {"SKILL_FARMING": 175}},
                            }
                        },
                    }
                ],
            },
        )

    async def _mojang_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/users/profiles/minecraft/Pankraz01"
        return httpx.Response(200, json={"id": player_uuid, "name": "Pankraz01"})

    settings = _marketguard_settings(hypixel_api_key="test-hypixel-key")
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
        marketguard_player_service=_marketguard_player_service(settings, _hypixel_handler, _mojang_handler),
    )

    with TestClient(app) as client:
        response = client.request(
            "QUERY",
            "/api/v1/players",
            json={"players": [{"player": "Pankraz01"}]},
        )

    assert response.status_code == 200
    response_payload = response.json()
    result = response_payload["players"][0]
    assert result["source"] == "hypixel"
    assert isinstance(result["fetchedAt"], int)
    result.pop("source")
    result.pop("fetchedAt")
    assert response_payload == {
        "status": "ok",
        "players": [
            {
                "status": "partial",
                "uuid": player_uuid,
                "name": "Pankraz01",
                "firstJoin": 1_587_483_921_000,
                "profile": {
                    "id": profile_id,
                    "name": "Apple",
                    "selected": True,
                    "wealth": {
                        "bank": 125_000_000.0,
                        "purse": 4_250_000.5,
                        "equipment": [
                            {
                                "slot": 0,
                                "id": "GAUNTLET_OF_CONTAGION",
                                "name": "Gauntlet of Contagion",
                                "count": 1,
                            }
                        ],
                        "armor": [
                            {
                                "slot": 0,
                                "id": "NECRON_HELMET",
                                "name": "Necron's Helmet",
                                "count": 1,
                            }
                        ],
                    },
                    "skills": {"farming": {"level": 2, "xp": 175.0}},
                    "activePet": {
                        "type": "ENDER_DRAGON",
                        "tier": "LEGENDARY",
                        "xp": 25_367_890.0,
                        "heldItem": "CROCHET_TIGER_PLUSHIE",
                    },
                    "activeWeapon": None,
                },
                "unavailableFields": ["activeWeapon"],
            }
        ],
    }
    assert "input" not in response.json()["players"][0]


def test_player_finance_returns_direct_museum_profile_without_raw_nbt(tmp_path: Path) -> None:
    player_uuid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    profile_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    special_nbt = _encode_inventory_bytes([("DCTR_SPACE_HELM", "Dctr's Space Helmet", 1)])

    async def _hypixel_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["api-key"] == "test-hypixel-key"
        assert request.url.params["profile"] == profile_id
        if request.url.path == "/v2/skyblock/profile":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "profile": {
                        "profile_id": profile_id,
                        "cute_name": "Apple",
                        "selected": True,
                        "banking": {"balance": 125_000_000},
                        "members": {player_uuid: {"coin_purse": 4_250_000.5}},
                    },
                },
            )
        assert request.url.path == "/v2/skyblock/museum"
        return httpx.Response(
            200,
            json={
                "success": True,
                "profile": {
                    "value": 85_000_000,
                    "appraisal": True,
                    "items": {
                        "NECRON_HELMET": {"items": {"data": "must-not-leak"}},
                        "ASPECT_OF_THE_END": {},
                    },
                    "special": [{"items": {"data": special_nbt}}],
                },
            },
        )

    settings = _marketguard_settings(hypixel_api_key="test-hypixel-key")
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_marketguard_app(
        settings=settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
        player_service=_marketguard_player_service(
            settings,
            _hypixel_handler,
            _unexpected_mojang_handler,
        ),
    )

    app.state.rate_limiter = None
    response = _request_without_lifespan(
        app,
        "QUERY",
        "/api/v1/player-finance",
        {
            "playerUuid": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
            "profileId": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["fetchedAt"], int)
    payload.pop("fetchedAt")
    assert payload == {
        "status": "ok",
        "stale": False,
        "playerUuid": player_uuid,
        "profile": {
            "id": profile_id,
            "name": "Apple",
            "selected": True,
            "finance": {
                "bank": 125_000_000.0,
                "purse": 4_250_000.5,
                "museumValue": 85_000_000.0,
                "knownTotal": 214_250_000.5,
            },
            "museum": {
                "value": 85_000_000.0,
                "appraisal": True,
                "donatedIds": ["ASPECT_OF_THE_END", "NECRON_HELMET"],
                "donatedCount": 2,
                "specialIds": ["DCTR_SPACE_HELM"],
                "specialCount": 1,
            },
        },
        "unavailableFields": [],
    }
    assert special_nbt not in response.text
    assert "must-not-leak" not in response.text


def test_player_finance_selects_only_requested_member_from_museum_map(tmp_path: Path) -> None:
    player_uuid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    other_uuid = "cccccccccccccccccccccccccccccccc"
    profile_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    async def _hypixel_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/skyblock/profile":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "profile": {
                        "profile_id": profile_id,
                        "cute_name": "Apple",
                        "selected": True,
                        "banking": {"balance": 10},
                        "members": {
                            player_uuid: {"coin_purse": 20},
                            other_uuid: {"coin_purse": 999_999},
                        },
                    },
                },
            )
        assert request.url.path == "/v2/skyblock/museum"
        return httpx.Response(
            200,
            json={
                "success": True,
                "profile": {
                    player_uuid: {
                        "value": 30,
                        "appraisal": False,
                        "items": {"REQUESTED_ITEM": {}},
                        "special": [],
                    },
                    other_uuid: {
                        "value": 999_999,
                        "appraisal": True,
                        "items": {"OTHER_MEMBER_SECRET_ITEM": {}},
                        "special": ["OTHER_SPECIAL"],
                    },
                },
            },
        )

    settings = _marketguard_settings(hypixel_api_key="test-hypixel-key")
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_marketguard_app(
        settings=settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
        player_service=_marketguard_player_service(
            settings,
            _hypixel_handler,
            _unexpected_mojang_handler,
        ),
    )

    app.state.rate_limiter = None
    response = _request_without_lifespan(
        app,
        "QUERY",
        "/api/v1/player-finance",
        {"playerUuid": player_uuid, "profileId": profile_id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["profile"]["finance"]["knownTotal"] == 60.0
    assert payload["profile"]["museum"]["donatedIds"] == ["REQUESTED_ITEM"]
    assert other_uuid not in response.text
    assert "OTHER_MEMBER_SECRET_ITEM" not in response.text
    assert "OTHER_SPECIAL" not in response.text


def test_player_finance_rejects_non_member_without_fetching_museum(tmp_path: Path) -> None:
    player_uuid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    other_uuid = "cccccccccccccccccccccccccccccccc"
    profile_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    museum_requested = False

    async def _hypixel_handler(request: httpx.Request) -> httpx.Response:
        nonlocal museum_requested
        if request.url.path == "/v2/skyblock/museum":
            museum_requested = True
            raise AssertionError("Museum must not be fetched for a player outside the profile.")
        return httpx.Response(
            200,
            json={
                "success": True,
                "profile": {
                    "profile_id": profile_id,
                    "cute_name": "Apple",
                    "selected": False,
                    "banking": {"balance": 10},
                    "members": {other_uuid: {"coin_purse": 20}},
                },
            },
        )

    settings = _marketguard_settings(hypixel_api_key="test-hypixel-key")
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_marketguard_app(
        settings=settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
        player_service=_marketguard_player_service(
            settings,
            _hypixel_handler,
            _unexpected_mojang_handler,
        ),
    )

    app.state.rate_limiter = None
    response = _request_without_lifespan(
        app,
        "QUERY",
        "/api/v1/player-finance",
        {"playerUuid": player_uuid, "profileId": profile_id},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "member_not_found"
    assert response.json()["unavailableFields"] == ["profile.member", "finance", "museum"]
    assert museum_requested is False


def test_player_finance_keeps_bank_and_purse_when_museum_is_private_or_unavailable(tmp_path: Path) -> None:
    player_uuid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    profile_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    async def _exercise(museum_status: int, museum_payload: dict[str, Any]) -> dict[str, Any]:
        async def _hypixel_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/skyblock/profile":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "profile": {
                            "profile_id": profile_id,
                            "cute_name": "Apple",
                            "selected": True,
                            "banking": {"balance": 10},
                            "members": {player_uuid: {"coin_purse": 20}},
                        },
                    },
                )
            return httpx.Response(museum_status, json=museum_payload)

        settings = _marketguard_settings(hypixel_api_key="test-hypixel-key")
        marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
        app = create_marketguard_app(
            settings=settings,
            service=marketguard_service,
            bazaar_service=marketguard_bazaar_service,
            player_service=_marketguard_player_service(
                settings,
                _hypixel_handler,
                _unexpected_mojang_handler,
            ),
        )
        app.state.rate_limiter = None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.request(
                "QUERY",
                "/api/v1/player-finance",
                json={"playerUuid": player_uuid, "profileId": profile_id},
            )
        assert response.status_code == 200
        return response.json()

    private_payload = asyncio.run(_exercise(200, {"success": True, "profile": {}}))
    unavailable_payload = asyncio.run(_exercise(500, {"success": False, "cause": "temporary"}))

    for payload in (private_payload, unavailable_payload):
        assert payload["status"] == "partial"
        assert payload["profile"]["finance"] == {
            "bank": 10.0,
            "purse": 20.0,
            "museumValue": None,
            "knownTotal": None,
        }
        assert payload["profile"]["museum"]["value"] is None
        assert "museum.value" in payload["unavailableFields"]
        assert "finance.knownTotal" in payload["unavailableFields"]


def test_player_finance_validates_exact_uuid_request_and_preserves_key_auth(tmp_path: Path) -> None:
    settings = _marketguard_settings(hypixel_api_key="")
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_marketguard_app(
        settings=settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
    )

    app.state.rate_limiter = None
    invalid = _request_without_lifespan(
        app,
        "QUERY",
        "/api/v1/player-finance",
        {"playerUuid": "not-a-uuid", "profileId": "b" * 32},
    )
    extra = _request_without_lifespan(
        app,
        "QUERY",
        "/api/v1/player-finance",
        {"playerUuid": "a" * 32, "profileId": "b" * 32, "secret": "never"},
    )
    missing_key = _request_without_lifespan(
        app,
        "QUERY",
        "/api/v1/player-finance",
        {"playerUuid": "a" * 32, "profileId": "b" * 32},
    )

    assert invalid.status_code == 422
    assert extra.status_code == 422
    assert missing_key.status_code == 419
    assert missing_key.json() == {"detail": "Hypixel API key is missing or invalid."}


def test_player_finance_returns_teapot_when_museum_rejects_server_key(tmp_path: Path) -> None:
    player_uuid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    profile_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    async def _hypixel_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/skyblock/profile":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "profile": {
                        "profile_id": profile_id,
                        "cute_name": "Apple",
                        "selected": True,
                        "members": {player_uuid: {"coin_purse": 20}},
                    },
                },
            )
        return httpx.Response(403, json={"success": False, "cause": "Invalid API key"})

    settings = _marketguard_settings(hypixel_api_key="rejected-key")
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_marketguard_app(
        settings=settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
        player_service=_marketguard_player_service(
            settings,
            _hypixel_handler,
            _unexpected_mojang_handler,
        ),
    )

    app.state.rate_limiter = None
    response = _request_without_lifespan(
        app,
        "QUERY",
        "/api/v1/player-finance",
        {"playerUuid": player_uuid, "profileId": profile_id},
    )

    assert response.status_code == 419
    assert response.json() == {"detail": "Hypixel API key is missing or invalid."}


def test_player_finance_preserves_museum_rate_limit_retry_after(tmp_path: Path) -> None:
    player_uuid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    profile_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    async def _hypixel_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/skyblock/profile":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "profile": {
                        "profile_id": profile_id,
                        "selected": True,
                        "members": {player_uuid: {"coin_purse": 20}},
                    },
                },
            )
        return httpx.Response(
            429,
            headers={"Retry-After": "17"},
            json={"success": False, "cause": "rate limited"},
        )

    settings = _marketguard_settings(hypixel_api_key="test-hypixel-key")
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_marketguard_app(
        settings=settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
        player_service=_marketguard_player_service(
            settings,
            _hypixel_handler,
            _unexpected_mojang_handler,
        ),
    )

    app.state.rate_limiter = None
    response = _request_without_lifespan(
        app,
        "QUERY",
        "/api/v1/player-finance",
        {"playerUuid": player_uuid, "profileId": profile_id},
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "17"
    assert response.json() == {"detail": "Player finance data is temporarily unavailable."}


def test_player_finance_coalesces_and_caches_normalized_requests() -> None:
    class _CountingFinanceService:
        def __init__(self) -> None:
            self.calls = 0

        async def get_player_finance(self, query) -> dict[str, object]:
            self.calls += 1
            await asyncio.sleep(0.05)
            return {
                "status": "profile_not_found",
                "stale": False,
                "fetchedAt": 1_700_000_000_000,
                "playerUuid": query.playerUuid,
                "profile": None,
                "unavailableFields": ["profile"],
            }

        async def aclose(self) -> None:
            return None

    settings = _marketguard_settings(hypixel_api_key="test-hypixel-key", players_rate_limit_per_minute=0)
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    counting_service = _CountingFinanceService()
    app = create_marketguard_app(
        settings=settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
        player_service=counting_service,
    )

    dashed = {
        "playerUuid": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        "profileId": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB",
    }
    compact = {
        "playerUuid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "profileId": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    }

    async def _exercise_route() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first, second = await asyncio.gather(
                client.request("QUERY", "/api/v1/player-finance", json=dashed),
                client.request("QUERY", "/api/v1/player-finance", json=compact),
            )
            third = await client.request("QUERY", "/api/v1/player-finance", json=dashed)
            normalized = f"{'a' * 32}:{'b' * 32}"
            cache_key = "player-finance:v1:" + hashlib.sha256(normalized.encode("ascii")).hexdigest()
            await app.state.marketguard_response_cache.set(
                cache_key,
                CachedResponse(payload=third.json(), is_stale=True),
            )
            fourth = await client.request("QUERY", "/api/v1/player-finance", json=compact)
            return [first, second, third, fourth]

    responses = asyncio.run(_exercise_route())

    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    assert counting_service.calls == 1
    assert all(response.json()["playerUuid"] == "a" * 32 for response in responses)
    assert responses[-1].json()["stale"] is True
    assert responses[-1].headers["x-data-stale"] == "true"


def test_players_query_returns_status_per_unknown_or_unavailable_profile(tmp_path: Path) -> None:
    player_uuid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    profile_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    async def _hypixel_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/resources/skyblock/skills":
            return httpx.Response(503, json={"success": False})
        if request.url.path == "/v2/player":
            return httpx.Response(200, json={"success": True, "player": {"displayname": "Known"}})
        return httpx.Response(
            200,
            json={
                "success": True,
                "profiles": [
                    {
                        "profile_id": profile_id,
                        "cute_name": "Hidden",
                        "selected": False,
                        "members": {},
                    }
                ],
            },
        )

    async def _mojang_handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("UUID input must not invoke Mojang name resolution.")

    settings = _marketguard_settings(hypixel_api_key="test-hypixel-key")
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
        marketguard_player_service=_marketguard_player_service(settings, _hypixel_handler, _mojang_handler),
    )

    with TestClient(app) as client:
        response = client.request(
            "QUERY",
            "/api/v1/players",
            json={"players": [{"player": player_uuid, "profileId": profile_id}]},
        )

    assert response.status_code == 200
    result = response.json()["players"][0]
    assert result["status"] == "profile_unavailable"
    assert result["uuid"] == player_uuid
    assert result["firstJoin"] is None
    assert result["source"] == "hypixel"
    assert isinstance(result["fetchedAt"], int)
    assert result["profile"]["wealth"] == {
        "bank": None,
        "purse": None,
        "equipment": None,
        "armor": None,
    }
    assert result["profile"]["skills"] is None
    assert result["unavailableFields"] == [
        "firstJoin",
        "bank",
        "purse",
        "equipment",
        "armor",
        "skills",
        "activePet",
        "activeWeapon",
    ]
    assert "input" not in result


def test_players_query_marks_confirmed_unknown_names_as_mojang_data(tmp_path: Path) -> None:
    async def _hypixel_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/resources/skyblock/skills":
            return httpx.Response(200, json={"success": True, "skills": {}})
        raise AssertionError("A Mojang-confirmed unknown name must not query Hypixel.")

    async def _mojang_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    settings = _marketguard_settings(hypixel_api_key="test-hypixel-key")
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
        marketguard_player_service=_marketguard_player_service(settings, _hypixel_handler, _mojang_handler),
    )

    with TestClient(app) as client:
        response = client.request("QUERY", "/api/v1/players", json={"players": [{"player": "UnknownPlayer"}]})

    assert response.status_code == 200
    result = response.json()["players"][0]
    assert result["status"] == "not_found"
    assert result["source"] == "mojang"
    assert isinstance(result["fetchedAt"], int)


def test_players_query_rejects_invalid_requests_and_rate_limits_public_access(tmp_path: Path) -> None:
    player_uuid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    profile_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    async def _hypixel_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/resources/skyblock/skills":
            return httpx.Response(200, json={"success": True, "skills": {}})
        if request.url.path == "/v2/player":
            return httpx.Response(200, json={"success": True, "player": {"displayname": "Known", "firstLogin": 1}})
        return httpx.Response(
            200,
            json={
                "success": True,
                "profiles": [
                    {
                        "profile_id": profile_id,
                        "members": {
                            player_uuid: {
                                "coin_purse": 0,
                                "equippment_contents": {"data": "invalid"},
                                "inv_armor": {"data": "invalid"},
                            }
                        },
                    }
                ],
            },
        )

    settings = _marketguard_settings(hypixel_api_key="test-hypixel-key", players_rate_limit_per_minute=1)
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
        marketguard_player_service=_marketguard_player_service(settings, _hypixel_handler, lambda _request: None),
    )

    with TestClient(app) as client:
        invalid = client.request("QUERY", "/api/v1/players", json={"players": []})
        first = client.request(
            "QUERY",
            "/api/v1/players",
            json={"players": [{"player": player_uuid, "profileId": profile_id}]},
        )
        second = client.request(
            "QUERY",
            "/api/v1/players",
            json={"players": [{"player": player_uuid, "profileId": profile_id}]},
        )

    assert invalid.status_code == 422
    assert first.status_code == 200
    first_result = first.json()["players"][0]
    assert first_result["status"] == "partial"
    assert first_result["profile"]["wealth"]["equipment"] is None
    assert first_result["profile"]["wealth"]["armor"] is None
    assert first_result["unavailableFields"] == [
        "bank",
        "equipment",
        "armor",
        "skills",
        "activePet",
        "activeWeapon",
    ]
    assert second.status_code == 429
    assert second.headers["retry-after"].isdigit()


def test_players_query_returns_teapot_without_server_hypixel_api_key(tmp_path: Path) -> None:
    settings = _marketguard_settings()
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        response = client.request(
            "QUERY",
            "/api/v1/players",
            json={
                "players": [
                    {
                        "player": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "profileId": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    }
                ]
            },
        )

    assert response.status_code == 419
    assert response.json() == {"detail": "Hypixel API key is missing or invalid."}


def test_players_query_returns_teapot_for_invalid_server_hypixel_api_key(tmp_path: Path) -> None:
    async def _hypixel_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "cause": "Invalid API key"})

    settings = _marketguard_settings(hypixel_api_key="invalid-hypixel-key")
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
        marketguard_player_service=_marketguard_player_service(
            settings,
            _hypixel_handler,
            lambda _request: None,
        ),
    )

    with TestClient(app) as client:
        response = client.request(
            "QUERY",
            "/api/v1/players",
            json={"players": [{"player": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]},
        )

    assert response.status_code == 419
    assert response.json() == {"detail": "Hypixel API key is missing or invalid."}


def test_players_query_preserves_unavailable_status_for_upstream_failures(tmp_path: Path) -> None:
    player_uuid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    profile_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    async def _hypixel_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/resources/skyblock/skills":
            return httpx.Response(200, json={"success": True, "skills": {}})
        if request.url.path == "/v2/player":
            return httpx.Response(503, json={"success": False})
        assert request.url.path == "/v2/skyblock/profiles"
        return httpx.Response(200, json={"success": True, "profiles": []})

    async def _mojang_handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("UUID input must not invoke Mojang name resolution.")

    settings = _marketguard_settings(hypixel_api_key="test-hypixel-key")
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
        marketguard_player_service=_marketguard_player_service(settings, _hypixel_handler, _mojang_handler),
    )

    with TestClient(app) as client:
        response = client.request(
            "QUERY",
            "/api/v1/players",
            json={"players": [{"player": player_uuid, "profileId": profile_id}]},
        )

    assert response.status_code == 200
    assert response.json()["players"] == [
        {
            "status": "unavailable",
            "uuid": player_uuid,
            "name": None,
            "firstJoin": None,
            "fetchedAt": None,
            "source": None,
            "profile": None,
            "unavailableFields": ["firstJoin", "profile"],
        }
    ]
    assert app.state.marketguard_player_query_metrics.snapshot()["upstreamFailures"] == 1


def test_players_query_coalesces_identical_cache_misses_and_tracks_efficiency() -> None:
    class _CountingPlayerService:
        def __init__(self) -> None:
            self.calls = 0

        async def get_players(self, queries) -> dict[str, object]:
            self.calls += 1
            await asyncio.sleep(0.05)
            return {
                "status": "ok",
                "players": [
                    {
                        "status": "not_found",
                        "uuid": None,
                        "name": None,
                        "firstJoin": None,
                        "profile": None,
                        "unavailableFields": [],
                    }
                    for _query in queries
                ],
            }

        async def aclose(self) -> None:
            return None

    settings = _marketguard_settings(players_rate_limit_per_minute=3, hypixel_api_key="test-key")
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    counting_player_service = _CountingPlayerService()
    app = create_marketguard_app(
        settings=settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
        player_service=counting_player_service,
    )
    body = {
        "players": [
            {
                "player": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
                "profileId": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB",
            }
        ]
    }
    alternate_body = {
        "players": [
            {
                "player": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "profileId": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            }
        ]
    }

    async def _exercise_route() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first, second = await asyncio.gather(
                client.request("QUERY", "/api/v1/players", json=body),
                client.request("QUERY", "/api/v1/players", json=alternate_body),
            )
            third = await client.request("QUERY", "/api/v1/players", json=body)
            await asyncio.sleep(0)
            return [first, second, third]

    responses = asyncio.run(_exercise_route())

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert counting_player_service.calls == 1
    metrics = app.state.marketguard_player_query_metrics.snapshot()
    assert metrics["cacheMisses"] == 1
    assert metrics["cacheHits"] == 1
    assert metrics["coalescedWaiters"] == 1
    assert metrics["completedRequests"] == 3
    assert metrics["activeUpstreamLoads"] == 0


def test_inventory_nbt_rejects_decompression_bombs() -> None:
    compressed = base64.b64encode(gzip.compress(b"x" * (8_000_001))).decode("ascii")

    assert parse_inventory_nbt(compressed) is None


def test_inventory_nbt_rejects_oversized_nbt_collections() -> None:
    oversized_list = bytes([9]) + _string_payload("i") + bytes([0]) + struct.pack(">i", 100_001)
    root = bytes([10]) + _string_payload("") + _compound_payload(oversized_list)
    encoded = base64.b64encode(gzip.compress(root)).decode("ascii")

    assert parse_inventory_nbt(encoded) is None


def test_lowestbin_v1_is_removed_from_openapi(tmp_path: Path) -> None:
    settings = _marketguard_settings()
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
    )

    schema = app.openapi()
    assert "/api/v1/lowestbin" not in schema["paths"]
    assert "/api/v1/player-finance" in schema["paths"]
    assert "deprecated" not in schema["paths"]["/api/v2/lowestbin"]["get"]


def test_marketguard_openapi_documents_response_codes_and_examples(tmp_path: Path) -> None:
    settings = _marketguard_settings()
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
    )

    schema = app.openapi()

    bazaar_get = schema["paths"]["/api/v1/bazaar"]["get"]
    assert set(bazaar_get["responses"]) == {"200", "429", "503"}
    assert bazaar_get["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/BazaarResponse")
    assert bazaar_get["responses"]["429"]["content"]["application/json"]["example"] == {"detail": "Too many requests."}
    assert bazaar_get["responses"]["503"]["content"]["application/json"]["example"] == {
        "detail": "Bazaar data is temporarily unavailable."
    }

    schemas = schema["components"]["schemas"]
    assert schemas["BazaarResponse"]["properties"]["status"]["examples"][0] == "ok"
    assert schemas["LowestBinV2Response"]["properties"]["status"]["examples"][0] == "ok"
    assert set(schemas["PlayersQueryResponse"]["properties"]["status"]["enum"]) == {"ok", "stale"}
    assert schemas["BazaarResponse"]["properties"]["products"]["examples"][0]["CORRUPTED_BAIT"]["buy"] == 101.950378482847
    assert schemas["BazaarProductResponse"]["properties"]["item_name"]["examples"][0] == "Corrupted Bait"
    assert schemas["LowestBinV2Product"]["properties"]["item_name"]["examples"][0] == "Hyperion"
    assert schemas["LowestBinV2Product"]["properties"]["avg7d"]["examples"][0] == 97500000
    assert schemas["LowestBinV2Product"]["properties"]["avg30d"]["examples"][0] == 96000000
    players_query = schema["paths"]["/api/v1/players"]["query"]
    assert set(players_query["responses"]) == {"200", "419", "422", "429", "503"}
    assert players_query["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/PlayersQueryResponse"
    )
    player_finance_query = schema["paths"]["/api/v1/player-finance"]["query"]
    assert set(player_finance_query["responses"]) == {"200", "419", "422", "429", "503"}
    assert player_finance_query["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/PlayerFinanceResponse"
    )
    assert schemas["PlayerFinanceQuery"]["additionalProperties"] is False
    assert set(schemas["PlayerFinanceResponse"]["properties"]["status"]["enum"]) == {
        "ok",
        "partial",
        "profile_not_found",
        "member_not_found",
    }
    assert "knownTotal" in schemas["PlayerFinanceValuesResponse"]["properties"]
    assert "roi" not in schemas["PlayerFinanceValuesResponse"]["properties"]
    assert "specialIds" in schemas["PlayerMuseumResponse"]["properties"]
    ready_get = schema["paths"]["/api/v1/ready"]["get"]
    assert set(ready_get["responses"]) == {"200", "206", "503"}
    assert ready_get["responses"]["200"]["description"] == "All MarketGuard datasets are fresh and available."
    assert ready_get["responses"]["206"]["description"] == "At least one MarketGuard dataset is stale."
    assert ready_get["responses"]["503"]["description"] == "At least one MarketGuard dataset is unavailable."


def test_combined_app_disables_docs_when_api_docs_disabled(tmp_path: Path) -> None:
    settings = _training_hub_settings(tmp_path, api_docs_enabled=False)
    marketguard_settings = _marketguard_settings()
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(marketguard_settings)
    app = create_app(
        training_hub_settings=settings,
        marketguard_settings=marketguard_settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        docs_response = client.get("/docs")
        openapi_response = client.get("/openapi.json")

    assert docs_response.status_code == 404
    assert openapi_response.status_code == 404


def test_combined_app_exposes_docs_when_api_docs_enabled(tmp_path: Path) -> None:
    settings = _training_hub_settings(tmp_path, api_docs_enabled=True)
    marketguard_settings = _marketguard_settings()
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(marketguard_settings)
    app = create_app(
        training_hub_settings=settings,
        marketguard_settings=marketguard_settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        docs_response = client.get("/docs")
        openapi_response = client.get("/openapi.json")

    assert docs_response.status_code == 200
    assert openapi_response.status_code == 200


def test_combined_app_openapi_only_exposes_marketguard_api_paths(tmp_path: Path) -> None:
    settings = _training_hub_settings(tmp_path, api_docs_enabled=True)
    marketguard_settings = _marketguard_settings()
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(marketguard_settings)
    app = create_app(
        training_hub_settings=settings,
        marketguard_settings=marketguard_settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["paths"]
    assert all(str(path).startswith("/api") for path in schema["paths"])
    assert "/" not in schema["paths"]
    assert "/login" not in schema["paths"]
    assert "/dashboard" not in schema["paths"]
    assert "/admin" not in schema["paths"]
    assert "/api/v1/health" not in schema["paths"]
    assert "/api/v1/metrics" not in schema["paths"]
    assert "/api/v1/client/auth/login" not in schema["paths"]
    assert "/api/v1/client/uploads" not in schema["paths"]
    assert "/api/v1/ready" in schema["paths"]
    assert "/api/v1/lowestbin" not in schema["paths"]
    assert "/api/v2/lowestbin" in schema["paths"]
    assert "/api/v1/bazaar" in schema["paths"]
    assert "/api/v1/players" in schema["paths"]


def test_marketguard_ready_returns_200_when_all_datasets_fresh() -> None:
    settings = _marketguard_settings()
    store = _MemoryMarketGuardStorage(retention_days=settings.history_retention_days)
    _seed_lowestbin_snapshot(store, snapshot_last_updated=1_700_000_000_000, item_prices={"HYPERION": 98_000_000.0})
    _seed_bazaar_snapshot(store, snapshot_last_updated=1_700_000_100_000)
    app = create_marketguard_app(
        settings=settings,
        service=_ReadinessLowestBinService(store),
        bazaar_service=_ReadinessBazaarService(store),
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-readiness-status"] == "ok"
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["checkedAt"].endswith("Z")
    assert payload["lowestbinV2"] == {"status": "ok", "lastUpdated": 1_700_000_000_000}
    assert payload["bazaar"] == {"status": "ok", "lastUpdated": 1_700_000_100_000}


def test_marketguard_ready_returns_206_when_any_dataset_is_stale() -> None:
    stale_age_seconds = 120
    scenarios = (
        (
            stale_age_seconds,
            0,
        ),
        (
            0,
            stale_age_seconds,
        ),
        (
            stale_age_seconds,
            stale_age_seconds,
        ),
    )

    for lowestbin_age_seconds, bazaar_age_seconds in scenarios:
        settings = _marketguard_settings()
        store = _MemoryMarketGuardStorage(retention_days=settings.history_retention_days)
        _seed_lowestbin_snapshot(
            store,
            snapshot_last_updated=1_700_000_000_000,
            item_prices={"HYPERION": 98_000_000.0},
            generated_at=_relative_snapshot_time(lowestbin_age_seconds),
        )
        _seed_bazaar_snapshot(
            store,
            snapshot_last_updated=1_700_000_100_000,
            generated_at=_relative_snapshot_time(bazaar_age_seconds),
        )
        app = create_marketguard_app(
            settings=settings,
            service=_ReadinessLowestBinService(store),
            bazaar_service=_ReadinessBazaarService(store),
        )

        with TestClient(app) as client:
            response = client.get("/api/v1/ready")

        assert response.status_code == 206
        assert response.headers["x-readiness-status"] == "degraded"
        payload = response.json()
        assert payload["status"] == "degraded"
        assert {payload["lowestbinV2"]["status"], payload["bazaar"]["status"]} <= {"ok", "stale"}
        assert "down" not in {payload["lowestbinV2"]["status"], payload["bazaar"]["status"]}


def test_marketguard_ready_returns_503_when_any_dataset_is_unavailable() -> None:
    expired_age_seconds = 360
    scenarios = (
        (
            None,
            0,
        ),
        (
            0,
            None,
        ),
        (
            120,
            None,
        ),
        (
            expired_age_seconds,
            0,
        ),
    )

    for lowestbin_age_seconds, bazaar_age_seconds in scenarios:
        settings = _marketguard_settings()
        store = _MemoryMarketGuardStorage(retention_days=settings.history_retention_days)
        if lowestbin_age_seconds is not None:
            _seed_lowestbin_snapshot(
                store,
                snapshot_last_updated=1_700_000_000_000,
                item_prices={"HYPERION": 98_000_000.0},
                generated_at=_relative_snapshot_time(lowestbin_age_seconds),
            )
        if bazaar_age_seconds is not None:
            _seed_bazaar_snapshot(
                store,
                snapshot_last_updated=1_700_000_100_000,
                generated_at=_relative_snapshot_time(bazaar_age_seconds),
            )
        app = create_marketguard_app(
            settings=settings,
            service=_ReadinessLowestBinService(store),
            bazaar_service=_ReadinessBazaarService(store),
        )

        with TestClient(app) as client:
            response = client.get("/api/v1/ready")

        assert response.status_code == 503
        assert response.headers["x-readiness-status"] == "unavailable"
        payload = response.json()
        assert payload["status"] == "unavailable"
        assert "down" in {payload["lowestbinV2"]["status"], payload["bazaar"]["status"]}


def test_marketguard_ready_head_uses_same_status_without_body() -> None:
    settings = _marketguard_settings()
    store = _MemoryMarketGuardStorage(retention_days=settings.history_retention_days)
    _seed_lowestbin_snapshot(
        store,
        snapshot_last_updated=1_700_000_000_000,
        item_prices={"HYPERION": 98_000_000.0},
        generated_at=_relative_snapshot_time(120),
    )
    _seed_bazaar_snapshot(store, snapshot_last_updated=1_700_000_100_000)
    app = create_marketguard_app(
        settings=settings,
        service=_ReadinessLowestBinService(store),
        bazaar_service=_ReadinessBazaarService(store),
    )

    with TestClient(app) as client:
        response = client.head("/api/v1/ready")

    assert response.status_code == 206
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["x-readiness-status"] == "degraded"
    assert response.content == b""


def test_marketguard_ready_rejects_post() -> None:
    settings = _marketguard_settings()
    store = _MemoryMarketGuardStorage(retention_days=settings.history_retention_days)
    app = create_marketguard_app(
        settings=settings,
        service=_ReadinessLowestBinService(store),
        bazaar_service=_ReadinessBazaarService(store),
    )

    with TestClient(app) as client:
        response = client.post("/api/v1/ready")

    assert response.status_code == 405


def test_combined_app_docs_csp_allows_swagger_assets(tmp_path: Path) -> None:
    settings = _training_hub_settings(tmp_path, api_docs_enabled=True)
    marketguard_settings = _marketguard_settings()
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(marketguard_settings)
    app = create_app(
        training_hub_settings=settings,
        marketguard_settings=marketguard_settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        docs_response = client.get("/docs")

    assert docs_response.status_code == 200
    csp = docs_response.headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in csp
    assert "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in csp


def test_combined_app_non_docs_csp_remains_strict(tmp_path: Path) -> None:
    settings = _training_hub_settings(tmp_path, api_docs_enabled=True)
    marketguard_settings = _marketguard_settings()
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(marketguard_settings)
    app = create_app(
        training_hub_settings=settings,
        marketguard_settings=marketguard_settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        response = client.get("/api/v2/lowestbin")

    assert response.status_code in {200, 502, 503}
    csp = response.headers["content-security-policy"]
    assert "script-src 'self';" in csp
    assert "https://cdn.jsdelivr.net" not in csp


def test_lowestbin_v2_does_not_emit_deprecation_headers(tmp_path: Path) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": 1_700_000_000_000,
                "auctions": [
                    _auction("HYPERION", 98_000_000),
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
        response = client.get("/api/v2/lowestbin")

    assert response.status_code == 200
    assert "deprecation" not in response.headers
    assert "sunset" not in response.headers


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
    assert response.headers["x-api-provider"] == "Pankraz01"
    assert response.json() == {
        "status": "ok",
        "lastUpdated": 1_715_478_978_620,
        "products": {
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
        },
    }
    assert request_count == 1


def test_lowestbin_v2_uses_cached_snapshot_between_requests(tmp_path: Path) -> None:
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
        first = client.get("/api/v2/lowestbin")
        second = client.get("/api/v2/lowestbin")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "ok"
    assert second.json()["status"] == "ok"
    assert first.json()["products"]["HYPERION"]["price"] == 99_000_000.0
    assert second.json()["products"]["HYPERION"]["price"] == 99_000_000.0
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
    assert first.json()["status"] == "ok"
    assert second.json()["status"] == "ok"
    assert first.json() == second.json()
    assert request_count == 1


def test_response_cache_chain_supports_local_redis_toggle_matrix() -> None:
    entry = CachedResponse(payload={"status": "ok", "lastUpdated": 1_700_000_000_000, "products": {}}, is_stale=False)

    local_only = ResponseCacheChain(LocalResponseCache(ttl_seconds=30, max_entries=4))
    asyncio.run(local_only.set("local-only", entry))
    assert asyncio.run(local_only.get("local-only")) == entry

    shared_entries: dict[str, CachedResponse] = {}
    redis_only = ResponseCacheChain(_SharedMemoryCacheBackend(shared_entries))
    asyncio.run(redis_only.set("redis-only", entry))
    assert asyncio.run(redis_only.get("redis-only")) == entry

    warmed_entries: dict[str, CachedResponse] = {"shared-hit": entry}
    local_backend = LocalResponseCache(ttl_seconds=30, max_entries=4)
    layered_cache = ResponseCacheChain(local_backend, _SharedMemoryCacheBackend(warmed_entries))
    assert asyncio.run(local_backend.get("shared-hit")) is None
    assert asyncio.run(layered_cache.get("shared-hit")) == entry
    warmed_entries.clear()
    assert asyncio.run(local_backend.get("shared-hit")) == entry


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


def test_lowestbin_v2_averages_deduplicate_snapshot_last_updated() -> None:
    clock = [0.0]
    request_count = 0
    duplicate_snapshot_last_updated = _epoch_millis(2025, 1, 10, 12, 0)
    newer_snapshot_last_updated = _epoch_millis(2025, 1, 10, 12, 5)

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count < 3:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "totalPages": 1,
                    "lastUpdated": duplicate_snapshot_last_updated,
                    "auctions": [
                        _auction("HYPERION", 100_000_000, item_name="Hyperion"),
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": newer_snapshot_last_updated,
                "auctions": [
                    _auction("HYPERION", 200_000_000, item_name="Hyperion"),
                ],
            },
        )

    settings = _marketguard_settings(cache_ttl_seconds=5, stale_if_error_seconds=30)
    service = _marketguard_service(settings, _handler, clock=lambda: clock[0])

    async def _exercise_service() -> tuple[Any, Any, Any]:
        first_snapshot = await service.get_lowest_bins_v2()
        clock[0] = 6.0
        second_snapshot = await service.get_lowest_bins_v2()
        clock[0] = 12.0
        third_snapshot = await service.get_lowest_bins_v2()
        await service.aclose()
        return first_snapshot, second_snapshot, third_snapshot

    first, second, third = asyncio.run(_exercise_service())

    assert first.items["HYPERION"].avg_7d == 100_000_000.0
    assert first.items["HYPERION"].avg_30d == 100_000_000.0
    assert second.items["HYPERION"].avg_7d == 100_000_000.0
    assert second.items["HYPERION"].avg_30d == 100_000_000.0
    assert third.items["HYPERION"].avg_7d == 150_000_000.0
    assert third.items["HYPERION"].avg_30d == 150_000_000.0
    assert request_count == 3


def test_lowestbin_v2_averages_respect_7d_and_30d_windows() -> None:
    clock = [0.0]
    request_count = 0
    snapshots = [
        (_epoch_millis(2025, 1, 1, 12, 0), 10_000_000),
        (_epoch_millis(2025, 1, 24, 12, 0), 20_000_000),
        (_epoch_millis(2025, 1, 30, 12, 0), 30_000_000),
        (_epoch_millis(2025, 2, 1, 12, 0), 40_000_000),
    ]

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        snapshot_last_updated, price = snapshots[request_count]
        request_count += 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": snapshot_last_updated,
                "auctions": [
                    _auction("HYPERION", price, item_name="Hyperion"),
                ],
            },
        )

    settings = _marketguard_settings(cache_ttl_seconds=5, stale_if_error_seconds=30)
    service = _marketguard_service(settings, _handler, clock=lambda: clock[0])

    async def _exercise_service() -> Any:
        await service.get_lowest_bins_v2()
        clock[0] = 6.0
        await service.get_lowest_bins_v2()
        clock[0] = 12.0
        await service.get_lowest_bins_v2()
        clock[0] = 18.0
        final_snapshot = await service.get_lowest_bins_v2()
        await service.aclose()
        return final_snapshot

    final_snapshot = asyncio.run(_exercise_service())
    entry = final_snapshot.items["HYPERION"]

    assert entry.price == 40_000_000.0
    assert entry.avg_7d == 35_000_000.0
    assert entry.avg_30d == 30_000_000.0
    assert request_count == 4


def test_lowestbin_v2_stale_cache_keeps_existing_averages_without_new_history_writes() -> None:
    clock = [0.0]
    request_count = 0
    first_snapshot_last_updated = _epoch_millis(2025, 2, 1, 12, 0)
    second_snapshot_last_updated = _epoch_millis(2025, 2, 1, 12, 5)

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "totalPages": 1,
                    "lastUpdated": first_snapshot_last_updated,
                    "auctions": [
                        _auction("HYPERION", 100_000_000, item_name="Hyperion"),
                    ],
                },
            )
        if request_count == 2:
            return httpx.Response(503, json={"success": False, "cause": "maintenance"})
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": second_snapshot_last_updated,
                "auctions": [
                    _auction("HYPERION", 200_000_000, item_name="Hyperion"),
                ],
            },
        )

    settings = _marketguard_settings(cache_ttl_seconds=5, stale_if_error_seconds=30)
    service = _marketguard_service(settings, _handler, clock=lambda: clock[0])

    async def _exercise_service() -> tuple[Any, Any, Any]:
        first_snapshot = await service.get_lowest_bins_v2()
        clock[0] = 6.0
        stale_snapshot = await service.get_lowest_bins_v2()
        clock[0] = 12.0
        refreshed_snapshot = await service.get_lowest_bins_v2()
        await service.aclose()
        return first_snapshot, stale_snapshot, refreshed_snapshot

    first, stale, refreshed = asyncio.run(_exercise_service())

    assert first.items["HYPERION"].avg_7d == 100_000_000.0
    assert stale.is_stale is True
    assert stale.items["HYPERION"].avg_7d == 100_000_000.0
    assert refreshed.items["HYPERION"].avg_7d == 150_000_000.0
    assert refreshed.items["HYPERION"].avg_30d == 150_000_000.0
    assert request_count == 3


def test_lowestbin_history_store_returns_none_for_missing_item_keys(tmp_path: Path) -> None:
    store = _MemoryMarketGuardStorage(retention_days=45)
    averages = store.get_averages(["HYPERION"], anchor_day=datetime(2025, 2, 1, tzinfo=timezone.utc).date())

    assert averages["HYPERION"].avg_7d is None
    assert averages["HYPERION"].avg_30d is None


def test_lowestbin_v2_history_persists_between_combined_and_standalone_apps(tmp_path: Path) -> None:
    shared_store = _MemoryMarketGuardStorage(retention_days=45)
    settings = _marketguard_settings(cache_ttl_seconds=5, stale_if_error_seconds=30)
    first_snapshot_last_updated = _epoch_millis(2025, 2, 1, 12, 0)
    second_snapshot_last_updated = _epoch_millis(2025, 2, 2, 12, 0)

    async def _first_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": first_snapshot_last_updated,
                "auctions": [
                    _auction("HYPERION", 100_000_000, item_name="Hyperion"),
                ],
            },
        )

    async def _second_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": second_snapshot_last_updated,
                "auctions": [
                    _auction("HYPERION", 200_000_000, item_name="Hyperion"),
                ],
            },
        )

    combined_app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=_marketguard_service(settings, _first_handler, clock=lambda: 0.0, storage=shared_store),
    )
    with TestClient(combined_app) as client:
        first_response = client.get("/api/v2/lowestbin")

    assert first_response.status_code == 200
    assert first_response.json()["products"]["HYPERION"]["avg30d"] == 100_000_000

    standalone_app = create_marketguard_app(
        settings=settings,
        service=_marketguard_service(settings, _second_handler, clock=lambda: 6.0, storage=shared_store),
    )
    with TestClient(standalone_app) as client:
        second_response = client.get("/api/v2/lowestbin")

    assert second_response.status_code == 200
    assert second_response.json()["products"]["HYPERION"]["avg7d"] == 150_000_000
    assert second_response.json()["products"]["HYPERION"]["avg30d"] == 150_000_000


def test_lowestbin_v2_rounds_average_values_half_up(tmp_path: Path) -> None:
    store = _MemoryMarketGuardStorage(retention_days=31)
    _seed_lowestbin_snapshot(
        store,
        snapshot_last_updated=_epoch_millis(2025, 1, 10, 12, 0),
        item_prices={"TRUE_ESSENCE": 23_437.0},
    )
    _seed_lowestbin_snapshot(
        store,
        snapshot_last_updated=_epoch_millis(2025, 1, 11, 12, 0),
        item_prices={"TRUE_ESSENCE": 23_438.0},
    )

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": _epoch_millis(2025, 1, 11, 12, 0),
                "auctions": [
                    _auction(
                        "TRUE_ESSENCE",
                        23_437.5,
                        item_name="True Essence",
                        auctioneer="cccccccccccccccccccccccccccccccc",
                    ),
                ],
            },
        )

    settings = _marketguard_settings()
    app = create_app(
        training_hub_settings=_training_hub_settings(tmp_path),
        marketguard_settings=settings,
        marketguard_service=_marketguard_service(settings, _handler, storage=store),
    )

    with TestClient(app) as client:
        response = client.get("/api/v2/lowestbin")

    assert response.status_code == 200
    assert response.json()["products"]["TRUE_ESSENCE"]["avg7d"] == 23_438
    assert response.json()["products"]["TRUE_ESSENCE"]["avg30d"] == 23_438


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
            "item_name": "Enchanted Gold",
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


def test_lowestbin_v2_rate_limit_uses_platform_limiter(tmp_path: Path) -> None:
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
        first = client.get("/api/v2/lowestbin")
        second = client.get("/api/v2/lowestbin")

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
        first = client.get("/api/v2/lowestbin")
        second = client.get("/api/v2/lowestbin")

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
    assert response.headers["x-api-provider"] == "Pankraz01"
    assert response.json() == {
        "status": "ok",
        "lastUpdated": 1_700_000_000_000,
        "products": {
            "ENCHANTED_GOLD": {
                "item_name": "Enchanted Gold",
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


def test_standalone_marketguard_app_exposes_internal_health() -> None:
    settings = _marketguard_settings(trusted_proxies={"testclient"})
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_marketguard_app(
        settings=settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        response = client.get("/api/internal/health", headers={"X-Forwarded-For": "127.0.0.1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "marketguard-api"


def test_standalone_marketguard_app_exposes_internal_live_metrics() -> None:
    settings = _marketguard_settings(trusted_proxies={"testclient"})
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_marketguard_app(
        settings=settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        client.get("/api/v2/lowestbin", headers={"User-Agent": "Mozilla/5.0"})
        client.get("/api/v1/bazaar", headers={"User-Agent": "ScamScreener-MarketGuard/1.0"})
        client.get("/api/v1/ready", headers={"User-Agent": "UptimeRobot/2.0"})
        response = client.get("/api/internal/live-metrics", headers={"X-Forwarded-For": "127.0.0.1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["totalToday"] == 2
    assert payload["totalSinceStart"] == 2
    assert payload["publicApi"]["requestsToday"] == 2
    assert payload["entries"][0]["endpoint"] in {"/api/v2/lowestbin", "/api/v1/bazaar"}
    assert {entry["endpoint"] for entry in payload["entries"]} == {"/api/v2/lowestbin", "/api/v1/bazaar"}


def test_standalone_marketguard_observability_endpoints_block_public_requests() -> None:
    settings = _marketguard_settings(trusted_proxies={"testclient"})
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_marketguard_app(
        settings=settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        health_response = client.get("/api/internal/health", headers={"X-Forwarded-For": "203.0.113.10"})
        metrics_response = client.get("/api/internal/live-metrics", headers={"X-Forwarded-For": "203.0.113.10"})

    assert health_response.status_code == 403
    assert metrics_response.status_code == 403
    assert health_response.json()["detail"] == "Observability endpoint is not available from public networks."
    assert metrics_response.json()["detail"] == "Observability endpoint is not available from public networks."


def test_combined_app_observability_endpoints_block_public_requests(tmp_path: Path) -> None:
    settings = _training_hub_settings(tmp_path, trusted_proxies={"testclient"})
    marketguard_settings = _marketguard_settings()
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(marketguard_settings)
    app = create_app(
        training_hub_settings=settings,
        marketguard_settings=marketguard_settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        health_response = client.get("/api/v1/health", headers={"X-Forwarded-For": "203.0.113.10"})
        metrics_response = client.get("/api/v1/metrics", headers={"X-Forwarded-For": "203.0.113.10"})

    assert health_response.status_code == 403
    assert metrics_response.status_code == 403


def test_combined_app_observability_endpoints_allow_internal_requests(tmp_path: Path) -> None:
    settings = _training_hub_settings(tmp_path, trusted_proxies={"testclient"})
    marketguard_settings = _marketguard_settings()
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(marketguard_settings)
    app = create_app(
        training_hub_settings=settings,
        marketguard_settings=marketguard_settings,
        marketguard_service=marketguard_service,
        marketguard_bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        health_response = client.get("/api/v1/health", headers={"X-Forwarded-For": "127.0.0.1"})
        metrics_response = client.get("/api/v1/metrics", headers={"X-Forwarded-For": "10.0.0.5"})

    assert health_response.status_code == 200
    assert metrics_response.status_code == 200
    assert health_response.json()["status"] == "ok"
    assert "scamscreener_users_total" in metrics_response.text


def test_standalone_marketguard_app_disables_docs_when_configured() -> None:
    settings = _marketguard_settings(api_docs_enabled=False)
    marketguard_service, marketguard_bazaar_service = _noop_marketguard_services(settings)
    app = create_marketguard_app(
        settings=settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
    )

    with TestClient(app) as client:
        docs_response = client.get("/docs")
        openapi_response = client.get("/openapi.json")

    assert docs_response.status_code == 404
    assert openapi_response.status_code == 404


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
    storage=None,
) -> LowestBinService:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport,
        base_url=settings.hypixel_api_base_url,
    )
    auction_client = HypixelAuctionClient(settings, client=client, close_client=True)
    return LowestBinService(
        settings,
        client=auction_client,
        clock=clock,
        storage=storage or _MemoryMarketGuardStorage(retention_days=settings.history_retention_days),
    )


def _marketguard_bazaar_service(
    settings: MarketGuardSettings,
    handler,
    *,
    clock=None,
    storage=None,
) -> BazaarService:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport,
        base_url=settings.hypixel_api_base_url,
    )
    bazaar_client = HypixelBazaarClient(settings, client=client, close_client=True)
    return BazaarService(
        settings,
        client=bazaar_client,
        clock=clock,
        storage=storage or _MemoryMarketGuardStorage(retention_days=settings.history_retention_days),
    )


def _marketguard_player_service(
    settings: MarketGuardSettings,
    hypixel_handler,
    mojang_handler,
) -> PlayerService:
    hypixel_client = httpx.AsyncClient(
        transport=httpx.MockTransport(hypixel_handler),
        base_url=settings.hypixel_api_base_url,
    )
    mojang_client = httpx.AsyncClient(
        transport=httpx.MockTransport(mojang_handler),
        base_url="https://api.mojang.com",
    )
    return PlayerService(
        settings,
        hypixel_client=HypixelPlayerClient(settings, client=hypixel_client, close_client=True),
        mojang_client=MojangNameClient(settings, client=mojang_client, close_client=True),
    )


async def _unexpected_mojang_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Unexpected Mojang request: {request.url}")


def _request_without_lifespan(app, method: str, path: str, payload: dict[str, Any]) -> httpx.Response:
    async def _request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, json=payload)

    return asyncio.run(_request())


def _marketguard_settings(
    *,
    cache_ttl_seconds: int = 60,
    stale_if_error_seconds: int = 300,
    history_retention_days: int = 45,
    lowestbin_rate_limit_per_minute: int = 30,
    players_rate_limit_per_minute: int = 3,
    hypixel_api_key: str = "",
    api_docs_enabled: bool = True,
    local_cache_enabled: bool = True,
    local_cache_ttl_seconds: int = 15,
    local_cache_max_entries: int = 32,
    redis_enabled: bool = False,
    redis_url: str = "",
    trusted_proxies: set[str] | None = None,
    background_refresh_enabled: bool = False,
    background_refresh_interval_seconds: int = 0,
    background_refresh_retry_seconds: int = 15,
    readiness_fresh_grace_seconds: int = 0,
) -> MarketGuardSettings:
    # Background refresh and the readiness grace window default to off here so
    # tests drive the refresh path explicitly instead of racing a timer.
    return MarketGuardSettings(
        hypixel_api_base_url="https://api.hypixel.net/v2",
        database_url="mariadb://scamscreener:test@127.0.0.1:3306/scamscreener_hub",
        hypixel_api_key=hypixel_api_key,
        cache_ttl_seconds=cache_ttl_seconds,
        stale_if_error_seconds=stale_if_error_seconds,
        background_refresh_enabled=background_refresh_enabled,
        background_refresh_interval_seconds=background_refresh_interval_seconds,
        background_refresh_retry_seconds=background_refresh_retry_seconds,
        readiness_fresh_grace_seconds=readiness_fresh_grace_seconds,
        history_retention_days=history_retention_days,
        lowestbin_rate_limit_per_minute=lowestbin_rate_limit_per_minute,
        players_rate_limit_per_minute=players_rate_limit_per_minute,
        local_cache_enabled=local_cache_enabled,
        local_cache_ttl_seconds=local_cache_ttl_seconds,
        local_cache_max_entries=local_cache_max_entries,
        redis_enabled=redis_enabled,
        redis_url=redis_url,
        trusted_proxies=set(trusted_proxies or set()),
        api_docs_enabled=api_docs_enabled,
    )


def _noop_marketguard_services(settings: MarketGuardSettings) -> tuple[LowestBinService, BazaarService]:
    shared_storage = _MemoryMarketGuardStorage(retention_days=settings.history_retention_days)

    async def _auction_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": 1_700_000_000_000,
                "auctions": [_auction("HYPERION", 99_000_000)],
            },
        )

    async def _bazaar_handler(_request: httpx.Request) -> httpx.Response:
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

    return (
        _marketguard_service(settings, _auction_handler, storage=shared_storage),
        _marketguard_bazaar_service(settings, _bazaar_handler, storage=shared_storage),
    )


def _training_hub_settings(
    tmp_path: Path,
    *,
    api_docs_enabled: bool = True,
    trusted_proxies: set[str] | None = None,
) -> TrainingHubSettings:
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
        trusted_proxies=set(trusted_proxies or set()),
        enable_rate_limit=True,
        enforce_origin_check=True,
        smtp_use_starttls=False,
        api_docs_enabled=api_docs_enabled,
    )


class _MemoryMarketGuardStorage:
    def __init__(self, *, retention_days: int) -> None:
        self._retention_days = max(31, int(retention_days))
        self._lowestbin_snapshot: StoredLowestBinSnapshot | None = None
        self._bazaar_snapshot: BazaarSnapshot | None = None
        self._processed_snapshots: set[int] = set()
        self._daily_aggregates: dict[tuple[str, str], tuple[float, int]] = {}

    def write_lowestbin_snapshot(
        self,
        snapshot: LowestBinSnapshot,
        *,
        auctioneer_uuids: Mapping[str, str],
        item_names: Mapping[str, str],
    ) -> None:
        normalized_items = dict(snapshot.items)
        self._lowestbin_snapshot = StoredLowestBinSnapshot(
            snapshot=replace(snapshot, items=normalized_items, is_stale=False),
            auctioneer_uuids={str(key): str(value) for key, value in auctioneer_uuids.items()},
            item_names={str(key): str(value) for key, value in item_names.items()},
        )
        if snapshot.snapshot_last_updated in self._processed_snapshots:
            return
        self._processed_snapshots.add(snapshot.snapshot_last_updated)
        snapshot_day = snapshot_day_from_last_updated(snapshot.snapshot_last_updated).isoformat()
        prune_before = (
            snapshot_day_from_last_updated(snapshot.snapshot_last_updated) - timedelta(days=self._retention_days - 1)
        ).isoformat()
        for item_key, price in normalized_items.items():
            current_sum, current_count = self._daily_aggregates.get((item_key, snapshot_day), (0.0, 0))
            self._daily_aggregates[(item_key, snapshot_day)] = (current_sum + float(price), current_count + 1)
        self._daily_aggregates = {
            key: value for key, value in self._daily_aggregates.items() if key[1] >= prune_before
        }

    def read_lowestbin_snapshot(self) -> StoredLowestBinSnapshot | None:
        return self._lowestbin_snapshot

    def get_averages(self, item_keys: list[str], *, anchor_day) -> dict[str, LowestBinAverageWindow]:
        day_7d_iso = (anchor_day - timedelta(days=6)).isoformat()
        day_30d_iso = (anchor_day - timedelta(days=29)).isoformat()
        averages: dict[str, LowestBinAverageWindow] = {}
        for item_key in item_keys:
            sum_7d = 0.0
            count_7d = 0
            sum_30d = 0.0
            count_30d = 0
            for (aggregate_item_key, aggregate_day), (price_sum, sample_count) in self._daily_aggregates.items():
                if aggregate_item_key != item_key or aggregate_day > anchor_day.isoformat() or aggregate_day < day_30d_iso:
                    continue
                sum_30d += price_sum
                count_30d += sample_count
                if aggregate_day >= day_7d_iso:
                    sum_7d += price_sum
                    count_7d += sample_count
            averages[item_key] = LowestBinAverageWindow(
                avg_7d=None if count_7d == 0 else sum_7d / count_7d,
                avg_30d=None if count_30d == 0 else sum_30d / count_30d,
            )
        return averages

    def write_bazaar_snapshot(self, snapshot: BazaarSnapshot) -> None:
        self._bazaar_snapshot = replace(snapshot, products=dict(snapshot.products), is_stale=False)

    def read_bazaar_snapshot(self) -> BazaarSnapshot | None:
        return self._bazaar_snapshot


class _ReadinessLowestBinService:
    def __init__(self, storage: _MemoryMarketGuardStorage) -> None:
        self._storage = storage

    async def get_lowest_bins_v2(self):
        raise AssertionError("Readiness route must not call get_lowest_bins_v2().")

    async def aclose(self) -> None:
        return None


class _ReadinessBazaarService:
    def __init__(self, storage: _MemoryMarketGuardStorage) -> None:
        self._storage = storage

    async def get_bazaar(self) -> BazaarSnapshot:
        raise AssertionError("Readiness route must not call get_bazaar().")

    async def aclose(self) -> None:
        return None


class _SharedMemoryCacheBackend:
    def __init__(self, shared_entries: dict[str, CachedResponse]) -> None:
        self._shared_entries = shared_entries

    async def get(self, key: str) -> CachedResponse | None:
        return self._shared_entries.get(str(key))

    async def set(self, key: str, entry: CachedResponse) -> None:
        self._shared_entries[str(key)] = entry

    async def aclose(self) -> None:
        return None


def _seed_lowestbin_snapshot(
    store: _MemoryMarketGuardStorage,
    *,
    snapshot_last_updated: int,
    item_prices: dict[str, float],
    generated_at: datetime | None = None,
) -> None:
    store.write_lowestbin_snapshot(
        LowestBinSnapshot(
            generated_at=generated_at or datetime.now(timezone.utc),
            snapshot_last_updated=snapshot_last_updated,
            total_pages=1,
            total_auctions=len(item_prices),
            total_bin_auctions=len(item_prices),
            items=dict(item_prices),
            is_stale=False,
        ),
        auctioneer_uuids={item_key: "cccccccccccccccccccccccccccccccc" for item_key in item_prices},
        item_names={item_key: item_key.replace("_", " ").title() for item_key in item_prices},
    )


def _seed_bazaar_snapshot(
    store: _MemoryMarketGuardStorage,
    *,
    snapshot_last_updated: int,
    generated_at: datetime | None = None,
) -> None:
    store.write_bazaar_snapshot(
        BazaarSnapshot(
            generated_at=generated_at or datetime.now(timezone.utc),
            snapshot_last_updated=snapshot_last_updated,
            products={
                "ENCHANTED_GOLD": {
                    "item_name": "Enchanted Gold",
                    "buy": 123.4,
                    "sell": 120.1,
                    "spread": 3.3,
                    "spreadPercentage": 2.747710241465445,
                    "buyVolume": 123456,
                    "sellVolume": 120000,
                    "buyMovingWeek": 543210,
                    "sellMovingWeek": 432100,
                }
            },
            is_stale=False,
        )
    )


def _relative_snapshot_time(age_seconds: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=max(0, int(age_seconds)))


def _auction(
    item_id: str,
    starting_bid: int,
    *,
    count: int = 1,
    item_name: str | None = None,
    bin: bool = True,
    auctioneer: str = "11111111111111111111111111111111",
    extra_attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged_extra_attributes = {"id": item_id}
    if extra_attributes:
        merged_extra_attributes.update(extra_attributes)

    return {
        "item_name": item_name or item_id,
        "starting_bid": starting_bid,
        "bin": bin,
        "auctioneer": auctioneer,
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


def _encode_inventory_bytes(items: list[tuple[str, str, int]]) -> str:
    encoded_items: list[bytes] = []
    for item_id, display_name, count in items:
        tag_payload = _compound_payload(
            _tag_compound("ExtraAttributes", _encode_compound_fields({"id": item_id})),
            _tag_compound("display", _compound_payload(_named_tag(8, "Name", _string_payload(display_name)))),
        )
        encoded_items.append(
            _compound_payload(
                _tag_byte("Count", count),
                _tag_compound("tag", tag_payload),
            )
        )
    root = bytes([10]) + _string_payload("") + _compound_payload(_tag_list("i", 10, *encoded_items))
    return base64.b64encode(gzip.compress(root)).decode("ascii")


def _epoch_millis(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


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


# --- Background snapshot refresh -------------------------------------------------
#
# Regression cover for the availability incident: the Hypixel refresh used to run
# inside client requests and decoded every auction payload on the event loop, so
# a single Uvicorn worker went unresponsive for seconds at a time on every cache
# expiry - which the uptime monitor reported as the API being offline.


def test_lowestbin_refresh_decodes_auctions_off_the_event_loop(monkeypatch) -> None:
    """The auction decode must run on a worker thread, not the event loop.

    Decoding a full auction house is seconds of base64 + gzip + NBT work. Running
    it on the loop froze every other request, the container health probe and the
    public readiness probe for the duration - the direct cause of the uptime
    monitor reporting the API offline. Asserting on thread identity keeps this
    deterministic instead of timing-dependent.
    """
    loop_thread_id = threading.get_ident()
    observed: dict[str, Any] = {"decode_thread_id": None, "ticks_during_decode": 0}
    decode_started = threading.Event()
    decode_may_finish = threading.Event()
    real_build_index = service_module.build_lowestbin_index

    def _instrumented_build_index(auctions):
        observed["decode_thread_id"] = threading.get_ident()
        decode_started.set()
        # Hold the decode open so the test can prove the loop still runs.
        decode_may_finish.wait(timeout=5)
        return real_build_index(auctions)

    monkeypatch.setattr(service_module, "build_lowestbin_index", _instrumented_build_index)

    async def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": _epoch_millis(2025, 3, 1, 12, 0),
                "auctions": [_auction("HYPERION", 100_000_000, item_name="Hyperion")],
            },
        )

    settings = _marketguard_settings()
    service = _marketguard_service(settings, _handler)

    async def _exercise() -> None:
        refresh_task = asyncio.create_task(service.refresh())
        # Tick the loop while the decode is blocked in its worker thread.
        while not decode_started.is_set():
            await asyncio.sleep(0.001)
        for _ in range(10):
            await asyncio.sleep(0.001)
            observed["ticks_during_decode"] += 1
        decode_may_finish.set()
        await refresh_task
        await service.aclose()

    asyncio.run(_exercise())

    assert observed["decode_thread_id"] is not None
    assert observed["decode_thread_id"] != loop_thread_id
    # The loop kept being scheduled the whole time the decode was running.
    assert observed["ticks_during_decode"] == 10


def test_background_refresher_keeps_requests_off_the_upstream_path() -> None:
    """Once the refresher owns the refresh, a stale snapshot is served, not refetched."""
    clock = [0.0]
    request_count = 0

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": _epoch_millis(2025, 3, 1, 12, 0) + request_count,
                "auctions": [_auction("HYPERION", 100_000_000, item_name="Hyperion")],
            },
        )

    settings = _marketguard_settings(cache_ttl_seconds=5, stale_if_error_seconds=300)
    service = _marketguard_service(settings, _handler, clock=lambda: clock[0])

    async def _exercise() -> tuple[Any, int, int]:
        refresher = MarketSnapshotRefresher("lowestbin", service, settings, sleep=_single_cycle_sleep())
        await refresher.start()
        assert await refresher.wait_for_first_refresh(timeout=5)
        after_first_refresh = request_count

        # Snapshot is now past its TTL, but the refresher owns refreshing it.
        clock[0] = 60.0
        snapshot = await service.get_lowest_bins()
        served_without_upstream = request_count

        await refresher.stop()
        await service.aclose()
        return snapshot, after_first_refresh, served_without_upstream

    snapshot, after_first_refresh, served_without_upstream = asyncio.run(_exercise())

    assert after_first_refresh >= 1
    assert snapshot.items == {"HYPERION": 100_000_000.0}
    assert snapshot.is_stale is True
    # The request served the stored snapshot instead of paying for a fetch.
    assert served_without_upstream == after_first_refresh


def test_background_refresher_restores_inline_refresh_after_stop() -> None:
    request_count = 0

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": _epoch_millis(2025, 3, 1, 12, 0) + request_count,
                "auctions": [_auction("HYPERION", 100_000_000, item_name="Hyperion")],
            },
        )

    settings = _marketguard_settings(cache_ttl_seconds=5, stale_if_error_seconds=300)
    service = _marketguard_service(settings, _handler, clock=lambda: 0.0)

    async def _exercise() -> tuple[bool, bool]:
        refresher = MarketSnapshotRefresher("lowestbin", service, settings, sleep=_single_cycle_sleep())
        await refresher.start()
        assert await refresher.wait_for_first_refresh(timeout=5)
        disabled_while_running = not service.inline_refresh_enabled
        await refresher.stop()
        enabled_after_stop = service.inline_refresh_enabled
        await service.aclose()
        return disabled_while_running, enabled_after_stop

    disabled_while_running, enabled_after_stop = asyncio.run(_exercise())

    assert disabled_while_running is True
    # Nothing else refreshes after shutdown, so requests must be able to again.
    assert enabled_after_stop is True


def test_background_refresher_survives_upstream_failure_and_retries_sooner() -> None:
    attempts = 0

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"success": False, "cause": "maintenance"})

    settings = _marketguard_settings(
        cache_ttl_seconds=60,
        stale_if_error_seconds=300,
        background_refresh_retry_seconds=15,
    )
    service = _marketguard_service(settings, _handler, clock=lambda: 0.0)

    async def _exercise() -> tuple[bool, float]:
        refresher = MarketSnapshotRefresher("lowestbin", service, settings, sleep=_single_cycle_sleep())
        await refresher.start()
        assert await refresher.wait_for_first_refresh(timeout=5)
        # A failed refresh must not hand the request path off to a snapshot that
        # does not exist yet.
        inline_still_enabled = service.inline_refresh_enabled
        retry_delay = refresher._next_delay_seconds(False)
        await refresher.stop()
        await service.aclose()
        return inline_still_enabled, retry_delay

    inline_still_enabled, retry_delay = asyncio.run(_exercise())

    assert attempts >= 1
    assert inline_still_enabled is True
    assert retry_delay == 15.0


def test_background_refresher_interval_defaults_to_cache_ttl_with_jitter() -> None:
    settings = _marketguard_settings(cache_ttl_seconds=45)
    assert settings.effective_background_refresh_interval_seconds == 45

    pinned = _marketguard_settings(cache_ttl_seconds=45, background_refresh_interval_seconds=20)
    assert pinned.effective_background_refresh_interval_seconds == 20

    service = _NoopRefreshableService()
    refresher = MarketSnapshotRefresher("lowestbin", service, settings, sleep=_single_cycle_sleep())
    delay = refresher._next_delay_seconds(True)
    # Jitter keeps multiple instances from hitting Hypixel in lockstep.
    assert 45.0 <= delay <= 45.0 * 1.1


def test_build_refresh_supervisor_respects_the_disable_switch() -> None:
    disabled = _marketguard_settings(background_refresh_enabled=False)
    assert (
        build_refresh_supervisor(
            disabled,
            lowestbin_service=_NoopRefreshableService(),
            bazaar_service=_NoopRefreshableService(),
        )
        is None
    )

    enabled = _marketguard_settings(background_refresh_enabled=True)
    supervisor = build_refresh_supervisor(
        enabled,
        lowestbin_service=_NoopRefreshableService(),
        bazaar_service=_NoopRefreshableService(),
    )
    assert supervisor is not None
    assert [refresher.name for refresher in supervisor.refreshers] == ["lowestbin", "bazaar"]


def test_bazaar_background_refresh_serves_stale_without_upstream_call() -> None:
    clock = [0.0]
    request_count = 0

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "lastUpdated": _epoch_millis(2025, 3, 1, 12, 0) + request_count,
                "products": {
                    "ENCHANTED_DIAMOND": {
                        "quick_status": {
                            "buyPrice": 100.0,
                            "sellPrice": 90.0,
                            "buyVolume": 10,
                            "sellVolume": 20,
                            "buyMovingWeek": 30,
                            "sellMovingWeek": 40,
                        }
                    }
                },
            },
        )

    settings = _marketguard_settings(cache_ttl_seconds=5, stale_if_error_seconds=300)
    service = _marketguard_bazaar_service(settings, _handler, clock=lambda: clock[0])

    async def _exercise() -> tuple[Any, int, int]:
        refresher = MarketSnapshotRefresher("bazaar", service, settings, sleep=_single_cycle_sleep())
        await refresher.start()
        assert await refresher.wait_for_first_refresh(timeout=5)
        after_first_refresh = request_count

        clock[0] = 60.0
        snapshot = await service.get_bazaar()
        served_without_upstream = request_count

        await refresher.stop()
        await service.aclose()
        return snapshot, after_first_refresh, served_without_upstream

    snapshot, after_first_refresh, served_without_upstream = asyncio.run(_exercise())

    assert after_first_refresh >= 1
    assert snapshot.is_stale is True
    assert served_without_upstream == after_first_refresh


def test_concurrent_lowestbin_requests_trigger_a_single_upstream_refresh() -> None:
    """A cold cache under load must not fan out into one Hypixel fetch per caller."""
    request_count = 0

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        await asyncio.sleep(0.02)
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": _epoch_millis(2025, 3, 1, 12, 0),
                "auctions": [_auction("HYPERION", 100_000_000, item_name="Hyperion")],
            },
        )

    settings = _marketguard_settings(cache_ttl_seconds=60)
    service = _marketguard_service(settings, _handler, clock=lambda: 0.0)

    async def _exercise() -> list[Any]:
        snapshots = await asyncio.gather(*(service.get_lowest_bins() for _ in range(8)))
        await service.aclose()
        return snapshots

    snapshots = asyncio.run(_exercise())

    assert all(snapshot.items == {"HYPERION": 100_000_000.0} for snapshot in snapshots)
    assert request_count == 1


def test_readiness_grace_window_keeps_a_normally_aged_snapshot_ready() -> None:
    """Hypixel regenerates roughly once a minute, so TTL+15s is healthy, not degraded."""
    snapshot_age_seconds = 75

    def _build_app(settings: MarketGuardSettings):
        store = _MemoryMarketGuardStorage(retention_days=settings.history_retention_days)
        _seed_lowestbin_snapshot(
            store,
            snapshot_last_updated=1_700_000_000_000,
            item_prices={"HYPERION": 98_000_000.0},
            generated_at=_relative_snapshot_time(snapshot_age_seconds),
        )
        _seed_bazaar_snapshot(
            store,
            snapshot_last_updated=1_700_000_100_000,
            generated_at=_relative_snapshot_time(snapshot_age_seconds),
        )
        return create_marketguard_app(
            settings=settings,
            service=_ReadinessLowestBinService(store),
            bazaar_service=_ReadinessBazaarService(store),
        )

    graced_app = _build_app(_marketguard_settings(cache_ttl_seconds=60, readiness_fresh_grace_seconds=120))
    with TestClient(graced_app) as client:
        graced = client.get("/api/v1/ready")

    strict_app = _build_app(_marketguard_settings(cache_ttl_seconds=60, readiness_fresh_grace_seconds=0))
    with TestClient(strict_app) as strict_client:
        strict = strict_client.get("/api/v1/ready")

    assert graced.status_code == 200
    assert graced.json()["status"] == "ok"
    assert graced.headers["x-readiness-status"] == "ok"
    # Without the grace window the same snapshot reads as degraded, which is what
    # made uptime probes requiring HTTP 200 flap between ready and degraded.
    assert strict.status_code == 206
    assert strict.json()["status"] == "degraded"


def _single_cycle_sleep():
    """Sleep stub that lets the refresher run exactly one refresh.

    The initial jitter returns immediately; every later delay parks until the
    task is cancelled by stop(). Without the park the refresher would spin on a
    zero-length sleep and race the assertions with extra upstream calls.
    """

    calls = {"count": 0}

    async def _sleep(_delay: float) -> None:
        calls["count"] += 1
        if calls["count"] <= 1:
            return
        await asyncio.Event().wait()

    return _sleep


class _NoopRefreshableService:
    def __init__(self) -> None:
        self.refresh_count = 0
        self.inline_refresh_enabled = True

    async def refresh(self) -> None:
        self.refresh_count += 1

    def disable_inline_refresh(self) -> None:
        self.inline_refresh_enabled = False

    def enable_inline_refresh(self) -> None:
        self.inline_refresh_enabled = True


def test_marketguard_app_starts_and_stops_the_background_refreshers() -> None:
    """The app lifespan must actually own the refreshers, not just build them."""
    settings = _marketguard_settings(background_refresh_enabled=True, cache_ttl_seconds=60)
    store = _MemoryMarketGuardStorage(retention_days=settings.history_retention_days)

    async def _lowestbin_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "totalPages": 1,
                "lastUpdated": _epoch_millis(2025, 3, 1, 12, 0),
                "auctions": [_auction("HYPERION", 100_000_000, item_name="Hyperion")],
            },
        )

    async def _bazaar_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "lastUpdated": _epoch_millis(2025, 3, 1, 12, 0),
                "products": {},
            },
        )

    app = create_marketguard_app(
        settings=settings,
        service=_marketguard_service(settings, _lowestbin_handler, storage=store),
        bazaar_service=_marketguard_bazaar_service(settings, _bazaar_handler, storage=store),
        player_service=_marketguard_player_service(
            settings,
            _unexpected_hypixel_handler,
            _unexpected_mojang_handler,
        ),
        response_cache=None,
    )

    supervisor = app.state.marketguard_refresh_supervisor
    assert supervisor is not None
    assert [refresher.name for refresher in supervisor.refreshers] == ["lowestbin", "bazaar"]

    with TestClient(app):
        assert all(refresher.is_running for refresher in supervisor.refreshers)

    # Shutdown must stop every refresher; a leaked task would keep hitting
    # Hypixel after the app is gone.
    assert not any(refresher.is_running for refresher in supervisor.refreshers)


async def _unexpected_hypixel_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Unexpected Hypixel request: {request.url}")
