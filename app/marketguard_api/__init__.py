from __future__ import annotations

__all__ = [
    "app",
    "create_marketguard_app",
    "register_marketguard_routes",
    "BazaarService",
    "LowestBinService",
    "MarketGuardSettings",
]


def __getattr__(name: str):
    if name == "MarketGuardSettings":
        from .config import MarketGuardSettings

        return MarketGuardSettings
    if name == "LowestBinService":
        from .service import LowestBinService

        return LowestBinService
    if name == "BazaarService":
        from .service import BazaarService

        return BazaarService
    if name == "register_marketguard_routes":
        from .routes import register_marketguard_routes

        return register_marketguard_routes
    if name in {"app", "create_marketguard_app"}:
        from .main import create_marketguard_app

        return {
            "app": create_marketguard_app(),
            "create_marketguard_app": create_marketguard_app,
        }[name]
    raise AttributeError(name)
