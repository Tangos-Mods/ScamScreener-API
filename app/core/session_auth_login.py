from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..infra import db as sqlite3
from .common import _now_utc_iso
from .session_auth_password import _verify_password


LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_MINUTES = 15


def _consume_login_attempt(
    database_path: Path,
    username_or_email: str,
    password: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    candidate = (username_or_email or "").strip().lower()

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        user = connection.execute(
            """
            SELECT id, password_hash, failed_login_attempts, lockout_until
            FROM users
            WHERE username = ? OR email = ?
            """,
            (candidate, candidate),
        ).fetchone()

        if user is None:
            return {"status": "invalid"}

        user_id = int(user["id"])
        lockout_until_raw = str(user["lockout_until"] or "").strip()
        if lockout_until_raw:
            try:
                lockout_until = datetime.fromisoformat(lockout_until_raw.replace("Z", "+00:00"))
            except ValueError:
                lockout_until = now
            if lockout_until > now:
                remaining_seconds = int((lockout_until - now).total_seconds()) + 1
                return {"status": "locked", "retry_after": max(1, remaining_seconds), "user_id": user_id}

        if not _verify_password(password, str(user["password_hash"])):
            attempts = int(user["failed_login_attempts"] or 0) + 1
            if attempts >= LOGIN_MAX_FAILURES:
                lockout_until = now + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
                connection.execute(
                    """
                    UPDATE users
                    SET failed_login_attempts = 0, lockout_until = ?
                    WHERE id = ?
                    """,
                    (lockout_until.isoformat().replace("+00:00", "Z"), user_id),
                )
                connection.commit()
                remaining_seconds = int((lockout_until - now).total_seconds()) + 1
                return {"status": "locked", "retry_after": max(1, remaining_seconds), "user_id": user_id}

            connection.execute(
                """
                UPDATE users
                SET failed_login_attempts = ?, lockout_until = NULL
                WHERE id = ?
                """,
                (attempts, user_id),
            )
            connection.commit()
            return {"status": "invalid", "user_id": user_id}

        connection.execute(
            """
            UPDATE users
            SET last_login_at = ?, failed_login_attempts = 0, lockout_until = NULL
            WHERE id = ?
            """,
            (_now_utc_iso(), user_id),
        )
        connection.commit()
        return {"status": "ok", "user_id": user_id}

