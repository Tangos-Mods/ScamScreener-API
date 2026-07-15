from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import time
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .cache import CachedResponse
from .config import MarketGuardSettings
from .exceptions import HypixelRateLimitError, HypixelUpstreamError, MarketGuardStorageError, MojangUpstreamError
from .models import (
    ApiErrorResponse,
    BazaarResponse,
    LowestBinV2Response,
    LowestBinQueryRequest,
    PlayersQueryRequest,
    PlayersQueryResponse,
    ReadinessResponse,
)
from .player_service import PlayerService
from .player_metrics import PlayerQueryMetrics
from .service import BazaarService, LowestBinService

_RATE_LIMIT_RETRY_AFTER_EXAMPLE = "60"
_CACHE_KEY_LOWESTBIN_V2 = "lowestbin:v2"
_CACHE_KEY_BAZAAR_V1 = "bazaar:v1"
_CACHE_KEY_PLAYERS_V1_PREFIX = "players:v1:"
_READINESS_OK_DETAIL = "All MarketGuard datasets are fresh and available."
_READINESS_DEGRADED_DETAIL = "At least one MarketGuard dataset is stale."
_READINESS_UNAVAILABLE_DETAIL = "At least one MarketGuard dataset is unavailable."


def _round_lowestbin_average(value: float | None) -> int | None:
    if value is None:
        return None
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _error_response_docs(detail: str, *, retry_after: bool = False) -> dict[str, object]:
    response_docs: dict[str, object] = {
        "model": ApiErrorResponse,
        "content": {
            "application/json": {
                "example": {
                    "detail": detail,
                }
            }
        },
    }
    if retry_after:
        response_docs["headers"] = {
            "Retry-After": {
                "description": "Seconds until the caller should retry.",
                "schema": {"type": "string", "example": _RATE_LIMIT_RETRY_AFTER_EXAMPLE},
            }
        }
    return response_docs


