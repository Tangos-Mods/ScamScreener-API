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
    app = FastAPI(title="MarketGuard API", version="1.0.0")
    app.state.rate_limiter = InMemoryRateLimiter()

    @app.middleware("http")
    async def add_security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    register_marketguard_routes(app, settings=settings, service=service, bazaar_service=bazaar_service)
    return app
