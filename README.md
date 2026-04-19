# ScamScreener Platform

This repository contains two separate applications in one repo:

- `Training Hub` for player-contributed training data and admin-side pipeline control
- `MarketGuard API` for Hypixel SkyBlock market data, including Lowest BIN aggregation

## What it provides

- Clear package split between `app/training_hub` and `app/marketguard_api`
- Player registration + login
- Optional admin MFA step-up with one-time email code
- Branded HTML emails with plain-text fallback for password reset and admin MFA
- Admin backup create/restore for DB + uploads + bundles
- Forgot-password + token-based password reset flow
- Player dashboard with own contribution stats
- Upload form for `training-cases-v2.jsonl` files
- Per-account upload history with download links
- Self-service upload deletion, full contribution purge, and account deletion
- Self-service account data export workflow delivered by email
- Admin view over users, basic case list, training runs, and audit log
- Monitoring metrics endpoint (`/api/v1/metrics`) and auth-spike alerting
- Public Lowest BIN endpoint at `/api/v1/lowestbin`
- Public Lowest BIN v2 endpoint at `/api/v2/lowestbin`
- Public Bazaar endpoint at `/api/v1/bazaar`
- Admin button to:
  - build one merged training bundle from all accepted uploads
- Audit log also records upload and bundle downloads

Data/state:

- the default deployment stores app state under `/app/data`
- SQLite stores users, sessions, uploads, cases, and audit metadata by default
- uploaded raw payloads and generated bundles are kept in the persistent app data volume

Frontend files:

- HTML templates: `sites/`
- CSS: `css/`

Application packages:

- `app/training_hub/` contains the Training Hub app, routes, storage, auth, and admin flows
- `app/marketguard_api/` contains the Hypixel auction client, Lowest BIN cache, and API routes
- `app/main.py` is the primary production entrypoint for the combined single-container deployment

## 1) Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

The sample `.env.example` is a local-development baseline. Before a real deployment, switch the production-only flags called out in section 4.
It is intentionally minimal: anything omitted falls back to the app defaults in `app/training_hub/config/settings.py` and `app/marketguard_api/config.py`.

Set at least:

- `TRAINING_HUB_SECRET_KEY` to a long random value (at least 32 characters recommended)

Optional:

- `TRAINING_HUB_ADMIN_USERNAMES` (comma-separated bootstrap allowlist for first admin account)
- MariaDB settings (`TRAINING_HUB_DB_*`) if you intentionally want to use an external MariaDB instance

Bootstrap note: first registration is locked until `TRAINING_HUB_ADMIN_USERNAMES` contains the first admin username.

## 2) Run locally

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.training_hub.main:create_app --factory --host 0.0.0.0 --port 8080
```

In a second shell for the MarketGuard API:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.marketguard_api.main:create_marketguard_app --factory --host 0.0.0.0 --port 8081
```

Open:

- `http://localhost:8080` (Training Hub landing page)
- `http://localhost:8080/hub` (redirects to login/dashboard)
- `http://localhost:8081/api/v1/lowestbin` (deprecated MarketGuard Lowest BIN JSON)
- `http://localhost:8081/api/v2/lowestbin` (MarketGuard Lowest BIN JSON with `lastUpdated`, `products`, and seller UUID)
- `http://localhost:8081/api/v1/bazaar` (MarketGuard Bazaar summary JSON)
- `http://localhost:8081/docs` (interactive OpenAPI docs for local validation)

## 3) Docker Deploy

The repository now ships a single Compose stack: the combined app behind bundled Caddy. It keeps persistent state under `/app/data`, auto-generates a strong app secret on first boot when you do not provide one, and serves both the Training Hub and `lowestbin` from one internal app process.

One-time setup:

```powershell
Copy-Item .env.production.example .env.production
# edit .env.production
```

Then start production:

```powershell
python scripts/update.py
```

What this path expects:

- a real public domain in `CADDY_SITE_ADDRESS` such as `scamscreener.creepans.net`
- `TRAINING_HUB_PUBLIC_BASE_URL` is set to the real public `https://...` URL
- SMTP is configured for password reset and admin MFA mail
- `TRAINING_HUB_SITE_*` values are reviewed for `/impressum` and `/datenschutz`
- persistent storage is kept on the Docker volumes

What this path provides automatically:

- one internal app container plus one public Caddy container
- automatic HTTPS via Caddy
- `/api/v1/health` container healthcheck that works with host validation and HTTPS enforcement
- public blocking of `/api/v1/health` and `/api/v1/metrics`
- default bootstrap admin username `admin` when `TRAINING_HUB_ADMIN_USERNAMES` is omitted
- generated persistent secret key when `TRAINING_HUB_SECRET_KEY` is omitted

