from __future__ import annotations

from pathlib import Path

from ..infra import db as sqlite3
from .common import _now_utc_iso


ACCESS_POLICY_DISABLE_LOGIN_NON_ADMIN = "disable_login_non_admin"
ACCESS_POLICY_DISABLE_SIGNUP = "disable_signup"
_ACCESS_POLICY_DEFAULTS: dict[str, bool] = {
    ACCESS_POLICY_DISABLE_LOGIN_NON_ADMIN: False,
    ACCESS_POLICY_DISABLE_SIGNUP: False,
}


def _ensure_access_policies_table(connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS access_policies (
            policy_key VARCHAR(64) PRIMARY KEY,
            policy_value VARCHAR(16) NOT NULL,
            updated_at VARCHAR(40) NOT NULL,
            updated_by_user_id BIGINT NULL,
            FOREIGN KEY (updated_by_user_id) REFERENCES users(id)
        )
        """
    )


def _normalize_access_policy_key(policy_key: str) -> str:
    normalized = str(policy_key or "").strip().lower()
    if normalized not in _ACCESS_POLICY_DEFAULTS:
        raise ValueError(f"Unsupported access policy: {policy_key}")
    return normalized


def _normalize_access_policy_value(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _access_policies(database_path: Path | str) -> dict[str, bool]:
    policies = dict(_ACCESS_POLICY_DEFAULTS)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_access_policies_table(connection)
        rows = connection.execute(
            """
            SELECT policy_key, policy_value
            FROM access_policies
            WHERE policy_key IN (?, ?)
            """,
            (
                ACCESS_POLICY_DISABLE_LOGIN_NON_ADMIN,
                ACCESS_POLICY_DISABLE_SIGNUP,
            ),
        ).fetchall()
    for row in rows:
        key = str(row["policy_key"] or "").strip().lower()
        if key in policies:
            policies[key] = _normalize_access_policy_value(row["policy_value"])
    return policies


def _set_access_policy(
    database_path: Path | str,
    *,
    policy_key: str,
    enabled: bool,
    actor_user_id: int | None,
) -> dict[str, object]:
    normalized_key = _normalize_access_policy_key(policy_key)
    normalized_enabled = bool(enabled)
    updated_at = _now_utc_iso()
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_access_policies_table(connection)
        existing = connection.execute(
            "SELECT policy_value FROM access_policies WHERE policy_key = ?",
            (normalized_key,),
        ).fetchone()
        previous_enabled = (
            _normalize_access_policy_value(existing["policy_value"]) if existing is not None else _ACCESS_POLICY_DEFAULTS[normalized_key]
        )
        if existing is None:
            connection.execute(
                """
                INSERT INTO access_policies (policy_key, policy_value, updated_at, updated_by_user_id)
                VALUES (?, ?, ?, ?)
                """,
                (normalized_key, "1" if normalized_enabled else "0", updated_at, actor_user_id),
            )
        else:
            connection.execute(
                """
                UPDATE access_policies
                SET policy_value = ?, updated_at = ?, updated_by_user_id = ?
                WHERE policy_key = ?
                """,
                ("1" if normalized_enabled else "0", updated_at, actor_user_id, normalized_key),
            )
        connection.commit()
    return {
        "policy_key": normalized_key,
        "enabled": normalized_enabled,
        "previous_enabled": previous_enabled,
        "changed": normalized_enabled != previous_enabled,
        "updated_at": updated_at,
    }


def _effective_registration_mode(configured_mode: str, policies: dict[str, bool]) -> str:
    if bool(policies.get(ACCESS_POLICY_DISABLE_SIGNUP)):
        return "closed"
    normalized = str(configured_mode or "").strip().lower()
    if normalized in {"open", "invite", "closed"}:
        return normalized
    return "open"
