from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from dotenv import load_dotenv

TRAINING_FORMAT = "training_case_v2"
TRAINING_SCHEMA_VERSION = 2
SESSION_COOKIE_NAME = "training_hub_session"
CSRF_COOKIE_NAME = "training_hub_csrf"


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw.strip())
    except ValueError:
        return default
    return max(min_value, min(max_value, parsed))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _env_csv_set(name: str) -> set[str]:
    raw = os.getenv(name, "")
    values = set()
    for part in raw.split(","):
        normalized = part.strip().lower()
        if normalized:
            values.add(normalized)
    return values


def _first(values: list[str] | None) -> str:
    if not values:
        return ""
    return str(values[0] or "").strip()


def _database_url_has_tls(database_url: str) -> bool:
    parsed = urlsplit(str(database_url or "").strip())
    if parsed.scheme.lower() not in {"mariadb", "mysql"}:
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    ssl_mode = _first(query.get("ssl_mode")).lower()
    if ssl_mode in {"required", "verify-ca", "verify-full"}:
        return True
    ssl_flag = _first(query.get("ssl")).lower()
    return ssl_flag in {"1", "true", "yes", "on", "required"}


@dataclass(frozen=True)
class TrainingHubSettings:
    host: str
    port: int
    database_url: str
    secret_key: str
    session_ttl_minutes: int
    max_upload_bytes: int
    storage_dir: Path
    pipeline_command: str
    project_root: Path
    admin_emails: set[str]
    admin_usernames: set[str]
    trusted_proxies: set[str]
    environment: str = "development"
    allowed_hosts: set[str] = field(default_factory=lambda: {"localhost", "127.0.0.1", "testserver"})
    registration_mode: str = "open"
    registration_invite_code: str = ""
    password_reset_ttl_minutes: int = 30
    password_reset_show_token: bool = False
    password_reset_send_email: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = False
    smtp_use_starttls: bool = True
    admin_mfa_required: bool = False
    admin_mfa_ttl_minutes: int = 30
    admin_mfa_max_attempts: int = 5
    enforce_https: bool = False
    enable_rate_limit: bool = True
    enforce_origin_check: bool = True
    session_bind_ip: bool = False
    session_bind_user_agent: bool = False
    max_upload_downloads_per_minute_per_user: int = 60
    max_bundle_downloads_per_minute_per_user: int = 30
    max_uploads_per_day_per_user: int = 40
    max_upload_bytes_per_day_per_user: int = 200 * 1024 * 1024
    max_upload_cases_per_day_per_user: int = 20_000
    max_uploads_per_day_per_ip: int = 120
    global_upload_storage_cap_bytes: int = 5 * 1024 * 1024 * 1024
    retention_sessions_days: int = 30
    retention_password_reset_days: int = 7
    retention_audit_logs_days: int = 180
    retention_uploads_days: int = 365
    retention_bundles_days: int = 365
    retention_backups_days: int = 30
    retention_rate_limit_days: int = 7
    retention_auto_enabled: bool = False
    retention_auto_interval_minutes: int = 1440
    backup_restore_max_bytes: int = 512 * 1024 * 1024
    security_alert_window_minutes: int = 15
    security_alert_cooldown_minutes: int = 15
    security_alert_failed_login_threshold: int = 10
    security_alert_mfa_failed_threshold: int = 6
    security_alert_password_reset_threshold: int = 10

    @property
    def database_path(self) -> Path | str:
        if self.database_url:
            return self.database_url
        return self.storage_dir / "training_hub.db"

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "uploads"

    @property
    def bundles_dir(self) -> Path:
        return self.storage_dir / "bundles"

    @property
    def backups_dir(self) -> Path:
        return self.storage_dir / "backups"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @classmethod
    def from_env(cls) -> "TrainingHubSettings":
        base_dir = Path(__file__).resolve().parents[2]
        load_dotenv(base_dir / ".env")

        environment_raw = (os.getenv("TRAINING_HUB_ENV", "development") or "development").strip().lower()
        environment = environment_raw if environment_raw in {"development", "staging", "production"} else "development"
        is_production = environment == "production"
        host = os.getenv("TRAINING_HUB_HOST", "0.0.0.0").strip() or "0.0.0.0"
        port = _env_int("TRAINING_HUB_PORT", 8080, 1, 65535)
        database_driver = (os.getenv("TRAINING_HUB_DB_DRIVER", "sqlite") or "sqlite").strip().lower()
        database_url_raw = (os.getenv("TRAINING_HUB_DATABASE_URL", "") or "").strip()
        db_host = (os.getenv("TRAINING_HUB_DB_HOST", "127.0.0.1") or "127.0.0.1").strip()
        db_port = _env_int("TRAINING_HUB_DB_PORT", 3306, 1, 65535)
        db_name = (os.getenv("TRAINING_HUB_DB_NAME", "scamscreener_hub") or "scamscreener_hub").strip()
        db_user = (os.getenv("TRAINING_HUB_DB_USER", "scamscreener") or "scamscreener").strip()
        db_password = (os.getenv("TRAINING_HUB_DB_PASSWORD", "") or "").strip()
        db_require_tls = _env_bool("TRAINING_HUB_DB_REQUIRE_TLS", bool(database_driver == "mariadb" and is_production))
        db_ssl_ca = (os.getenv("TRAINING_HUB_DB_SSL_CA", "") or "").strip()
        db_ssl_cert = (os.getenv("TRAINING_HUB_DB_SSL_CERT", "") or "").strip()
        db_ssl_key = (os.getenv("TRAINING_HUB_DB_SSL_KEY", "") or "").strip()
        db_ssl_verify_hostname = _env_bool("TRAINING_HUB_DB_SSL_VERIFY_HOSTNAME", True)
        secret_key = os.getenv("TRAINING_HUB_SECRET_KEY", "change-me-in-env").strip() or "change-me-in-env"
        session_ttl_minutes = _env_int("TRAINING_HUB_SESSION_TTL_MINUTES", 720, 30, 43_200)
        max_upload_bytes = _env_int("TRAINING_HUB_MAX_UPLOAD_BYTES", 5 * 1024 * 1024, 64 * 1024, 100 * 1024 * 1024)
        enforce_https = _env_bool("TRAINING_HUB_ENFORCE_HTTPS", False)
        enable_rate_limit = _env_bool("TRAINING_HUB_ENABLE_RATE_LIMIT", True)
        enforce_origin_check = _env_bool("TRAINING_HUB_ENFORCE_ORIGIN_CHECK", True)
        session_bind_ip = _env_bool("TRAINING_HUB_SESSION_BIND_IP", False)
        session_bind_user_agent = _env_bool("TRAINING_HUB_SESSION_BIND_USER_AGENT", False)
        registration_mode_raw = os.getenv("TRAINING_HUB_REGISTRATION_MODE", "open").strip().lower()
        registration_mode = registration_mode_raw if registration_mode_raw in {"open", "invite", "closed"} else "open"
        registration_invite_code = os.getenv("TRAINING_HUB_REGISTRATION_INVITE_CODE", "").strip()
        password_reset_ttl_minutes = _env_int("TRAINING_HUB_PASSWORD_RESET_TTL_MINUTES", 30, 5, 240)
        password_reset_show_token = _env_bool("TRAINING_HUB_PASSWORD_RESET_SHOW_TOKEN", False)
        password_reset_send_email = _env_bool("TRAINING_HUB_PASSWORD_RESET_SEND_EMAIL", False)
        smtp_host = os.getenv("TRAINING_HUB_SMTP_HOST", "").strip()
        smtp_port = _env_int("TRAINING_HUB_SMTP_PORT", 587, 1, 65535)
        smtp_username = os.getenv("TRAINING_HUB_SMTP_USERNAME", "").strip()
        smtp_password = os.getenv("TRAINING_HUB_SMTP_PASSWORD", "").strip()
        smtp_from_email = os.getenv("TRAINING_HUB_SMTP_FROM_EMAIL", "").strip()
        smtp_use_tls = _env_bool("TRAINING_HUB_SMTP_USE_TLS", False)
        smtp_use_starttls = _env_bool("TRAINING_HUB_SMTP_USE_STARTTLS", True)
        admin_mfa_required = _env_bool("TRAINING_HUB_ADMIN_MFA_REQUIRED", False)
        admin_mfa_ttl_minutes = _env_int("TRAINING_HUB_ADMIN_MFA_TTL_MINUTES", 30, 5, 1440)
        admin_mfa_max_attempts = _env_int("TRAINING_HUB_ADMIN_MFA_MAX_ATTEMPTS", 5, 1, 20)
        max_upload_downloads_per_minute_per_user = _env_int(
            "TRAINING_HUB_MAX_UPLOAD_DOWNLOADS_PER_MINUTE_PER_USER",
            60,
            1,
            10000,
        )
        max_bundle_downloads_per_minute_per_user = _env_int(
            "TRAINING_HUB_MAX_BUNDLE_DOWNLOADS_PER_MINUTE_PER_USER",
            30,
            1,
            10000,
        )
        max_uploads_per_day_per_user = _env_int("TRAINING_HUB_MAX_UPLOADS_PER_DAY_PER_USER", 40, 1, 10000)
        max_upload_bytes_per_day_per_user = _env_int(
            "TRAINING_HUB_MAX_UPLOAD_BYTES_PER_DAY_PER_USER",
            200 * 1024 * 1024,
            1 * 1024 * 1024,
            10 * 1024 * 1024 * 1024,
        )
        max_upload_cases_per_day_per_user = _env_int("TRAINING_HUB_MAX_UPLOAD_CASES_PER_DAY_PER_USER", 20_000, 1, 1_000_000)
        max_uploads_per_day_per_ip = _env_int("TRAINING_HUB_MAX_UPLOADS_PER_DAY_PER_IP", 120, 1, 100000)
        global_upload_storage_cap_bytes = _env_int(
            "TRAINING_HUB_GLOBAL_UPLOAD_STORAGE_CAP_BYTES",
            5 * 1024 * 1024 * 1024,
            10 * 1024 * 1024,
            200 * 1024 * 1024 * 1024,
        )
        retention_sessions_days = _env_int("TRAINING_HUB_RETENTION_SESSIONS_DAYS", 30, 1, 3650)
        retention_password_reset_days = _env_int("TRAINING_HUB_RETENTION_PASSWORD_RESET_DAYS", 7, 1, 3650)
        retention_audit_logs_days = _env_int("TRAINING_HUB_RETENTION_AUDIT_LOGS_DAYS", 180, 1, 3650)
        retention_uploads_days = _env_int("TRAINING_HUB_RETENTION_UPLOADS_DAYS", 365, 1, 3650)
        retention_bundles_days = _env_int("TRAINING_HUB_RETENTION_BUNDLES_DAYS", 365, 1, 3650)
        retention_backups_days = _env_int("TRAINING_HUB_RETENTION_BACKUPS_DAYS", 30, 1, 3650)
        retention_rate_limit_days = _env_int("TRAINING_HUB_RETENTION_RATE_LIMIT_DAYS", 7, 1, 3650)
        retention_auto_enabled = _env_bool("TRAINING_HUB_RETENTION_AUTO_ENABLED", False)
        retention_auto_interval_minutes = _env_int("TRAINING_HUB_RETENTION_AUTO_INTERVAL_MINUTES", 1440, 1, 10080)
        backup_restore_max_bytes = _env_int(
            "TRAINING_HUB_BACKUP_RESTORE_MAX_BYTES",
            512 * 1024 * 1024,
            10 * 1024 * 1024,
            5 * 1024 * 1024 * 1024,
        )
        security_alert_window_minutes = _env_int("TRAINING_HUB_SECURITY_ALERT_WINDOW_MINUTES", 15, 1, 1440)
        security_alert_cooldown_minutes = _env_int("TRAINING_HUB_SECURITY_ALERT_COOLDOWN_MINUTES", 15, 1, 1440)
        security_alert_failed_login_threshold = _env_int(
            "TRAINING_HUB_SECURITY_ALERT_FAILED_LOGIN_THRESHOLD",
            10,
            1,
            100000,
        )
        security_alert_mfa_failed_threshold = _env_int(
            "TRAINING_HUB_SECURITY_ALERT_MFA_FAILED_THRESHOLD",
            6,
            1,
            100000,
        )
        security_alert_password_reset_threshold = _env_int(
            "TRAINING_HUB_SECURITY_ALERT_PASSWORD_RESET_THRESHOLD",
            10,
            1,
            100000,
        )
        storage_dir_raw = os.getenv("TRAINING_HUB_STORAGE_DIR", str(base_dir / "data")).strip()
        pipeline_command = os.getenv("TRAINING_HUB_PIPELINE_COMMAND", "").strip()
        project_root_raw = os.getenv("TRAINING_HUB_PROJECT_ROOT", "").strip()
        allowed_hosts = _env_csv_set("TRAINING_HUB_ALLOWED_HOSTS")
        if not allowed_hosts and not is_production:
            allowed_hosts = {"localhost", "127.0.0.1", "testserver"}
        default_project_root = base_dir.parent if (base_dir.parent / "scripts").exists() else base_dir
        project_root = Path(project_root_raw).expanduser().resolve() if project_root_raw else default_project_root
        if database_url_raw:
            database_url = database_url_raw
        elif database_driver == "mariadb":
            if not db_password:
                raise ValueError("TRAINING_HUB_DB_PASSWORD must be set when TRAINING_HUB_DB_DRIVER=mariadb.")
            query_parts: list[str] = []
            if db_require_tls:
                query_parts.append(f"ssl_mode={'verify-full' if db_ssl_verify_hostname else 'verify-ca'}")
                if db_ssl_ca:
                    query_parts.append(f"ssl_ca={quote(db_ssl_ca, safe='/:._-')}")
                if db_ssl_cert:
                    query_parts.append(f"ssl_cert={quote(db_ssl_cert, safe='/:._-')}")
                if db_ssl_key:
                    query_parts.append(f"ssl_key={quote(db_ssl_key, safe='/:._-')}")
            query_suffix = f"?{'&'.join(query_parts)}" if query_parts else ""
            database_url = (
                f"mariadb://{quote(db_user, safe='')}:{quote(db_password, safe='')}"
                f"@{db_host}:{db_port}/{quote(db_name, safe='')}{query_suffix}"
            )
        else:
            database_url = ""

        if enforce_https and (secret_key == "change-me-in-env" or len(secret_key) < 32):
            raise ValueError(
                "TRAINING_HUB_SECRET_KEY must be set to a strong value (>=32 chars) when TRAINING_HUB_ENFORCE_HTTPS=true."
            )
        if registration_mode == "invite" and not registration_invite_code:
            raise ValueError(
                "TRAINING_HUB_REGISTRATION_INVITE_CODE must be set when TRAINING_HUB_REGISTRATION_MODE=invite."
            )
        if (password_reset_send_email or admin_mfa_required) and not smtp_host:
            raise ValueError(
                "TRAINING_HUB_SMTP_HOST must be set when password reset email or admin MFA email is enabled."
            )
        if (password_reset_send_email or admin_mfa_required) and not smtp_from_email:
            raise ValueError(
                "TRAINING_HUB_SMTP_FROM_EMAIL must be set when password reset email or admin MFA email is enabled."
            )
        if smtp_use_tls and smtp_use_starttls:
            raise ValueError("Set only one of TRAINING_HUB_SMTP_USE_TLS or TRAINING_HUB_SMTP_USE_STARTTLS.")
        if is_production:
            if not enforce_https:
                raise ValueError("TRAINING_HUB_ENFORCE_HTTPS must be true when TRAINING_HUB_ENV=production.")
            if secret_key == "change-me-in-env" or len(secret_key) < 32:
                raise ValueError("TRAINING_HUB_SECRET_KEY must be at least 32 chars in production.")
            if not admin_mfa_required:
                raise ValueError("TRAINING_HUB_ADMIN_MFA_REQUIRED must be true in production.")
            if password_reset_show_token:
                raise ValueError("TRAINING_HUB_PASSWORD_RESET_SHOW_TOKEN must be false in production.")
            if not enable_rate_limit:
                raise ValueError("TRAINING_HUB_ENABLE_RATE_LIMIT must be true in production.")
            if not enforce_origin_check:
                raise ValueError("TRAINING_HUB_ENFORCE_ORIGIN_CHECK must be true in production.")
            if "*" in _env_csv_set("TRAINING_HUB_TRUSTED_PROXIES"):
                raise ValueError("TRAINING_HUB_TRUSTED_PROXIES must not contain '*' in production.")
            if not allowed_hosts or "*" in allowed_hosts:
                raise ValueError("TRAINING_HUB_ALLOWED_HOSTS must be set to explicit hostnames in production.")
            if database_driver == "mariadb":
                if not _database_url_has_tls(database_url):
                    raise ValueError("MariaDB connections must enable TLS in production.")
                if db_require_tls and not db_ssl_ca and not database_url_raw:
                    raise ValueError("TRAINING_HUB_DB_SSL_CA should be set for verified MariaDB TLS in production.")
            if (password_reset_send_email or admin_mfa_required) and not (smtp_use_tls or smtp_use_starttls):
                raise ValueError("SMTP transport encryption (TLS or STARTTLS) is required in production.")

        return cls(
            host=host,
            port=port,
            database_url=database_url,
            secret_key=secret_key,
            session_ttl_minutes=session_ttl_minutes,
            max_upload_bytes=max_upload_bytes,
            storage_dir=Path(storage_dir_raw).expanduser().resolve(),
            pipeline_command=pipeline_command,
            project_root=project_root,
            admin_emails=_env_csv_set("TRAINING_HUB_ADMIN_EMAILS"),
            admin_usernames=_env_csv_set("TRAINING_HUB_ADMIN_USERNAMES"),
            trusted_proxies=_env_csv_set("TRAINING_HUB_TRUSTED_PROXIES"),
            environment=environment,
            allowed_hosts=allowed_hosts,
            registration_mode=registration_mode,
            registration_invite_code=registration_invite_code,
            password_reset_ttl_minutes=password_reset_ttl_minutes,
            password_reset_show_token=password_reset_show_token,
            password_reset_send_email=password_reset_send_email,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_username=smtp_username,
            smtp_password=smtp_password,
            smtp_from_email=smtp_from_email,
            smtp_use_tls=smtp_use_tls,
            smtp_use_starttls=smtp_use_starttls,
            admin_mfa_required=admin_mfa_required,
            admin_mfa_ttl_minutes=admin_mfa_ttl_minutes,
            admin_mfa_max_attempts=admin_mfa_max_attempts,
            enforce_https=enforce_https,
            enable_rate_limit=enable_rate_limit,
            enforce_origin_check=enforce_origin_check,
            session_bind_ip=session_bind_ip,
            session_bind_user_agent=session_bind_user_agent,
            max_upload_downloads_per_minute_per_user=max_upload_downloads_per_minute_per_user,
            max_bundle_downloads_per_minute_per_user=max_bundle_downloads_per_minute_per_user,
            max_uploads_per_day_per_user=max_uploads_per_day_per_user,
            max_upload_bytes_per_day_per_user=max_upload_bytes_per_day_per_user,
            max_upload_cases_per_day_per_user=max_upload_cases_per_day_per_user,
            max_uploads_per_day_per_ip=max_uploads_per_day_per_ip,
            global_upload_storage_cap_bytes=global_upload_storage_cap_bytes,
            retention_sessions_days=retention_sessions_days,
            retention_password_reset_days=retention_password_reset_days,
            retention_audit_logs_days=retention_audit_logs_days,
            retention_uploads_days=retention_uploads_days,
            retention_bundles_days=retention_bundles_days,
            retention_backups_days=retention_backups_days,
            retention_rate_limit_days=retention_rate_limit_days,
            retention_auto_enabled=retention_auto_enabled,
            retention_auto_interval_minutes=retention_auto_interval_minutes,
            backup_restore_max_bytes=backup_restore_max_bytes,
            security_alert_window_minutes=security_alert_window_minutes,
            security_alert_cooldown_minutes=security_alert_cooldown_minutes,
            security_alert_failed_login_threshold=security_alert_failed_login_threshold,
            security_alert_mfa_failed_threshold=security_alert_mfa_failed_threshold,
            security_alert_password_reset_threshold=security_alert_password_reset_threshold,
        )

