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


def _run_retention_cleanup(settings: TrainingHubSettings) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    session_cutoff = (now - timedelta(days=int(settings.retention_sessions_days))).isoformat().replace("+00:00", "Z")
    reset_cutoff = (now - timedelta(days=int(settings.retention_password_reset_days))).isoformat().replace("+00:00", "Z")
    mfa_cutoff = reset_cutoff
    audit_cutoff = (now - timedelta(days=int(settings.retention_audit_logs_days))).isoformat().replace("+00:00", "Z")
    uploads_cutoff = (now - timedelta(days=int(settings.retention_uploads_days))).isoformat().replace("+00:00", "Z")
    bundles_cutoff = (now - timedelta(days=int(settings.retention_bundles_days))).isoformat().replace("+00:00", "Z")
    rate_limit_cutoff = int((now - timedelta(days=int(settings.retention_rate_limit_days))).timestamp())

    removed_sessions = 0
    removed_password_reset_tokens = 0
    removed_admin_mfa_challenges = 0
    removed_audit_logs = 0
    removed_uploads = 0
    removed_bundles = 0
    removed_rate_limit_hits = 0

    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row

        sessions_cursor = connection.execute(
            """
            DELETE FROM sessions
            WHERE (revoked_at IS NOT NULL AND revoked_at < ?)
               OR (revoked_at IS NULL AND expires_at < ?)
            """,
            (session_cutoff, session_cutoff),
        )
        removed_sessions = int(sessions_cursor.rowcount or 0)

        reset_cursor = connection.execute(
            """
            DELETE FROM password_reset_tokens
            WHERE expires_at < ?
               OR (consumed_at IS NOT NULL AND consumed_at < ?)
               OR created_at < ?
            """,
            (reset_cutoff, reset_cutoff, reset_cutoff),
        )
        removed_password_reset_tokens = int(reset_cursor.rowcount or 0)

        mfa_cursor = connection.execute(
            """
            DELETE FROM admin_mfa_challenges
            WHERE expires_at < ?
               OR (consumed_at IS NOT NULL AND consumed_at < ?)
               OR created_at < ?
            """,
            (mfa_cutoff, mfa_cutoff, mfa_cutoff),
        )
        removed_admin_mfa_challenges = int(mfa_cursor.rowcount or 0)

        audit_cursor = connection.execute(
            "DELETE FROM audit_logs WHERE created_at < ?",
            (audit_cutoff,),
        )
        removed_audit_logs = int(audit_cursor.rowcount or 0)

        upload_rows = connection.execute(
            "SELECT id, stored_path FROM uploads WHERE created_at < ?",
            (uploads_cutoff,),
        ).fetchall()
        for row in upload_rows:
            upload_id = int(row["id"])
            stored_path = Path(str(row["stored_path"]))
            if _is_path_within(settings.uploads_dir, stored_path) and stored_path.exists():
                try:
                    stored_path.unlink()
                except OSError:
                    pass
            connection.execute(
                "UPDATE training_cases SET source_upload_id = NULL WHERE source_upload_id = ?",
                (upload_id,),
            )
            connection.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
            removed_uploads += 1

        bundle_rows = connection.execute(
            "SELECT id, bundle_path FROM training_runs WHERE created_at < ?",
            (bundles_cutoff,),
        ).fetchall()
        for row in bundle_rows:
            run_id = int(row["id"])
            bundle_path = Path(str(row["bundle_path"]))
            if _is_path_within(settings.bundles_dir, bundle_path) and bundle_path.exists():
                try:
                    bundle_path.unlink()
                except OSError:
                    pass
            connection.execute("DELETE FROM training_runs WHERE id = ?", (run_id,))
            removed_bundles += 1

        rate_cursor = connection.execute(
            "DELETE FROM rate_limit_hits WHERE bucket_start < ?",
            (rate_limit_cutoff,),
        )
        removed_rate_limit_hits = int(rate_cursor.rowcount or 0)

        connection.commit()

    return {
        "sessions": removed_sessions,
        "password_reset_tokens": removed_password_reset_tokens,
        "admin_mfa_challenges": removed_admin_mfa_challenges,
        "audit_logs": removed_audit_logs,
        "uploads": removed_uploads,
        "bundles": removed_bundles,
        "rate_limit_hits": removed_rate_limit_hits,
    }


