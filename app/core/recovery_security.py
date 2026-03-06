from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import shutil
import tarfile
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..infra import db as sqlite3
from ..config.settings import TrainingHubSettings
from .common import _is_path_within, _normalize_user_agent_for_binding, _now_utc_iso
from .session_auth import _hash_password, _validate_password


def _password_reset_token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _admin_mfa_token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _admin_mfa_code_hash(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()


def _create_admin_mfa_challenge(
    database_path: Path,
    user_id: int,
    ttl_minutes: int,
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat().replace("+00:00", "Z")
    expires_at = (now + timedelta(minutes=max(5, int(ttl_minutes)))).isoformat().replace("+00:00", "Z")
    token = secrets.token_urlsafe(40)
    code = f"{secrets.randbelow(1_000_000):06d}"
    token_sha = _admin_mfa_token_hash(token)
    code_sha = _admin_mfa_code_hash(code)

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        user = connection.execute(
            "SELECT id, email, is_admin FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if user is None or int(user["is_admin"]) != 1:
            return {"issued": False}

        connection.execute(
            "DELETE FROM admin_mfa_challenges WHERE user_id = ? AND consumed_at IS NULL",
            (int(user_id),),
        )
        connection.execute(
            """
            INSERT INTO admin_mfa_challenges (
                created_at,
                user_id,
                token_sha256,
                code_sha256,
                expires_at,
                consumed_at,
                source_ip,
                user_agent
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                now_iso,
                int(user_id),
                token_sha,
                code_sha,
                expires_at,
                (source_ip or "").strip()[:80],
                (user_agent or "").strip()[:300],
            ),
        )
        connection.commit()

    return {
        "issued": True,
        "user_id": int(user_id),
        "user_email": str(user["email"]),
        "token": token,
        "code": code,
        "expires_at": expires_at,
    }


def _validate_admin_mfa_challenge(
    database_path: Path,
    token: str,
    source_ip: str = "",
    user_agent: str = "",
    max_attempts: int = 5,
) -> dict[str, Any]:
    normalized_token = (token or "").strip()
    if not normalized_token:
        return {"ok": False, "error": "Missing verification challenge."}

    token_sha = _admin_mfa_token_hash(normalized_token)
    now_iso = _now_utc_iso()

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, user_id, expires_at, consumed_at, source_ip, user_agent, failed_attempts
            FROM admin_mfa_challenges
            WHERE token_sha256 = ?
            """,
            (token_sha,),
        ).fetchone()

        if row is None:
            return {"ok": False, "error": "Verification challenge is invalid or expired."}
        if row["consumed_at"] is not None:
            return {"ok": False, "error": "Verification challenge is invalid or expired."}
        if str(row["expires_at"]) <= now_iso:
            connection.execute(
                "UPDATE admin_mfa_challenges SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Verification challenge has expired."}
        if int(row["failed_attempts"] or 0) >= int(max_attempts):
            connection.execute(
                "UPDATE admin_mfa_challenges SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Verification challenge has expired."}

        expected_ip = str(row["source_ip"] or "").strip()
        if expected_ip and source_ip and expected_ip != source_ip:
            connection.execute(
                "UPDATE admin_mfa_challenges SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Verification challenge is invalid for this client."}

        expected_ua = _normalize_user_agent_for_binding(str(row["user_agent"] or ""))
        actual_ua = _normalize_user_agent_for_binding(user_agent)
        if expected_ua and actual_ua and expected_ua != actual_ua:
            connection.execute(
                "UPDATE admin_mfa_challenges SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Verification challenge is invalid for this client."}

    return {
        "ok": True,
        "challenge_id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "expires_at": str(row["expires_at"]),
        "failed_attempts": int(row["failed_attempts"] or 0),
    }


def _consume_admin_mfa_challenge(
    database_path: Path,
    token: str,
    code: str,
    source_ip: str = "",
    user_agent: str = "",
    max_attempts: int = 5,
) -> dict[str, Any]:
    normalized_code = (code or "").strip()
    if not re.fullmatch(r"[0-9]{6}", normalized_code):
        return {"ok": False, "error": "Verification code must be 6 digits.", "status_code": 400}

    normalized_token = (token or "").strip()
    if not normalized_token:
        return {"ok": False, "error": "Missing verification challenge.", "status_code": 400}

    token_sha = _admin_mfa_token_hash(normalized_token)
    submitted_code_sha = _admin_mfa_code_hash(normalized_code)
    now_iso = _now_utc_iso()

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                id,
                user_id,
                code_sha256,
                expires_at,
                consumed_at,
                source_ip,
                user_agent,
                failed_attempts
            FROM admin_mfa_challenges
            WHERE token_sha256 = ?
            """,
            (token_sha,),
        ).fetchone()
        if row is None or row["consumed_at"] is not None:
            return {"ok": False, "error": "Verification challenge is invalid or expired.", "status_code": 400}

        if str(row["expires_at"]) <= now_iso:
            connection.execute(
                "UPDATE admin_mfa_challenges SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Verification challenge has expired.", "status_code": 400}

        failed_attempts = int(row["failed_attempts"] or 0)
        if failed_attempts >= int(max_attempts):
            connection.execute(
                "UPDATE admin_mfa_challenges SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Verification challenge has expired.", "status_code": 400}

        expected_ip = str(row["source_ip"] or "").strip()
        if expected_ip and source_ip and expected_ip != source_ip:
            connection.execute(
                "UPDATE admin_mfa_challenges SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Verification challenge is invalid for this client.", "status_code": 400}

        expected_ua = _normalize_user_agent_for_binding(str(row["user_agent"] or ""))
        actual_ua = _normalize_user_agent_for_binding(user_agent)
        if expected_ua and actual_ua and expected_ua != actual_ua:
            connection.execute(
                "UPDATE admin_mfa_challenges SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Verification challenge is invalid for this client.", "status_code": 400}

        stored_code_sha = str(row["code_sha256"] or "")
        if not stored_code_sha or not hmac.compare_digest(stored_code_sha, submitted_code_sha):
            updated_attempts = failed_attempts + 1
            consumed_at = now_iso if updated_attempts >= int(max_attempts) else None
            connection.execute(
                """
                UPDATE admin_mfa_challenges
                SET failed_attempts = ?, consumed_at = COALESCE(?, consumed_at)
                WHERE id = ? AND consumed_at IS NULL
                """,
                (updated_attempts, consumed_at, int(row["id"])),
            )
            connection.commit()
            if updated_attempts >= int(max_attempts):
                return {
                    "ok": False,
                    "error": "Verification challenge has expired.",
                    "status_code": 400,
                }
            return {"ok": False, "error": "Invalid verification code.", "status_code": 401}

        consumed_cursor = connection.execute(
            "UPDATE admin_mfa_challenges SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (now_iso, int(row["id"])),
        )
        connection.commit()
        if int(consumed_cursor.rowcount or 0) == 0:
            return {"ok": False, "error": "Verification challenge is invalid or expired.", "status_code": 400}

    return {
        "ok": True,
        "user_id": int(row["user_id"]),
        "challenge_id": int(row["id"]),
    }


def _create_password_reset_request(
    database_path: Path,
    username_or_email: str,
    ttl_minutes: int,
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    candidate = (username_or_email or "").strip().lower()
    if not candidate:
        return {"issued": False}

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat().replace("+00:00", "Z")
    expires_at = (now + timedelta(minutes=max(5, int(ttl_minutes)))).isoformat().replace("+00:00", "Z")
    token = secrets.token_urlsafe(36)
    token_sha = _password_reset_token_hash(token)

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        user = connection.execute(
            "SELECT id, email FROM users WHERE username = ? OR email = ?",
            (candidate, candidate),
        ).fetchone()
        if user is None:
            return {"issued": False}

        user_id = int(user["id"])
        user_email = str(user["email"])
        connection.execute(
            "DELETE FROM password_reset_tokens WHERE user_id = ? AND consumed_at IS NULL",
            (user_id,),
        )
        connection.execute(
            """
            INSERT INTO password_reset_tokens (
                created_at,
                user_id,
                token_sha256,
                expires_at,
                consumed_at,
                source_ip,
                user_agent
            ) VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                now_iso,
                user_id,
                token_sha,
                expires_at,
                (source_ip or "").strip()[:80],
                (user_agent or "").strip()[:300],
            ),
        )
        connection.commit()

    return {
        "issued": True,
        "user_id": user_id,
        "user_email": user_email,
        "token": token,
        "expires_at": expires_at,
    }


def _validate_password_reset_token(database_path: Path, token: str) -> dict[str, Any]:
    normalized_token = (token or "").strip()
    if not normalized_token:
        return {"ok": False, "error": "Missing token."}
    token_hash = _password_reset_token_hash(normalized_token)

    now_iso = _now_utc_iso()
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, user_id, expires_at
            FROM password_reset_tokens
            WHERE token_sha256 = ?
              AND consumed_at IS NULL
              AND expires_at > ?
            """,
            (token_hash, now_iso),
        ).fetchone()
    if row is None:
        return {"ok": False, "error": "Reset token is invalid or expired."}
    return {
        "ok": True,
        "token_id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "expires_at": str(row["expires_at"]),
    }


def _reset_password_with_token(
    database_path: Path,
    token: str,
    new_password: str,
) -> dict[str, Any]:
    new_password_error = _validate_password(new_password)
    if new_password_error:
        return {"ok": False, "error": new_password_error, "status_code": 400}

    token_state = _validate_password_reset_token(database_path, token)
    if not bool(token_state.get("ok")):
        return {"ok": False, "error": str(token_state.get("error", "Invalid reset token.")), "status_code": 400}

    user_id = int(token_state["user_id"])
    token_id = int(token_state["token_id"])

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "User account not found.", "status_code": 404}

        connection.execute(
            """
            UPDATE users
            SET password_hash = ?, failed_login_attempts = 0, lockout_until = NULL
            WHERE id = ?
            """,
            (_hash_password(new_password), user_id),
        )
        sessions_cursor = connection.execute(
            """
            UPDATE sessions
            SET revoked_at = ?, revoke_reason = ?
            WHERE user_id = ? AND revoked_at IS NULL
            """,
            (_now_utc_iso(), "password-reset", user_id),
        )
        connection.execute(
            "UPDATE password_reset_tokens SET consumed_at = ? WHERE id = ?",
            (_now_utc_iso(), token_id),
        )
        connection.execute(
            "DELETE FROM password_reset_tokens WHERE user_id = ? AND consumed_at IS NULL",
            (user_id,),
        )
        connection.commit()

    return {"ok": True, "user_id": user_id, "revoked_sessions": int(sessions_cursor.rowcount or 0)}