def register_marketguard_routes(
    app: FastAPI,
    settings: MarketGuardSettings | None = None,
    service: LowestBinService | None = None,
    bazaar_service: BazaarService | None = None,
    player_service: PlayerService | None = None,
) -> None:
    if bool(getattr(app.state, "marketguard_routes_registered", False)):
        return

    marketguard_settings = settings or MarketGuardSettings.from_env()
    shared_storage = getattr(service, "_storage", None) or getattr(bazaar_service, "_storage", None)
    marketguard_service = service or LowestBinService(marketguard_settings, storage=shared_storage)
    marketguard_bazaar_service = bazaar_service or BazaarService(marketguard_settings, storage=shared_storage)
    marketguard_player_service = player_service or PlayerService(marketguard_settings)
    player_query_metrics = PlayerQueryMetrics()
    player_query_inflight: dict[str, asyncio.Task[dict[str, object]]] = {}
    player_query_inflight_lock = asyncio.Lock()

    app.state.marketguard_settings = marketguard_settings
    app.state.marketguard_service = marketguard_service
    app.state.marketguard_bazaar_service = marketguard_bazaar_service
    app.state.marketguard_player_service = marketguard_player_service
    app.state.marketguard_player_query_metrics = player_query_metrics
    app.state.marketguard_routes_registered = True
    app.add_event_handler("shutdown", marketguard_service.aclose)
    app.add_event_handler("shutdown", marketguard_bazaar_service.aclose)
    app.add_event_handler("shutdown", marketguard_player_service.aclose)

    async def _load_players_query_payload(
        cache_key: str,
        query: PlayersQueryRequest,
    ) -> dict[str, object]:
        async with player_query_inflight_lock:
            task = player_query_inflight.get(cache_key)
            if task is None:
                player_query_metrics.record_cache_miss()
                player_query_metrics.start_upstream_load()
                task = asyncio.create_task(marketguard_player_service.get_players(query.players))
                player_query_inflight[cache_key] = task

                def _schedule_flight_cleanup(completed_task: asyncio.Task[dict[str, object]]) -> None:
                    if not completed_task.cancelled():
                        completed_task.exception()

                    async def _clear_completed_flight() -> None:
                        async with player_query_inflight_lock:
                            if player_query_inflight.get(cache_key) is completed_task:
                                player_query_inflight.pop(cache_key, None)
                                player_query_metrics.finish_upstream_load()

                    asyncio.create_task(_clear_completed_flight())

                task.add_done_callback(_schedule_flight_cleanup)
            else:
                player_query_metrics.record_coalesced_waiter()

        return await asyncio.shield(task)

    async def _load_lowestbin_payload(request: Request) -> tuple[dict[str, object], bool]:
        cached_response = await _read_cached_response(request, _CACHE_KEY_LOWESTBIN_V2)
        if cached_response is not None:
            return (
                _marketguard_payload_with_status(cached_response.payload, is_stale=cached_response.is_stale),
                cached_response.is_stale,
            )
        try:
            snapshot = await marketguard_service.get_lowest_bins_v2()
        except HypixelRateLimitError as exc:
            headers = {"Retry-After": str(exc.retry_after_seconds)} if exc.retry_after_seconds else None
            raise HTTPException(
                status_code=503,
                detail="Lowest BIN data is temporarily unavailable.",
                headers=headers,
            ) from exc
        except (HypixelUpstreamError, MarketGuardStorageError) as exc:
            raise HTTPException(
                status_code=503,
                detail="Lowest BIN data is temporarily unavailable.",
            ) from exc

        payload: dict[str, object] = {
            "status": _marketguard_top_level_status(snapshot.is_stale),
            "lastUpdated": snapshot.snapshot_last_updated,
            "products": {
                item_key: {
                    "price": entry.price,
                    "auctioneerUuid": entry.auctioneer_uuid,
                    "item_name": entry.item_name,
                    "avg7d": _round_lowestbin_average(entry.avg_7d),
                    "avg30d": _round_lowestbin_average(entry.avg_30d),
                }
                for item_key, entry in snapshot.items.items()
            },
        }
        await _write_cached_response(request, _CACHE_KEY_LOWESTBIN_V2, payload, is_stale=snapshot.is_stale)
        return payload, snapshot.is_stale

    @app.get(
        "/api/v2/lowestbin",
        response_model=LowestBinV2Response,
        responses={
            429: _error_response_docs("Too many requests.", retry_after=True),
            503: _error_response_docs("Lowest BIN data is temporarily unavailable.", retry_after=True),
        },
    )
    async def lowestbin_v2(request: Request, response: Response) -> JSONResponse:
        await _apply_rate_limit(
            request,
            route_key="lowestbin",
            max_requests=int(marketguard_settings.lowestbin_rate_limit_per_minute),
            trusted_proxies=marketguard_settings.trusted_proxies,
        )
        payload, is_stale = await _load_lowestbin_payload(request)
        return _json_cache_response(marketguard_settings, payload, is_stale=is_stale)

    @app.api_route(
        "/api/v2/lowestbin",
        methods=["QUERY"],
        response_model=LowestBinV2Response,
        responses={
            429: _error_response_docs("Too many requests.", retry_after=True),
            503: _error_response_docs("Lowest BIN data is temporarily unavailable.", retry_after=True),
        },
    )
    async def lowestbin_v2_query(request: Request, query: LowestBinQueryRequest) -> JSONResponse:
        await _apply_rate_limit(
            request,
            route_key="lowestbin",
            max_requests=int(marketguard_settings.lowestbin_rate_limit_per_minute),
            trusted_proxies=marketguard_settings.trusted_proxies,
        )
        payload, is_stale = await _load_lowestbin_payload(request)
        products = payload.get("products")
        if not isinstance(products, dict):
            raise HTTPException(status_code=503, detail="Lowest BIN data is temporarily unavailable.")
        requested = dict.fromkeys(query.products)
        filtered_payload = dict(payload)
        filtered_payload["products"] = {key: products[key] for key in requested if key in products}
        return _json_cache_response(marketguard_settings, filtered_payload, is_stale=is_stale)

    @app.get(
        "/api/v1/bazaar",
        response_model=BazaarResponse,
        responses={
            429: _error_response_docs("Too many requests.", retry_after=True),
            503: _error_response_docs("Bazaar data is temporarily unavailable.", retry_after=True),
        },
    )
    async def bazaar(request: Request, response: Response) -> JSONResponse:
        await _apply_rate_limit(
            request,
            route_key="bazaar",
            max_requests=int(marketguard_settings.lowestbin_rate_limit_per_minute),
            trusted_proxies=marketguard_settings.trusted_proxies,
        )
        cached_response = await _read_cached_response(request, _CACHE_KEY_BAZAAR_V1)
        if cached_response is not None:
            return _json_cache_response(
                marketguard_settings,
                _marketguard_payload_with_status(cached_response.payload, is_stale=cached_response.is_stale),
                is_stale=cached_response.is_stale,
            )
        try:
            snapshot = await marketguard_bazaar_service.get_bazaar()
        except HypixelRateLimitError as exc:
            headers = {"Retry-After": str(exc.retry_after_seconds)} if exc.retry_after_seconds else None
            raise HTTPException(
                status_code=503,
                detail="Bazaar data is temporarily unavailable.",
                headers=headers,
            ) from exc
        except (HypixelUpstreamError, MarketGuardStorageError) as exc:
            raise HTTPException(
                status_code=503,
                detail="Bazaar data is temporarily unavailable.",
            ) from exc

        payload = {
            "status": _marketguard_top_level_status(snapshot.is_stale),
            "lastUpdated": snapshot.snapshot_last_updated,
            "products": snapshot.products,
        }
        await _write_cached_response(request, _CACHE_KEY_BAZAAR_V1, payload, is_stale=snapshot.is_stale)
        return _json_cache_response(marketguard_settings, payload, is_stale=snapshot.is_stale)

    @app.api_route(
        "/api/v1/players",
        methods=["QUERY"],
        response_model=PlayersQueryResponse,
        responses={
            429: _error_response_docs("Too many requests.", retry_after=True),
            503: _error_response_docs("Player data is temporarily unavailable.", retry_after=True),
        },
    )
    async def players_query(request: Request, query: PlayersQueryRequest) -> JSONResponse:
        await _apply_rate_limit(
            request,
            route_key="players",
            max_requests=int(marketguard_settings.players_rate_limit_per_minute),
            trusted_proxies=marketguard_settings.trusted_proxies,
        )
        started_at = time.perf_counter()
        try:
            cache_key = _players_query_cache_key(query)
            cached_response = await _read_cached_response(request, cache_key)
            if cached_response is not None:
                player_query_metrics.record_cache_hit()
                return _json_cache_response(
                    marketguard_settings,
                    _marketguard_payload_with_status(cached_response.payload, is_stale=cached_response.is_stale),
                    is_stale=cached_response.is_stale,
                )
            try:
                payload = await _load_players_query_payload(cache_key, query)
            except HypixelRateLimitError as exc:
                player_query_metrics.record_upstream_failure()
                headers = {"Retry-After": str(exc.retry_after_seconds)} if exc.retry_after_seconds else None
                raise HTTPException(
                    status_code=503,
                    detail="Player data is temporarily unavailable.",
                    headers=headers,
                ) from exc
            except (HypixelUpstreamError, MojangUpstreamError) as exc:
                player_query_metrics.record_upstream_failure()
                raise HTTPException(status_code=503, detail="Player data is temporarily unavailable.") from exc

            player_results = payload.get("players")
            if (
                marketguard_settings.hypixel_api_key.strip()
                and isinstance(player_results, list)
                and any(isinstance(result, dict) and result.get("status") == "unavailable" for result in player_results)
            ):
                player_query_metrics.record_upstream_failure()
            await _write_cached_response(request, cache_key, payload, is_stale=False)
            return _json_cache_response(marketguard_settings, payload, is_stale=False)
        finally:
            player_query_metrics.record_response((time.perf_counter() - started_at) * 1_000)

    async def _readiness_payload() -> tuple[dict[str, object], int, dict[str, str]]:
        now_epoch_seconds = datetime.now(timezone.utc).timestamp()
        lowestbin_component, bazaar_component = await asyncio.gather(
            _readiness_lowestbin_component(
                marketguard_service,
                now_epoch_seconds=now_epoch_seconds,
                settings=marketguard_settings,
            ),
            _readiness_bazaar_component(
                marketguard_bazaar_service,
                now_epoch_seconds=now_epoch_seconds,
                settings=marketguard_settings,
            ),
        )

        payload: dict[str, object] = {
            "status": "ok",
            "checkedAt": _now_utc_iso(),
            "lowestbinV2": lowestbin_component,
            "bazaar": bazaar_component,
        }
        status_code = _readiness_status_code(payload)

        if status_code == 206:
            payload["status"] = "degraded"
        elif status_code == 503:
            payload["status"] = "unavailable"
        return payload, status_code, _readiness_headers(status_code)

    @app.get(
        "/api/v1/ready",
        response_model=ReadinessResponse,
        responses={
            200: _readiness_response_docs(_READINESS_OK_DETAIL),
            206: _readiness_response_docs(_READINESS_DEGRADED_DETAIL),
            503: _readiness_response_docs(_READINESS_UNAVAILABLE_DETAIL),
        },
    )
    async def readiness() -> JSONResponse:
        payload, status_code, headers = await _readiness_payload()
        return JSONResponse(payload, status_code=status_code, headers=headers)

    @app.head("/api/v1/ready", include_in_schema=False)
    async def readiness_head() -> Response:
        _payload, status_code, headers = await _readiness_payload()
        return Response(status_code=status_code, headers=headers)


