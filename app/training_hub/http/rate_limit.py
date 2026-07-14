from __future__ import annotations

import time
from pathlib import Path

from fastapi import Request

from ..infra import db as sqlite3
from ..config.settings import TrainingHubSettings
from .security import _client_ip


class _DatabaseRateLimiter:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = database_path

    def allow(self, key: str, max_requests: int, window_seconds: int) -> tuple[bool, int]:
        now = int(time.time())
        safe_window = max(1, int(window_seconds))
        bucket_start = now - (now % safe_window)
        retry_after = max(1, (bucket_start + safe_window) - now)
        stale_before = bucket_start - (safe_window * 12)

        if sqlite3.is_mariadb_target(self.database_path):
            return self._allow_mariadb(key, max_requests, bucket_start, retry_after, stale_before, now)
        return self._allow_sqlite(key, max_requests, bucket_start, retry_after, stale_before, now)

    def _allow_sqlite(
        self,
        key: str,
        max_requests: int,
        bucket_start: int,
        retry_after: int,
        stale_before: int,
        now: int,
    ) -> tuple[bool, int]:
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

    def _allow_mariadb(
        self,
        key: str,
        max_requests: int,
        bucket_start: int,
        retry_after: int,
        stale_before: int,
        now: int,
    ) -> tuple[bool, int]:
        for _attempt in range(3):
            with sqlite3.connect(self.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM rate_limit_hits WHERE bucket_start < ?", (stale_before,))

                updated = connection.execute(
                    """
                    UPDATE rate_limit_hits
                    SET count = count + 1, updated_at = ?
                    WHERE bucket_key = ? AND bucket_start = ? AND count < ?
                    """,
                    (str(now), key, bucket_start, max_requests),
                )
                if updated.rowcount == 1:
                    connection.commit()
                    return True, 0

                row = connection.execute(
                    """
                    SELECT count
                    FROM rate_limit_hits
                    WHERE bucket_key = ? AND bucket_start = ?
                    """,
                    (key, bucket_start),
                ).fetchone()
                if row is not None:
                    current_count = int(row[0])
                    connection.commit()
                    if current_count >= max_requests:
                        return False, retry_after
                    continue

                try:
                    connection.execute(
                        """
                        INSERT INTO rate_limit_hits (bucket_key, bucket_start, count, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (key, bucket_start, 1, str(now)),
                    )
                    connection.commit()
                    return True, 0
                except Exception as exc:
                    connection.rollback()
                    if not self._is_duplicate_key_error(exc):
                        raise

        return False, retry_after

    @staticmethod
    def _is_duplicate_key_error(exc: Exception) -> bool:
        message = str(exc).lower()
        error_name = exc.__class__.__name__.lower()
        return (
            "duplicate" in message
            or "unique" in message
            or "integrity" in error_name
            or "1062" in message
        )


def _rate_limit_rule(
    method: str,
    path: str,
    settings: TrainingHubSettings,
) -> tuple[str, int, int] | None:
    normalized_method = method.upper()

    if normalized_method == "POST":
        if path == "/login":
            return "auth.login", 12, 300
        if path == "/dashboard/upload":
            return "upload.submit", 12, 600
        if path == "/api/v1/client/uploads":
            return "upload.api-submit", 12, 600
        if path == "/api/v1/client/uploads/anonymous":
            return "upload.api-anonymous-submit", 12, 600
        if path == "/api/v1/client/auth/logout":
            return "auth.api-logout", 30, 600
        if path == "/admin/train":
            return "admin.train", 4, 600
        if path == "/admin/retention/run":
            return "admin.retention", 4, 600
        if path == "/admin/cases/rejected/delete":
            return "admin.case-rejected-delete", 4, 600
        if path == "/admin/backups/create":
            return "admin.backup-create", 4, 600
        if path == "/admin/backups/restore":
            return "admin.backup-restore", 2, 600
        if path.startswith("/admin/users/") and path.endswith("/admin"):
            return "admin.user-role", 30, 600
        if path == "/admin/users/access-policy":
            return "admin.user-access-policy", 20, 600
        if path.startswith("/admin/users/") and path.endswith("/delete"):
            return "admin.user-delete", 20, 600
        if path.startswith("/admin/cases/") and path.endswith("/delete"):
            return "admin.case-delete", 40, 600
        return None

    if normalized_method == "GET":
        if path.startswith("/auth/external/") and not path.endswith("/callback"):
            return "auth.external-start", 20, 300
        if path.startswith("/auth/external/") and path.endswith("/callback"):
            return "auth.external-callback", 20, 300
        if path.startswith("/account/confirm/external/"):
            return "auth.external-step-up-start", 20, 300
        if path.startswith("/dashboard/uploads/") and path.endswith("/download"):
            return "download.upload", settings.max_upload_downloads_per_minute_per_user, 60
        if path.startswith("/admin/runs/") and path.endswith("/bundle"):
            return "download.bundle", settings.max_bundle_downloads_per_minute_per_user, 60
        if path == "/admin/quarantine/bundle":
            return "download.quarantine-bundle", settings.max_bundle_downloads_per_minute_per_user, 60
    return None


def _rate_limit_identity(request: Request, settings: TrainingHubSettings) -> str:
    user = getattr(request.state, "user", None)
    if isinstance(user, dict) and "id" in user:
        try:
            return f"user:{int(user['id'])}"
        except (TypeError, ValueError):
            pass
    return f"ip:{_client_ip(request, settings)}"

