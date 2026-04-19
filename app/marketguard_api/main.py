from __future__ import annotations

from fastapi import FastAPI

from .config import MarketGuardSettings
from .rate_limit import InMemoryRateLimiter
from .routes import register_marketguard_routes
from .service import BazaarService, LowestBinService


def create_marketguard_app(
    settings: MarketGuardSettings | None = None,
    service: LowestBinService | None = None,
    bazaar_service: BazaarService | None = None,
) -> FastAPI:
    runtime_settings = settings or MarketGuardSettings.from_env()
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

    @app.middleware("http")
    async def add_security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    register_marketguard_routes(app, settings=runtime_settings, service=service, bazaar_service=bazaar_service)
    return app
