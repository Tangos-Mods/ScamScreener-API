# Deploy On Ubuntu

This guide assumes:

- you have a fresh Ubuntu server
- your domain already points to the server IP
- ports `80` and `443` can be reached from the internet
- you want to run the app with the included `docker-compose.yml`
- you want to start in `staging`, verify everything, then switch to real `production`

This repo already includes:

- `caddy` as the public reverse proxy
- internal `mariadb`
- internal `mailpit` for local/staging mail testing
- `scripts/promote_to_production.sh` to switch a working staging server to true production mode

## 1) Prepare DNS

Before touching the server, make sure your DNS points to it.

- Create an `A` record for your domain, for example `hub.example.com`
- If you use IPv6, also create an `AAAA` record
- Wait until DNS resolves correctly

Check from your own machine:

```bash
dig +short hub.example.com
```

The result should be your server IP.

## 2) First Login And Base Packages

SSH into the server:

```bash
ssh root@YOUR_SERVER_IP
```

Update the system and install basic packages:

```bash
apt update
apt upgrade -y
DEBIAN_FRONTEND=noninteractive apt install -y ca-certificates curl gnupg git openssl iptables-persistent
```

## 3) Optional: Create A Deploy User

Running everything as `root` works, but a dedicated sudo user is cleaner.

```bash
adduser deploy
usermod -aG sudo deploy
```

If you use SSH keys:

```bash
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy
```

Then reconnect:

```bash
ssh deploy@YOUR_SERVER_IP
```

## 4) Configure Firewall With `iptables`

This guide assumes you manage the host firewall directly with `iptables`.

Keep your current SSH session open while applying rules. If you use a non-standard SSH port, replace `22` below.

IPv4 rules:

```bash
sudo iptables -F INPUT
sudo iptables -A INPUT -i lo -j ACCEPT
sudo iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 22 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 443 -j ACCEPT
sudo iptables -P INPUT DROP
sudo iptables -P FORWARD ACCEPT
sudo iptables -P OUTPUT ACCEPT
sudo netfilter-persistent save
```

If the server uses IPv6 publicly, mirror the rules with `ip6tables`:

```bash
sudo ip6tables -F INPUT
sudo ip6tables -A INPUT -i lo -j ACCEPT
sudo ip6tables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
sudo ip6tables -A INPUT -p tcp --dport 22 -j ACCEPT
sudo ip6tables -A INPUT -p tcp --dport 80 -j ACCEPT
sudo ip6tables -A INPUT -p tcp --dport 443 -j ACCEPT
sudo ip6tables -P INPUT DROP
sudo ip6tables -P FORWARD ACCEPT
sudo ip6tables -P OUTPUT ACCEPT
sudo netfilter-persistent save
```

Notes:

- do not set `FORWARD` to `DROP` unless you also understand Docker's forwarding chains
- if a cloud firewall exists at your provider, `80` and `443` must also be opened there

## 5) Install Docker Engine And Compose Plugin

Set up Docker from the official repository:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

If you use the `deploy` user, allow docker commands without `sudo`:

```bash
sudo usermod -aG docker "$USER"
newgrp docker
```

Verify:

```bash
docker --version
docker compose version
```

## 6) Clone The Repository

Pick a stable directory:

```bash
cd /srv
sudo mkdir -p scamscreener
sudo chown "$USER":"$USER" scamscreener
cd scamscreener
git clone YOUR_REPO_URL app
cd app
```

## 7) Create The Server `.env`

Start from the minimal sample:

```bash
cp .env.example .env
```

Edit it:

```bash
nano .env
```

For the initial staging deployment, set at least:

```env
CADDY_SITE_ADDRESS=hub.example.com
TRAINING_HUB_ENV=staging
TRAINING_HUB_ALLOWED_HOSTS=hub.example.com
TRAINING_HUB_ENFORCE_HTTPS=true

TRAINING_HUB_SECRET_KEY=REPLACE_WITH_LONG_RANDOM_SECRET
TRAINING_HUB_DB_PASSWORD=REPLACE_WITH_STRONG_DB_PASSWORD
TRAINING_HUB_DB_ROOT_PASSWORD=REPLACE_WITH_STRONG_ROOT_PASSWORD

TRAINING_HUB_ADMIN_USERNAMES=admin
TRAINING_HUB_ADMIN_MFA_REQUIRED=true
TRAINING_HUB_PASSWORD_RESET_SEND_EMAIL=true
TRAINING_HUB_SMTP_HOST=smtp.example.com
TRAINING_HUB_SMTP_PORT=587
TRAINING_HUB_SMTP_USERNAME=YOUR_SMTP_USERNAME
TRAINING_HUB_SMTP_PASSWORD=YOUR_SMTP_PASSWORD
TRAINING_HUB_SMTP_FROM_EMAIL=no-reply@hub.example.com
TRAINING_HUB_SMTP_USE_STARTTLS=true

TRAINING_HUB_SESSION_BIND_USER_AGENT=true
```

Notes:

- use a real SMTP server, not Mailpit, on the public server
- keep `TRAINING_HUB_ENV=staging` for the first public deployment
- do not use `production` yet; that comes later through the promotion script

Generate a strong secret if needed:

```bash
openssl rand -hex 48
```

## 8) Start The Staging Stack

Start everything:

```bash
docker compose up -d --build
```

Check status:

