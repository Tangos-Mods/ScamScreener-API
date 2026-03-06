from __future__ import annotations

from contextlib import suppress

from ..config.settings import TrainingHubSettings


def _ensure_storage(settings: TrainingHubSettings) -> None:
    for directory in (settings.storage_dir, settings.uploads_dir, settings.bundles_dir, settings.backups_dir):
        directory.mkdir(parents=True, exist_ok=True)
        with suppress(OSError, PermissionError):
            directory.chmod(0o700)

