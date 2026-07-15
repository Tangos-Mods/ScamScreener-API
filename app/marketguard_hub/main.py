from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import MarketGuardHubSettings
from .mojang import MojangProfileResolver

logger = logging.getLogger(__name__)


class PlayerNameLookupRequest(BaseModel):
    uuids: list[str] = Field(default_factory=list, max_length=32)


def _content_security_policy() -> str:
    return (
        "default-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "connect-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:"
    )


def create_marketguard_hub_app(
    settings: MarketGuardHubSettings | None = None,
    profile_resolver: MojangProfileResolver | None = None,
) -> FastAPI:
    base_dir = Path(__file__).resolve().parents[2]
    runtime_settings = settings or MarketGuardHubSettings.from_env()
    runtime_profile_resolver = profile_resolver or MojangProfileResolver()

    @asynccontextmanager
    async def app_lifespan(_: FastAPI):
        try:
            yield
        finally:
            try:
                await runtime_profile_resolver.aclose()
            except Exception:
                logger.exception("Could not close MarketGuard profile resolver during shutdown.")

    app = FastAPI(
        title="MarketGuard Hub",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=app_lifespan,
    )
    if runtime_settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(runtime_settings.allowed_hosts))

    app.state.settings = runtime_settings
    app.state.profile_resolver = runtime_profile_resolver
    app.state.templates = Jinja2Templates(directory=str(base_dir / "sites"))
    app.mount("/assets/css", StaticFiles(directory=str(base_dir / "css")), name="marketguard-hub-css")
    app.mount("/assets/js", StaticFiles(directory=str(base_dir / "js")), name="marketguard-hub-js")
    app.mount("/market/assets/css", StaticFiles(directory=str(base_dir / "css")), name="marketguard-hub-css-prefixed")
    app.mount("/market/assets/js", StaticFiles(directory=str(base_dir / "js")), name="marketguard-hub-js-prefixed")

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
        )
        response.headers["Content-Security-Policy"] = _content_security_policy()
        if request.url.path != "/internal/health":
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        if runtime_settings.enforce_https:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    def _render_market_page(request: Request, *, active_view: str) -> Response:
        return app.state.templates.TemplateResponse(
            request,
            "marketguard_hub.html",
            {
                "request": request,
                "active_view": active_view,
                "base_path": runtime_settings.base_path,
                "refresh_interval_ms": runtime_settings.refresh_interval_seconds * 1000,
                "page_title": "Bazaar" if active_view == "bazaar" else "Lowest BIN",
                "page_description": (
                    "Track Bazaar spreads, volumes, and current buy/sell prices."
                    if active_view == "bazaar"
                    else "Monitor current Lowest BIN pricing with 7d and 30d average context."
                ),
                "api_url": "/api/v1/bazaar" if active_view == "bazaar" else "/api/v2/lowestbin",
            },
        )

    @app.get("/", include_in_schema=False)
    async def market_home(request: Request):
        return _render_market_page(request, active_view="lowestbin")

    @app.get("/market", include_in_schema=False)
    async def market_home_prefixed_no_slash() -> RedirectResponse:
        return RedirectResponse(url="/market/", status_code=307)

    @app.get("/market/", include_in_schema=False)
    async def market_home_prefixed(request: Request):
        return _render_market_page(request, active_view="lowestbin")

    @app.get("/bazaar", include_in_schema=False)
    async def market_bazaar(request: Request):
        return _render_market_page(request, active_view="bazaar")

    @app.get("/bazaar/", include_in_schema=False)
    async def market_bazaar_slash() -> RedirectResponse:
        return RedirectResponse(url="/bazaar", status_code=307)

    @app.get("/market/bazaar", include_in_schema=False)
    async def market_bazaar_prefixed(request: Request):
        return _render_market_page(request, active_view="bazaar")

    @app.get("/market/bazaar/", include_in_schema=False)
    async def market_bazaar_prefixed_slash() -> RedirectResponse:
        return RedirectResponse(url="/market/bazaar", status_code=307)

    @app.get("/internal/health", include_in_schema=False)
    async def internal_health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": "marketguard-hub",
                "utc": datetime.now(timezone.utc).isoformat(),
            }
        )

    @app.post("/api/player-names", include_in_schema=False)
    @app.post("/market/api/player-names", include_in_schema=False)
    async def player_names(payload: PlayerNameLookupRequest) -> JSONResponse:
        resolver = app.state.profile_resolver
        names = await resolver.resolve_many(payload.uuids)
        return JSONResponse({"playerNames": names})

    return app


def create_app(settings: MarketGuardHubSettings | None = None) -> FastAPI:
    return create_marketguard_hub_app(settings=settings)
