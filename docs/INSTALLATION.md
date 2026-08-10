# Installation and operations

Fakturek does not require Apache, PHP or a host-installed database. The recommended
deployment runs the FastAPI application and MariaDB in Docker Compose. A small reverse
proxy such as Caddy publishes the loopback-only application over HTTPS.

## Local Docker installation

Requirements: Docker with Compose, Python 3 and OpenSSL. MariaDB 10.11 is included in the
stack and is the supported database; SQLite is not a supported deployment target.

```bash
git clone https://github.com/Kerrycek/fakturek.git
cd fakturek
./tools/init_env.sh
docker compose up -d --build
docker compose ps
```

Open the setup URL printed by `init_env.sh`. The application and database are bound only
to the local Docker host/network by default. The database has no published host port.

After creating the first account, remove `SETUP_TOKEN` from `.env` and restart:

```bash
sed -i '/^SETUP_TOKEN=/d' .env
docker compose up -d --force-recreate app
```

## Fresh Debian VPS with Caddy

This walkthrough was verified on a clean Debian 13 VPS. Debian 12 works with the same
package names. Commands in this section assume a root shell; when using a regular admin
account, prefix system commands with `sudo`.

### 1. Point a domain at the server

Create an `A` record for a domain such as `invoices.example.com` pointing to the VPS IPv4
address. Add an `AAAA` record only when IPv6 is configured and reachable. Wait until the
name resolves to the server before starting Caddy, because public DNS and inbound ports
80/443 are required for automatic TLS certificates.

Allow these inbound TCP ports in the provider firewall and host firewall:

- `22` for SSH (preferably restricted to trusted source addresses);
- `80` for the HTTP-to-HTTPS redirect and ACME validation;
- `443` for the application over HTTPS.

Do not expose ports `8000` or `3306`. The provided Compose file binds the application only
to `127.0.0.1` and does not publish MariaDB at all.

### 2. Install the host packages

```bash
apt update
apt install -y docker.io docker-compose caddy git curl python3 openssl unattended-upgrades
systemctl enable --now docker caddy
```

Check that Docker and Compose are available:

```bash
docker version
docker compose version
```

The Debian `docker-compose` package provides the modern `docker compose` command. You do
not need Python, MariaDB, Apache, Nginx or PHP installed directly on the host.

### 3. Download and configure Fakturek

```bash
install -d -m 0755 /opt/fakturek
git clone https://github.com/Kerrycek/fakturek.git /opt/fakturek
cd /opt/fakturek
./tools/init_env.sh https://invoices.example.com
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Replace `invoices.example.com` with the exact public hostname. `init_env.sh` creates a
mode-`0600` `.env` file with independent random application and database secrets. Store
the printed one-time setup URL somewhere private until bootstrap is complete.

The initial image build can take several minutes on a small VPS. Follow progress or inspect
startup failures with:

```bash
docker compose logs -f app
docker compose logs --tail=200 db
```

### 4. Publish it through Caddy

Write `/etc/caddy/Caddyfile`:

```caddyfile
invoices.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
```

Then validate and reload Caddy:

```bash
caddy fmt --overwrite /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
journalctl -u caddy --since "10 minutes ago" --no-pager
```

Caddy obtains and renews the TLS certificate automatically. If certificate issuance fails,
verify DNS and that ports 80 and 443 reach this VPS.

### 5. Create the first account and close setup

Open the `/setup` URL printed by `init_env.sh` and paste the separately printed one-time
token into the form. Do not publish or log the token. Create the first account with a
unique password of at least 12 characters.

Immediately afterward, remove the bootstrap token and recreate the app container:

```bash
cd /opt/fakturek
sed -i '/^SETUP_TOKEN=/d' .env
docker compose up -d --force-recreate app
```

Confirm that setup is closed and the application is healthy:

```bash
curl --fail --silent --show-error https://invoices.example.com/healthz
curl --fail --silent --show-error https://invoices.example.com/healthz/db
curl --output /dev/null --silent --write-out '%{http_code}\n' \
  https://invoices.example.com/setup
```

The health endpoints should return `{"status":"ok"}` and `/setup` should return `404`.

### 6. Finish the application setup

Sign in and configure the issuer profile and at least one bank account in Settings. Then
create a test contact and invoice, and verify all of the following before real use:

- invoice detail and generated PDF;
- public invoice link in an incognito browser;
- CSV and ZIP exports;
- SMTP delivery, when e-mail sending is required;
- a database dump and an archive of the `app_data` volume.

### 7. Secure and operate the VPS

Before exposing a real installation:

- add an SSH key, create a named sudo administrator and disable password-only root login;
- rotate any password that was ever shared through chat, tickets or shell history;
- enable a firewall and allow only SSH, HTTP and HTTPS;
- keep automatic security updates enabled and review pending reboots;
- keep `.env` mode `0600`, outside backups that are shared with third parties;
- schedule encrypted, off-server backups and test a restore;
- monitor `docker compose ps`, application logs and available disk space.

## Production installation

Generate production configuration with the canonical HTTPS origin:

```bash
./tools/init_env.sh https://invoices.example.com
```

Put a TLS-terminating reverse proxy in front of `127.0.0.1:${APP_PORT:-8000}`. Keep the
database internal, configure `TRUSTED_PROXY_IPS` to the actual proxy addresses, and do not
publish the application container directly to the internet.

Before accepting users:

- configure SMTP if invoice e-mail delivery is required;
- back up the MariaDB volume and the `app_data` volume together;
- remove `SETUP_TOKEN` after bootstrap;
- verify `/healthz` and `/healthz/db` through the public HTTPS origin;
- test PDF generation and a public invoice link;
- keep all values in `.env` outside version control.

The complete copy-paste deployment path is in
[Fresh Debian VPS with Caddy](#fresh-debian-vps-with-caddy).

## Updates

```bash
git pull --ff-only
docker compose build --pull app
docker compose up -d
docker compose ps
```

The container entrypoint runs `alembic upgrade head` before starting the web process.
Take a database backup before every update.

## Backups

Back up both named volumes. A database dump is preferable to copying a live database
volume. Because shell variables in the command are expanded on the host, load the
protected `.env` first:

```bash
set -a
. ./.env
set +a
docker compose exec -T db mariadb-dump \
  -u"$MARIADB_USER" -p"$MARIADB_PASSWORD" "$MARIADB_DATABASE" > fakturek.sql
test -s fakturek.sql
```

Also archive the `app_data` volume, which contains persisted PDFs, imports and logs. Test
restores regularly; an untested backup is not a recovery plan.

## Troubleshooting

```bash
docker compose ps
docker compose logs --tail=200 app
docker compose logs --tail=200 db
docker compose exec app alembic -c /app/alembic.ini current
docker compose exec app python -m pip check
```

Production startup intentionally fails when security keys are missing, duplicated, too
short, or still contain placeholder values. It also rejects non-canonical production URLs.
