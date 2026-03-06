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

from .http.rate_limit import _SqliteRateLimiter, _rate_limit_identity, _rate_limit_rule
from .http.security import _apply_security_headers, _is_same_origin_post, _request_is_https
from .core.hub_core import _current_user_from_request, _ensure_storage, _init_database, _new_csrf_token, _run_retention_cleanup
from .routes import register_admin_routes, register_public_routes
from .config.settings import CSRF_COOKIE_NAME, TrainingHubSettings

logger = logging.getLogger(__name__)


def create_app(settings: TrainingHubSettings | None = None) -> FastAPI:
    base_dir = Path(__file__).resolve().parents[1]
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
            retention_task = getattr(app.state, "retention_task", None)
            if retention_task is not None:
                retention_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await retention_task

    app = FastAPI(title="ScamScreener Training Hub", version="2.0.0", lifespan=app_lifespan)
    app.state.settings = settings
    app.state.templates = Jinja2Templates(directory=str(base_dir / "sites"))
    app.state.rate_limiter = _SqliteRateLimiter(settings.database_path)
    app.mount("/css", StaticFiles(directory=str(base_dir / "css")), name="css")

    @app.middleware("http")
    async def attach_session_user(request: Request, call_next):
        if settings.enforce_https and not _request_is_https(request, settings):
            https_url = request.url.replace(scheme="https")
            redirect = RedirectResponse(url=str(https_url), status_code=307)
            return _apply_security_headers(redirect, settings.enforce_https)

        if settings.enforce_origin_check and not _is_same_origin_post(request, settings):
            rejected = PlainTextResponse("Invalid request origin.", status_code=403)
            return _apply_security_headers(rejected, settings.enforce_https)

        request.state.user = await run_in_threadpool(_current_user_from_request, request, settings)

        csrf_token = str(request.cookies.get(CSRF_COOKIE_NAME, "")).strip()
        set_csrf_cookie = False
        if len(csrf_token) < 24:
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
                            samesite="lax",
                            secure=settings.enforce_https,
                            max_age=settings.session_ttl_minutes * 60,
                        )
                    return _apply_security_headers(limited, settings.enforce_https)

        response = await call_next(request)
        if set_csrf_cookie:
            response.set_cookie(
                CSRF_COOKIE_NAME,
                csrf_token,
                httponly=False,
                samesite="lax",
                secure=settings.enforce_https,
                max_age=settings.session_ttl_minutes * 60,
            )
        return _apply_security_headers(response, settings.enforce_https)

    register_public_routes(app, settings)
    register_admin_routes(app, settings)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    runtime_settings = TrainingHubSettings.from_env()
    uvicorn.run(
        "app.main:app",
        host=runtime_settings.host,
        port=runtime_settings.port,
        reload=False,
        factory=False,
    )

