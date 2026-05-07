from __future__ import annotations

__all__ = ["app", "create_app", "create_marketguard_hub_app", "MarketGuardHubSettings"]


def __getattr__(name: str):
    if name in {"app", "create_app", "create_marketguard_hub_app", "MarketGuardHubSettings"}:
        from .config import MarketGuardHubSettings
        from .main import create_app, create_marketguard_hub_app

        return {
            "app": create_marketguard_hub_app(),
            "create_app": create_app,
            "create_marketguard_hub_app": create_marketguard_hub_app,
            "MarketGuardHubSettings": MarketGuardHubSettings,
        }[name]
    raise AttributeError(name)
