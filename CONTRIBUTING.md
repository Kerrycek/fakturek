# Contributing

Thank you for helping improve Fakturek.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
```

Use development-only random values in `.env`, start MariaDB with `docker compose up -d db`,
then run migrations and the app:

```bash
alembic -c alembic.ini upgrade head
uvicorn fakturek.main:app --reload
```

## Before opening a pull request

```bash
./run_audit_tests.sh
```

Add focused regression tests for behavior changes. Schema changes require an Alembic
migration with one valid parent revision. Do not commit `.env`, customer data, generated
PDFs, database dumps, screenshots containing real data or credentials.

Keep changes focused and follow the existing server-rendered UI patterns. Security issues
must be reported privately according to `SECURITY.md`, not through a public issue.
