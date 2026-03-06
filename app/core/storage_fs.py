from __future__ import annotations

from ..config.settings import TrainingHubSettings


def _ensure_storage(settings: TrainingHubSettings) -> None:
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.bundles_dir.mkdir(parents=True, exist_ok=True)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)

