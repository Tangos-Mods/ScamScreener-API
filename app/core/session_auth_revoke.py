from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..infra import db as sqlite3
from .common import _now_utc_iso


def _revoke_session_by_token(database_path: Path, session_token: str, reason: str = "logout") -> None:
    normalized_token = (session_token or "").strip()
    if not normalized_token:
        return
    token_sha = _session_token_hash(normalized_token)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE sessions
            SET revoked_at = ?, revoke_reason = ?
            WHERE token_sha256 = ? AND revoked_at IS NULL
            """,
            (_now_utc_iso(), (reason or "").strip()[:100], token_sha),
        )
        connection.commit()


def _revoke_all_user_sessions(database_path: Path, user_id: int, reason: str = "security") -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE sessions
            SET revoked_at = ?, revoke_reason = ?
            WHERE user_id = ? AND revoked_at IS NULL
            """,
            (_now_utc_iso(), (reason or "").strip()[:100], int(user_id)),
        )
        connection.commit()


def _revoke_user_session_by_id(
    database_path: Path,
    user_id: int,
    session_id: int,
    reason: str = "session-revoke",
) -> bool:
    with sqlite3.connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE sessions
            SET revoked_at = ?, revoke_reason = ?
            WHERE id = ? AND user_id = ? AND revoked_at IS NULL
            """,
            (_now_utc_iso(), (reason or "").strip()[:100], int(session_id), int(user_id)),
        )
        connection.commit()
        return int(cursor.rowcount or 0) > 0


def _revoke_other_user_sessions(
    database_path: Path,
    user_id: int,
    current_session_id: int | None,
    reason: str = "revoke-others",
) -> int:
    with sqlite3.connect(database_path) as connection:
        if current_session_id is None:
            cursor = connection.execute(
                """
                UPDATE sessions
                SET revoked_at = ?, revoke_reason = ?
                WHERE user_id = ? AND revoked_at IS NULL
                """,
                (_now_utc_iso(), (reason or "").strip()[:100], int(user_id)),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE sessions
                SET revoked_at = ?, revoke_reason = ?
                WHERE user_id = ? AND revoked_at IS NULL AND id != ?
                """,
                (_now_utc_iso(), (reason or "").strip()[:100], int(user_id), int(current_session_id)),
            )
        connection.commit()
        return int(cursor.rowcount or 0)


def _user_active_sessions(database_path: Path, user_id: int, current_session_id: int | None) -> list[dict[str, Any]]:
    now_iso = _now_utc_iso()
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, created_at, expires_at, remote_addr, user_agent
            FROM sessions
            WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (int(user_id), now_iso),
        ).fetchall()

    sessions: list[dict[str, Any]] = []
    for row in rows:
        session_id = int(row["id"])
        sessions.append(
            {
                "id": session_id,
                "created_at": str(row["created_at"]),
                "expires_at": str(row["expires_at"]),
                "remote_addr": str(row["remote_addr"] or "-"),
                "user_agent": str(row["user_agent"] or "-"),
                "is_current": current_session_id is not None and session_id == int(current_session_id),
            }
        )
    return sessions


def _session_token_hash(session_token: str) -> str:
    return hashlib.sha256(session_token.encode("utf-8")).hexdigest()

