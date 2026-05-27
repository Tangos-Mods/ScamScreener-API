from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
import qrcode
import qrcode.image.svg
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialHint,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..config.settings import TrainingHubSettings
from ..infra import db as sqlite3
from .common import _normalize_user_agent_for_binding, _now_utc_iso

logger = logging.getLogger(__name__)

LOGIN_CHALLENGE_COOKIE_NAME = "training_hub_login_challenge"
TOTP_ENROLLMENT_TTL_MINUTES = 15
WEBAUTHN_FLOW_TTL_MINUTES = 15
TOTP_DIGITS = 6
TOTP_PERIOD_SECONDS = 30
TOTP_SKEW_STEPS = 2
BACKUP_CODE_COUNT = 10
_BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_BACKUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _mfa_hash(value: str, secret_key: str = "") -> str:
    payload = (value or "").encode("utf-8")
    secret = (secret_key or "").encode("utf-8")
    if secret:
        return hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def _candidate_hashes(value: str, secret_key: str) -> list[str]:
    preferred = _mfa_hash(value, secret_key)
    legacy = _mfa_hash(value)
    if preferred == legacy:
        return [preferred]
    return [preferred, legacy]


def _fernet(secret_key: str) -> Fernet:
    key_material = hashlib.sha256((secret_key or "").encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key_material))


def _encrypt_value(value: str, secret_key: str) -> str:
    return _fernet(secret_key).encrypt((value or "").encode("utf-8")).decode("utf-8")


def _decrypt_value(value: str, secret_key: str) -> str:
    try:
        return _fernet(secret_key).decrypt((value or "").encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError) as exception:
        raise ValueError("Stored MFA secret could not be decrypted.") from exception


def _urlsafe_b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    normalized = (value or "").strip()
    padding = "=" * (-len(normalized) % 4)
    return base64.urlsafe_b64decode((normalized + padding).encode("ascii"))


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _json_loads(payload: str) -> dict[str, Any]:
    try:
        loaded = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _normalize_webauthn_origin(origin: str) -> str:
    return (origin or "").strip().rstrip("/")


def _resolve_webauthn_rp_id(settings: TrainingHubSettings, requested_rp_id: str = "") -> str:
    candidate = (requested_rp_id or "").strip().lower()
    if candidate:
        return candidate
    return str(settings.webauthn_rp_id or "").strip().lower()


def _resolve_webauthn_origins(
    settings: TrainingHubSettings,
    requested_origin: str = "",
) -> list[str]:
    normalized_requested = _normalize_webauthn_origin(requested_origin)
    if normalized_requested:
        return [normalized_requested]
    return [_normalize_webauthn_origin(origin) for origin in settings.webauthn_origins if _normalize_webauthn_origin(origin)]


def _webauthn_flow_rp_id(settings: TrainingHubSettings, payload: dict[str, Any]) -> str:
    return _resolve_webauthn_rp_id(settings, str((payload or {}).get("rp_id", "")))


def _webauthn_flow_origins(settings: TrainingHubSettings, payload: dict[str, Any]) -> list[str]:
    flow_payload = payload or {}
    expected_origins = flow_payload.get("expected_origins")
    if isinstance(expected_origins, list):
        normalized = [_normalize_webauthn_origin(str(origin)) for origin in expected_origins]
        filtered = [origin for origin in normalized if origin]
        if filtered:
            return filtered
    legacy_origin = _normalize_webauthn_origin(str(flow_payload.get("expected_origin", "")))
    if legacy_origin:
        return [legacy_origin]
    return _resolve_webauthn_origins(settings)


def _challenge_expiry(ttl_minutes: int) -> str:
    now = datetime.now(timezone.utc)
    return (now + timedelta(minutes=max(1, int(ttl_minutes)))).isoformat().replace("+00:00", "Z")


