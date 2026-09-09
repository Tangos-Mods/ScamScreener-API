from __future__ import annotations

import contextlib
import logging

from fastapi import FastAPI

from .marketguard_api.config import MarketGuardSettings
from .marketguard_api.player_service import PlayerService
from .marketguard_api.refresher import build_refresh_supervisor
from .marketguard_api.routes import register_marketguard_routes
from .marketguard_api.service import BazaarService, LowestBinService
from .training_hub.config.settings import TrainingHubSettings
from .training_hub.main import create_training_hub_app, install_filtered_openapi

logger = logging.getLogger(__name__)


def _install_marketguard_refreshers(app: FastAPI) -> None:
    """Run the snapshot refreshers alongside the Training Hub lifespan.

    The combined entrypoint reuses the hub's lifespan, so the refreshers are
    layered on top of it rather than replacing it. Without them the MarketGuard
    routes would fall back to refreshing inside client requests.
    """
    supervisor = build_refresh_supervisor(
        getattr(app.state, "marketguard_settings", None),
        lowestbin_service=getattr(app.state, "marketguard_service", None),
        bazaar_service=getattr(app.state, "marketguard_bazaar_service", None),
    )
    app.state.marketguard_refresh_supervisor = supervisor
    if supervisor is None:
        return

    hub_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def combined_lifespan(scoped_app: FastAPI):
        async with hub_lifespan(scoped_app):
            await supervisor.start()
            try:
                yield
            finally:
                try:
                    await supervisor.stop()
                except Exception:
                    logger.exception("Could not stop the MarketGuard snapshot refreshers during shutdown.")

    app.router.lifespan_context = combined_lifespan


def create_app(
    training_hub_settings: TrainingHubSettings | None = None,
    marketguard_settings: MarketGuardSettings | None = None,
    marketguard_service: LowestBinService | None = None,
    marketguard_bazaar_service: BazaarService | None = None,
    marketguard_player_service: PlayerService | None = None,
) -> FastAPI:
    app = create_training_hub_app(training_hub_settings)
    register_marketguard_routes(
        app,
        settings=marketguard_settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
        player_service=marketguard_player_service,
    )
    _install_marketguard_refreshers(app)
    install_filtered_openapi(
        app,
        route_filter=lambda route: str(route.endpoint.__module__) == "app.marketguard_api.routes",
    )
    app.title = "ScamScreener Platform"
    app.version = "3.0.0"
    return app


if __name__ == "__main__":
    import uvicorn

    runtime_settings = TrainingHubSettings.from_env()
    uvicorn.run(
        "app.main:create_app",
        host=runtime_settings.host,
        port=runtime_settings.port,
        reload=False,
        factory=True,
    )
