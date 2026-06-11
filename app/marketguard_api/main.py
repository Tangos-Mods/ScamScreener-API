from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .cache import ResponseCacheChain, build_response_cache
from .config import MarketGuardSettings
from .rate_limit import InMemoryRateLimiter
from .routes import register_marketguard_routes
from .service import BazaarService, LowestBinService
from .storage import MarketGuardStorage
from app.training_hub.http.live_api_metrics import LiveApiRequestMetrics, live_api_metrics_snapshot
from app.training_hub.http.persistent_api_metrics import PersistentApiMetricsRecorder
from app.training_hub.core.common import _request_originates_from_internal_network


def create_marketguard_app(
    settings: MarketGuardSettings | None = None,
    service: LowestBinService | None = None,
    bazaar_service: BazaarService | None = None,
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
    docs_url = "/docs" if runtime_settings.api_docs_enabled else None
    redoc_url = "/redoc" if runtime_settings.api_docs_enabled else None
    openapi_url = "/openapi.json" if runtime_settings.api_docs_enabled else None
    app = FastAPI(
        title="MarketGuard API",
        version="1.0.0",
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.marketguard_response_cache = runtime_response_cache
    app.state.live_api_metrics = LiveApiRequestMetrics()
    app.state.persistent_api_metrics = PersistentApiMetricsRecorder(runtime_settings.database_url)

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

    if runtime_response_cache is not None:
        app.add_event_handler("shutdown", runtime_response_cache.aclose)
    app.add_event_handler("shutdown", app.state.persistent_api_metrics.flush)

    register_marketguard_routes(
        app,
        settings=runtime_settings,
        service=lowestbin_service,
        bazaar_service=runtime_bazaar_service,
    )
    return app


def create_app(
    settings: MarketGuardSettings | None = None,
    service: LowestBinService | None = None,
    bazaar_service: BazaarService | None = None,
    response_cache: ResponseCacheChain | None = None,
) -> FastAPI:
    return create_marketguard_app(
        settings=settings,
        service=service,
        bazaar_service=bazaar_service,
        response_cache=response_cache,
    )
