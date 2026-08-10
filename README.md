# Fakturek

Fakturek is a self-hosted invoicing application for small businesses and solo operators.
It is built with FastAPI, Jinja2, HTMX and MySQL/MariaDB.

The project covers the practical workflow around contacts, invoices, public invoice links,
PDF output, exports, imports and basic payment tracking.

## Project Status

Fakturek is open-source software under active development. The Docker Compose setup is the
recommended path for a fresh self-hosted installation.

## Features

- Contacts and invoice customers
- Multiple subjects / issuer profiles
- Draft, issued, sent and paid invoice workflow
- Invoice numbering series
- PDF generation and persisted PDFs for issued invoices
- Public invoice links with PDF download
- ISDOC export
- CSV/XML/ZIP import and export tools
- ARES and Slovak RPO company lookup
- E-mail sending through SMTP
- Payment overview and manual payment state handling
- API v1 for selected invoice and contact operations
- Optional extension hook for installation-specific customizations

## Tech Stack

- Python 3.12+
- FastAPI / Starlette
- Jinja2 templates
- HTMX-enhanced server-rendered UI
- SQLAlchemy + Alembic
- MySQL or MariaDB
- WeasyPrint for PDF rendering, with ReportLab fallback

## Quick Start with Docker

You do not need a LAMP stack. Fakturek ships its Python application and MariaDB in
Docker Compose; the host only needs Docker, Compose and a reverse proxy for HTTPS.

```bash
git clone https://github.com/Kerrycek/fakturek.git
cd fakturek
./tools/init_env.sh
```

The script creates `.env` with independent random secrets and prints the local setup URL
and a separate one-time token.
For a production installation, pass the canonical HTTPS origin to the script as described
in the installation guide. Then start the stack:

```bash
docker compose up -d --build
docker compose ps
```

The app listens only on `127.0.0.1:${APP_PORT:-8000}`. MariaDB is available only
inside the Compose network. Open `/setup`, paste the token printed by the script, create
the first account, then remove `SETUP_TOKEN` from `.env` and restart the app.

