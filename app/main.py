from __future__ import annotations

from fastapi import FastAPI

from .marketguard_api.config import MarketGuardSettings
from .marketguard_api.routes import register_marketguard_routes
from .marketguard_api.service import BazaarService, LowestBinService
from .training_hub.config.settings import TrainingHubSettings
from .training_hub.main import create_training_hub_app


def create_app(
    training_hub_settings: TrainingHubSettings | None = None,
    marketguard_settings: MarketGuardSettings | None = None,
    marketguard_service: LowestBinService | None = None,
    marketguard_bazaar_service: BazaarService | None = None,
) -> FastAPI:
    app = create_training_hub_app(training_hub_settings)
    register_marketguard_routes(
        app,
        settings=marketguard_settings,
        service=marketguard_service,
        bazaar_service=marketguard_bazaar_service,
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