def _create_auth_flow(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    flow_type: str,
    payload: dict[str, Any],
    ttl_minutes: int,
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    token = secrets.token_urlsafe(40)
    token_sha = _mfa_hash(token, settings.secret_key)
    created_at = _now_utc_iso()
    expires_at = _challenge_expiry(ttl_minutes)
    with sqlite3.connect(settings.database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO auth_flow_tokens (
                created_at,
                user_id,
                flow_type,
                token_sha256,
                payload_json,
                expires_at,
                consumed_at,
                failed_attempts,
                source_ip,
                user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
            """,
            (
                created_at,
                int(user_id),
                str(flow_type or "").strip(),
                token_sha,
                _json_dumps(payload),
                expires_at,
                (source_ip or "").strip()[:80],
                (user_agent or "").strip()[:300],
            ),
        )
        connection.commit()
    return {"id": int(cursor.lastrowid), "token": token, "expires_at": expires_at, "payload": dict(payload)}


def _load_auth_flow(
    settings: TrainingHubSettings,
    *,
    token: str,
    flow_type: str | None = None,
    source_ip: str = "",
    user_agent: str = "",
    max_attempts: int | None = None,
) -> dict[str, Any]:
    normalized_token = (token or "").strip()
    if not normalized_token:
        return {"ok": False, "error": "Missing authentication flow.", "status_code": 400}

    token_hashes = _candidate_hashes(normalized_token, settings.secret_key)
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        if len(token_hashes) == 1:
            row = connection.execute(
                """
                SELECT id, user_id, flow_type, payload_json, expires_at, consumed_at, failed_attempts, source_ip, user_agent
                FROM auth_flow_tokens
                WHERE token_sha256 = ?
                """,
                (token_hashes[0],),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT id, user_id, flow_type, payload_json, expires_at, consumed_at, failed_attempts, source_ip, user_agent
                FROM auth_flow_tokens
                WHERE token_sha256 IN (?, ?)
                """,
                (token_hashes[0], token_hashes[1]),
            ).fetchone()
        if row is None or row["consumed_at"] is not None:
            return {"ok": False, "error": "Authentication flow is invalid or expired.", "status_code": 400}
        if flow_type and str(row["flow_type"] or "").strip() != str(flow_type):
            return {"ok": False, "error": "Authentication flow is invalid or expired.", "status_code": 400}
        now_iso = _now_utc_iso()
        if str(row["expires_at"] or "") <= now_iso:
            connection.execute(
                "UPDATE auth_flow_tokens SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Authentication flow has expired.", "status_code": 400}
        if max_attempts is not None and int(row["failed_attempts"] or 0) >= int(max_attempts):
            connection.execute(
                "UPDATE auth_flow_tokens SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Authentication flow has expired.", "status_code": 400}

        expected_ip = str(row["source_ip"] or "").strip()
        if expected_ip and source_ip and expected_ip != source_ip:
            connection.execute(
                "UPDATE auth_flow_tokens SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Authentication flow is invalid for this client.", "status_code": 400}

        expected_ua = _normalize_user_agent_for_binding(str(row["user_agent"] or ""))
        actual_ua = _normalize_user_agent_for_binding(user_agent)
        if expected_ua and actual_ua and expected_ua != actual_ua:
            connection.execute(
                "UPDATE auth_flow_tokens SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now_iso, int(row["id"])),
            )
            connection.commit()
            return {"ok": False, "error": "Authentication flow is invalid for this client.", "status_code": 400}

    return {
        "ok": True,
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "flow_type": str(row["flow_type"]),
        "payload": _json_loads(str(row["payload_json"] or "{}")),
        "expires_at": str(row["expires_at"]),
        "failed_attempts": int(row["failed_attempts"] or 0),
    }


def _consume_auth_flow_by_id(settings: TrainingHubSettings, flow_id: int) -> None:
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "UPDATE auth_flow_tokens SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (_now_utc_iso(), int(flow_id)),
        )
        connection.commit()


def _increment_auth_flow_failures(settings: TrainingHubSettings, flow_id: int) -> None:
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            """
            UPDATE auth_flow_tokens
            SET failed_attempts = failed_attempts + 1
            WHERE id = ? AND consumed_at IS NULL
            """,
            (int(flow_id),),
        )
        connection.commit()


