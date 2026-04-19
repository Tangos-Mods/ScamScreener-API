from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .http.rate_limit import _SqliteRateLimiter, _rate_limit_identity, _rate_limit_rule
from .http.security import _apply_security_headers, _is_same_origin_post, _request_is_https
from .core.hub_core import (
    _current_user_from_request,
    _ensure_storage,
    _init_database,
    _new_csrf_token,
    _process_next_data_export_request,
    _run_retention_cleanup,
)
from .core.common import _format_utc_timestamp
from .routes import register_admin_routes, register_public_routes
from .config.settings import CSRF_COOKIE_NAME, TrainingHubSettings

logger = logging.getLogger(__name__)


def create_training_hub_app(settings: TrainingHubSettings | None = None) -> FastAPI:
    base_dir = Path(__file__).resolve().parents[2]
    settings = settings or TrainingHubSettings.from_env()
    if settings.enforce_https and (settings.secret_key == "change-me-in-env" or len(settings.secret_key) < 32):
        raise ValueError(
            "TRAINING_HUB_SECRET_KEY must be set to a strong value (>=32 chars) when TRAINING_HUB_ENFORCE_HTTPS=true."
        )
    _ensure_storage(settings)
    _init_database(settings.database_path)

    @contextlib.asynccontextmanager
    async def app_lifespan(app: FastAPI):
        app.state.retention_task = None
        app.state.data_export_task = None
        app.state.data_export_wake = asyncio.Event()

        async def _data_export_worker() -> None:
            app.state.data_export_wake.set()
            while True:
                try:
                    processed = await run_in_threadpool(_process_next_data_export_request, settings)
                    if processed:
                        await asyncio.sleep(0)
                        continue
                except Exception:
                    logger.exception("Account data export worker failed.")
                await app.state.data_export_wake.wait()
                app.state.data_export_wake.clear()

        app.state.data_export_task = asyncio.create_task(_data_export_worker(), name="account-data-export-worker")
        if settings.retention_auto_enabled:

            async def _retention_worker() -> None:
                interval_seconds = max(60, int(settings.retention_auto_interval_minutes) * 60)
                while True:
                    try:
                        summary = await run_in_threadpool(_run_retention_cleanup, settings)
                        logger.info("Auto retention cleanup completed: %s", summary)
                    except Exception:
                        logger.exception("Auto retention cleanup failed.")
                    await asyncio.sleep(interval_seconds)

            app.state.retention_task = asyncio.create_task(_retention_worker(), name="retention-auto-cleanup")

        try:
            yield
        finally:
            data_export_task = getattr(app.state, "data_export_task", None)
            if data_export_task is not None:
                data_export_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await data_export_task
            retention_task = getattr(app.state, "retention_task", None)
            if retention_task is not None:
                retention_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await retention_task

    docs_url = "/docs" if settings.api_docs_enabled else None
    redoc_url = "/redoc" if settings.api_docs_enabled else None
    openapi_url = "/openapi.json" if settings.api_docs_enabled else None
    app = FastAPI(
        title="ScamScreener Training Hub",
        version="2.0.0",
        lifespan=app_lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )
    if settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(settings.allowed_hosts))
    app.state.settings = settings
    app.state.templates = Jinja2Templates(directory=str(base_dir / "sites"))
    app.state.templates.env.filters["datetime_utc"] = _format_utc_timestamp
    app.state.rate_limiter = _SqliteRateLimiter(settings.database_path)
    app.mount("/css", StaticFiles(directory=str(base_dir / "css")), name="css")

    def _apply_no_store_headers(response, request: Request) -> None:
        path = request.url.path
        if (
            path.startswith("/admin")
            or path.startswith("/dashboard")
            or path.startswith("/api/v1/client/")
            or path in {"/login", "/register", "/forgot-password", "/reset-password", "/admin/mfa"}
            or "set-cookie" in response.headers
        ):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"

    @app.middleware("http")
    async def attach_session_user(request: Request, call_next):
        if settings.enforce_https and not _request_is_https(request, settings):
            https_url = request.url.replace(scheme="https")
            redirect = RedirectResponse(url=str(https_url), status_code=307)
            _apply_no_store_headers(redirect, request)
            return _apply_security_headers(redirect, settings.enforce_https)

        if settings.enforce_origin_check and not _is_same_origin_post(request, settings):
            rejected = PlainTextResponse("Invalid request origin.", status_code=403)
            _apply_no_store_headers(rejected, request)
            return _apply_security_headers(rejected, settings.enforce_https)

        request.state.user = await run_in_threadpool(_current_user_from_request, request, settings)

        should_manage_csrf_cookie = not request.url.path.startswith("/api/")
        csrf_token = str(request.cookies.get(CSRF_COOKIE_NAME, "")).strip() if should_manage_csrf_cookie else ""
        set_csrf_cookie = False
        if should_manage_csrf_cookie and len(csrf_token) < 24:
            csrf_token = _new_csrf_token()
            set_csrf_cookie = True
        request.state.csrf_token = csrf_token

        if settings.enable_rate_limit:
            rule = _rate_limit_rule(request.method, request.url.path, settings)
            if rule is not None:
                bucket, max_requests, window_seconds = rule
                key = f"{bucket}:{_rate_limit_identity(request, settings)}"
                allowed, retry_after = await run_in_threadpool(
                    app.state.rate_limiter.allow,
                    key,
                    max_requests,
                    window_seconds,
                )
                if not allowed:
                    limited = PlainTextResponse("Too many requests.", status_code=429)
                    limited.headers["Retry-After"] = str(retry_after)
                    if set_csrf_cookie:
                        limited.set_cookie(
                            CSRF_COOKIE_NAME,
                            csrf_token,
                            httponly=False,
                            samesite="strict",
                            secure=settings.enforce_https,
                            max_age=settings.session_ttl_minutes * 60,
                            path="/",
                        )
                    _apply_no_store_headers(limited, request)
                    return _apply_security_headers(limited, settings.enforce_https)

        response = await call_next(request)
        if should_manage_csrf_cookie and set_csrf_cookie:
            response.set_cookie(
                CSRF_COOKIE_NAME,
                csrf_token,
                httponly=False,
                samesite="strict",
                secure=settings.enforce_https,
                max_age=settings.session_ttl_minutes * 60,
                path="/",
            )
        _apply_no_store_headers(response, request)
        return _apply_security_headers(response, settings.enforce_https)

    register_public_routes(app, settings)
    register_admin_routes(app, settings)
    return app


def create_app(settings: TrainingHubSettings | None = None) -> FastAPI:
    return create_training_hub_app(settings)


if __name__ == "__main__":
    import uvicorn

    runtime_settings = TrainingHubSettings.from_env()
    uvicorn.run(
        "app.training_hub.main:create_app",
        host=runtime_settings.host,
        port=runtime_settings.port,
        reload=False,
        factory=True,
    )

