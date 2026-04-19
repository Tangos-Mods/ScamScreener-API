# Ubuntu Production Deployment

This guide deploys the repository on a fresh Ubuntu server with:

- Docker Engine + Compose plugin
- one internal application container
- one public Caddy container for HTTPS and reverse proxy
- automatic Let's Encrypt certificates through Caddy
- persistent application data in Docker volumes

The resulting public topology is:

- `caddy` exposed on `80/443`
- `scamscreener` internal only
- `/api/v1/health` and `/api/v1/metrics` blocked publicly by Caddy

## Architecture

Production now uses:

- [docker-compose.yml](/C:/Users/mine6/Documents/GitHub/ScamScreener-API/docker-compose.yml)
- [Caddyfile](/C:/Users/mine6/Documents/GitHub/ScamScreener-API/Caddyfile)
- [Dockerfile](/C:/Users/mine6/Documents/GitHub/ScamScreener-API/Dockerfile)
- [docker/entrypoint.sh](/C:/Users/mine6/Documents/GitHub/ScamScreener-API/docker/entrypoint.sh)

## Prerequisites

Before you start, make sure:

- your domain already points to the Ubuntu server
- ports `80` and `443` are reachable from the internet
- you have SMTP credentials for admin MFA and password-reset mail
- you can log in to the server via SSH

## 1) Create The Server

Use a fresh Ubuntu LTS server.

Recommended minimum:

- 2 vCPU
- 2 GB RAM
- 20 GB SSD

## 2) Point DNS To The Server

Create DNS records for your domain:

- `A` record to the server IPv4
- `AAAA` record if you use IPv6

Verify from your own machine:

```bash
dig +short scamscreener.creepans.net
dig +short AAAA scamscreener.creepans.net
```

The output must point to your server.

## 3) Log In And Update Ubuntu

SSH into the server:

```bash
ssh root@YOUR_SERVER_IP
```

Update the system:

```bash
apt update
apt upgrade -y
DEBIAN_FRONTEND=noninteractive apt install -y ca-certificates curl gnupg git iptables-persistent
```

## 4) Optional: Create A Deploy User

Using a dedicated deploy user is cleaner than operating everything as `root`.

```bash
adduser scamscreener
usermod -aG sudo scamscreener
usermod -aG docker scamscreener 2>/dev/null || true
```

If you want to continue as that user after Docker is installed, reconnect later with:

```bash
ssh scamscreener@YOUR_SERVER_IP
```

## 5) Configure The Firewall With iptables

Keep your current SSH session open while applying rules. If you use a non-standard SSH port, replace `22` below.

IPv4 rules:

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

If the server uses public IPv6, mirror the rules with `ip6tables`:

```bash
ip6tables -F INPUT
ip6tables -A INPUT -i lo -j ACCEPT
ip6tables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A INPUT -p tcp --dport 22 -j ACCEPT
ip6tables -A INPUT -p tcp --dport 80 -j ACCEPT
ip6tables -A INPUT -p tcp --dport 443 -j ACCEPT
ip6tables -P INPUT DROP
ip6tables -P FORWARD ACCEPT
ip6tables -P OUTPUT ACCEPT
netfilter-persistent save
```

Notes:

- do not set `FORWARD` to `DROP`, because Docker relies on packet forwarding
- if your cloud provider has its own firewall or security group, open `80` and `443` there as well

## 6) Install Docker Engine And Compose Plugin

Set up Docker from the official repository:

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

If you created a deploy user:

```bash
usermod -aG docker scamscreener
```

Then reconnect as that user, or run:

```bash
newgrp docker
```

## 7) Prepare The Upload Directory

Create a stable application directory and hand it to the deploy user:

```bash
sudo mkdir -p /srv/scamscreener
sudo chown -R scamscreener:scamscreener /srv/scamscreener
sudo chmod 750 /srv/scamscreener
```

Upload the repository contents from your local machine via SFTP or FTP into:

```text
/srv/scamscreener
```

Then continue on the server as the `scamscreener` user:

```bash
cd /srv/scamscreener
```

Upload the project contents, not a nested extra folder layer. After upload, the server should contain files like:

```text
/srv/scamscreener/docker-compose.yml
/srv/scamscreener/Dockerfile
/srv/scamscreener/Caddyfile
/srv/scamscreener/scripts/update.py
/srv/scamscreener/scripts/reset.py
/srv/scamscreener/app
/srv/scamscreener/css
/srv/scamscreener/sites
```

Set script permissions once after upload:

```bash
cd /srv/scamscreener
chmod 750 scripts/*.sh
chmod 750 scripts/*.py
```

## 8) Create The Production Environment File

Copy the provided production example:

```bash
cp .env.production.example .env.production
```

Edit it:

```bash
nano .env.production
```

Set at least these values:

