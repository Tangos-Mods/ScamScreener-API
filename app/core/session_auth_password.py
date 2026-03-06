from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from pathlib import Path
from typing import Any

from ..infra import db as sqlite3


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 210_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    parts = encoded.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False

    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected_digest = bytes.fromhex(parts[3])
    except ValueError:
        return False

    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected_digest)


def _normalize_username(value: str) -> str:
    normalized = (value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{3,32}", normalized):
        return ""
    return normalized


def _normalize_email(value: str) -> str:
    normalized = (value or "").strip().lower()
    if len(normalized) > 254 or "@" not in normalized:
        return ""
    local, _, domain = normalized.partition("@")
    if not local or not domain or "." not in domain:
        return ""
    return normalized


def _validate_password(password: str) -> str:
    if password is None:
        return "Password is required."
    if len(password) < 8:
        return "Password must have at least 8 characters."
    if len(password) > 128:
        return "Password must be at most 128 characters."
    return ""


def _change_user_password(
    database_path: Path,
    user_id: int,
    current_password: str,
    new_password: str,
) -> dict[str, Any]:
    new_password_error = _validate_password(new_password)
    if new_password_error:
        return {"ok": False, "error": new_password_error, "status_code": 400}

    if (current_password or "") == (new_password or ""):
        return {
            "ok": False,
            "error": "New password must be different from current password.",
            "status_code": 400,
        }

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, password_hash FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "User not found.", "status_code": 404}

        current_hash = str(row["password_hash"])
        if not _verify_password(current_password, current_hash):
            return {"ok": False, "error": "Current password is incorrect.", "status_code": 401}

        connection.execute(
            """
            UPDATE users
            SET password_hash = ?, failed_login_attempts = 0, lockout_until = NULL
            WHERE id = ?
            """,
            (_hash_password(new_password), int(user_id)),
        )
        connection.commit()
    return {"ok": True}

