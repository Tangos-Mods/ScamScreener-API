# ScamScreener Training Hub

Web application for player-contributed training data and admin-side pipeline control.

## What it provides

- Player registration + login
- Optional admin MFA step-up with one-time email code
- Admin backup create/restore for DB + uploads + bundles
- Forgot-password + token-based password reset flow
- Player dashboard with own contribution stats
- Upload form for `training-cases-v2.jsonl` files
- Per-account upload history with download links
- Admin view over users, basic case list, training runs, and audit log
- Monitoring metrics endpoint (`/api/v1/metrics`) and auth-spike alerting
- Admin button to:
  - build one merged training bundle from all accepted uploads
- Audit log also records upload and bundle downloads

Data/state:

- MariaDB stores users/sessions/uploads/cases/audit metadata
- `data/uploads/*.jsonl` keeps raw upload payloads
- `data/bundles/*.jsonl` keeps generated training bundles

Frontend files:

- HTML templates: `sites/`
- CSS: `css/`

## 1) Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Set at least:

- `TRAINING_HUB_SECRET_KEY` to a long random value (at least 32 characters recommended)

Optional:

- `TRAINING_HUB_ADMIN_USERNAMES` (comma-separated bootstrap allowlist for first admin account)
- `TRAINING_HUB_TRUSTED_PROXIES` (comma-separated hosts/IPs allowed to send `X-Forwarded-Proto`)
- MariaDB settings (`TRAINING_HUB_DB_*`) if not using the included docker-compose DB service

Bootstrap note: first registration is locked until `TRAINING_HUB_ADMIN_USERNAMES` contains the first admin username.

## 2) Run locally

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

or:

```powershell
.\run.ps1
```

Open:

- `http://localhost:8080` (ScamScreener Landing Page)
- `http://localhost:8080/hub` (redirects to login/dashboard)
- `http://localhost:8025` (Mailpit inbox for password-reset emails)

## 3) Docker

```powershell
Copy-Item .env.example .env
# edit .env
docker compose up --build -d
```

Open:

- `http://localhost:8080` (ScamScreener Landing Page)
- `http://localhost:8080/hub` (redirects to login/dashboard)

`docker-compose.yml` mounts repo root to `/workspace` and sets
`TRAINING_HUB_PROJECT_ROOT=/workspace` so pipeline commands can access project scripts.

## 4) Environment variables

