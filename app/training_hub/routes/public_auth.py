from __future__ import annotations

from fastapi import FastAPI

from ..config.settings import TrainingHubSettings
from .public_auth_external import register_public_auth_external_routes


def register_public_auth_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    register_public_auth_external_routes(app, settings)

