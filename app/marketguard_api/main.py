from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .cache import ResponseCacheChain, build_response_cache
from .config import MarketGuardSettings
from .player_service import PlayerService
from .rate_limit import InMemoryRateLimiter
from .refresher import build_refresh_supervisor
from .routes import register_marketguard_routes
from .service import BazaarService, LowestBinService
from .storage import MarketGuardStorage
from app.training_hub.http.live_api_metrics import LiveApiRequestMetrics, live_api_metrics_snapshot
from app.training_hub.http.persistent_api_metrics import PersistentApiMetricsRecorder
from app.training_hub.core.common import _request_originates_from_internal_network

logger = logging.getLogger(__name__)


def create_marketguard_app(
    settings: MarketGuardSettings | None = None,
    service: LowestBinService | None = None,
    bazaar_service: BazaarService | None = None,
    player_service: PlayerService | None = None,
    response_cache: ResponseCacheChain | None = None,
) -> FastAPI:
    runtime_settings = settings or MarketGuardSettings.from_env()
    runtime_response_cache = response_cache or build_response_cache(
        local_cache_enabled=runtime_settings.local_cache_enabled,
        local_cache_ttl_seconds=runtime_settings.local_cache_ttl_seconds,
        local_cache_max_entries=runtime_settings.local_cache_max_entries,
        redis_enabled=runtime_settings.redis_enabled,
        redis_url=runtime_settings.redis_url,
        redis_key_prefix=runtime_settings.redis_key_prefix,
        redis_cache_ttl_seconds=runtime_settings.redis_cache_ttl_seconds,
    )
    shared_storage = getattr(service, "_storage", None) or getattr(bazaar_service, "_storage", None)
    storage: MarketGuardStorage | None = shared_storage
    if service is None or bazaar_service is None:
        storage = storage or MarketGuardStorage(runtime_settings.database_url, runtime_settings.history_retention_days)
    lowestbin_service = service or LowestBinService(runtime_settings, storage=storage)
    runtime_bazaar_service = bazaar_service or BazaarService(runtime_settings, storage=storage)
    runtime_player_service = player_service or PlayerService(runtime_settings)
    persistent_api_metrics = PersistentApiMetricsRecorder(runtime_settings.database_url)
    refresh_supervisor = build_refresh_supervisor(
        runtime_settings,
        lowestbin_service=lowestbin_service,
        bazaar_service=runtime_bazaar_service,
    )
    docs_url = "/docs" if runtime_settings.api_docs_enabled else None
    redoc_url = "/redoc" if runtime_settings.api_docs_enabled else None
    openapi_url = "/openapi.json" if runtime_settings.api_docs_enabled else None

    @asynccontextmanager
    async def app_lifespan(_: FastAPI):
        if refresh_supervisor is not None:
            await refresh_supervisor.start()
        try:
            yield
        finally:
            if refresh_supervisor is not None:
                try:
                    await refresh_supervisor.stop()
                except Exception:
                    logger.exception("Could not stop the MarketGuard snapshot refreshers during shutdown.")
            try:
                await run_in_threadpool(persistent_api_metrics.flush)
            except Exception:
                logger.exception("Could not flush persistent MarketGuard API metrics during shutdown.")
            for resource_name, resource in (
                ("MarketGuard player service", runtime_player_service),
                ("MarketGuard Bazaar service", runtime_bazaar_service),
                ("MarketGuard Lowest BIN service", lowestbin_service),
                ("MarketGuard response cache", runtime_response_cache),
            ):
                if resource is None:
                    continue
                try:
                    await resource.aclose()
                except Exception:
                    logger.exception("Could not close %s during shutdown.", resource_name)

    app = FastAPI(
        title="MarketGuard API",
        version="1.0.0",
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
        lifespan=app_lifespan,
    )
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.marketguard_response_cache = runtime_response_cache
    app.state.live_api_metrics = LiveApiRequestMetrics()
    app.state.persistent_api_metrics = persistent_api_metrics
    app.state.marketguard_refresh_supervisor = refresh_supervisor

    def _require_internal_observability_access(request: Request) -> None:
        if _request_originates_from_internal_network(request, runtime_settings.trusted_proxies):
            return
        raise HTTPException(status_code=403, detail="Observability endpoint is not available from public networks.")

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        live_metrics_token = app.state.live_api_metrics.start_request(
            request.url.path,
            str(request.headers.get("user-agent", "")),
        )
        if live_metrics_token is not None:
            app.state.persistent_api_metrics.record_request(
                live_metrics_token.bucket,
                live_metrics_token.endpoint,
                live_metrics_token.agent,
            )
        try:
            response = await call_next(request)
        finally:
            app.state.live_api_metrics.finish_request(live_metrics_token)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/api/internal/health", include_in_schema=False)
    async def internal_health(request: Request) -> JSONResponse:
        _require_internal_observability_access(request)
        return JSONResponse(
            {
                "status": "ok",
                "service": "marketguard-api",
                "utc": datetime.now(timezone.utc).isoformat(),
            }
        )

    @app.get("/api/internal/live-metrics", include_in_schema=False)
    async def internal_live_metrics(request: Request) -> JSONResponse:
        _require_internal_observability_access(request)
        await run_in_threadpool(app.state.persistent_api_metrics.flush)
        return JSONResponse(live_api_metrics_snapshot(app.state))

    register_marketguard_routes(
        app,
        settings=runtime_settings,
        service=lowestbin_service,
        bazaar_service=runtime_bazaar_service,
        player_service=runtime_player_service,
    )
    return app


def create_app(
    settings: MarketGuardSettings | None = None,
    service: LowestBinService | None = None,
    bazaar_service: BazaarService | None = None,
    player_service: PlayerService | None = None,
    response_cache: ResponseCacheChain | None = None,
) -> FastAPI:
    return create_marketguard_app(
        settings=settings,
        service=service,
        bazaar_service=bazaar_service,
        player_service=player_service,
        response_cache=response_cache,
    )
