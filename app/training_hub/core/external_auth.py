from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config.settings import TrainingHubSettings
from ..infra import db as sqlite3
from .common import _normalize_user_agent_for_binding, _now_utc_iso
from .session_auth_password import _hash_password, _normalize_email, _normalize_username

EXTERNAL_AUTH_STATE_TTL_MINUTES = 10
EXTERNAL_AUTH_PROVIDER_GITHUB = "github"
EXTERNAL_AUTH_PROVIDER_AUTHELIA = "authelia"
_GITHUB_ISSUER = "https://github.com"
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


@dataclass(frozen=True)
class ExternalAuthProfile:
    provider: str
    issuer: str
    subject: str
    email: str
    username: str
    display_name: str


def external_auth_provider_options(settings: TrainingHubSettings) -> list[dict[str, str]]:
    providers: list[dict[str, str]] = []
    if settings.github_oauth_enabled:
        providers.append(
            {
                "name": EXTERNAL_AUTH_PROVIDER_GITHUB,
                "label": "Continue with GitHub",
                "description": "Use your approved GitHub account to sign in.",
            }
        )
    if settings.authelia_oidc_enabled:
        providers.append(
            {
                "name": EXTERNAL_AUTH_PROVIDER_AUTHELIA,
                "label": "Continue with Authelia",
                "description": "Use your approved Authelia identity to sign in.",
            }
        )
    return providers


def external_auth_provider_enabled(settings: TrainingHubSettings, provider: str) -> bool:
    normalized = str(provider or "").strip().lower()
    if normalized == EXTERNAL_AUTH_PROVIDER_GITHUB:
        return settings.github_oauth_enabled
    if normalized == EXTERNAL_AUTH_PROVIDER_AUTHELIA:
        return settings.authelia_oidc_enabled
    return False


