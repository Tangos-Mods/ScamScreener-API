# Migration To The OAuth/OIDC Production Stack

This guide describes the real migration path for this repository:

- old production: already the split Docker Compose stack with `scamscreener-hub`, `scamscreener-api`, `marketguard-hub`, `scamscreener-db`, `caddy`, and optional Redis
- new production: the same split Docker Compose topology, but with external OAuth/OIDC sign-in as the production entrypoint

This is not a topology migration from single-container to multi-container.

It is an in-place production migration of:

- application code
- environment configuration
- authentication flow
- database schema and auth data, migrated by the updated app on startup

## Preferred Operational Path

The repository now provides `python3 scripts/migrate.py` for this exact cutover.

That script:

- creates a repo snapshot
- archives the detected Compose volumes
- runs the hardened OAuth/OIDC preflight
- stops the old stack without deleting volumes
- starts the new release through the normal `update.py` path

Use the manual steps below to understand or audit the process. In normal operation, upload the new release, update `.env.production`, and run:

```bash
python3 scripts/migrate.py
```

## What Actually Changes

The important change is authentication, not container layout.

Old stack characteristics:

- split Compose deployment already in use
- MariaDB already in use
- existing persistent volumes already in use
- users signed in through the old local account flow

New stack characteristics:

- same Compose deployment model
- same persistent Docker volumes
- external sign-in through GitHub OAuth and/or Authelia OIDC
- external identities stored and linked to existing local users

In normal cases you do not restore into a fresh empty stack. You update the existing stack in place and let the application run its schema migrations.

## Critical Compatibility Rule

Existing local users will only map cleanly to the new external sign-in flow if their old account email addresses match the verified email returned by the external provider.

That matters because first external sign-in links users by provider identity or, for the first login, by existing local email address.

Before cutover, verify at least these accounts in the old system:

- your main admin account
- any other admin accounts
- any production user accounts that must retain access immediately after cutover

If a user's old email does not match the external provider's verified email, the new login can create a different account or fail to link as intended.

## Before You Start

Plan a maintenance window.

This migration changes the public login method. Treat it like a production auth cutover, not like a routine rebuild.

Do not use `python3 scripts/reset.py` during this migration.

`reset.py` deletes persistent volumes and is the wrong tool for an in-place upgrade.

## 1) Confirm You Are On The Old Split Stack

SSH to the server and go to the deployment directory:

```bash
cd /srv/scamscreener
```

Check the running services:

```bash
docker compose ps
```

You should already see the split stack services, typically:

- `scamscreener-db`
- `scamscreener-hub`
- `scamscreener-api`
- `marketguard-hub`
- `scamscreener-caddy`
- optionally `scamscreener-redis`

If you do not see this topology, stop here and reassess. This guide assumes the old stack is already the split Compose deployment.

## 2) Verify The Existing Persistent State

Capture the currently used volumes:

```bash
docker compose config --volumes
docker inspect scamscreener-db --format '{{range .Mounts}}{{println .Destination "|" .Type "|" .Name "|" .Source}}{{end}}'
docker inspect scamscreener-hub --format '{{range .Mounts}}{{println .Destination "|" .Type "|" .Name "|" .Source}}{{end}}'
docker inspect scamscreener-caddy --format '{{range .Mounts}}{{println .Destination "|" .Type "|" .Name "|" .Source}}{{end}}'
```

Save that output in your change notes.

You want to preserve the existing volumes across the upgrade.

## 3) Create A Functional Application Backup

While the old stack is still online:

1. log in to the old admin UI
2. create an admin backup archive
3. download it off the server

Keep that file as an application-level restore point.

## 4) Create Volume-Level Rollback Backups

If you use `scripts/migrate.py`, it creates these backups for you automatically in a timestamped sibling directory such as `/srv/scamscreener-migration-backups/20260610-120000Z/`.

Create a backup directory:

```bash
mkdir -p /srv/scamscreener-migration-backups
chmod 700 /srv/scamscreener-migration-backups
```

Backup the current release files:

```bash
cp -a /srv/scamscreener /srv/scamscreener-migration-backups/repo-pre-oauth-migration
```

Archive the shared app data volume:

```bash
docker run --rm \
  -v <APP_DATA_VOLUME_FROM_STEP_2>:/from:ro \
  -v /srv/scamscreener-migration-backups:/to \
  alpine:3.20 \
  sh -c 'cd /from && tar czf /to/scamscreener-data-pre-oauth.tar.gz .'
```

Archive the MariaDB volume:

```bash
docker run --rm \
  -v <MARIADB_VOLUME_FROM_STEP_2>:/from:ro \
  -v /srv/scamscreener-migration-backups:/to \
  alpine:3.20 \
  sh -c 'cd /from && tar czf /to/scamscreener-db-pre-oauth.tar.gz .'
```

Optionally archive the Caddy state too:

```bash
docker run --rm \
  -v <CADDY_DATA_VOLUME_FROM_STEP_2>:/from:ro \
  -v /srv/scamscreener-migration-backups:/to \
  alpine:3.20 \
  sh -c 'cd /from && tar czf /to/caddy-data-pre-oauth.tar.gz .'

docker run --rm \
  -v <CADDY_CONFIG_VOLUME_FROM_STEP_2>:/from:ro \
  -v /srv/scamscreener-migration-backups:/to \
  alpine:3.20 \
  sh -c 'cd /from && tar czf /to/caddy-config-pre-oauth.tar.gz .'
```

If Redis is enabled and you want a fully conservative rollback point, back that state up too.

## 5) Prepare The Future External Login Mapping

Before deployment, decide which provider will be authoritative for the first cutover.

Supported production choices in the current codebase:

- GitHub OAuth
- Authelia OIDC