async def _apply_rate_limit(
    request: Request,
    *,
    route_key: str,
    max_requests: int,
    trusted_proxies: set[str],
) -> None:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if max_requests <= 0 or limiter is None:
        return

    client_ip = _resolve_client_ip(request, trusted_proxies)
    allowed, retry_after = await run_in_threadpool(
        limiter.allow,
        f"marketguard.{route_key}:ip:{client_ip}",
        max_requests,
        60,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests.",
            headers={"Retry-After": str(retry_after)},
        )


def _resolve_client_ip(request: Request, trusted_proxies: set[str]) -> str:
    client_host = ""
    if request.client is not None and request.client.host:
        client_host = request.client.host.strip().lower()

    trusted_proxy = False
    if client_host:
        if "*" in trusted_proxies or client_host in trusted_proxies:
            trusted_proxy = True
        else:
            try:
                client_ip = ipaddress.ip_address(client_host)
            except ValueError:
                client_ip = None
            if client_ip is not None:
                for candidate in trusted_proxies:
                    normalized = str(candidate or "").strip().lower()
                    if "/" not in normalized:
                        continue
                    try:
                        if client_ip in ipaddress.ip_network(normalized, strict=False):
                            trusted_proxy = True
                            break
                    except ValueError:
                        continue

    if trusted_proxy:
        forwarded_for = str(request.headers.get("x-forwarded-for", "")).strip()
        if forwarded_for:
            first_hop = forwarded_for.split(",")[0].strip()
            if first_hop:
                return first_hop

    return client_host or "unknown"


