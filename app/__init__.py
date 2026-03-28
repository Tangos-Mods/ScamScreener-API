from __future__ import annotations

__all__ = [
    "app",
    "create_app",
    "create_marketguard_app",
    "create_training_hub_app",
    "MarketGuardSettings",
    "TrainingHubSettings",
]


def __getattr__(name: str):
    if name in {"app", "create_app"}:
        from .main import create_app

        return {"app": create_app(), "create_app": create_app}[name]
    if name in {"create_marketguard_app", "MarketGuardSettings"}:
        from .marketguard_api.config import MarketGuardSettings
        from .marketguard_api.main import create_marketguard_app

        return {
            "create_marketguard_app": create_marketguard_app,
            "MarketGuardSettings": MarketGuardSettings,
        }[name]
    if name in {"create_training_hub_app", "TrainingHubSettings"}:
        from .training_hub.config.settings import TrainingHubSettings
        from .training_hub.main import create_training_hub_app

        return {
            "create_training_hub_app": create_training_hub_app,
            "TrainingHubSettings": TrainingHubSettings,
        }[name]
    raise AttributeError(name)