- `TRAINING_HUB_HOST` default `0.0.0.0`
- `TRAINING_HUB_PORT` default `8080`
- `TRAINING_HUB_ENV` default `development` (`production` enforces strict startup checks)
- `TRAINING_HUB_ALLOWED_HOSTS` optional allowlist for `Host` header validation
- `TRAINING_HUB_DB_DRIVER` default `sqlite` (`mariadb` for production)
- `TRAINING_HUB_DATABASE_URL` optional full DSN override (`mariadb://user:pass@host:3306/db`)
- `TRAINING_HUB_DB_HOST` default `127.0.0.1`
- `TRAINING_HUB_DB_PORT` default `3306`
- `TRAINING_HUB_DB_NAME` default `scamscreener_hub`
- `TRAINING_HUB_DB_USER` default `scamscreener`
- `TRAINING_HUB_DB_PASSWORD` required when driver is `mariadb`
- `TRAINING_HUB_DB_REQUIRE_TLS` default `false` (`true` enforced for MariaDB in production)
- `TRAINING_HUB_DB_SSL_CA` optional CA path for MariaDB TLS verification
- `TRAINING_HUB_DB_SSL_CERT` optional client certificate for MariaDB TLS
- `TRAINING_HUB_DB_SSL_KEY` optional client key for MariaDB TLS
- `TRAINING_HUB_DB_SSL_VERIFY_HOSTNAME` default `true`
- `TRAINING_HUB_SECRET_KEY` required
- `TRAINING_HUB_SESSION_TTL_MINUTES` default `720`
- `TRAINING_HUB_SESSION_BIND_IP` default `false`
- `TRAINING_HUB_SESSION_BIND_USER_AGENT` default `false`
- `TRAINING_HUB_REGISTRATION_MODE` default `open` (`open`, `invite`, `closed`)
- `TRAINING_HUB_REGISTRATION_INVITE_CODE` required when mode is `invite`
- `TRAINING_HUB_PASSWORD_RESET_TTL_MINUTES` default `30`
- `TRAINING_HUB_PASSWORD_RESET_SHOW_TOKEN` default `false` (dev only)
- `TRAINING_HUB_PASSWORD_RESET_SEND_EMAIL` default `false`
- `TRAINING_HUB_SMTP_HOST` SMTP server host
- `TRAINING_HUB_SMTP_PORT` SMTP server port (default `587`)
- `TRAINING_HUB_SMTP_USERNAME` optional SMTP username
- `TRAINING_HUB_SMTP_PASSWORD` optional SMTP password
- `TRAINING_HUB_SMTP_FROM_EMAIL` sender address for reset emails
- `TRAINING_HUB_SMTP_USE_TLS` default `false` (implicit TLS/SMTPS)
- `TRAINING_HUB_SMTP_USE_STARTTLS` default `true` (explicit STARTTLS)
- `TRAINING_HUB_ADMIN_MFA_REQUIRED` default `false`
- `TRAINING_HUB_ADMIN_MFA_TTL_MINUTES` default `30`
- `TRAINING_HUB_ADMIN_MFA_MAX_ATTEMPTS` default `5`
- `TRAINING_HUB_ENFORCE_HTTPS` default `false` (`true` in production)
- `TRAINING_HUB_ENABLE_RATE_LIMIT` default `true`
- `TRAINING_HUB_ENFORCE_ORIGIN_CHECK` default `true`
- `TRAINING_HUB_MAX_UPLOAD_BYTES` default `5242880`
- `TRAINING_HUB_MAX_UPLOAD_DOWNLOADS_PER_MINUTE_PER_USER` default `60`
- `TRAINING_HUB_MAX_BUNDLE_DOWNLOADS_PER_MINUTE_PER_USER` default `30`
- `TRAINING_HUB_MAX_UPLOADS_PER_DAY_PER_USER` default `40`
- `TRAINING_HUB_MAX_UPLOAD_BYTES_PER_DAY_PER_USER` default `209715200`
- `TRAINING_HUB_MAX_UPLOAD_CASES_PER_DAY_PER_USER` default `20000`
- `TRAINING_HUB_MAX_UPLOADS_PER_DAY_PER_IP` default `120`
- `TRAINING_HUB_GLOBAL_UPLOAD_STORAGE_CAP_BYTES` default `5368709120`
- `TRAINING_HUB_RETENTION_SESSIONS_DAYS` default `30`
- `TRAINING_HUB_RETENTION_PASSWORD_RESET_DAYS` default `7`
- `TRAINING_HUB_RETENTION_AUDIT_LOGS_DAYS` default `180`
- `TRAINING_HUB_RETENTION_UPLOADS_DAYS` default `365`
- `TRAINING_HUB_RETENTION_BUNDLES_DAYS` default `365`
- `TRAINING_HUB_RETENTION_BACKUPS_DAYS` default `30`
- `TRAINING_HUB_RETENTION_RATE_LIMIT_DAYS` default `7`
- `TRAINING_HUB_RETENTION_AUTO_ENABLED` default `false`
- `TRAINING_HUB_RETENTION_AUTO_INTERVAL_MINUTES` default `1440`
- `TRAINING_HUB_BACKUP_RESTORE_MAX_BYTES` default `536870912`
- `TRAINING_HUB_SECURITY_ALERT_WINDOW_MINUTES` default `15`
- `TRAINING_HUB_SECURITY_ALERT_COOLDOWN_MINUTES` default `15`
- `TRAINING_HUB_SECURITY_ALERT_FAILED_LOGIN_THRESHOLD` default `10`
- `TRAINING_HUB_SECURITY_ALERT_MFA_FAILED_THRESHOLD` default `6`
- `TRAINING_HUB_SECURITY_ALERT_PASSWORD_RESET_THRESHOLD` default `10`
- `TRAINING_HUB_STORAGE_DIR` default `./data`
- `TRAINING_HUB_ADMIN_EMAILS` optional, comma-separated (informational only)
- `TRAINING_HUB_ADMIN_USERNAMES` required for first-account admin bootstrap
- `TRAINING_HUB_TRUSTED_PROXIES` optional, comma-separated
- `TRAINING_HUB_PROJECT_ROOT` optional

Production-mode startup checks (`TRAINING_HUB_ENV=production`) enforce:
- `TRAINING_HUB_ENFORCE_HTTPS=true`
- strong `TRAINING_HUB_SECRET_KEY` (>= 32 chars)
- `TRAINING_HUB_ADMIN_MFA_REQUIRED=true`
- `TRAINING_HUB_ENABLE_RATE_LIMIT=true`
- `TRAINING_HUB_ENFORCE_ORIGIN_CHECK=true`
- explicit `TRAINING_HUB_ALLOWED_HOSTS` (no wildcard)
- MariaDB TLS enabled
- no token disclosure in forgot-password UI (`TRAINING_HUB_PASSWORD_RESET_SHOW_TOKEN=false`)

Admin trigger creates a merged bundle and records the run as `prepared`.

Security headers include CSP, COOP/CORP, `X-Frame-Options`, and `Permissions-Policy`.
Failed/locked login attempts for known accounts are written to the audit log.
Users can change their password from the dashboard; this revokes other active sessions.
Admin can run retention cleanup from `/admin` to prune stale sessions, reset tokens, MFA challenges, logs, uploads, bundles, backups, and rate-limit rows.
Automatic retention cleanup runs in the background when `TRAINING_HUB_RETENTION_AUTO_ENABLED=true`.
Admin can create and restore backups from `/admin` (archive includes DB export + uploads + bundles; restore requires valid signed manifest).
Prometheus-compatible monitoring is available at `/api/v1/metrics`.

Container hardening defaults:
- runs as non-root user
- read-only root filesystem in `docker-compose.yml`
- dropped Linux capabilities (`cap_drop: ALL`)
- `no-new-privileges` enabled

Supply-chain checks:
- GitHub Actions workflow `.github/workflows/server-security.yml` runs `pip-audit` and `trivy`
- Dependabot config `.github/dependabot.yml` enables weekly dependency updates

## 5) Health endpoint

- `GET /api/v1/health`

Returns status, UTC time, user/upload counts, and storage metadata.
