# Ubuntu Production Deployment

This document describes the current production deployment for the repository.

Use this document when you want to deploy the current multi-service stack with external OAuth/OIDC sign-in on a server from scratch.

If you are upgrading an older production installation that already uses the split Docker Compose stack but still relies on the old local sign-in flow, follow [migrate.md](migrate.md) first. That migration keeps the existing Compose topology and persistent volumes, but it changes the production authentication model.

## Target Topology

The current production stack uses Docker Compose with:

- `scamscreener-db` as the internal MariaDB service
- `scamscreener-hub` as the internal Training Hub service
- `scamscreener-api` as the internal public Lowest BIN and Bazaar API service
- `marketguard-hub` as the internal public MarketGuard frontend under `/market/`
- `caddy` as the public HTTPS reverse proxy
- `scamscreener-redis` as an optional internal Redis cache when `MARKETGUARD_REDIS_ENABLED=true`

Only Caddy is exposed publicly on `80/443`.

## Architecture Files

Production is driven by:

- `docker-compose.yml`
- `Caddyfile`
- `Dockerfile`
- `docker/entrypoint.sh`
- `docker/mariadb-entrypoint.sh`
- `docker/redis-entrypoint.sh`
- `scripts/preflight.sh`
- `scripts/update.py`
- `scripts/reset.py`

## Prerequisites

Before deployment, make sure:

- your public domain already points to the server
- inbound `80/tcp` and `443/tcp` are open
- you can SSH to the server
- you have credentials for at least one supported external sign-in provider
- you have SMTP credentials if you keep admin MFA mail, password-reset mail, or other outbound account operations enabled
- you have reviewed the legal/privacy values rendered on `/impressum` and `/datenschutz`

## 1) Provision The Server

Use a fresh Ubuntu LTS host.

Recommended minimum:

- 2 vCPU
- 2 GB RAM
- 20 GB SSD

## 2) Verify DNS

Create the required DNS records for your public host.

Verify from your workstation:

```bash
dig +short scamscreener.example.com
dig +short AAAA scamscreener.example.com
```

Both lookups must resolve to your server before you start the public deploy.

## 3) Update Ubuntu

SSH to the server and install base packages:

```bash
ssh root@YOUR_SERVER_IP
apt update
apt upgrade -y
DEBIAN_FRONTEND=noninteractive apt install -y ca-certificates curl gnupg git iptables-persistent
```

## 4) Optional: Create A Deploy User

```bash
adduser scamscreener
usermod -aG sudo scamscreener
```

You can add the user to the Docker group after Docker is installed.

## 5) Apply A Host Firewall

Keep the current SSH session open while applying rules.

```bash
iptables -F INPUT
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -p tcp --dport 22 -j ACCEPT
iptables -A INPUT -p tcp --dport 80 -j ACCEPT
iptables -A INPUT -p tcp --dport 443 -j ACCEPT
iptables -P INPUT DROP
iptables -P FORWARD ACCEPT
iptables -P OUTPUT ACCEPT
netfilter-persistent save
```

If the host has public IPv6, mirror the rules with `ip6tables`.

Do not set `FORWARD` to `DROP`; Docker needs packet forwarding.

## 6) Install Docker Engine And Compose

Install Docker from the official repository:

```bash
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Verify:

```bash
docker --version
docker compose version
```

If you use the deploy user:

```bash
usermod -aG docker scamscreener
```

Reconnect afterwards or run `newgrp docker`.

## 7) Upload The Repository

Create the deployment directory:

```bash
sudo mkdir -p /srv/scamscreener
sudo chown -R scamscreener:scamscreener /srv/scamscreener
sudo chmod 750 /srv/scamscreener
```

Upload the repository contents into `/srv/scamscreener`, then continue there:

```bash
cd /srv/scamscreener
chmod 750 scripts/*.sh
chmod 750 scripts/*.py
```

The directory should contain at least:

```text
/srv/scamscreener/docker-compose.yml
/srv/scamscreener/Caddyfile
/srv/scamscreener/Dockerfile
/srv/scamscreener/.env.production.example
/srv/scamscreener/scripts/update.py
/srv/scamscreener/scripts/migrate.py
/srv/scamscreener/scripts/reset.py
```

## 8) Create `.env.production`

Start from the production example:

```bash
cp .env.production.example .env.production
nano .env.production
chmod 600 .env.production
```

Set the base production values:

```env
CADDY_SITE_ADDRESS=scamscreener.example.com

TRAINING_HUB_ENV=production
TRAINING_HUB_PUBLIC_BASE_URL=https://scamscreener.example.com
TRAINING_HUB_ENFORCE_HTTPS=true
TRAINING_HUB_ALLOWED_HOSTS=scamscreener.example.com
TRAINING_HUB_ADMIN_MFA_REQUIRED=false
TRAINING_HUB_PASSWORD_RESET_SEND_EMAIL=false
TRAINING_HUB_SESSION_BIND_USER_AGENT=true
TRAINING_HUB_RETENTION_AUTO_ENABLED=true

SCAMSCREENER_DB_MANAGED=true
SCAMSCREENER_DB_NAME=scamscreener_hub
SCAMSCREENER_DB_USER=scamscreener
SCAMSCREENER_REDIS_MANAGED=true

TRAINING_HUB_SECRET_KEY=SET_A_REAL_SECRET_WITH_AT_LEAST_32_CHARACTERS
TRAINING_HUB_ADMIN_USERNAMES=your-admin-username
TRAINING_HUB_ADMIN_EMAILS=admin@example.com

TRAINING_HUB_SMTP_HOST=
TRAINING_HUB_SMTP_PORT=587
TRAINING_HUB_SMTP_USERNAME=
TRAINING_HUB_SMTP_PASSWORD=
TRAINING_HUB_SMTP_FROM_EMAIL=
TRAINING_HUB_SMTP_USE_STARTTLS=true
TRAINING_HUB_SMTP_USE_TLS=false

TRAINING_HUB_SITE_PROJECT_CLASSIFICATION=Private non-commercial community project
TRAINING_HUB_SITE_OPERATOR_NAME=YOUR_LEGAL_NAME_OR_ENTITY
TRAINING_HUB_SITE_POSTAL_ADDRESS=YOUR_SERVICEABLE_POSTAL_ADDRESS
TRAINING_HUB_SITE_CONTACT_CHANNEL=YOUR_PUBLIC_CONTACT
TRAINING_HUB_SITE_PRIVACY_CONTACT=YOUR_PRIVACY_CONTACT
TRAINING_HUB_SITE_HOSTING_LOCATION=Ashburn, Virginia, USA

MARKETGUARD_API_DOCS_ENABLED=false
TRAINING_HUB_API_DOCS_ENABLED=false
```

Then configure at least one external provider block.

GitHub OAuth example:

```env
TRAINING_HUB_GITHUB_OAUTH_CLIENT_ID=YOUR_GITHUB_CLIENT_ID
TRAINING_HUB_GITHUB_OAUTH_CLIENT_SECRET=YOUR_GITHUB_CLIENT_SECRET
TRAINING_HUB_GITHUB_OAUTH_ALLOWED_LOGINS=your-github-login
TRAINING_HUB_GITHUB_OAUTH_ALLOWED_EMAILS=admin@example.com
```

Authelia OIDC example:

```env
TRAINING_HUB_AUTHELIA_OIDC_ISSUER_URL=https://auth.example.com
TRAINING_HUB_AUTHELIA_OIDC_CLIENT_ID=YOUR_AUTHELIA_CLIENT_ID
TRAINING_HUB_AUTHELIA_OIDC_CLIENT_SECRET=YOUR_AUTHELIA_CLIENT_SECRET
TRAINING_HUB_AUTHELIA_OIDC_ALLOWED_EMAILS=admin@example.com
```

Optional MarketGuard Redis cache:

```env
MARKETGUARD_REDIS_ENABLED=true
MARKETGUARD_REDIS_DB=0
MARKETGUARD_REDIS_REQUIRE_TLS=false
MARKETGUARD_REDIS_CACHE_TTL_SECONDS=60
MARKETGUARD_REDIS_KEY_PREFIX=marketguard:response
MARKETGUARD_REDIS_MAXMEMORY=128mb
MARKETGUARD_REDIS_MAXMEMORY_POLICY=allkeys-lru
```

Important notes:

- `SCAMSCREENER_DB_MANAGED=true` tells the app containers to use the bundled internal MariaDB service
- the managed MariaDB container generates and persists strong app/root passwords on first boot
- if `TRAINING_HUB_SECRET_KEY` is omitted, the app generates one in `/app/data/runtime`; for production, set it explicitly
- at least one external provider must be configured, otherwise the public login flow has no usable sign-in method
- set `TRAINING_HUB_ADMIN_USERNAMES` and/or `TRAINING_HUB_ADMIN_EMAILS` before the first OAuth/OIDC deploy; preflight blocks if both are empty
- keep the provider allowlists explicit; do not rely on open provider access in production
- leave SMTP blank unless you intentionally enable password reset or admin MFA mail delivery
- keep `TRAINING_HUB_TRUSTED_PROXIES=127.0.0.1` unless you know you need more
- `/docs`, `/redoc`, and `/openapi.json` should stay disabled publicly unless you intentionally expose them

## 9) Run Preflight

Before the first deploy:

```bash
bash scripts/preflight.sh
```

This checks:

- required files exist
- required production values exist
- Caddy host and public base URL match
- GitHub OAuth and Authelia OIDC blocks are complete when enabled
- at least one external provider is configured
- bootstrap admin anchors are present
- SMTP settings are internally consistent only when SMTP-related features are enabled
- MariaDB and Redis settings are coherent for Compose
- Compose resolves successfully

## 10) Start The Stack

Build and start production:

```bash
python3 scripts/update.py
```

This performs:

- preflight validation unless you pass `--skip-preflight`
- `docker compose build --pull`
- `docker compose up -d --remove-orphans`
- health waiting for the DB, hub, API, and market services
- a final recreate of `caddy`
- persistence of an OAuth deployment marker in the shared app-data volume
- final `docker compose ps`

If you want to skip base-image pulls:

```bash
python3 scripts/update.py --skip-pull
```

If the server is still running the older split stack with local sign-in routes, `update.py` stops and tells you to use `python3 scripts/migrate.py` instead.

## 11) Verify Container Health

```bash
docker compose ps
docker compose logs --tail=100 scamscreener-db
docker compose logs --tail=100 scamscreener-hub
docker compose logs --tail=100 scamscreener-api
docker compose logs --tail=100 marketguard-hub
docker compose logs --tail=100 caddy
```

If Redis is enabled, also check:

```bash
docker compose logs --tail=100 scamscreener-redis
```

Expected:

- `scamscreener-db` is `healthy`
- `scamscreener-hub` is `healthy`
- `scamscreener-api` is `healthy`
- `marketguard-hub` is `healthy`
- `caddy` is `running`

## 12) Verify The Public Surface

From your workstation:

```bash
curl -I https://scamscreener.example.com/
curl -I https://scamscreener.example.com/hub
curl -I https://scamscreener.example.com/market/
curl -I https://scamscreener.example.com/api/v1/lowestbin
curl -I https://scamscreener.example.com/api/v2/lowestbin
curl -I https://scamscreener.example.com/api/v1/health
curl -I https://scamscreener.example.com/api/v1/metrics
curl -I https://scamscreener.example.com/docs
```

Expected:

- the site responds successfully
- `/market/` responds successfully
- `lowestbin` endpoints respond successfully
- `/api/v1/health` returns `403` publicly
- `/api/v1/metrics` returns `403` publicly
- `/docs` returns `404` unless you explicitly enabled docs

## 13) Bootstrap And Verify Admin Access

After startup:

1. open `https://scamscreener.example.com/hub`
2. sign in through the configured external provider
3. verify the user reaches `/dashboard`
4. verify admin access works for the intended account
5. verify MFA or the configured step-up flow works as expected

If you rely on external sign-in, keep `TRAINING_HUB_ADMIN_USERNAMES` or `TRAINING_HUB_ADMIN_EMAILS` aligned with the real bootstrap admin identity.

## 14) Regular Updates

Upload the changed release files, keep `.env.production`, then run:

```bash
cd /srv/scamscreener
python3 scripts/update.py
```

## 15) Logs And Restarts

Tail logs:

```bash
docker compose logs -f scamscreener-db
docker compose logs -f scamscreener-hub
docker compose logs -f scamscreener-api
docker compose logs -f marketguard-hub
docker compose logs -f caddy
```

Restart individual services:

```bash
docker compose restart scamscreener-db
docker compose restart scamscreener-hub
docker compose restart scamscreener-api
docker compose restart marketguard-hub
docker compose restart caddy
```

## 16) Stop Without Deleting Data

```bash
docker compose down
```

Do not add `-v` unless you intentionally want to destroy persistent volumes.

## 17) Full Reset

Only use this for an intentional clean-room rebuild:

```bash
python3 scripts/reset.py
```

This deletes containers and persistent volumes for:

- application shared data
- MariaDB data
- Caddy certificates/config

For migration from the old stack, do not use `scripts/reset.py`. Use [migrate.md](migrate.md) instead.

## 18) Backups

Keep two backup layers:

1. application-level backups from the admin UI
2. Docker-volume or host-level backups

Persistent volumes in the current stack:

- `scamscreener_data`
- `scamscreener_db_data`
- `caddy_data`
- `caddy_config`

## 19) Rollback Strategy

A safe rollback requires:

- a copy of the previous release files
- the previous `.env.production`
- a verified backup of the old persistent state

If the new release fails after cutover:

1. stop the current stack with `docker compose down`
2. restore the previous release files and previous env file
3. start the previous stack again
4. if necessary, restore the matching data backup

If you are rolling back a migration from the old local-sign-in split stack, use the rollback section in [migrate.md](migrate.md).

## 20) Common Problems

### Caddy does not obtain certificates

Check:

- DNS points to the server
- ports `80` and `443` are reachable
- no other process is already bound to `80` or `443`

### The app keeps redirecting to HTTPS

Check:

- `TRAINING_HUB_PUBLIC_BASE_URL` uses `https://`
- `TRAINING_HUB_ENFORCE_HTTPS=true`
- Caddy is running
- trusted proxies were not loosened incorrectly

### Startup fails in preflight

Run:

```bash
bash scripts/preflight.sh
```

Fix the exact value the script reports before retrying the deploy.

### Compose starts but the app is unhealthy

Check:

- `docker compose logs --tail=200 scamscreener-hub`
- `docker compose logs --tail=200 scamscreener-api`
- `docker compose logs --tail=200 scamscreener-db`

Typical causes:

- invalid production env values
- missing SMTP values while admin MFA or password-reset mail is enabled
- mismatched domain settings
- missing external-auth provider credentials when that flow is enabled

## Final Expected State

For a healthy production deployment:

- only Caddy is exposed publicly
- the hub, API, market frontend, MariaDB, and optional Redis are internal only
- `TRAINING_HUB_ENV=production`
- `TRAINING_HUB_PUBLIC_BASE_URL` and `CADDY_SITE_ADDRESS` point to the same public host
- `lowestbin v1` and `v2` are available publicly
- `/api/v1/health`, `/api/v1/metrics`, and internal-only routes are blocked publicly
- docs endpoints are not publicly exposed unless intentionally enabled