For a fresh Debian VPS, including DNS, Caddy, HTTPS, firewall and post-install checks,
follow the tested [production walkthrough](docs/INSTALLATION.md#fresh-debian-vps-with-caddy).

## Manual Development Setup

### 1. Create a virtual environment

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install --require-hashes -r requirements-dev.lock
```

### 2. Configure environment

```bash
cp .env.example .env
```

For local development, check at least these values in `.env`:

```bash
APP_ENV=dev
AUTH_REQUIRED=0
DATABASE_URL=mysql+pymysql://fakturek:fakturek@127.0.0.1:3306/fakturek
SESSION_SIGNING_KEY=change-me-in-development
SIGNUP_TOKEN_KEY=change-me-in-development-signup
PUBLIC_LINK_HMAC_KEY=change-me-in-development-public
DATA_ENCRYPTION_KEY=change-me-in-development-encryption
INTERNAL_JOB_TOKEN=change-me-in-development-jobs
SETUP_TOKEN=change-me-in-development-setup
PUBLIC_BASE_URL=http://127.0.0.1:8000
PDF_STORAGE_DIR=var/pdfs
```

### 3. Start MariaDB with Docker Compose

```bash
docker compose up -d db
```

If you prefer a local MariaDB/MySQL server, create the database manually:

```sql
CREATE DATABASE fakturek CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'fakturek'@'localhost' IDENTIFIED BY 'fakturek';
CREATE USER 'fakturek'@'127.0.0.1' IDENTIFIED BY 'fakturek';
GRANT ALL PRIVILEGES ON fakturek.* TO 'fakturek'@'localhost';
GRANT ALL PRIVILEGES ON fakturek.* TO 'fakturek'@'127.0.0.1';
FLUSH PRIVILEGES;
```

### 4. Run migrations

```bash
alembic -c alembic.ini upgrade head
```

### 5. Start the app

```bash
uvicorn fakturek.main:app --reload --port 8000
```

Open:

- App: http://127.0.0.1:8000/
- Setup: http://127.0.0.1:8000/setup (paste `SETUP_TOKEN` into the form)
- Health check: http://127.0.0.1:8000/healthz
- Database health check: http://127.0.0.1:8000/healthz/db

## First Account

When the database is empty, create the first account through `/setup`.

For production-like environments:

```bash
APP_ENV=prod
AUTH_REQUIRED=1
SETUP_TOKEN=some-long-random-token
```

Then open `/setup` and paste the token into the protected form:

```text
http://127.0.0.1:8000/setup
```

After the first account exists, setup is skipped unless the environment explicitly allows it.

## Common Configuration

Most runtime configuration is done through environment variables. See `.env.example` for
the full list.

Important groups:

- `DATABASE_URL` - SQLAlchemy database connection string
- `SESSION_SIGNING_KEY` - session-cookie signing
- `SIGNUP_TOKEN_KEY` - signup and password-reset links
- `PUBLIC_LINK_HMAC_KEY` - public invoice-link signatures
- `DATA_ENCRYPTION_KEY` - encryption of stored banking and integration secrets
- `INTERNAL_JOB_TOKEN` - internal maintenance endpoint authentication
- `AUTH_REQUIRED` - require login for the app UI
- `PUBLIC_BASE_URL` - base URL used for public invoice links and e-mails
- `PDF_STORAGE_DIR` - directory for persisted invoice PDFs
- `SMTP_*` - SMTP host, credentials, TLS mode and sender identity
- `ARES_*`, `SK_RPO_*`, `SK_ORSR_*` - company lookup providers
- `PUBLIC_RATE_LIMIT_*` - lightweight public-link rate limiting
- `IMPORT_STORAGE_DIR` - uploaded import file storage

Secrets should never be committed. Use `.env`, environment-specific secret stores, systemd
environment files or your platform's secret manager.

## Development

Run the test suite:

```bash
.venv/bin/python -m pytest
```

Verify that the distributable tree contains no hosted Fakturek.cz billing or
operator surface:

```bash
./tools/verify_public_release.py
```

Run a narrower test file:

```bash
.venv/bin/python -m pytest tests/test_pages.py -q
```

Check Python syntax quickly:

```bash
python3 -m py_compile fakturek/main.py
```

Apply database migrations after pulling schema changes:

```bash
alembic -c alembic.ini upgrade head
```

## PDFs

Invoice PDFs are rendered from HTML. The preferred renderer is WeasyPrint; if it is not
available, Fakturek can fall back to ReportLab for a simpler PDF output.

Issued invoices can store generated PDFs on disk. Keep `PDF_STORAGE_DIR` outside the web
root and include it in backups.

## Public Invoice Links

Issued invoices can expose a tokenized public link. Public endpoints are rate-limited and
do not require account login.

Typical public routes:

```text
/{public_username}/i/{token}/{invoice_number}
/{public_username}/i/{token}/{invoice_number}/pdf
```

## Imports and Exports

Fakturek includes import/export helpers for practical migration and bookkeeping workflows,
including CSV, XML, ISDOC and ZIP-based flows. Some importers are intentionally conservative:
they parse input into reviewable results before mutating live data.

## Project Documentation

- [Installation and operations](docs/INSTALLATION.md)
- [Architecture overview](docs/ARCHITECTURE.md)
- [API v1](docs/API_V1.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## Optional Extensions

The core can load an optional extension module through `fakturek.extensions`. Set
`FAKTUREK_EXTENSION_MODULE` to an installed Python module exposing
`register_fakturek_extension(app, context=...)`. Leave it unset for the standalone app.

An extension runs inside the Fakturek process and receives access to the application,
runtime settings, templates, and project paths. Treat it as fully trusted server code:
install only modules you control, pin their versions, and never use this hook to load
tenant-provided or otherwise untrusted packages.

## Production Notes

For production deployments:

- Use `APP_ENV=prod` and `AUTH_REQUIRED=1`
- Set independent random values for every security key
- Keep `.env` and PDF/import storage outside the repository
- Run behind a TLS-terminating reverse proxy
- Back up the database and `PDF_STORAGE_DIR`
- Run migrations before app restart
- Configure SMTP only through secrets/environment
- Remove `SETUP_TOKEN` after bootstrap
- For multiple workers or replicas, enforce a shared rate limit at the reverse proxy or with
  an external rate-limit service; built-in limits are local to one application process

MariaDB/MySQL is required. SQLite is used by selected unit tests, but it is not a supported
deployment database and the historical Alembic chain is not designed for it.

## Security

See [`SECURITY.md`](SECURITY.md). Do not report vulnerabilities through public issues.

## License

Fakturek is released under the MIT License. See [`LICENSE`](LICENSE).
