from __future__ import annotations

from fastapi import FastAPI

from ..services.mailer import send_admin_mfa_email, send_password_reset_email
from ..config.settings import TrainingHubSettings
from .public_auth_login import register_public_auth_login_routes
from .public_auth_mfa import register_public_auth_mfa_routes
from .public_auth_register import register_public_auth_register_routes
from .public_auth_reset import register_public_auth_password_reset_routes


def register_public_auth_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    register_public_auth_register_routes(app, settings)
    register_public_auth_password_reset_routes(app, settings)
    register_public_auth_mfa_routes(app, settings)
    register_public_auth_login_routes(app, settings)