def _cache_headers(settings: MarketGuardSettings, *, is_stale: bool) -> dict[str, str]:
    return {
        "Cache-Control": (
            f"public, max-age={settings.cache_ttl_seconds}, "
            f"stale-if-error={settings.stale_if_error_seconds}"
        ),
        "X-Data-Stale": "true" if is_stale else "false",
        "X-API-Provider": "Pankraz01",
    }


def _json_cache_response(settings: MarketGuardSettings, payload: dict[str, object], *, is_stale: bool) -> JSONResponse:
    return JSONResponse(payload, headers=_cache_headers(settings, is_stale=is_stale))


def _players_query_cache_key(query: PlayersQueryRequest) -> str:
    payload = json.dumps(
        {
            "players": [
                {
                    "player": _normalize_players_cache_identifier(player_query.player),
                    "profileId": player_query.profileId.lower().replace("-", ""),
                }
                for player_query in query.players
            ]
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_CACHE_KEY_PLAYERS_V1_PREFIX}{digest}"


def _normalize_players_cache_identifier(value: str) -> str:
    normalized = str(value or "").strip().lower()
    compact_uuid = normalized.replace("-", "")
    if len(compact_uuid) == 32 and all(character in "0123456789abcdef" for character in compact_uuid):
        return compact_uuid
    return normalized


def _marketguard_top_level_status(is_stale: bool) -> str:
    return "stale" if is_stale else "ok"


def _marketguard_payload_with_status(payload: dict[str, object], *, is_stale: bool) -> dict[str, object]:
    normalized_payload = dict(payload)
    normalized_payload["status"] = _marketguard_top_level_status(is_stale)
    return normalized_payload


def _readiness_response_docs(detail: str) -> dict[str, object]:
    return {
        "description": detail,
        "model": ReadinessResponse,
    }


async def _readiness_lowestbin_component(
    service: LowestBinService,
    *,
    now_epoch_seconds: float,
    settings: MarketGuardSettings,
) -> dict[str, object]:
    storage = getattr(service, "_storage", None)
    if storage is None or not hasattr(storage, "read_lowestbin_snapshot"):
        return {"status": "down", "lastUpdated": None}
    try:
        stored_snapshot = await run_in_threadpool(storage.read_lowestbin_snapshot)
    except Exception:
        return {"status": "down", "lastUpdated": None}
    if stored_snapshot is None:
        return {"status": "down", "lastUpdated": None}
    return _readiness_component_payload(
        getattr(stored_snapshot, "snapshot", None),
        now_epoch_seconds=now_epoch_seconds,
        settings=settings,
    )


async def _readiness_bazaar_component(
    service: BazaarService,
    *,
    now_epoch_seconds: float,
    settings: MarketGuardSettings,
) -> dict[str, object]:
    storage = getattr(service, "_storage", None)
    if storage is None or not hasattr(storage, "read_bazaar_snapshot"):
        return {"status": "down", "lastUpdated": None}
    try:
        snapshot = await run_in_threadpool(storage.read_bazaar_snapshot)
    except Exception:
        return {"status": "down", "lastUpdated": None}
    return _readiness_component_payload(
        snapshot,
        now_epoch_seconds=now_epoch_seconds,
        settings=settings,
    )


def _readiness_component_payload(
    snapshot: object,
    *,
    now_epoch_seconds: float,
    settings: MarketGuardSettings,
) -> dict[str, object]:
    if snapshot is None:
        return {"status": "down", "lastUpdated": None}
    snapshot_last_updated = getattr(snapshot, "snapshot_last_updated", None)
    generated_at = getattr(snapshot, "generated_at", None)
    try:
        normalized_last_updated = int(snapshot_last_updated)
    except (TypeError, ValueError):
        return {"status": "down", "lastUpdated": None}
    if not isinstance(generated_at, datetime):
        return {"status": "down", "lastUpdated": None}
    snapshot_age_seconds = now_epoch_seconds - generated_at.timestamp()
    if snapshot_age_seconds < int(settings.cache_ttl_seconds):
        component_status = "ok"
    elif snapshot_age_seconds < int(settings.stale_if_error_seconds):
        component_status = "stale"
    else:
        component_status = "down"
    return {
        "status": component_status,
        "lastUpdated": normalized_last_updated,
    }


def _readiness_status_code(payload: dict[str, object]) -> int:
    components = (
        payload["lowestbinV2"],
        payload["bazaar"],
    )
    statuses = {str(component.get("status", "")) for component in components if isinstance(component, dict)}
    if "down" in statuses:
        return 503
    if "stale" in statuses:
        return 206
    return 200


def _readiness_headers(status_code: int) -> dict[str, str]:
    return {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "X-Readiness-Status": (
            "ok"
            if status_code == 200
            else "degraded"
            if status_code == 206
            else "unavailable"
        ),
    }


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _read_cached_response(request: Request, cache_key: str) -> CachedResponse | None:
    cache = getattr(request.app.state, "marketguard_response_cache", None)
    if cache is None:
        return None
    return await cache.get(cache_key)


async def _write_cached_response(
    request: Request,
    cache_key: str,
    payload: dict[str, object],
    *,
    is_stale: bool,
) -> None:
    cache = getattr(request.app.state, "marketguard_response_cache", None)
    if cache is None:
        return
    await cache.set(cache_key, CachedResponse(payload=payload, is_stale=is_stale))