def external_auth_provider_display_name(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized == EXTERNAL_AUTH_PROVIDER_GITHUB:
        return "GitHub"
    if normalized == EXTERNAL_AUTH_PROVIDER_AUTHELIA:
        return "Authelia"
    return "External provider"


def create_external_auth_redirect(
    settings: TrainingHubSettings,
    *,
    provider: str,
    next_path: str,
    reauth: bool = False,
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    normalized_provider = str(provider or "").strip().lower()
    if not external_auth_provider_enabled(settings, normalized_provider):
        return {"ok": False, "error": "External provider is not configured.", "status_code": 404}

    state = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(24)
    redirect_path = _sanitize_redirect_path(next_path)
    _store_external_auth_state(
        settings,
        provider=normalized_provider,
        state=state,
        code_verifier=code_verifier,
        nonce=nonce,
        redirect_path=redirect_path,
        source_ip=source_ip,
        user_agent=user_agent,
    )

    authorization_url = _authorization_url_for_provider(
        settings,
        provider=normalized_provider,
        state=state,
        code_verifier=code_verifier,
        nonce=nonce,
        reauth=reauth,
    )
    return {"ok": True, "redirect_url": authorization_url}


def complete_external_auth_exchange(
    settings: TrainingHubSettings,
    *,
    provider: str,
    state: str,
    code: str,
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    normalized_provider = str(provider or "").strip().lower()
    flow = _consume_external_auth_state(
        settings,
        provider=normalized_provider,
        state=state,
        user_agent=user_agent,
    )
    if not bool(flow.get("ok")):
        return flow

    if not code.strip():
        return {"ok": False, "error": "Authorization code is missing.", "status_code": 400}

    if normalized_provider == EXTERNAL_AUTH_PROVIDER_GITHUB:
        profile = _github_profile_from_code(
            settings,
            code=code,
            code_verifier=str(flow["code_verifier"]),
        )
    elif normalized_provider == EXTERNAL_AUTH_PROVIDER_AUTHELIA:
        profile = _authelia_profile_from_code(
            settings,
            code=code,
            code_verifier=str(flow["code_verifier"]),
            expected_nonce=str(flow["nonce"]),
        )
    else:
        return {"ok": False, "error": "External provider is not supported.", "status_code": 404}

    allowed_result = _authorize_external_profile(settings, profile)
    if not bool(allowed_result.get("ok")):
        return allowed_result

    return {
        "ok": True,
        "redirect_path": str(flow["redirect_path"]),
        "profile": profile,
    }


def complete_external_auth_callback(
    settings: TrainingHubSettings,
    *,
    provider: str,
    state: str,
    code: str,
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    exchange_result = complete_external_auth_exchange(
        settings,
        provider=provider,
        state=state,
        code=code,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    if not bool(exchange_result.get("ok")):
        return exchange_result

    profile = exchange_result["profile"]
    user_result = _upsert_external_identity_user(settings, profile)
    if not bool(user_result.get("ok")):
        return user_result

    return {
        "ok": True,
        "user_id": int(user_result["user_id"]),
        "redirect_path": str(exchange_result["redirect_path"]),
        "profile": profile,
    }


def _authorization_url_for_provider(
    settings: TrainingHubSettings,
    *,
    provider: str,
    state: str,
    code_verifier: str,
    nonce: str,
    reauth: bool = False,
) -> str:
    redirect_uri = _callback_url(settings, provider)
    challenge = _pkce_code_challenge(code_verifier)

    if provider == EXTERNAL_AUTH_PROVIDER_GITHUB:
        params = {
            "client_id": settings.github_oauth_client_id,
            "redirect_uri": redirect_uri,
            "scope": "read:user user:email",
            "state": state,
            "allow_signup": "false",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return "https://github.com/login/oauth/authorize?" + urlencode(params)

    if provider == EXTERNAL_AUTH_PROVIDER_AUTHELIA:
        discovery = _oidc_discovery_document(settings.authelia_oidc_issuer_url)
        params = {
            "client_id": settings.authelia_oidc_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(settings.authelia_oidc_scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if reauth:
            params["prompt"] = "login"
            params["max_age"] = "0"
        return str(discovery["authorization_endpoint"]) + "?" + urlencode(params)

    raise ValueError(f"Unsupported external auth provider: {provider}")


def _callback_url(settings: TrainingHubSettings, provider: str) -> str:
    return f"{settings.public_base_url}/auth/external/{provider}/callback"


def _sanitize_redirect_path(candidate: str) -> str:
    path = str(candidate or "").strip()
    if not path.startswith("/") or path.startswith("//") or any(char in path for char in "\r\n"):
        return "/dashboard"
    return path[:1024]


def _state_hash(state: str, secret_key: str) -> str:
    return hashlib.sha256(f"{secret_key}:{state}".encode("utf-8")).hexdigest()


def _state_expiry(ttl_minutes: int = EXTERNAL_AUTH_STATE_TTL_MINUTES) -> str:
    now = datetime.now(timezone.utc)
    return (now + timedelta(minutes=max(1, int(ttl_minutes)))).isoformat().replace("+00:00", "Z")


def _pkce_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256((code_verifier or "").encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _store_external_auth_state(
    settings: TrainingHubSettings,
    *,
    provider: str,
    state: str,
    code_verifier: str,
    nonce: str,
    redirect_path: str,
    source_ip: str,
    user_agent: str,
) -> None:
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO external_auth_states (
                created_at,
                provider,
                state_sha256,
                code_verifier,
                nonce,
                redirect_path,
                expires_at,
                consumed_at,
                source_ip,
                user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                _now_utc_iso(),
                provider,
                _state_hash(state, settings.secret_key),
                code_verifier,
                nonce,
                redirect_path,
                _state_expiry(),
                (source_ip or "").strip()[:80],
                (user_agent or "").strip()[:300],
            ),
        )
        connection.commit()


def _consume_external_auth_state(
    settings: TrainingHubSettings,
    *,
    provider: str,
    state: str,
    user_agent: str,
) -> dict[str, Any]:
    now_iso = _now_utc_iso()
    state_hash = _state_hash(state, settings.secret_key)
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, provider, code_verifier, nonce, redirect_path, expires_at, consumed_at, user_agent
            FROM external_auth_states
            WHERE state_sha256 = ?
            """,
            (state_hash,),
        ).fetchone()
        if row is None or row["consumed_at"] is not None:
            return {"ok": False, "error": "Authorization state is invalid or expired.", "status_code": 400}
        if str(row["provider"] or "").strip() != provider:
            connection.execute(
                "UPDATE external_auth_states SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Authorization state is invalid or expired.", "status_code": 400}
        if str(row["expires_at"] or "") <= now_iso:
            connection.execute(
                "UPDATE external_auth_states SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Authorization state has expired.", "status_code": 400}

        expected_user_agent = _normalize_user_agent_for_binding(str(row["user_agent"] or ""))
        actual_user_agent = _normalize_user_agent_for_binding(user_agent)
        if expected_user_agent and actual_user_agent and expected_user_agent != actual_user_agent:
            connection.execute(
                "UPDATE external_auth_states SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Authorization state is invalid for this client.", "status_code": 400}

        connection.execute(
            "UPDATE external_auth_states SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (now_iso, int(row["id"])),
        )
        connection.commit()
    return {
        "ok": True,
        "id": int(row["id"]),
        "code_verifier": str(row["code_verifier"]),
        "nonce": str(row["nonce"]),
        "redirect_path": str(row["redirect_path"] or "/dashboard"),
    }


def _oidc_discovery_document(issuer_url: str) -> dict[str, Any]:
    issuer = str(issuer_url or "").strip().rstrip("/")
    well_known_url = f"{issuer}/.well-known/openid-configuration"
    payload = _http_get_json(well_known_url)
    if str(payload.get("issuer", "")).strip().rstrip("/") != issuer:
        raise ValueError("OIDC discovery issuer did not match the configured Authelia issuer.")
    for required_key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
        if not str(payload.get(required_key, "")).strip():
            raise ValueError(f"OIDC discovery document is missing {required_key}.")
    return payload


def _http_get_json(url: str, *, headers: dict[str, str] | None = None) -> Any:
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=False) as client:
        response = client.get(url, headers=headers)
    _raise_for_http_error(response)
    return response.json()


def _http_post_form_json(
    url: str,
    *,
    data: dict[str, str],
    headers: dict[str, str] | None = None,
    auth: httpx.Auth | None = None,
) -> dict[str, Any]:
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=False) as client:
        response = client.post(url, data=data, headers=headers, auth=auth)
    _raise_for_http_error(response)
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Provider returned an invalid JSON payload.")
    return payload


def _raise_for_http_error(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exception:
        raise ValueError(f"Provider request failed with HTTP {response.status_code}.") from exception


def _github_profile_from_code(
    settings: TrainingHubSettings,
    *,
    code: str,
    code_verifier: str,
) -> ExternalAuthProfile:
    token_payload = _http_post_form_json(
        "https://github.com/login/oauth/access_token",
        data={
            "client_id": settings.github_oauth_client_id,
            "client_secret": settings.github_oauth_client_secret,
            "code": code,
            "redirect_uri": _callback_url(settings, EXTERNAL_AUTH_PROVIDER_GITHUB),
            "code_verifier": code_verifier,
        },
        headers={"Accept": "application/json"},
    )
    access_token = str(token_payload.get("access_token", "")).strip()
    if not access_token:
        raise ValueError("GitHub did not return an access token.")

    auth_headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {access_token}",
    }
    user_payload = _http_get_json("https://api.github.com/user", headers=auth_headers)
    if not isinstance(user_payload, dict):
        raise ValueError("GitHub returned an invalid profile response.")
    emails_payload = _http_get_json("https://api.github.com/user/emails", headers=auth_headers)
    emails = emails_payload if isinstance(emails_payload, list) else []
    verified_email = _github_verified_email(emails)
    subject = str(user_payload.get("id", "")).strip()
    if not subject:
        raise ValueError("GitHub did not return a stable user identifier.")
    username = str(user_payload.get("login", "") or "").strip().lower()
    display_name = str(user_payload.get("name", "") or username or verified_email or "GitHub user").strip()
    return ExternalAuthProfile(
        provider=EXTERNAL_AUTH_PROVIDER_GITHUB,
        issuer=_GITHUB_ISSUER,
        subject=subject,
        email=verified_email,
        username=username,
        display_name=display_name,
    )


def _github_verified_email(payload: list[Any]) -> str:
    if not isinstance(payload, list):
        return ""
    primary_verified = ""
    fallback_verified = ""
    for row in payload:
        if not isinstance(row, dict):
            continue
        email = _normalize_email(str(row.get("email", "")))
        if not email or not bool(row.get("verified")):
            continue
        if bool(row.get("primary")):
            primary_verified = email
            break
        if not fallback_verified:
            fallback_verified = email
    return primary_verified or fallback_verified


def _authelia_profile_from_code(
    settings: TrainingHubSettings,
    *,
    code: str,
    code_verifier: str,
    expected_nonce: str,
) -> ExternalAuthProfile:
    discovery = _oidc_discovery_document(settings.authelia_oidc_issuer_url)
    token_payload = _http_post_form_json(
        str(discovery["token_endpoint"]),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _callback_url(settings, EXTERNAL_AUTH_PROVIDER_AUTHELIA),
            "code_verifier": code_verifier,
        },
        auth=httpx.BasicAuth(settings.authelia_oidc_client_id, settings.authelia_oidc_client_secret),
    )
    access_token = str(token_payload.get("access_token", "")).strip()
    if not access_token:
        raise ValueError("Authelia did not return an access token.")
    if str(token_payload.get("token_type", "Bearer")).strip().lower() != "bearer":
        raise ValueError("Authelia returned an unsupported token type.")

    userinfo_payload = _http_get_json(
        str(discovery["userinfo_endpoint"]),
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if not isinstance(userinfo_payload, dict):
        raise ValueError("Authelia returned an invalid userinfo response.")
    subject = str(userinfo_payload.get("sub", "")).strip()
    if not subject:
        raise ValueError("Authelia did not return a stable user identifier.")

    if "nonce" in userinfo_payload and str(userinfo_payload.get("nonce", "")).strip() != expected_nonce:
        raise ValueError("Authelia userinfo nonce did not match the original login request.")

    email = _normalize_email(str(userinfo_payload.get("email", "")))
    username = str(userinfo_payload.get("preferred_username", "") or userinfo_payload.get("name", "") or "").strip().lower()
    display_name = str(
        userinfo_payload.get("name", "")
        or userinfo_payload.get("preferred_username", "")
        or email
        or subject
    ).strip()
    return ExternalAuthProfile(
        provider=EXTERNAL_AUTH_PROVIDER_AUTHELIA,
        issuer=str(discovery["issuer"]).strip().rstrip("/"),
        subject=subject,
        email=email,
        username=username,
        display_name=display_name,
    )


def _authorize_external_profile(settings: TrainingHubSettings, profile: ExternalAuthProfile) -> dict[str, Any]:
    normalized_email = _normalize_email(profile.email)
    normalized_username = str(profile.username or "").strip().lower()
    subject = str(profile.subject or "").strip()

    if profile.provider == EXTERNAL_AUTH_PROVIDER_GITHUB:
        if (
            subject in settings.github_oauth_allowed_subjects
            or normalized_email in settings.github_oauth_allowed_emails
            or normalized_username in settings.github_oauth_allowed_logins
        ):
            return {"ok": True}
        return {"ok": False, "error": "This GitHub account is not allowed to sign in.", "status_code": 403}

    if profile.provider == EXTERNAL_AUTH_PROVIDER_AUTHELIA:
        if (
            subject in settings.authelia_oidc_allowed_subjects
            or normalized_email in settings.authelia_oidc_allowed_emails
            or normalized_username in settings.authelia_oidc_allowed_usernames
        ):
            return {"ok": True}
        return {"ok": False, "error": "This Authelia account is not allowed to sign in.", "status_code": 403}

    return {"ok": False, "error": "External provider is not supported.", "status_code": 404}


def _upsert_external_identity_user(settings: TrainingHubSettings, profile: ExternalAuthProfile) -> dict[str, Any]:
    normalized_email = _normalize_email(profile.email)
    normalized_username = str(profile.username or "").strip().lower()
    now_iso = _now_utc_iso()

    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        identity_row = connection.execute(
            """
            SELECT ei.user_id, u.id, u.username, u.email, u.is_admin, u.mfa_enabled
            FROM external_identities ei
            JOIN users u ON u.id = ei.user_id
            WHERE ei.provider = ? AND ei.issuer = ? AND ei.subject = ?
            """,
            (profile.provider, profile.issuer, profile.subject),
        ).fetchone()
        if identity_row is not None:
            user_id = int(identity_row["user_id"])
            connection.execute(
                """
                UPDATE external_identities
                SET updated_at = ?, email = ?, username = ?, last_login_at = ?
                WHERE provider = ? AND issuer = ? AND subject = ?
                """,
                (now_iso, normalized_email, normalized_username, now_iso, profile.provider, profile.issuer, profile.subject),
            )
            connection.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (now_iso, user_id),
            )
            connection.commit()
            return {"ok": True, "user_id": user_id}

        if not normalized_email:
            return {
                "ok": False,
                "error": "The external identity did not provide a verified email address for first sign-in.",
                "status_code": 403,
            }

        existing_user = connection.execute(
            "SELECT id, username, email, is_admin, mfa_enabled FROM users WHERE email = ?",
            (normalized_email,),
        ).fetchone()
        if existing_user is None:
            username = _unique_external_username(
                connection,
                preferred_username=normalized_username,
                email=normalized_email,
                provider=profile.provider,
            )
            is_admin = 1 if _should_external_user_be_admin(settings, username, normalized_email, connection) else 0
            cursor = connection.execute(
                """
                INSERT INTO users (created_at, username, email, password_hash, is_admin, mfa_enabled, last_login_at)
                VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    now_iso,
                    username,
                    normalized_email,
                    _hash_password(secrets.token_urlsafe(32)),
                    is_admin,
                    now_iso,
                ),
            )
            user_id = int(cursor.lastrowid)
        else:
            user_id = int(existing_user["id"])
            connection.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now_iso, user_id))

        connection.execute(
            """
            INSERT INTO external_identities (
                created_at,
                updated_at,
                user_id,
                provider,
                issuer,
                subject,
                email,
                username,
                last_login_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso,
                now_iso,
                user_id,
                profile.provider,
                profile.issuer,
                profile.subject,
                normalized_email,
                normalized_username,
                now_iso,
            ),
        )
        connection.commit()
    return {"ok": True, "user_id": user_id}


def lookup_external_identity_user(settings: TrainingHubSettings, profile: ExternalAuthProfile) -> dict[str, Any]:
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT user_id
            FROM external_identities
            WHERE provider = ? AND issuer = ? AND subject = ?
            """,
            (profile.provider, profile.issuer, profile.subject),
        ).fetchone()
    if row is None:
        return {
            "ok": False,
            "error": "The external identity is not linked to a local account.",
            "status_code": 403,
        }
    return {"ok": True, "user_id": int(row["user_id"])}


def external_identities_for_user(settings: TrainingHubSettings, user_id: int) -> list[dict[str, Any]]:
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, provider, issuer, subject, email, username, last_login_at, updated_at
            FROM external_identities
            WHERE user_id = ?
            ORDER BY provider ASC, id ASC
            """,
            (int(user_id),),
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "provider": str(row["provider"]),
            "provider_label": external_auth_provider_display_name(str(row["provider"])),
            "issuer": str(row["issuer"]),
            "subject": str(row["subject"]),
            "email": str(row["email"] or ""),
            "username": str(row["username"] or ""),
            "last_login_at": str(row["last_login_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }
        for row in rows
    ]


def _should_external_user_be_admin(
    settings: TrainingHubSettings,
    username: str,
    email: str,
    connection,
) -> bool:
    if username in settings.admin_usernames or email in settings.admin_emails:
        return True
    admin_count = int(connection.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0])
    return admin_count == 0


def _unique_external_username(
    connection,
    *,
    preferred_username: str,
    email: str,
    provider: str,
) -> str:
    bases = [
        preferred_username,
        email.partition("@")[0],
        f"{provider}-admin",
        f"{provider}-user",
    ]
    for base in bases:
        candidate = _sanitize_external_username(base)
        if candidate:
            available = _first_available_username(connection, candidate)
            if available:
                return available
    return _first_available_username(connection, "user-admin") or "user-admin"


def _sanitize_external_username(value: str) -> str:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return ""
    normalized = re.sub(r"[^a-z0-9_-]+", "-", lowered).strip("-_")
    normalized = normalized[:32]
    if len(normalized) < 3:
        return ""
    return normalized if _normalize_username(normalized) else ""


def _first_available_username(connection, base_username: str) -> str | None:
    candidate = _normalize_username(base_username)
    if candidate and _username_available(connection, candidate):
        return candidate

    trimmed_base = base_username[:24].rstrip("-_") or "user"
    for suffix in range(1, 1000):
        candidate = _normalize_username(f"{trimmed_base}-{suffix}")
        if candidate and _username_available(connection, candidate):
            return candidate
    return None


def _username_available(connection, username: str) -> bool:
    row = connection.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
    return row is None
