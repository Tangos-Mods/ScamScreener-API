from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from ..infra import db as sqlite3
from ..config.settings import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, TrainingHubSettings
from .common import _authorization_bearer_token, _normalize_user_agent_for_binding, _request_client_ip
from .session_auth_revoke import _revoke_session_by_token


def _current_user_from_request(request: Request, settings: TrainingHubSettings) -> dict[str, Any] | None:
    session_token = str(request.cookies.get(SESSION_COOKIE_NAME, "")).strip()
    if not session_token:
        session_token = _authorization_bearer_token(str(request.headers.get("authorization", "")))
    if not session_token:
        return None

    current_ip = _request_client_ip(request, settings)
    current_user_agent = str(request.headers.get("user-agent", ""))
    resolved = _resolve_user_from_session(
        settings.database_path,
        session_token,
        settings,
        current_ip,
        current_user_agent,
    )
    if resolved is None:
        return None

    request.state.session_id = int(resolved["session_id"])
    return resolved["user"]


def _session_token_hash(session_token: str, secret_key: str = "") -> str:
    token_bytes = (session_token or "").encode("utf-8")
    secret = (secret_key or "").encode("utf-8")
    if secret:
        return hmac.new(secret, token_bytes, hashlib.sha256).hexdigest()
    return hashlib.sha256(token_bytes).hexdigest()


def _create_session(
    database_path: Path,
    user_id: int,
    ttl_minutes: int,
    remote_addr: str,
    user_agent: str,
    secret_key: str,
) -> str:
    session_token = secrets.token_urlsafe(48)
    token_sha = _session_token_hash(session_token, secret_key)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=ttl_minutes)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO sessions (
                created_at,
                user_id,
                token_sha256,
                expires_at,
                revoked_at,
                remote_addr,
                user_agent,
                revoke_reason
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, '')
            """,
            (
                now.isoformat().replace("+00:00", "Z"),
                user_id,
                token_sha,
                expires_at.isoformat().replace("+00:00", "Z"),
                (remote_addr or "").strip(),
                (user_agent or "").strip()[:300],
            ),
        )
        connection.commit()
    return session_token


def _resolve_user_from_session(
    database_path: Path,
    session_token: str,
    settings: TrainingHubSettings,
    current_ip: str,
    current_user_agent: str,
) -> dict[str, Any] | None:
    token_sha = _session_token_hash(session_token, settings.secret_key)
    legacy_token_sha = _session_token_hash(session_token)
    token_hashes = [token_sha] if token_sha == legacy_token_sha else [token_sha, legacy_token_sha]
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        if len(token_hashes) == 1:
            row = connection.execute(
                """
                SELECT
                    s.id AS session_id,
                    s.remote_addr,
                    s.user_agent,
                    u.id AS user_id,
                    u.username,
                    u.email,
                    u.is_admin
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_sha256 = ?
                  AND s.revoked_at IS NULL
                  AND s.expires_at > ?
                """,
                (token_hashes[0], now),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT
                    s.id AS session_id,
                    s.remote_addr,
                    s.user_agent,
                    u.id AS user_id,
                    u.username,
                    u.email,
                    u.is_admin
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_sha256 IN (?, ?)
                  AND s.revoked_at IS NULL
                  AND s.expires_at > ?
                """,
                (token_hashes[0], token_hashes[1], now),
            ).fetchone()
    if row is None:
        return None

    session_remote_addr = str(row["remote_addr"] or "").strip()
    session_user_agent = str(row["user_agent"] or "").strip()

    if settings.session_bind_ip and session_remote_addr and current_ip and session_remote_addr != current_ip:
        _revoke_session_by_token(database_path, session_token, "session-ip-mismatch", settings.secret_key)
        return None

    if settings.session_bind_user_agent:
        expected_agent = _normalize_user_agent_for_binding(session_user_agent)
        actual_agent = _normalize_user_agent_for_binding(current_user_agent)
        if expected_agent and actual_agent and expected_agent != actual_agent:
            _revoke_session_by_token(database_path, session_token, "session-ua-mismatch", settings.secret_key)
            return None

    return {
        "session_id": int(row["session_id"]),
        "user": {
            "id": int(row["user_id"]),
            "username": str(row["username"]),
            "email": str(row["email"]),
            "is_admin": int(row["is_admin"]),
        },
    }


def _refresh_user(database_path: Path, user_id: int) -> dict[str, Any] | None:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, username, email, is_admin FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _set_session_cookie(response: RedirectResponse, settings: TrainingHubSettings, user_id: int, request: Request) -> None:
    remote_addr = _request_client_ip(request, settings)
    user_agent = str(request.headers.get("user-agent", ""))
    token = _create_session(
        settings.database_path,
        user_id=user_id,
        ttl_minutes=settings.session_ttl_minutes,
        remote_addr=remote_addr,
        user_agent=user_agent,
        secret_key=settings.secret_key,
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        secure=settings.enforce_https,
        max_age=settings.session_ttl_minutes * 60,
        path="/",
    )


def _new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _validate_csrf_token(request: Request, submitted_token: str) -> None:
    cookie_token = str(request.cookies.get(CSRF_COOKIE_NAME, "")).strip()
    normalized_submitted = (submitted_token or "").strip()
    if not cookie_token or not normalized_submitted or not hmac.compare_digest(normalized_submitted, cookie_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")

