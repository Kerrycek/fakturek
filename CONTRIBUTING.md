# Contributing

Thank you for helping improve Fakturek.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install --require-hashes -r requirements-dev.lock
cp .env.example .env
```

When dependencies change, regenerate the hashed lock files with
`pip-tools==7.5.3`:

```bash
pip-compile --generate-hashes --strip-extras \
  --output-file=requirements.lock requirements.txt
pip-compile --generate-hashes --allow-unsafe --strip-extras \
  --constraint=requirements.lock \
  --output-file=requirements-dev.lock requirements-dev.txt
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