Operational helpers for this path:

- `python scripts/update.py` runs preflight, rebuilds the image, restarts the stack, and waits for app health
- `python scripts/update.py --skip-pull` skips upstream base-image pulls during rebuild
- `python scripts/reset.py` asks for confirmation and then deletes the full compose deployment state for a clean restart
- `python scripts/reset.py --yes --prune-images` also removes the locally built app image

Direct `docker run` is also supported if you prefer not to use Compose, but then you still need external HTTPS termination in front of the container:

```powershell
docker build -t scamscreener .
docker run -d --name scamscreener `
  --env-file .env.production `
  -p 8080:8080 `
  -v scamscreener_data:/app/data `
  --init `
  --read-only `
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m `
  --cap-drop ALL `
  --security-opt no-new-privileges:true `
  scamscreener
```

## 4) Environment variables

- `CADDY_SITE_ADDRESS` default `http://localhost` (set a real domain for public Caddy TLS)
- `CADDY_HTTP_PORT` default `80`
- `CADDY_HTTPS_PORT` default `443`
- `PORT` optional runtime port override used by the single-container image
- `WEB_CONCURRENCY` optional worker count for the single-container image (default `1`)
- `TRAINING_HUB_HOST` default `0.0.0.0`
- `TRAINING_HUB_PORT` default `8080`
- `TRAINING_HUB_ENV` default `development` (`production` enforces strict startup checks)
- `TRAINING_HUB_PUBLIC_BASE_URL` optional absolute public base URL; recommended for production and used for reset links plus allowed-host fallback
- `TRAINING_HUB_ALLOWED_HOSTS` optional allowlist for `Host` header validation
- `TRAINING_HUB_DB_DRIVER` default `sqlite` (`mariadb` if you intentionally use an external MariaDB instance)
- `TRAINING_HUB_DATABASE_URL` optional full DSN override (`mariadb://user:pass@host:3306/db`)
- `TRAINING_HUB_DB_HOST` default `127.0.0.1`
- `TRAINING_HUB_DB_PORT` default `3306`
- `TRAINING_HUB_DB_NAME` default `scamscreener_hub`
- `TRAINING_HUB_DB_USER` default `scamscreener`
- `TRAINING_HUB_DB_PASSWORD` required when driver is `mariadb`
- `TRAINING_HUB_DB_REQUIRE_TLS` default `false` (`true` required for MariaDB in production)
- `TRAINING_HUB_DB_SSL_CA` optional CA path for MariaDB TLS verification (required for verified MariaDB TLS in production)
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
- `TRAINING_HUB_SITE_PROJECT_CLASSIFICATION` default `Private non-commercial community project`
- `TRAINING_HUB_SITE_OPERATOR_NAME` optional operator/provider name rendered on `/impressum`
- `TRAINING_HUB_SITE_POSTAL_ADDRESS` optional postal address rendered on `/impressum`
- `TRAINING_HUB_SITE_CONTACT_CHANNEL` optional public contact channel rendered on `/impressum`
- `TRAINING_HUB_SITE_PRIVACY_CONTACT` optional privacy contact rendered on `/datenschutz`
- `TRAINING_HUB_SITE_HOSTING_LOCATION` default `Ashburn, Virginia, USA`
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
- `TRAINING_HUB_TRUSTED_PROXIES` optional, comma-separated exact IPs or CIDR ranges (`docker-compose.yml` keeps `127.0.0.1` for the internal healthcheck and appends the internal Caddy IP automatically)
- `TRAINING_HUB_PROJECT_ROOT` optional
- `MARKETGUARD_HYPIXEL_API_BASE_URL` default `https://api.hypixel.net/v2`
- `MARKETGUARD_REQUEST_TIMEOUT_SECONDS` default `10`
- `MARKETGUARD_MAX_PARALLEL_PAGES` default `8`
- `MARKETGUARD_SNAPSHOT_RETRIES` default `3`
- `MARKETGUARD_CACHE_TTL_SECONDS` default `60`
- `MARKETGUARD_STALE_IF_ERROR_SECONDS` default `300`
- `MARKETGUARD_LOWESTBIN_RATE_LIMIT_PER_MINUTE` default `30`
- `MARKETGUARD_HTTP_USER_AGENT` default `ScamScreener-MarketGuard/1.0`
- `MARKETGUARD_TRUSTED_PROXIES` optional, comma-separated exact IPs or CIDR ranges (falls back to `TRAINING_HUB_TRUSTED_PROXIES` when unset)
- `TRAINING_HUB_API_DOCS_ENABLED` default `true` outside production, `false` in production
- `MARKETGUARD_API_DOCS_ENABLED` default `true` for the standalone MarketGuard app, set `false` in production