```bash
docker compose ps
```

Check logs:

```bash
docker compose logs --tail=100 caddy
docker compose logs --tail=100 training-hub
docker compose logs --tail=100 mariadb
```

What you want to see:

- `mariadb` becomes healthy
- `training-hub` starts without a traceback
- `caddy` obtains a certificate for your public domain

## 9) Verify Staging Works

From your own machine, open:

- `https://hub.example.com`
- `https://hub.example.com/hub`

Then verify:

1. the site loads over HTTPS
2. registration/login works
3. admin MFA mail arrives
4. password reset mail arrives

Useful server-side checks:

```bash
curl -I https://hub.example.com
curl -I https://hub.example.com/hub
curl -I https://hub.example.com/api/v1/health
```

Expected:

- the main site returns `200` or `303`
- public `health` should be blocked by Caddy from non-private networks

## 10) Important MariaDB Password Rule

The MariaDB container only applies these on first initialization:

- `TRAINING_HUB_DB_PASSWORD`
- `TRAINING_HUB_DB_ROOT_PASSWORD`

If you change them later, the old `mariadb_data` volume still keeps the old credentials.

Fresh server and no data to keep:

```bash
docker compose down -v
docker compose up -d --build
```

Existing data must stay:

- update the MariaDB users manually
- or use the production promotion script, which already synchronizes the app user password

## 11) Promote To Real Production

Once staging is working, run the one-time production switch:

```bash
chmod +x scripts/promote_to_production.sh
./scripts/promote_to_production.sh
```

What it does:

- backs up `.env`
- generates an internal CA for MariaDB
- generates a MariaDB server certificate for host `mariadb`
- writes MariaDB TLS config under `ops/mariadb/conf.d/ssl.cnf`
- switches `.env` to `TRAINING_HUB_ENV=production`
- enables verified MariaDB TLS in the app config
- forces production security flags
- syncs the MariaDB app user password with the current `.env`
- restarts the production stack

After it finishes, verify:

```bash
docker compose ps
docker compose logs --tail=100 mariadb
docker compose logs --tail=100 training-hub
docker compose logs --tail=100 caddy
```

At this point the app should be in true `production` mode, not only `staging`.

## 12) Post-Production Checks

Run these checks after promotion:

```bash
curl -I https://hub.example.com
curl -I https://hub.example.com/hub
curl -I https://hub.example.com/api/v1/health
curl -I https://hub.example.com/api/v1/metrics
```

Expected:

- main pages work over HTTPS
- `health` and `metrics` are blocked publicly

Also test in the browser:

1. admin login
2. MFA delivery
3. password reset flow
4. upload flow
5. backup creation from admin area

## 13) Day-2 Operations

### Update The App

```bash
cd /srv/scamscreener/app
git pull
docker compose up -d --build
```

### Watch Logs

```bash
docker compose logs -f training-hub
docker compose logs -f caddy
docker compose logs -f mariadb
```

### Restart Services

```bash
docker compose restart training-hub
docker compose restart caddy
docker compose restart mariadb
```

### Stop Everything

```bash
docker compose down
```

Do not use `docker compose down -v` unless you intentionally want to delete the MariaDB volume.

## 14) Backups

There are two layers:

- application-level backups from the admin UI
- infrastructure-level backups of the server and Docker volumes

Recommended:

1. use the admin backup function regularly
2. snapshot or back up the host directory and Docker volumes
3. store backups off-server

Important directories:

- repo: `/srv/scamscreener/app`
- app data: `/srv/scamscreener/app/data`
- generated MariaDB TLS files: `/srv/scamscreener/app/ops/mariadb/ssl`

## 15) Rollback

If a deploy breaks after a code update:

```bash
cd /srv/scamscreener/app
git log --oneline -n 5
git checkout KNOWN_GOOD_COMMIT
docker compose up -d --build
```

If the production promotion changed `.env` and you need to revert:

```bash
ls -1 .env.pre-production.*.bak
cp .env.pre-production.YYYYMMDDTHHMMSSZ.bak .env
docker compose up -d --build
```

## 16) Troubleshooting

### `Access denied for user 'scamscreener'`

Most likely:

- `.env` password changed after MariaDB volume initialization
- old `mariadb_data` volume still exists

Fix:

- fresh server: `docker compose down -v` and start again
- existing data: reset the MariaDB user password manually or rerun the synchronization path

### Caddy does not get a certificate

Check:

- domain resolves to the server
- ports `80` and `443` are open
- no other service is using those ports
- cloud firewall/provider firewall allows inbound `80/443`

### `training-hub` crashes on startup

Check:

```bash
docker compose logs --tail=200 training-hub
```

Common causes:

- wrong SMTP settings
- wrong MariaDB credentials
- `TRAINING_HUB_ALLOWED_HOSTS` not matching the public domain
- trying to run `production` before MariaDB TLS is actually in place

## 17) Recommended Final State

For a correctly deployed production server, the final setup should look like this:

- `Caddy` serves the public domain on `80/443`
- only `caddy` is publicly exposed
- `mariadb` and `training-hub` stay internal
- app runs with `TRAINING_HUB_ENV=production`
- `TRAINING_HUB_ENFORCE_HTTPS=true`
- admin MFA is enabled
- password reset mail is enabled
- verified TLS is enabled between app and MariaDB
- `health` and `metrics` are not publicly reachable
