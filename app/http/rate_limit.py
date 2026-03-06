from __future__ import annotations

import time
from pathlib import Path

from fastapi import Request

from ..infra import db as sqlite3
from .security import _client_ip
from ..config.settings import TrainingHubSettings


class _SqliteRateLimiter:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def allow(self, key: str, max_requests: int, window_seconds: int) -> tuple[bool, int]:
        now = int(time.time())
        safe_window = max(1, int(window_seconds))
        bucket_start = now - (now % safe_window)
        retry_after = max(1, (bucket_start + safe_window) - now)
        stale_before = bucket_start - (safe_window * 12)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM rate_limit_hits WHERE bucket_start < ?", (stale_before,))
            row = connection.execute(
                """
                SELECT count
                FROM rate_limit_hits
                WHERE bucket_key = ? AND bucket_start = ?
                """,
                (key, bucket_start),
            ).fetchone()

            if row is None:
                connection.execute(
                    """
                    INSERT INTO rate_limit_hits (bucket_key, bucket_start, count, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (key, bucket_start, 1, str(now)),
                )
                connection.commit()
                return True, 0

            current_count = int(row[0])
            if current_count >= max_requests:
                connection.commit()
                return False, retry_after

            connection.execute(
                """
                UPDATE rate_limit_hits
                SET count = ?, updated_at = ?
                WHERE bucket_key = ? AND bucket_start = ?
                """,
                (current_count + 1, str(now), key, bucket_start),
            )
            connection.commit()
            return True, 0


def _rate_limit_rule(
    method: str,
    path: str,
    settings: TrainingHubSettings,
) -> tuple[str, int, int] | None:
    normalized_method = method.upper()

    if normalized_method == "POST":
        if path == "/login":
            return "auth.login", 12, 300
        if path == "/admin/mfa":
            return "auth.admin-mfa", 12, 600
        if path == "/forgot-password":
            return "auth.password-reset-request", 10, 600
        if path == "/reset-password":
            return "auth.password-reset-submit", 10, 600
        if path == "/register":
            return "auth.register", 10, 600
        if path == "/dashboard/upload":
            return "upload.submit", 12, 600
        if path == "/dashboard/password":
            return "auth.password-change", 10, 600
        if path == "/admin/train":
            return "admin.train", 4, 600
        if path == "/admin/retention/run":
            return "admin.retention", 4, 600
        if path == "/admin/backups/create":
            return "admin.backup-create", 4, 600
        if path == "/admin/backups/restore":
            return "admin.backup-restore", 2, 600
        if path.startswith("/admin/users/") and path.endswith("/admin"):
            return "admin.user-role", 30, 600
        if path.startswith("/admin/cases/") and path.endswith("/delete"):
            return "admin.case-delete", 40, 600
        return None

    if normalized_method == "GET":
        if path.startswith("/dashboard/uploads/") and path.endswith("/download"):
            return "download.upload", settings.max_upload_downloads_per_minute_per_user, 60
        if path.startswith("/admin/runs/") and path.endswith("/bundle"):
            return "download.bundle", settings.max_bundle_downloads_per_minute_per_user, 60
    return None


def _rate_limit_identity(request: Request, settings: TrainingHubSettings) -> str:
    user = getattr(request.state, "user", None)
    if isinstance(user, dict) and "id" in user:
        try:
            return f"user:{int(user['id'])}"
        except (TypeError, ValueError):
            pass
    return f"ip:{_client_ip(request, settings)}"