Production-mode startup checks (`TRAINING_HUB_ENV=production`) enforce:
- `TRAINING_HUB_ENFORCE_HTTPS=true`
- strong `TRAINING_HUB_SECRET_KEY` (>= 32 chars)
- `TRAINING_HUB_ADMIN_MFA_REQUIRED=true`
- `TRAINING_HUB_ENABLE_RATE_LIMIT=true`
- `TRAINING_HUB_ENFORCE_ORIGIN_CHECK=true`
- explicit `TRAINING_HUB_ALLOWED_HOSTS` (no wildcard)
- MariaDB TLS enabled when MariaDB is configured
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

## 5) API endpoints

- `GET /api/v1/health`
- `GET /api/v1/lowestbin`
- `GET /api/v2/lowestbin`
- `GET /api/v1/bazaar`
- `POST /api/v1/client/auth/login`
- `POST /api/v1/client/uploads`
- `POST /api/v1/client/auth/logout`

`/api/v1/health` returns status, UTC time, user/upload counts, and storage metadata.
`/api/v1/lowestbin` returns a flat Moulberry-compatible JSON object whose keys are item identifiers and whose values are the current Lowest BIN prices. This endpoint is deprecated and emits `Deprecation: true` plus `Sunset: Mon, 01 Jun 2026 00:00:00 GMT`.
`/api/v2/lowestbin` returns an object with top-level `lastUpdated` plus a `products` object whose keys are item identifiers and whose values contain the current Lowest BIN `price` and seller `auctioneerUuid`.

Example `GET /api/v1/lowestbin` response:

```json
{
  "HYPERION": 98000000.0,
  "TRUE_ESSENCE": 23437.5
}
```

Example `GET /api/v2/lowestbin` response:

```json
{
  "lastUpdated": 1700000000000,
  "products": {
    "HYPERION": {
      "price": 98000000.0,
      "auctioneerUuid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    },
    "TRUE_ESSENCE": {
      "price": 23437.5,
      "auctioneerUuid": "cccccccccccccccccccccccccccccccc"
    }
  }
}
```

Example deprecation headers for `GET /api/v1/lowestbin`:

```http
Deprecation: true
Sunset: Mon, 01 Jun 2026 00:00:00 GMT
```

API documentation:

- `/docs`, `/redoc`, and `/openapi.json` are intended for local development and controlled internal use
- the combined production app disables them by default when `TRAINING_HUB_ENV=production`
- the standalone MarketGuard app can disable them explicitly with `MARKETGUARD_API_DOCS_ENABLED=false`

The client upload API is meant for non-browser clients such as a Minecraft mod. It uses the same account database and server-side sessions as the web app, but the client authenticates with a Bearer session token over HTTPS instead of cookies. Do not add custom application-layer crypto on top of it unless you have a concrete threat model for that; the transport encryption here is TLS.

Example login:

```bash
curl -sS https://scamscreener.creepans.net/api/v1/client/auth/login \
  -H "Content-Type: application/json" \
  -d '{"usernameOrEmail":"alice","password":"supersecret"}'
```

Example upload:

```bash
curl -sS https://scamscreener.creepans.net/api/v1/client/uploads \
  -X POST \
  -H "Authorization: Bearer YOUR_SESSION_TOKEN" \
  -H "Content-Type: application/x-ndjson" \
  -H "X-ScamScreener-Filename: training-cases-v2.jsonl" \
  --data-binary @training-cases-v2.jsonl
```

Example logout:

```bash
curl -sS https://scamscreener.creepans.net/api/v1/client/auth/logout \
  -X POST \
  -H "Authorization: Bearer YOUR_SESSION_TOKEN"
```

Notes:

- Admin accounts are intentionally blocked from API login when `TRAINING_HUB_ADMIN_MFA_REQUIRED=true`; use a non-admin uploader account for the Minecraft client.
- `/api/v1/client/auth/login` requires `application/json`.
- `/api/v1/client/uploads` accepts the raw JSONL body and applies the same validation, quotas, deduplication, and audit logging as the dashboard upload form.
- Full mod-side integration guidance: `MINECRAFT_MOD_INTEGRATION.md`

## License

This repository is licensed under the GNU Affero General Public License v3.0 only.
SPDX identifier: `AGPL-3.0-only`
