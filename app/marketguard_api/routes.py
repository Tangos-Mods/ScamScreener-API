from __future__ import annotations

import asyncio
import ipaddress
import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .cache import CachedResponse
from .config import MarketGuardSettings
from .exceptions import HypixelRateLimitError, HypixelUpstreamError, MarketGuardStorageError
from .models import (
    ApiErrorResponse,
    BazaarResponse,
    LowestBinQueryRequest,
    LowestBinV2Response,
    ReadinessResponse,
)
from .service import BazaarService, LowestBinService

_LOWESTBIN_V1_GONE_DETAIL = "Lowest BIN v1 has been removed. Use /api/v2/lowestbin instead."
_RATE_LIMIT_RETRY_AFTER_EXAMPLE = "60"
_CACHE_KEY_LOWESTBIN_V2 = "lowestbin:v2"
_CACHE_KEY_BAZAAR_V1 = "bazaar:v1"
_READINESS_OK_DETAIL = "All MarketGuard datasets are fresh and available."
_READINESS_DEGRADED_DETAIL = "At least one MarketGuard dataset is stale."
_READINESS_UNAVAILABLE_DETAIL = "At least one MarketGuard dataset is unavailable."
_QUERY_CONTENT_TYPE = "application/json"
# Accept-Query is an HTTP Structured Fields List; media types are strings here.
_ACCEPT_QUERY_HEADER_VALUE = '"application/json"'


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
) -> None:
    if bool(getattr(app.state, "marketguard_routes_registered", False)):
        return

    marketguard_settings = settings or MarketGuardSettings.from_env()
    shared_storage = getattr(service, "_storage", None) or getattr(bazaar_service, "_storage", None)
    marketguard_service = service or LowestBinService(marketguard_settings, storage=shared_storage)
    marketguard_bazaar_service = bazaar_service or BazaarService(marketguard_settings, storage=shared_storage)

    app.state.marketguard_settings = marketguard_settings
    app.state.marketguard_service = marketguard_service
    app.state.marketguard_bazaar_service = marketguard_bazaar_service
    app.state.marketguard_routes_registered = True
    app.add_event_handler("shutdown", marketguard_service.aclose)
    app.add_event_handler("shutdown", marketguard_bazaar_service.aclose)

    @app.get(
        "/api/v1/lowestbin",
        responses={
            410: _error_response_docs(_LOWESTBIN_V1_GONE_DETAIL),
        },
    )
    async def lowestbin_v1_gone() -> JSONResponse:
        return JSONResponse(
            {"detail": _LOWESTBIN_V1_GONE_DETAIL},
            status_code=410,
        )

    @app.get(
        "/api/v2/lowestbin",
        response_model=LowestBinV2Response,
        responses={
            429: _error_response_docs("Too many requests.", retry_after=True),
            503: _error_response_docs("Lowest BIN data is temporarily unavailable.", retry_after=True),
        },
    )
    async def lowestbin_v2(request: Request, response: Response) -> JSONResponse:
        cached_response = await _lowestbin_cached_response(request, marketguard_settings, marketguard_service)
        return _json_cache_response(
            marketguard_settings,
            _marketguard_payload_with_status(cached_response.payload, is_stale=cached_response.is_stale),
            is_stale=cached_response.is_stale,
            accept_query=True,
        )

    @app.api_route(
        "/api/v2/lowestbin",
        methods=["QUERY"],
        response_model=LowestBinV2Response,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["products"],
                            "properties": {
                                "products": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 100,
                                    "items": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 64,
                                    },
                                }
                            },
                        }
                    }
                },
            }
        },
        responses={
            400: _error_response_docs("QUERY requests require a valid application/json body."),
            406: _error_response_docs("Only application/json responses are available."),
            415: _error_response_docs("QUERY requests must use Content-Type: application/json."),
            422: _error_response_docs("The query could not be processed."),
            429: _error_response_docs("Too many requests.", retry_after=True),
            503: _error_response_docs("Lowest BIN data is temporarily unavailable.", retry_after=True),
        },
    )
    async def query_lowestbin_v2(request: Request) -> JSONResponse:
        """Return a safe, idempotent subset of Lowest BIN v2 products (RFC 10008)."""
        _require_json_query_response(request)
        query = await _parse_lowestbin_query(request)
        cached_response = await _lowestbin_cached_response(request, marketguard_settings, marketguard_service)
        payload = _marketguard_payload_with_status(cached_response.payload, is_stale=cached_response.is_stale)
        products = payload.get("products")
        if not isinstance(products, dict):
            raise HTTPException(status_code=503, detail="Lowest BIN data is temporarily unavailable.")

        requested_product_ids = query.products
        if len(set(requested_product_ids)) != len(requested_product_ids):
            raise HTTPException(status_code=422, detail="Product identifiers must not be repeated.")
        unknown_product_ids = [product_id for product_id in requested_product_ids if product_id not in products]
        if unknown_product_ids:
            raise HTTPException(
                status_code=422,
                detail="One or more requested product identifiers are unavailable.",
            )

        payload["products"] = {product_id: products[product_id] for product_id in requested_product_ids}
        return JSONResponse(
            payload,
            headers={
                "Accept-Query": _ACCEPT_QUERY_HEADER_VALUE,
                # RFC 10008 requires QUERY cache keys to include request content. Do not
                # rely on intermediaries that have not yet implemented that requirement.
                "Cache-Control": "no-store",
                "X-Data-Stale": str(cached_response.is_stale).lower(),
                "X-API-Provider": "Pankraz01",
            },
        )

    @app.get(
        "/api/v1/bazaar",
        response_model=BazaarResponse,
        responses={
            429: _error_response_docs("Too many requests.", retry_after=True),
            503: _error_response_docs("Bazaar data is temporarily unavailable.", retry_after=True),
        },
    )
    async def bazaar(request: Request, response: Response) -> JSONResponse:
        cached_response = await _read_cached_response(request, _CACHE_KEY_BAZAAR_V1)
        if cached_response is not None:
            return _json_cache_response(
                marketguard_settings,
                _marketguard_payload_with_status(cached_response.payload, is_stale=cached_response.is_stale),
                is_stale=cached_response.is_stale,
            )
        try:
            cached_response = await _read_or_refresh_response(
                request,
                cache_key=_CACHE_KEY_BAZAAR_V1,
                route_key="bazaar",
                max_requests=int(marketguard_settings.lowestbin_rate_limit_per_minute),
                trusted_proxies=marketguard_settings.trusted_proxies,
                factory=lambda: _build_bazaar_response(marketguard_bazaar_service),
            )
        except HypixelRateLimitError as exc:
            headers = {"Retry-After": str(exc.retry_after_seconds)} if exc.retry_after_seconds else None
            raise HTTPException(
                status_code=503,
                detail="Bazaar data is temporarily unavailable.",
                headers=headers,
            ) from exc
        except (HypixelUpstreamError, MarketGuardStorageError, RuntimeError) as exc:
            raise HTTPException(
                status_code=503,
                detail="Bazaar data is temporarily unavailable.",
            ) from exc
        return _json_cache_response(
            marketguard_settings,
            _marketguard_payload_with_status(cached_response.payload, is_stale=cached_response.is_stale),
            is_stale=cached_response.is_stale,
        )

    @app.api_route(
        "/api/v1/ready",
        methods=["GET", "HEAD"],
        response_model=ReadinessResponse,
        responses={
            200: _readiness_response_docs(_READINESS_OK_DETAIL),
            206: _readiness_response_docs(_READINESS_DEGRADED_DETAIL),
            503: _readiness_response_docs(_READINESS_UNAVAILABLE_DETAIL),
        },
    )
    async def readiness(request: Request) -> Response:
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

        payload = {
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

        headers = _readiness_headers(status_code)
        if request.method.upper() == "HEAD":
            return Response(status_code=status_code, headers=headers)
        return JSONResponse(payload, status_code=status_code, headers=headers)


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


def _json_cache_response(
    settings: MarketGuardSettings,
    payload: dict[str, object],
    *,
    is_stale: bool,
    accept_query: bool = False,
) -> JSONResponse:
    headers = _cache_headers(settings, is_stale=is_stale)
    if accept_query:
        headers["Accept-Query"] = _ACCEPT_QUERY_HEADER_VALUE
    return JSONResponse(payload, headers=headers)


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


async def _lowestbin_cached_response(
    request: Request,
    settings: MarketGuardSettings,
    service: LowestBinService,
) -> CachedResponse:
    cached_response = await _read_cached_response(request, _CACHE_KEY_LOWESTBIN_V2)
    if cached_response is not None:
        return cached_response
    try:
        return await _read_or_refresh_response(
            request,
            cache_key=_CACHE_KEY_LOWESTBIN_V2,
            route_key="lowestbin",
            max_requests=int(settings.lowestbin_rate_limit_per_minute),
            trusted_proxies=settings.trusted_proxies,
            factory=lambda: _build_lowestbin_response(service),
        )
    except HypixelRateLimitError as exc:
        headers = {"Retry-After": str(exc.retry_after_seconds)} if exc.retry_after_seconds else None
        raise HTTPException(
            status_code=503,
            detail="Lowest BIN data is temporarily unavailable.",
            headers=headers,
        ) from exc
    except (HypixelUpstreamError, MarketGuardStorageError, RuntimeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Lowest BIN data is temporarily unavailable.",
        ) from exc


def _query_error_headers() -> dict[str, str]:
    return {"Accept-Query": _ACCEPT_QUERY_HEADER_VALUE}


def _require_json_query_response(request: Request) -> None:
    accept = request.headers.get("accept", "*/*")
    accepted_media_types = {
        media_range.strip().split(";", 1)[0].strip().lower()
        for media_range in accept.split(",")
    }
    if accepted_media_types.isdisjoint({"*/*", "application/*", _QUERY_CONTENT_TYPE}):
        raise HTTPException(
            status_code=406,
            detail="Only application/json responses are available.",
            headers=_query_error_headers(),
        )


async def _parse_lowestbin_query(request: Request) -> LowestBinQueryRequest:
    content_type = request.headers.get("content-type")
    if not content_type:
        raise HTTPException(
            status_code=400,
            detail="QUERY requests require Content-Type: application/json.",
            headers=_query_error_headers(),
        )
    if content_type.split(";", 1)[0].strip().lower() != _QUERY_CONTENT_TYPE:
        raise HTTPException(
            status_code=415,
            detail="QUERY requests must use Content-Type: application/json.",
            headers=_query_error_headers(),
        )

    try:
        content = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(
            status_code=400,
            detail="QUERY requests require a valid application/json body.",
            headers=_query_error_headers(),
        ) from None
    try:
        return LowestBinQueryRequest.model_validate(content)
    except ValidationError:
        raise HTTPException(
            status_code=422,
            detail="The query could not be processed.",
            headers=_query_error_headers(),
        ) from None


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


async def _read_or_refresh_response(
    request: Request,
    *,
    cache_key: str,
    route_key: str,
    max_requests: int,
    trusted_proxies: set[str],
    factory,
) -> CachedResponse:
    cache = getattr(request.app.state, "marketguard_response_cache", None)
    cached_response = await _read_cached_response(request, cache_key)
    if cached_response is not None:
        return cached_response

    async def _refreshing_factory() -> CachedResponse:
        await _apply_rate_limit(
            request,
            route_key=route_key,
            max_requests=max_requests,
            trusted_proxies=trusted_proxies,
        )
        return await factory()

    if cache is None or not hasattr(cache, "get_or_fill"):
        return await _refreshing_factory()

    return await cache.get_or_fill(cache_key, _refreshing_factory)


async def _build_lowestbin_response(service: LowestBinService) -> CachedResponse:
    snapshot = await service.get_lowest_bins_v2()
    payload = {
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
    return CachedResponse(payload=payload, is_stale=snapshot.is_stale)


async def _build_bazaar_response(service: BazaarService) -> CachedResponse:
    snapshot = await service.get_bazaar()
    payload = {
        "status": _marketguard_top_level_status(snapshot.is_stale),
        "lastUpdated": snapshot.snapshot_last_updated,
        "products": snapshot.products,
    }
    return CachedResponse(payload=payload, is_stale=snapshot.is_stale)
