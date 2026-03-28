from __future__ import annotations

import ipaddress

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .config import MarketGuardSettings
from .exceptions import HypixelRateLimitError, HypixelUpstreamError
from .service import LowestBinService


def register_marketguard_routes(
    app: FastAPI,
    settings: MarketGuardSettings | None = None,
    service: LowestBinService | None = None,
) -> None:
    if bool(getattr(app.state, "marketguard_routes_registered", False)):
        return

    marketguard_settings = settings or MarketGuardSettings.from_env()
    marketguard_service = service or LowestBinService(marketguard_settings)

    app.state.marketguard_settings = marketguard_settings
    app.state.marketguard_service = marketguard_service
    app.state.marketguard_routes_registered = True
    app.add_event_handler("shutdown", marketguard_service.aclose)

    @app.get("/api/v1/lowestbin")
    async def lowestbin(request: Request, response: Response) -> JSONResponse:
        await _apply_rate_limit(request, marketguard_settings)
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
        return JSONResponse(
            snapshot.items,
            headers={
                "Cache-Control": response.headers["Cache-Control"],
                "X-Data-Stale": response.headers["X-Data-Stale"],
            },
        )


async def _apply_rate_limit(request: Request, settings: MarketGuardSettings) -> None:
    max_requests = int(settings.lowestbin_rate_limit_per_minute)
    limiter = getattr(request.app.state, "rate_limiter", None)
    if max_requests <= 0 or limiter is None:
        return

    client_ip = _resolve_client_ip(request, settings.trusted_proxies)
    allowed, retry_after = await run_in_threadpool(
        limiter.allow,
        f"marketguard.lowestbin:ip:{client_ip}",
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