def _load_auth_flow_by_id(settings: TrainingHubSettings, flow_id: int) -> dict[str, Any] | None:
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, user_id, flow_type, payload_json, expires_at, consumed_at, failed_attempts
            FROM auth_flow_tokens
            WHERE id = ?
            """,
            (int(flow_id),),
        ).fetchone()
    if row is None or row["consumed_at"] is not None:
        return None
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "flow_type": str(row["flow_type"]),
        "payload": _json_loads(str(row["payload_json"] or "{}")),
        "expires_at": str(row["expires_at"]),
        "failed_attempts": int(row["failed_attempts"] or 0),
    }


def _set_user_mfa_enabled(connection, user_id: int, enabled: bool) -> None:
    connection.execute(
        "UPDATE users SET mfa_enabled = ? WHERE id = ?",
        (1 if enabled else 0, int(user_id)),
    )


def _user_factor_counts(connection, user_id: int) -> dict[str, int]:
    totp_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM user_totp_factors WHERE user_id = ? AND verified_at IS NOT NULL",
            (int(user_id),),
        ).fetchone()[0]
    )
    passkey_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM user_passkeys WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()[0]
    )
    backup_code_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM user_backup_codes WHERE user_id = ? AND consumed_at IS NULL",
            (int(user_id),),
        ).fetchone()[0]
    )
    return {
        "totp": totp_count,
        "passkeys": passkey_count,
        "backup_codes": backup_code_count,
        "standard": totp_count + passkey_count,
    }


def _mfa_state(settings: TrainingHubSettings, user_id: int) -> dict[str, Any]:
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        user_row = connection.execute(
            "SELECT id, username, email, is_admin, mfa_enabled FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if user_row is None:
            return {
                "user_found": False,
                "mfa_enabled": False,
                "standard_factor_count": 0,
                "totp_factor_count": 0,
                "passkey_count": 0,
                "backup_code_count": 0,
                "admin_setup_required": False,
                "email_bridge_allowed": False,
                "mfa_required_for_login": False,
                "can_disable_mfa": False,
                "is_admin": False,
            }
        counts = _user_factor_counts(connection, int(user_id))

    is_admin = int(user_row["is_admin"]) == 1
    stored_enabled = int(user_row["mfa_enabled"] or 0) == 1
    standard_factor_count = int(counts["standard"])
    admin_setup_required = bool(is_admin and settings.admin_mfa_required and standard_factor_count == 0)
    mfa_enabled = bool(stored_enabled or admin_setup_required)
    mfa_required_for_login = bool((stored_enabled and standard_factor_count > 0) or admin_setup_required or (is_admin and settings.admin_mfa_required and standard_factor_count > 0))
    return {
        "user_found": True,
        "user_id": int(user_row["id"]),
        "username": str(user_row["username"]),
        "email": str(user_row["email"]),
        "is_admin": is_admin,
        "mfa_enabled": mfa_enabled,
        "totp_factor_count": int(counts["totp"]),
        "passkey_count": int(counts["passkeys"]),
        "backup_code_count": int(counts["backup_codes"]),
        "standard_factor_count": standard_factor_count,
        "admin_setup_required": admin_setup_required,
        "email_bridge_allowed": admin_setup_required,
        "mfa_required_for_login": mfa_required_for_login,
        "can_disable_mfa": bool(stored_enabled and not is_admin),
        "stored_mfa_enabled": stored_enabled,
    }


def _user_requires_admin_mfa_setup(settings: TrainingHubSettings, user_id: int) -> bool:
    return bool(_mfa_state(settings, int(user_id)).get("admin_setup_required"))


def _normalize_totp_code(code: str) -> str:
    normalized = re.sub(r"\s+", "", (code or "").strip())
    if not re.fullmatch(r"[0-9]{6}", normalized):
        return ""
    return normalized


def _normalize_backup_code(code: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "", (code or "").strip()).upper()
    if len(normalized) < 6:
        return ""
    return normalized


def _generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _base32_decode(secret: str) -> bytes:
    normalized = re.sub(r"[^A-Z2-7]", "", (secret or "").strip().upper())
    if not normalized:
        raise ValueError("TOTP secret is invalid.")
    padding = "=" * (-len(normalized) % 8)
    return base64.b32decode(normalized + padding, casefold=True)


def _totp_at(secret: str, timestamp: int) -> str:
    counter = int(timestamp // TOTP_PERIOD_SECONDS)
    counter_bytes = counter.to_bytes(8, byteorder="big", signed=False)
    digest = hmac.new(_base32_decode(secret), counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = int.from_bytes(digest[offset : offset + 4], byteorder="big") & 0x7FFFFFFF
    return str(code % (10**TOTP_DIGITS)).zfill(TOTP_DIGITS)


def _verify_totp_secret(
    secret: str,
    submitted_code: str,
    now: datetime | None = None,
    *,
    skew_steps: int = TOTP_SKEW_STEPS,
) -> bool:
    normalized_code = _normalize_totp_code(submitted_code)
    if not normalized_code:
        return False
    instant = now or datetime.now(timezone.utc)
    timestamp = int(instant.timestamp())
    bounded_skew_steps = max(0, int(skew_steps))
    for step_offset in range(-bounded_skew_steps, bounded_skew_steps + 1):
        if hmac.compare_digest(_totp_at(secret, timestamp + (step_offset * TOTP_PERIOD_SECONDS)), normalized_code):
            return True
    return False


def _totp_otpauth_uri(secret: str, account_name: str, issuer: str = "ScamScreener") -> str:
    label = quote(f"{issuer}:{account_name}")
    issuer_param = quote(issuer)
    secret_param = quote(secret)
    return f"otpauth://totp/{label}?secret={secret_param}&issuer={issuer_param}&algorithm=SHA1&digits=6&period=30"


def _totp_qr_code_svg_data_uri(otpauth_uri: str) -> str:
    qr_image = qrcode.make(
        otpauth_uri,
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=8,
        border=4,
    )
    buffer = io.BytesIO()
    qr_image.save(buffer)
    svg_bytes = buffer.getvalue()
    return f"data:image/svg+xml;base64,{base64.b64encode(svg_bytes).decode('ascii')}"


def _user_totp_factors(settings: TrainingHubSettings, user_id: int) -> list[dict[str, Any]]:
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, created_at, label, verified_at, last_used_at
            FROM user_totp_factors
            WHERE user_id = ? AND verified_at IS NOT NULL
            ORDER BY created_at ASC, id ASC
            """,
            (int(user_id),),
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "created_at": str(row["created_at"]),
            "label": str(row["label"] or ""),
            "verified_at": str(row["verified_at"] or ""),
            "last_used_at": str(row["last_used_at"] or ""),
        }
        for row in rows
    ]