def _monitoring_snapshot(settings: TrainingHubSettings) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since_iso = (
        now - timedelta(minutes=max(1, int(settings.security_alert_window_minutes)))
    ).isoformat().replace("+00:00", "Z")

    with sqlite3.connect(settings.database_path) as connection:
        users = int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        uploads = int(connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0])
        cases = int(connection.execute("SELECT COUNT(*) FROM training_cases").fetchone()[0])
        runs = int(connection.execute("SELECT COUNT(*) FROM training_runs").fetchone()[0])
        audits = int(connection.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0])

        login_failed = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM audit_logs
                WHERE action = 'auth.login.failed' AND created_at >= ?
                """,
                (since_iso,),
            ).fetchone()[0]
        )
        login_locked = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM audit_logs
                WHERE action = 'auth.login.locked' AND created_at >= ?
                """,
                (since_iso,),
            ).fetchone()[0]
        )
        mfa_failed = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM audit_logs
                WHERE action = 'auth.mfa.failed' AND created_at >= ?
                """,
                (since_iso,),
            ).fetchone()[0]
        )
        password_reset_requested = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM audit_logs
                WHERE action = 'auth.password.reset.requested' AND created_at >= ?
                """,
                (since_iso,),
            ).fetchone()[0]
        )

    login_events = login_failed + login_locked
    alerts = {
        "failed_login_spike": int(login_events >= int(settings.security_alert_failed_login_threshold)),
        "mfa_failed_spike": int(mfa_failed >= int(settings.security_alert_mfa_failed_threshold)),
        "password_reset_spike": int(
            password_reset_requested >= int(settings.security_alert_password_reset_threshold)
        ),
    }
    return {
        "window_minutes": int(settings.security_alert_window_minutes),
        "totals": {
            "users": users,
            "uploads": uploads,
            "training_cases": cases,
            "training_runs": runs,
            "audit_logs": audits,
        },
        "events": {
            "login_failed": login_failed,
            "login_locked": login_locked,
            "login_failures_total": login_events,
            "mfa_failed": mfa_failed,
            "password_reset_requested": password_reset_requested,
        },
        "alerts": alerts,
    }


def _maybe_raise_security_alert(
    settings: TrainingHubSettings,
    actor_user_id: int,
    source_ip: str,
    signal_action: str,
    threshold: int,
) -> dict[str, Any]:
    normalized_ip = (source_ip or "").strip()
    normalized_signal = (signal_action or "").strip()
    if not normalized_ip or not normalized_signal or int(threshold) <= 0:
        return {"triggered": False, "count": 0}

    now = datetime.now(timezone.utc)
    window_since = (
        now - timedelta(minutes=max(1, int(settings.security_alert_window_minutes)))
    ).isoformat().replace("+00:00", "Z")
    cooldown_since = (
        now - timedelta(minutes=max(1, int(settings.security_alert_cooldown_minutes)))
    ).isoformat().replace("+00:00", "Z")

    with sqlite3.connect(settings.database_path) as connection:
        signal_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM audit_logs
                WHERE action = ? AND source_ip = ? AND created_at >= ?
                """,
                (normalized_signal, normalized_ip, window_since),
            ).fetchone()[0]
        )
        if signal_count < int(threshold):
            return {"triggered": False, "count": signal_count}

        dedupe_key = f"signal={normalized_signal};ip={normalized_ip};"
        existing = connection.execute(
            """
            SELECT id
            FROM audit_logs
            WHERE action = 'security.alert.raised'
              AND source_ip = ?
              AND created_at >= ?
              AND details LIKE ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (normalized_ip, cooldown_since, f"{dedupe_key}%"),
        ).fetchone()
        if existing is not None:
            return {"triggered": False, "count": signal_count}

        details = (
            f"{dedupe_key}count={signal_count};"
            f"threshold={int(threshold)};"
            f"window_minutes={int(settings.security_alert_window_minutes)}"
        )
        connection.execute(
            """
            INSERT INTO audit_logs (created_at, actor_user_id, action, target_type, target_id, details, source_ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_utc_iso(),
                int(actor_user_id),
                "security.alert.raised",
                "auth_signal",
                None,
                details,
                normalized_ip[:80],
                "system-alert",
            ),
        )
        connection.commit()
    return {"triggered": True, "count": signal_count}

