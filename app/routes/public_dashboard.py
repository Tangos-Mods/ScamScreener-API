from __future__ import annotations

from fastapi import FastAPI

from ..config.settings import TrainingHubSettings
from .public_dashboard_account import register_public_dashboard_account_routes
from .public_dashboard_uploads import register_public_dashboard_upload_routes


def register_public_dashboard_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    register_public_dashboard_account_routes(app, settings)
    register_public_dashboard_upload_routes(app, settings)