```env
CADDY_SITE_ADDRESS=scamscreener.creepans.net

TRAINING_HUB_ENV=production
TRAINING_HUB_PUBLIC_BASE_URL=https://scamscreener.creepans.net
TRAINING_HUB_ENFORCE_HTTPS=true
TRAINING_HUB_ADMIN_MFA_REQUIRED=true
TRAINING_HUB_PASSWORD_RESET_SEND_EMAIL=true
TRAINING_HUB_SESSION_BIND_USER_AGENT=true
TRAINING_HUB_RETENTION_AUTO_ENABLED=true

TRAINING_HUB_SMTP_HOST=smtp.example.com
TRAINING_HUB_SMTP_PORT=587
TRAINING_HUB_SMTP_USERNAME=YOUR_SMTP_USERNAME
TRAINING_HUB_SMTP_PASSWORD=YOUR_SMTP_PASSWORD
TRAINING_HUB_SMTP_FROM_EMAIL=no-reply@scamscreener.creepans.net
TRAINING_HUB_SMTP_USE_STARTTLS=true
TRAINING_HUB_SMTP_USE_TLS=false

TRAINING_HUB_SITE_PROJECT_CLASSIFICATION=Private non-commercial community project
TRAINING_HUB_SITE_OPERATOR_NAME=YOUR_LEGAL_NAME_OR_ENTITY
TRAINING_HUB_SITE_POSTAL_ADDRESS=YOUR_SERVICEABLE_POSTAL_ADDRESS
TRAINING_HUB_SITE_CONTACT_CHANNEL=YOUR_PUBLIC_CONTACT
TRAINING_HUB_SITE_PRIVACY_CONTACT=YOUR_PRIVACY_CONTACT
TRAINING_HUB_SITE_HOSTING_LOCATION=Ashburn, Virginia, USA
```

Optional:

```env
TRAINING_HUB_ADMIN_USERNAMES=your-admin-username
TRAINING_HUB_SECRET_KEY=YOUR_OWN_LONG_RANDOM_SECRET
WEB_CONCURRENCY=2
MARKETGUARD_LOWESTBIN_RATE_LIMIT_PER_MINUTE=30
TRAINING_HUB_API_DOCS_ENABLED=false
MARKETGUARD_API_DOCS_ENABLED=false
```

Important notes:

- if `TRAINING_HUB_SECRET_KEY` is omitted, the app generates one on first boot and persists it in the app data volume
- if `TRAINING_HUB_ADMIN_USERNAMES` is omitted, the default bootstrap admin username is `admin`
- keep `TRAINING_HUB_TRUSTED_PROXIES=127.0.0.1` unless you intentionally know you need extra proxy ranges; Docker Compose appends the internal Caddy IP automatically
- `/impressum` and `/datenschutz` render from the `TRAINING_HUB_SITE_*` variables
- `/docs`, `/redoc`, and `/openapi.json` should remain disabled on public production unless you have an explicit internal-access requirement
- if you operate the site publicly in Germany or the EU, a pseudonym or Discord handle alone is likely not sufficient for the provider-identification fields; `scripts/preflight.sh` warns about obviously incomplete values but cannot replace legal review

Lock down the file permissions:

```bash
chmod 600 .env.production
```

## 9) Run Preflight Checks

Before the first deployment, run:

```bash
bash scripts/preflight.sh
```

This checks:

- required files exist
- required production environment values exist
- Caddy domain and public base URL match
- SMTP transport encryption is configured sanely
- Compose resolves successfully

## 10) Start The Production Stack

Build and start everything:

```bash
python3 scripts/update.py
```

This starts:

- `scamscreener` as the internal FastAPI app
- `caddy` as the public reverse proxy with automatic HTTPS

Internally the update script does:

- optional preflight validation
- `docker compose build --pull`
- `docker compose up -d --remove-orphans`
- wait for the app container health check
- print final service state

Only Caddy is exposed publicly.

## 11) Verify Container Health

Check service state:

```bash
docker compose ps
```

You want:

- `scamscreener` status `healthy`
- `caddy` status `running`

Check logs:

```bash
docker compose logs --tail=100 scamscreener
docker compose logs --tail=100 caddy
```

You do not want:

- Python tracebacks
- Caddy ACME errors
- settings validation failures

## 12) Verify The Public Site

From your own machine, check:

```bash
curl -I https://scamscreener.creepans.net
curl -I https://scamscreener.creepans.net/hub
curl -I https://scamscreener.creepans.net/api/v1/lowestbin
curl -I https://scamscreener.creepans.net/api/v2/lowestbin
curl -I https://scamscreener.creepans.net/api/v1/health
curl -I https://scamscreener.creepans.net/api/v1/metrics
curl -I https://scamscreener.creepans.net/docs
```

Expected:

- main site responds with `200`, `303`, or similar valid app response
- `lowestbin v1` responds with `200` and includes `Deprecation: true`
- `lowestbin v1` includes `Sunset: Mon, 01 Jun 2026 00:00:00 GMT`
- `lowestbin v2` responds with `200`
- `health` and `metrics` respond with `403` from public networks
- `docs` responds with `404` unless you intentionally enabled API docs

## 13) Bootstrap The First Admin

If `TRAINING_HUB_ADMIN_USERNAMES` was not set, the first allowed admin username is:

```text
admin
```

If you set `TRAINING_HUB_ADMIN_USERNAMES`, the first account must use one of those names.

After startup:

1. open `https://scamscreener.creepans.net/hub`
2. register the first admin account
3. log in
4. complete the admin MFA flow via email

## 14) Basic Post-Deploy Checks

After the first admin works, verify:

1. login works
2. admin MFA mail arrives
3. password-reset mail arrives
4. uploads work
5. `lowestbin v1` works publicly and shows deprecation headers
6. `lowestbin v2` works publicly
7. admin area loads
8. backup creation works

## 15) Updating The Server

When you want to deploy a new version with FTP/SFTP:

1. upload the changed repository files to `/srv/scamscreener`
2. do not overwrite `.env.production` unless you intentionally changed it
3. on the server run:

```bash
cd /srv/scamscreener
python3 scripts/update.py
```

If you only changed static configuration and want to skip base-image pulls:

```bash
cd /srv/scamscreener
python3 scripts/update.py --skip-pull
```

## 16) Full Reset For A Clean Restart

If you intentionally want to delete the full deployment state and start from zero, run:

```bash
cd /srv/scamscreener
python3 scripts/reset.py
```

The script asks for the exact confirmation phrase before it proceeds. It removes:

- containers in the production compose stack
- Docker volumes for app data
- Docker volumes for Caddy certificates and config

Optional full local image cleanup:

```bash
cd /srv/scamscreener
python3 scripts/reset.py --prune-images
```

If you want to skip the interactive prompt explicitly:

```bash
cd /srv/scamscreener
python3 scripts/reset.py --yes --prune-images
```

After a reset, upload the desired release if needed and start again with:

```bash
cd /srv/scamscreener
python3 scripts/update.py
```

## 17) Watching Logs

```bash
docker compose logs -f scamscreener
docker compose logs -f caddy
```

## 18) Restarting Services

```bash
docker compose restart scamscreener
docker compose restart caddy
```

## 19) Stopping Services

```bash
docker compose down
```

Do not add `-v` unless you intentionally want to delete the persistent data volumes.

## 20) Backups

You should keep two layers of backups:

1. application-level backups from the admin UI
2. Docker volume / host-level backups

Relevant volumes:

- `scamscreener_data`
- `caddy_data`
- `caddy_config`

Relevant app data inside the app container:

- `/app/data`

## 21) Rollback

Because you deploy via FTP/SFTP, rollback means re-uploading the last known-good application files and redeploying.

Recommended rollback workflow:

1. keep a dated local archive of each uploaded release
2. if a new release breaks, re-upload the last known-good release files
3. run:

```bash
cd /srv/scamscreener
python3 scripts/update.py --skip-pull
```

Because app state is kept in Docker volumes, rolling back code does not remove your application data.

## 22) Troubleshooting

### Caddy does not get a certificate

Check:

- DNS points to the server
- ports `80` and `443` are reachable
- no other service is already using `80` or `443`
- your cloud firewall allows inbound `80/443`

### The app redirects forever to HTTPS

Check:

- `TRAINING_HUB_PUBLIC_BASE_URL` uses `https://`
- Caddy is running
- you did not remove the internal trusted proxy configuration

### SMTP or MFA errors on startup

Check:

- `TRAINING_HUB_SMTP_HOST`
- `TRAINING_HUB_SMTP_PORT`
- `TRAINING_HUB_SMTP_USERNAME`
- `TRAINING_HUB_SMTP_PASSWORD`
- `TRAINING_HUB_SMTP_FROM_EMAIL`
- only one of `TRAINING_HUB_SMTP_USE_TLS` or `TRAINING_HUB_SMTP_USE_STARTTLS` is `true`

### First admin registration is blocked

Check:

- `TRAINING_HUB_ADMIN_USERNAMES` contains the intended bootstrap username
- if you left it unset, use username `admin`

### `python3 scripts/update.py` fails before startup

Run:

```bash
cd /srv/scamscreener
bash scripts/preflight.sh
```

This will usually tell you exactly which required setting is missing or inconsistent.

### `docker compose` fails after a partial FTP upload

Most likely:

- not all files were uploaded
- the upload created an extra nested directory
- `Caddyfile` or `docker-compose.yml` was not replaced consistently

Check:

```bash
cd /srv/scamscreener
ls -la
find scripts -maxdepth 1 -type f
```

Then re-upload the full release and run:

```bash
bash scripts/preflight.sh
```

## 23) Final Expected State

For a healthy production server, the final state should be:

- the Ubuntu host exposes only `80/443`
- Caddy terminates TLS publicly
- the application container is not exposed directly
- `TRAINING_HUB_ENV=production`
- admin MFA is enabled
- password-reset mail is enabled
- `lowestbin v1` is public, deprecated, and emits the planned sunset date
- `lowestbin v2` is public
- `health` and `metrics` are blocked publicly
- `docs`, `redoc`, and `openapi.json` are not exposed publicly unless explicitly enabled