For each admin who must retain access immediately, make sure:

- the provider account is explicitly allowlisted
- the provider returns the same verified email address that the old local account already has
- `TRAINING_HUB_ADMIN_USERNAMES` and/or `TRAINING_HUB_ADMIN_EMAILS` still contain the intended admin identity

Recommended minimum cutover identity set:

- one primary admin by username
- one admin email
- one provider allowlist entry for the same person

## 6) Update `.env.production`

Copy the new release into the same deployment directory if possible, then edit the existing production env file:

```bash
cd /srv/scamscreener
nano .env.production
chmod 600 .env.production
```

Keep the existing values that are still valid:

- `CADDY_SITE_ADDRESS`
- `TRAINING_HUB_PUBLIC_BASE_URL`
- `TRAINING_HUB_ALLOWED_HOSTS`
- MariaDB and Redis settings
- SMTP settings
- legal/privacy values
- `TRAINING_HUB_SECRET_KEY`

Then add the new external provider configuration.

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

Keep these admin anchors explicit:

```env
TRAINING_HUB_ADMIN_USERNAMES=your-admin-username
TRAINING_HUB_ADMIN_EMAILS=admin@example.com
```

If your production policy still keeps email-based admin MFA or password-reset operations enabled, also keep SMTP configured.

## 7) Verify The New Auth Config Before Downtime

Run preflight against the updated release and env file:

```bash
bash scripts/preflight.sh
```

Do not continue until it passes.

Specifically validate:

- domain and public base URL still match
- SMTP settings still satisfy the enabled account operations

Also manually verify before cutover:

- provider client ID and secret are both present
- provider allowlists are not empty
- the chosen provider account really returns the email address you expect to map

## 8) Stop The Old Stack Without Deleting Volumes

Once backups and config are ready:

```bash
docker compose down
```

Do not use `docker compose down -v`.

Do not delete any volumes.

## 9) Upload The New Release

Upload the updated repository contents into the same deployment directory.

Then confirm the expected files exist:

```bash
ls -la
test -f docker-compose.yml
test -f .env.production.example
test -f scripts/update.py
test -f scripts/preflight.sh
```

If you changed files while the old stack was still running, rerun preflight once after upload:

```bash
bash scripts/preflight.sh
```

## 10) Start The New OAuth/OIDC Release In Place

Preferred command:

```bash
python3 scripts/migrate.py
```

Equivalent manual start path after backup and validation:

```bash
python3 scripts/update.py
```

This should reuse the existing volumes and run the new application against the current MariaDB data.

Verify service state:

```bash
docker compose ps
```

Expected:

- `scamscreener-db` healthy
- `scamscreener-hub` healthy
- `scamscreener-api` healthy
- `marketguard-hub` healthy
- `caddy` running

## 11) Perform The First External Sign-In

Open:

```text
https://YOUR_DOMAIN/hub
```

Sign in with the external provider account that matches your existing admin account.

What should happen on the first successful login:

- the provider account is accepted by the allowlist
- the verified provider email matches the existing local user email
- the external identity is linked to the existing local user
- the existing admin privileges remain intact

If that admin mapping fails, do not continue broad rollout before you understand why.

## 12) Verify Admin And User Mapping

After the first login, verify:

1. the intended admin account still lands on the correct existing profile
2. admin access still works
3. no duplicate unintended admin user was created
4. one representative normal user can also sign in successfully if available
5. dashboard data, uploads, and bundles are still present

Recommended checks:

```bash
docker compose logs --tail=100 scamscreener-hub
docker compose logs --tail=100 scamscreener-api
docker compose logs --tail=100 scamscreener-db
docker compose logs --tail=100 marketguard-hub
docker compose logs --tail=100 caddy
```

## 13) Verify The Public Surface

From your workstation:

```bash
curl -I https://YOUR_DOMAIN/
curl -I https://YOUR_DOMAIN/hub
curl -I https://YOUR_DOMAIN/market/
curl -I https://YOUR_DOMAIN/api/v1/lowestbin
curl -I https://YOUR_DOMAIN/api/v2/lowestbin
curl -I https://YOUR_DOMAIN/api/v1/health
curl -I https://YOUR_DOMAIN/api/v1/metrics
```

Expected:

- the site loads
- the hub login entrypoint works
- the market frontend works
- public API endpoints work
- health and metrics stay blocked publicly

## 14) Post-Cutover Cleanup

After the migration is proven stable:

- remove obsolete local-auth operational notes from your internal runbooks
- update any admin instructions to reference external sign-in
- keep the backup archives until you have completed a full rollback-free observation period

## 15) Rollback If The OAuth Cutover Fails

If the new release is unhealthy or the auth mapping is wrong:

1. stop the new release:

```bash
docker compose down
```

2. restore the previous repository checkout from `/srv/scamscreener-migration-backups/repo-pre-oauth-migration`
3. restore the previous `.env.production`
4. start the previous release again:

```bash
docker compose up -d
```

If the newer release modified database or shared data in a way you do not trust, restore the archived volumes before restarting the old release.

## 16) Mistakes To Avoid

Do not:

- treat this like a single-container-to-compose migration
- run `reset.py`
- run `docker compose down -v`
- change the deployment directory name without understanding Compose volume naming
- cut over before verifying provider emails match existing local accounts
- allow broad user traffic before at least one real admin mapping succeeds

## Migration Summary

The real sequence is:

1. verify the old stack is already the split Compose deployment
2. back up app data and volumes
3. prepare external provider allowlists and matching admin identities
4. update `.env.production`
5. run preflight
6. stop the old release without deleting volumes
7. upload the new release
8. start the updated stack in place
9. test the first external admin login
10. validate user mapping and public behavior