def _user_passkeys(settings: TrainingHubSettings, user_id: int) -> list[dict[str, Any]]:
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, created_at, label, credential_id, last_used_at, aaguid, credential_device_type, backed_up
            FROM user_passkeys
            WHERE user_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (int(user_id),),
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "created_at": str(row["created_at"]),
            "label": str(row["label"] or ""),
            "credential_id": str(row["credential_id"]),
            "last_used_at": str(row["last_used_at"] or ""),
            "aaguid": str(row["aaguid"] or ""),
            "credential_device_type": str(row["credential_device_type"] or ""),
            "backed_up": int(row["backed_up"] or 0) == 1,
        }
        for row in rows
    ]


def _first_passkey_user_id(settings: TrainingHubSettings) -> int | None:
    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT user_id FROM user_passkeys ORDER BY id ASC LIMIT 1",
        ).fetchone()
    if row is None:
        return None
    return int(row[0])


def _create_login_challenge(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    state = _mfa_state(settings, int(user_id))
    allow_email_bridge = bool(state.get("email_bridge_allowed"))
    payload: dict[str, Any] = {"allow_email_bridge": allow_email_bridge}
    if allow_email_bridge:
        email_code = f"{secrets.randbelow(1_000_000):06d}"
        payload["email_code_sha256"] = _mfa_hash(email_code, settings.secret_key)
        flow = _create_auth_flow(
            settings,
            user_id=int(user_id),
            flow_type="login-mfa",
            payload=payload,
            ttl_minutes=settings.admin_mfa_ttl_minutes,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        flow["email_code"] = email_code
        flow["allow_email_bridge"] = True
        return flow

    flow = _create_auth_flow(
        settings,
        user_id=int(user_id),
        flow_type="login-mfa",
        payload=payload,
        ttl_minutes=settings.admin_mfa_ttl_minutes,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    flow["allow_email_bridge"] = False
    return flow


def _validate_login_challenge(
    settings: TrainingHubSettings,
    *,
    token: str,
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    flow = _load_auth_flow(
        settings,
        token=token,
        flow_type="login-mfa",
        source_ip=source_ip,
        user_agent=user_agent,
        max_attempts=settings.admin_mfa_max_attempts,
    )
    if not bool(flow.get("ok")):
        return flow
    state = _mfa_state(settings, int(flow["user_id"]))
    flow["state"] = state
    return flow


def _complete_login_challenge_with_code(
    settings: TrainingHubSettings,
    *,
    token: str,
    code: str,
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    flow = _validate_login_challenge(settings, token=token, source_ip=source_ip, user_agent=user_agent)
    if not bool(flow.get("ok")):
        return flow

    flow_id = int(flow["id"])
    user_id = int(flow["user_id"])
    payload = dict(flow["payload"])
    state = dict(flow["state"])
    normalized_totp = _normalize_totp_code(code)
    normalized_backup = _normalize_backup_code(code)

    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        if bool(payload.get("allow_email_bridge")) and bool(state.get("admin_setup_required")):
            expected_email_hash = str(payload.get("email_code_sha256", ""))
            if not normalized_totp or not expected_email_hash or not hmac.compare_digest(
                _mfa_hash(normalized_totp, settings.secret_key),
                expected_email_hash,
            ):
                _increment_auth_flow_failures(settings, flow_id)
                return {"ok": False, "error": "Invalid verification code.", "status_code": 401}
            _consume_auth_flow_by_id(settings, flow_id)
            return {"ok": True, "user_id": user_id, "method": "email-bridge", "admin_setup_required": True}

        if normalized_totp:
            totp_rows = connection.execute(
                """
                SELECT id, encrypted_secret
                FROM user_totp_factors
                WHERE user_id = ? AND verified_at IS NOT NULL
                ORDER BY id ASC
                """,
                (user_id,),
            ).fetchall()
            for row in totp_rows:
                secret = _decrypt_value(str(row["encrypted_secret"]), settings.secret_key)
                if _verify_totp_secret(secret, normalized_totp, skew_steps=settings.totp_skew_steps):
                    connection.execute(
                        "UPDATE user_totp_factors SET last_used_at = ? WHERE id = ?",
                        (_now_utc_iso(), int(row["id"])),
                    )
                    connection.commit()
                    _consume_auth_flow_by_id(settings, flow_id)
                    return {"ok": True, "user_id": user_id, "method": "totp", "admin_setup_required": False}

        if normalized_backup:
            backup_rows = connection.execute(
                """
                SELECT id, code_sha256
                FROM user_backup_codes
                WHERE user_id = ? AND consumed_at IS NULL
                ORDER BY id ASC
                """,
                (user_id,),
            ).fetchall()
            submitted_hash = _mfa_hash(normalized_backup, settings.secret_key)
            for row in backup_rows:
                if hmac.compare_digest(str(row["code_sha256"]), submitted_hash):
                    connection.execute(
                        "UPDATE user_backup_codes SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                        (_now_utc_iso(), int(row["id"])),
                    )
                    connection.commit()
                    _consume_auth_flow_by_id(settings, flow_id)
                    return {"ok": True, "user_id": user_id, "method": "backup-code", "admin_setup_required": False}

    _increment_auth_flow_failures(settings, flow_id)
    return {"ok": False, "error": "Invalid verification code.", "status_code": 401}


def _verify_user_step_up_code(settings: TrainingHubSettings, *, user_id: int, code: str) -> dict[str, Any]:
    normalized_totp = _normalize_totp_code(code)
    normalized_backup = _normalize_backup_code(code)

    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        if normalized_totp:
            totp_rows = connection.execute(
                """
                SELECT id, encrypted_secret
                FROM user_totp_factors
                WHERE user_id = ? AND verified_at IS NOT NULL
                ORDER BY id ASC
                """,
                (int(user_id),),
            ).fetchall()
            for row in totp_rows:
                secret = _decrypt_value(str(row["encrypted_secret"]), settings.secret_key)
                if _verify_totp_secret(secret, normalized_totp, skew_steps=settings.totp_skew_steps):
                    connection.execute(
                        "UPDATE user_totp_factors SET last_used_at = ? WHERE id = ?",
                        (_now_utc_iso(), int(row["id"])),
                    )
                    connection.commit()
                    return {"ok": True, "method": "totp"}

        if normalized_backup:
            backup_rows = connection.execute(
                """
                SELECT id, code_sha256
                FROM user_backup_codes
                WHERE user_id = ? AND consumed_at IS NULL
                ORDER BY id ASC
                """,
                (int(user_id),),
            ).fetchall()
            submitted_hash = _mfa_hash(normalized_backup, settings.secret_key)
            for row in backup_rows:
                if hmac.compare_digest(str(row["code_sha256"]), submitted_hash):
                    connection.execute(
                        "UPDATE user_backup_codes SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                        (_now_utc_iso(), int(row["id"])),
                    )
                    connection.commit()
                    return {"ok": True, "method": "backup-code"}

    return {"ok": False, "error": "Invalid verification code.", "status_code": 401}


def _create_totp_enrollment(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    label: str,
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    secret = _generate_totp_secret()
    normalized_label = (label or "").strip()[:80] or "Authenticator App"
    flow = _create_auth_flow(
        settings,
        user_id=int(user_id),
        flow_type="totp-enrollment",
        payload={
            "label": normalized_label,
            "encrypted_secret": _encrypt_value(secret, settings.secret_key),
        },
        ttl_minutes=TOTP_ENROLLMENT_TTL_MINUTES,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    state = _mfa_state(settings, int(user_id))
    flow["secret"] = secret
    flow["label"] = normalized_label
    flow["otpauth_uri"] = _totp_otpauth_uri(secret, str(state.get("email") or state.get("username") or f"user-{user_id}"))
    flow["qr_code_svg_data_uri"] = _totp_qr_code_svg_data_uri(str(flow["otpauth_uri"]))
    return flow


def _verify_totp_enrollment(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    enrollment_token: str,
    code: str,
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    flow = _load_auth_flow(
        settings,
        token=enrollment_token,
        flow_type="totp-enrollment",
        source_ip=source_ip,
        user_agent=user_agent,
        max_attempts=settings.admin_mfa_max_attempts,
    )
    if not bool(flow.get("ok")):
        return flow
    if int(flow["user_id"]) != int(user_id):
        return {"ok": False, "error": "TOTP enrollment is invalid or expired.", "status_code": 403}
    secret = _decrypt_value(str(flow["payload"].get("encrypted_secret", "")), settings.secret_key)
    if not _verify_totp_secret(secret, code, skew_steps=settings.totp_skew_steps):
        _increment_auth_flow_failures(settings, int(flow["id"]))
        return {"ok": False, "error": "Invalid authenticator code.", "status_code": 401}

    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO user_totp_factors (
                created_at,
                user_id,
                label,
                encrypted_secret,
                verified_at,
                last_used_at
            ) VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                _now_utc_iso(),
                int(user_id),
                str(flow["payload"].get("label", "Authenticator App"))[:80],
                str(flow["payload"].get("encrypted_secret", "")),
                _now_utc_iso(),
            ),
        )
        _set_user_mfa_enabled(connection, int(user_id), True)
        connection.commit()
    _consume_auth_flow_by_id(settings, int(flow["id"]))
    return {"ok": True, "label": str(flow["payload"].get("label", "Authenticator App"))}


def _pending_totp_enrollment(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    enrollment_token: str,
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    flow = _load_auth_flow(
        settings,
        token=enrollment_token,
        flow_type="totp-enrollment",
        source_ip=source_ip,
        user_agent=user_agent,
        max_attempts=settings.admin_mfa_max_attempts,
    )
    if not bool(flow.get("ok")):
        return flow
    if int(flow["user_id"]) != int(user_id):
        return {"ok": False, "error": "TOTP enrollment is invalid or expired.", "status_code": 403}
    state = _mfa_state(settings, int(user_id))
    secret = _decrypt_value(str(flow["payload"].get("encrypted_secret", "")), settings.secret_key)
    return {
        "ok": True,
        "token": enrollment_token,
        "label": str(flow["payload"].get("label", "Authenticator App")),
        "secret": secret,
        "otpauth_uri": _totp_otpauth_uri(secret, str(state.get("email") or state.get("username") or f"user-{user_id}")),
        "qr_code_svg_data_uri": _totp_qr_code_svg_data_uri(
            _totp_otpauth_uri(secret, str(state.get("email") or state.get("username") or f"user-{user_id}"))
        ),
        "expires_at": str(flow.get("expires_at", "")),
    }


def _generate_backup_code() -> str:
    raw = "".join(secrets.choice(_BACKUP_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _regenerate_backup_codes(settings: TrainingHubSettings, user_id: int) -> dict[str, Any]:
    state = _mfa_state(settings, int(user_id))
    if int(state.get("standard_factor_count", 0)) <= 0:
        return {"ok": False, "error": "Set up TOTP or a passkey before generating backup codes.", "status_code": 409}

    codes = [_generate_backup_code() for _ in range(BACKUP_CODE_COUNT)]
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("DELETE FROM user_backup_codes WHERE user_id = ?", (int(user_id),))
        now = _now_utc_iso()
        for code in codes:
            connection.execute(
                """
                INSERT INTO user_backup_codes (
                    created_at,
                    user_id,
                    code_sha256,
                    consumed_at
                ) VALUES (?, ?, ?, NULL)
                """,
                (now, int(user_id), _mfa_hash(_normalize_backup_code(code), settings.secret_key)),
            )
        _set_user_mfa_enabled(connection, int(user_id), True)
        connection.commit()
    return {"ok": True, "codes": codes}


def _remove_totp_factor(settings: TrainingHubSettings, user_id: int, factor_id: int) -> dict[str, Any]:
    state = _mfa_state(settings, int(user_id))
    if int(state.get("standard_factor_count", 0)) <= 1:
        if bool(state.get("is_admin")) and settings.admin_mfa_required:
            return {"ok": False, "error": "Admin accounts must keep at least one MFA method.", "status_code": 409}
        return {"ok": False, "error": "Disable MFA before removing your last MFA method.", "status_code": 409}

    with sqlite3.connect(settings.database_path) as connection:
        cursor = connection.execute(
            "DELETE FROM user_totp_factors WHERE id = ? AND user_id = ?",
            (int(factor_id), int(user_id)),
        )
        connection.commit()
    if int(cursor.rowcount or 0) != 1:
        return {"ok": False, "error": "Authenticator app entry not found.", "status_code": 404}
    return {"ok": True}


def _remove_passkey(settings: TrainingHubSettings, user_id: int, passkey_id: int) -> dict[str, Any]:
    state = _mfa_state(settings, int(user_id))
    if int(state.get("standard_factor_count", 0)) <= 1:
        if bool(state.get("is_admin")) and settings.admin_mfa_required:
            return {"ok": False, "error": "Admin accounts must keep at least one MFA method.", "status_code": 409}
        return {"ok": False, "error": "Disable MFA before removing your last MFA method.", "status_code": 409}

    with sqlite3.connect(settings.database_path) as connection:
        cursor = connection.execute(
            "DELETE FROM user_passkeys WHERE id = ? AND user_id = ?",
            (int(passkey_id), int(user_id)),
        )
        connection.commit()
    if int(cursor.rowcount or 0) != 1:
        return {"ok": False, "error": "Passkey not found.", "status_code": 404}
    return {"ok": True}


def _disable_user_mfa(settings: TrainingHubSettings, user_id: int) -> dict[str, Any]:
    state = _mfa_state(settings, int(user_id))
    if bool(state.get("is_admin")) and settings.admin_mfa_required:
        return {"ok": False, "error": "Admin accounts cannot disable MFA.", "status_code": 409}
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("DELETE FROM user_totp_factors WHERE user_id = ?", (int(user_id),))
        connection.execute("DELETE FROM user_passkeys WHERE user_id = ?", (int(user_id),))
        connection.execute("DELETE FROM user_backup_codes WHERE user_id = ?", (int(user_id),))
        _set_user_mfa_enabled(connection, int(user_id), False)
        connection.commit()
    return {"ok": True}


def _resolve_user_by_identifier(database_path: Path | str, identifier: str) -> dict[str, Any] | None:
    candidate = (identifier or "").strip().lower()
    if not candidate:
        return None
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, username, email, is_admin, mfa_enabled FROM users WHERE username = ? OR email = ?",
            (candidate, candidate),
        ).fetchone()
    return dict(row) if row is not None else None


def _generate_passkey_registration_options(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    label: str,
    rp_id: str = "",
    expected_origin: str = "",
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    state = _mfa_state(settings, int(user_id))
    effective_rp_id = _resolve_webauthn_rp_id(settings, rp_id)
    effective_origins = _resolve_webauthn_origins(settings, expected_origin)
    exclude_credentials = [
        PublicKeyCredentialDescriptor(id=_urlsafe_b64decode(row["credential_id"]))
        for row in _user_passkeys(settings, int(user_id))
    ]
    options = generate_registration_options(
        rp_id=effective_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_name=str(state.get("username") or f"user-{user_id}"),
        user_id=str(user_id).encode("utf-8"),
        user_display_name=str(state.get("email") or state.get("username") or f"user-{user_id}"),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=exclude_credentials,
        hints=[
            PublicKeyCredentialHint.CLIENT_DEVICE,
            PublicKeyCredentialHint.HYBRID,
            PublicKeyCredentialHint.SECURITY_KEY,
        ],
    )
    payload = {
        "challenge_b64": _urlsafe_b64encode(options.challenge),
        "label": (label or "").strip()[:80] or "Passkey",
        "rp_id": effective_rp_id,
        "expected_origins": effective_origins,
    }
    flow = _create_auth_flow(
        settings,
        user_id=int(user_id),
        flow_type="webauthn-register",
        payload=payload,
        ttl_minutes=WEBAUTHN_FLOW_TTL_MINUTES,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    flow["options_json"] = options_to_json(options)
    return flow


def _verify_passkey_registration(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    flow_token: str,
    credential: dict[str, Any],
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    flow = _load_auth_flow(
        settings,
        token=flow_token,
        flow_type="webauthn-register",
        source_ip=source_ip,
        user_agent=user_agent,
    )
    if not bool(flow.get("ok")):
        return flow
    if int(flow["user_id"]) != int(user_id):
        return {"ok": False, "error": "Passkey registration is invalid or expired.", "status_code": 403}

    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=_urlsafe_b64decode(str(flow["payload"].get("challenge_b64", ""))),
            expected_rp_id=_webauthn_flow_rp_id(settings, flow["payload"]),
            expected_origin=_webauthn_flow_origins(settings, flow["payload"]),
            require_user_verification=False,
        )
    except WebAuthnException as exception:
        logger.warning(
            "Passkey registration verification rejected for user_id=%s rp_id=%s origins=%s: %s",
            int(user_id),
            _webauthn_flow_rp_id(settings, flow["payload"]),
            _webauthn_flow_origins(settings, flow["payload"]),
            exception,
        )
        return {"ok": False, "error": "Passkey registration could not be verified.", "status_code": 400}
    credential_id = _urlsafe_b64encode(verified.credential_id)
    with sqlite3.connect(settings.database_path) as connection:
        existing = connection.execute(
            "SELECT id FROM user_passkeys WHERE credential_id = ?",
            (credential_id,),
        ).fetchone()
        if existing is not None:
            return {"ok": False, "error": "This passkey is already registered.", "status_code": 409}
        connection.execute(
            """
            INSERT INTO user_passkeys (
                created_at,
                user_id,
                label,
                credential_id,
                public_key,
                sign_count,
                aaguid,
                credential_device_type,
                backed_up,
                last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                _now_utc_iso(),
                int(user_id),
                str(flow["payload"].get("label", "Passkey")),
                credential_id,
                _urlsafe_b64encode(verified.credential_public_key),
                int(verified.sign_count),
                str(verified.aaguid or ""),
                str(getattr(verified.credential_device_type, "value", verified.credential_device_type)),
                1 if bool(verified.credential_backed_up) else 0,
            ),
        )
        _set_user_mfa_enabled(connection, int(user_id), True)
        connection.commit()
    _consume_auth_flow_by_id(settings, int(flow["id"]))
    return {"ok": True, "label": str(flow["payload"].get("label", "Passkey"))}


def _generate_passkey_auth_options(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    purpose: str,
    discoverable: bool = False,
    rp_id: str = "",
    expected_origin: str = "",
    source_ip: str = "",
    user_agent: str = "",
    login_flow_id: int | None = None,
) -> dict[str, Any]:
    passkeys = _user_passkeys(settings, int(user_id)) if not discoverable else []
    if not discoverable and not passkeys:
        return {"ok": False, "error": "No passkeys are registered for this account.", "status_code": 404}
    effective_rp_id = _resolve_webauthn_rp_id(settings, rp_id)
    effective_origins = _resolve_webauthn_origins(settings, expected_origin)
    allow_credentials = None if discoverable else [
        PublicKeyCredentialDescriptor(id=_urlsafe_b64decode(row["credential_id"]))
        for row in passkeys
    ]
    options = generate_authentication_options(
        rp_id=effective_rp_id,
        allow_credentials=allow_credentials,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    payload: dict[str, Any] = {
        "challenge_b64": _urlsafe_b64encode(options.challenge),
        "purpose": str(purpose),
        "rp_id": effective_rp_id,
        "expected_origins": effective_origins,
        "discoverable": bool(discoverable),
    }
    if login_flow_id is not None:
        payload["login_flow_id"] = int(login_flow_id)
    flow_user_id = int(user_id)
    if discoverable:
        flow_user_id = int(_first_passkey_user_id(settings) or 0)
        if flow_user_id <= 0:
            return {"ok": False, "error": "No passkeys are registered for this service.", "status_code": 404}
    flow = _create_auth_flow(
        settings,
        user_id=flow_user_id,
        flow_type="webauthn-auth",
        payload=payload,
        ttl_minutes=WEBAUTHN_FLOW_TTL_MINUTES,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return {"ok": True, "flow_token": flow["token"], "options_json": options_to_json(options)}


def _verify_passkey_authentication(
    settings: TrainingHubSettings,
    *,
    flow_token: str,
    credential: dict[str, Any],
    source_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    flow = _load_auth_flow(
        settings,
        token=flow_token,
        flow_type="webauthn-auth",
        source_ip=source_ip,
        user_agent=user_agent,
    )
    if not bool(flow.get("ok")):
        return flow

    credential_id = str((credential or {}).get("id", "")).strip()
    if not credential_id:
        return {"ok": False, "error": "Passkey response is missing a credential ID.", "status_code": 400}

    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        if bool(flow["payload"].get("discoverable")):
            passkey_row = connection.execute(
                """
                SELECT id, user_id, credential_id, public_key, sign_count
                FROM user_passkeys
                WHERE credential_id = ?
                """,
                (credential_id,),
            ).fetchone()
        else:
            passkey_row = connection.execute(
                """
                SELECT id, user_id, credential_id, public_key, sign_count
                FROM user_passkeys
                WHERE user_id = ? AND credential_id = ?
                """,
                (int(flow["user_id"]), credential_id),
            ).fetchone()
        if passkey_row is None:
            return {"ok": False, "error": "Passkey is not registered for this account.", "status_code": 404}

        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=_urlsafe_b64decode(str(flow["payload"].get("challenge_b64", ""))),
                expected_rp_id=_webauthn_flow_rp_id(settings, flow["payload"]),
                expected_origin=_webauthn_flow_origins(settings, flow["payload"]),
                credential_public_key=_urlsafe_b64decode(str(passkey_row["public_key"])),
                credential_current_sign_count=int(passkey_row["sign_count"] or 0),
                require_user_verification=True,
            )
        except WebAuthnException as exception:
            logger.warning(
                "Passkey authentication verification rejected for user_id=%s rp_id=%s origins=%s: %s",
                int(passkey_row["user_id"]),
                _webauthn_flow_rp_id(settings, flow["payload"]),
                _webauthn_flow_origins(settings, flow["payload"]),
                exception,
            )
            return {"ok": False, "error": "Passkey authentication could not be verified.", "status_code": 400}
        connection.execute(
            "UPDATE user_passkeys SET sign_count = ?, last_used_at = ? WHERE id = ?",
            (int(verified.new_sign_count), _now_utc_iso(), int(passkey_row["id"])),
        )
        connection.commit()

    _consume_auth_flow_by_id(settings, int(flow["id"]))
    return {
        "ok": True,
        "user_id": int(passkey_row["user_id"]),
        "purpose": str(flow["payload"].get("purpose", "")),
        "login_flow_id": int(flow["payload"]["login_flow_id"]) if "login_flow_id" in flow["payload"] else None,
    }
