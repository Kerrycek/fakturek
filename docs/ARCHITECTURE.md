# Architecture

Fakturek is a server-rendered FastAPI application. It keeps the deployment deliberately
small: one web process, one MariaDB database and persistent storage for generated files.

## Main components

- `fakturek/main.py` creates the FastAPI application and owns browser routes and workflows.
- `fakturek/api_v1.py` contains the bearer-token API.
- `fakturek/models.py` defines the SQLAlchemy persistence model.
- `alembic/` contains the ordered database migrations.
- `templates/` contains Jinja2 views; `static/` contains the application styles and scripts.
- `fakturek/pdf.py`, `pdf_store.py` and `isdoc.py` generate and persist document outputs.
- `fakturek/importing.py` and `export_formats.py` handle data exchange.
- `fakturek/bank_sync.py` and `registry_sync.py` implement optional external synchronization.
- `fakturek/extensions.py` provides the optional deployment extension hook.

## Trust boundaries

Browser authentication uses signed sessions and CSRF protection. API clients use scoped
bearer tokens tied to one subject. Public invoice links use separate HMAC material and are
rate-limited. Stored integration secrets use a dedicated encryption key.

The four key classes are intentionally independent:

- session signing;
- signup and password-reset tokens;
- public invoice links;
- encryption of stored secrets.

Production configuration rejects missing, weak, placeholder or duplicated values.

## Data and files

MariaDB is the source of truth for accounts, subjects, contacts, invoices and audit data.
Issued PDFs and uploaded imports live under the configured persistent storage directory.
Database and file storage must be backed up together.

## Request lifecycle

Middleware validates trusted hosts, request size and security headers before a route parses
the request. Authenticated mutations pass session and CSRF checks. Tenant access is checked
against `user_subjects`; API tokens are scoped independently. Audit records are written for
sensitive account, invoice and integration actions.

## Extensions

An installation may set `FAKTUREK_EXTENSION_MODULE` to an installed Python module with a
`register_fakturek_extension(app, context=...)` hook. The context currently contains the
application settings, Jinja environment and project root. The core never requires an
extension, and a clean self-hosted installation runs without one.

Extensions are not sandboxed plugins. They execute with the same operating-system
permissions and access to runtime secrets as the core application. Operators must only
configure audited, pinned modules under their control; untrusted extension code is a
complete application compromise.
