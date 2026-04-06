from __future__ import annotations

from fastapi import FastAPI

from ..config.settings import TrainingHubSettings
from ..services.mailer import send_admin_mfa_email, send_password_reset_email
from .public_api_client import register_public_api_client_routes
from .public_auth import register_public_auth_routes
from .public_dashboard import register_public_dashboard_routes
from .public_site import register_public_site_routes


def register_public_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    register_public_site_routes(app, settings)
    register_public_auth_routes(app, settings)
    register_public_api_client_routes(app, settings)
    register_public_dashboard_routes(app, settings)

