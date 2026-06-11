from __future__ import annotations

from fastapi import FastAPI

from ..config.settings import TrainingHubSettings
from .admin_analytics import register_admin_analytics_routes
from .admin_backups import register_admin_backup_routes
from .admin_cases import register_admin_case_routes
from .admin_downloads import register_admin_download_routes
from .admin_overview import register_admin_overview_routes
from .admin_scrub_rules import register_admin_scrub_rule_routes
from .admin_users import register_admin_user_routes


def register_admin_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    register_admin_overview_routes(app, settings)
    register_admin_analytics_routes(app, settings)
    register_admin_backup_routes(app, settings)
    register_admin_download_routes(app, settings)
    register_admin_user_routes(app, settings)
    register_admin_case_routes(app, settings)
    register_admin_scrub_rule_routes(app, settings)

