from __future__ import annotations

import ipaddress

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .config import MarketGuardSettings
from .exceptions import HypixelRateLimitError, HypixelUpstreamError
from .models import ApiErrorResponse, BazaarResponse, LowestBinV1Response, LowestBinV2Response
from .service import BazaarService, LowestBinService

_LOWESTBIN_V1_DEPRECATION_HEADER = "true"
_LOWESTBIN_V1_SUNSET_HEADER = "Mon, 01 Jun 2026 00:00:00 GMT"
_RATE_LIMIT_RETRY_AFTER_EXAMPLE = "60"


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
    marketguard_service = service or LowestBinService(marketguard_settings)
    marketguard_bazaar_service = bazaar_service or BazaarService(marketguard_settings)

    app.state.marketguard_settings = marketguard_settings
    app.state.marketguard_service = marketguard_service
    app.state.marketguard_bazaar_service = marketguard_bazaar_service
    app.state.marketguard_routes_registered = True
    app.add_event_handler("shutdown", marketguard_service.aclose)
    app.add_event_handler("shutdown", marketguard_bazaar_service.aclose)

    @app.get(
        "/api/v1/lowestbin",
        deprecated=True,
        response_model=LowestBinV1Response,
        responses={
            429: _error_response_docs("Too many requests.", retry_after=True),
            503: _error_response_docs("Lowest BIN data is temporarily unavailable.", retry_after=True),
        },
    )
    async def lowestbin(request: Request, response: Response) -> JSONResponse:
        await _apply_rate_limit(
            request,
            route_key="lowestbin",
            max_requests=int(marketguard_settings.lowestbin_rate_limit_per_minute),
            trusted_proxies=marketguard_settings.trusted_proxies,
        )
        try:
            snapshot = await marketguard_service.get_lowest_bins()
        except HypixelRateLimitError as exc:
            headers = {"Retry-After": str(exc.retry_after_seconds)} if exc.retry_after_seconds else None
            raise HTTPException(
                status_code=503,
                detail="Lowest BIN data is temporarily unavailable.",
                headers=headers,
            ) from exc
        except HypixelUpstreamError as exc:
            raise HTTPException(
                status_code=503,
                detail="Lowest BIN data is temporarily unavailable.",
            ) from exc

        response.headers["Cache-Control"] = (
            f"public, max-age={marketguard_settings.cache_ttl_seconds}, "
            f"stale-if-error={marketguard_settings.stale_if_error_seconds}"
        )
        response.headers["X-Data-Stale"] = "true" if snapshot.is_stale else "false"
        response.headers["X-API-Provider"] = "Pankraz01"
        return JSONResponse(
            snapshot.items,
            headers={
                "Cache-Control": response.headers["Cache-Control"],
                "X-Data-Stale": response.headers["X-Data-Stale"],
                "X-API-Provider": response.headers["X-API-Provider"],
                "Deprecation": _LOWESTBIN_V1_DEPRECATION_HEADER,
                "Sunset": _LOWESTBIN_V1_SUNSET_HEADER,
            },
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
        await _apply_rate_limit(
            request,
            route_key="lowestbin",
            max_requests=int(marketguard_settings.lowestbin_rate_limit_per_minute),
            trusted_proxies=marketguard_settings.trusted_proxies,
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
        except HypixelUpstreamError as exc:
            raise HTTPException(
                status_code=503,
                detail="Lowest BIN data is temporarily unavailable.",
            ) from exc

        response.headers["Cache-Control"] = (
            f"public, max-age={marketguard_settings.cache_ttl_seconds}, "
            f"stale-if-error={marketguard_settings.stale_if_error_seconds}"
        )
        response.headers["X-Data-Stale"] = "true" if snapshot.is_stale else "false"
        response.headers["X-API-Provider"] = "Pankraz01"
        return JSONResponse(
            {
                "lastUpdated": snapshot.snapshot_last_updated,
                "products": {
                    item_key: {
                        "price": entry.price,
                        "auctioneerUuid": entry.auctioneer_uuid,
                        "item_name": entry.item_name,
                    }
                    for item_key, entry in snapshot.items.items()
                },
            },
            headers={
                "Cache-Control": response.headers["Cache-Control"],
                "X-Data-Stale": response.headers["X-Data-Stale"],
                "X-API-Provider": response.headers["X-API-Provider"],
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
        await _apply_rate_limit(
            request,
            route_key="bazaar",
            max_requests=int(marketguard_settings.lowestbin_rate_limit_per_minute),
            trusted_proxies=marketguard_settings.trusted_proxies,
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
        except HypixelUpstreamError as exc:
            raise HTTPException(
                status_code=503,
                detail="Bazaar data is temporarily unavailable.",
            ) from exc

        response.headers["Cache-Control"] = (
            f"public, max-age={marketguard_settings.cache_ttl_seconds}, "
            f"stale-if-error={marketguard_settings.stale_if_error_seconds}"
        )
        response.headers["X-Data-Stale"] = "true" if snapshot.is_stale else "false"
        response.headers["X-API-Provider"] = "Pankraz01"
        return JSONResponse(
            {
                "lastUpdated": snapshot.snapshot_last_updated,
                "products": snapshot.products,
            },
            headers={
                "Cache-Control": response.headers["Cache-Control"],
                "X-Data-Stale": response.headers["X-Data-Stale"],
                "X-API-Provider": response.headers["X-API-Provider"],
            },
        )


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
