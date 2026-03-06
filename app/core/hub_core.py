from __future__ import annotations

# Backward-compatible facade for existing imports.
from .admin_ops import (
    _admin_audit_logs,
    _admin_case_detail,
    _admin_cases,
    _admin_runs,
    _admin_user_count,
    _admin_users,
    _create_audit_log,
    _delete_training_case,
    _normalize_case_messages,
    _normalize_int_list,
    _normalize_stage_results,
    _normalize_str_list,
)
from .common import (
    _is_path_within,
    _is_request_from_trusted_proxy,
    _normalize_user_agent_for_binding,
    _now_utc_iso,
    _request_client_ip,
)
from .pipeline import _count_non_empty_lines, _run_training_pipeline
from .recovery import (
    _admin_mfa_code_hash,
    _admin_mfa_token_hash,
    _consume_admin_mfa_challenge,
    _create_admin_mfa_challenge,
    _create_backup_archive,
    _create_password_reset_request,
    _maybe_raise_security_alert,
    _monitoring_snapshot,
    _password_reset_token_hash,
    _reset_password_with_token,
    _restore_backup_archive,
    _run_retention_cleanup,
    _validate_admin_mfa_challenge,
    _validate_password_reset_token,
)
from .rendering import _render_admin, _render_auth, _render_dashboard
from .session_auth import (
    LOGIN_LOCKOUT_MINUTES,
    LOGIN_MAX_FAILURES,
    _change_user_password,
    _consume_login_attempt,
    _create_session,
    _current_user_from_request,
    _hash_password,
    _new_csrf_token,
    _normalize_email,
    _normalize_username,
    _refresh_user,
    _resolve_user_from_session,
    _revoke_all_user_sessions,
    _revoke_other_user_sessions,
    _revoke_session_by_token,
    _revoke_user_session_by_id,
    _session_token_hash,
    _set_session_cookie,
    _user_active_sessions,
    _validate_csrf_token,
    _validate_password,
    _verify_password,
)
from .storage import (
    _ensure_storage,
    _init_database,
    _init_database_mariadb,
    _migrate_admin_mfa_challenge_columns,
    _migrate_audit_log_columns,
    _migrate_password_reset_token_columns,
    _migrate_training_cases_payload_json,
    _migrate_uploads_security_columns,
    _migrate_users_security_columns,
)
from .training_data import (
    _extract_case_fields,
    _global_stats,
    _ingest_cases_from_upload,
    _json_dumps,
    _parse_training_cases,
    _safe_file_name,
    _upload_quota_violation,
    _user_uploads,
    _write_payload,
)

