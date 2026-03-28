from __future__ import annotations

__all__ = ["app", "create_app", "create_training_hub_app", "TrainingHubSettings"]


def __getattr__(name: str):
    if name == "TrainingHubSettings":
        from .config.settings import TrainingHubSettings

        return TrainingHubSettings
    if name in {"app", "create_app", "create_training_hub_app"}:
        from .main import create_app, create_training_hub_app

        return {
            "app": create_training_hub_app(),
            "create_app": create_app,
            "create_training_hub_app": create_training_hub_app,
        }[name]
    raise AttributeError(name)
