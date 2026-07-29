#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -e .env ]]; then
  echo ".env already exists; refusing to overwrite it." >&2
  exit 1
fi

base_url="${1:-http://127.0.0.1:8000}"
case "$base_url" in
  https://*) app_env=prod ;;
  http://127.0.0.1:*|http://localhost:*) app_env=dev ;;
  *)
    echo "Use a local HTTP URL or a canonical HTTPS origin." >&2
    echo "Examples: $0 or $0 https://invoices.example.com" >&2
    exit 1
    ;;
esac

random_secret() {
  # Hex stays safe when used in query strings, shell env files and SQLAlchemy URLs.
  openssl rand -hex 32
}

setup_token="$(random_secret)"
db_password="$(random_secret)"
db_root_password="$(random_secret)"

cp .env.example .env
python3 - "$app_env" "$base_url" "$setup_token" "$db_password" "$db_root_password" <<'PY'
from pathlib import Path
import sys

path = Path(".env")
text = path.read_text()
values = {
    "APP_ENV": sys.argv[1],
    "SESSION_SIGNING_KEY": __import__("secrets").token_urlsafe(48),
    "SIGNUP_TOKEN_KEY": __import__("secrets").token_urlsafe(48),
    "PUBLIC_LINK_HMAC_KEY": __import__("secrets").token_urlsafe(48),
    "DATA_ENCRYPTION_KEY": __import__("secrets").token_urlsafe(48),
    "INTERNAL_JOB_TOKEN": __import__("secrets").token_urlsafe(48),
    "SETUP_TOKEN": sys.argv[3],
    "MARIADB_ROOT_PASSWORD": sys.argv[5],
    "MARIADB_PASSWORD": sys.argv[4],
    "PUBLIC_BASE_URL": sys.argv[2],
    "APP_BASE_URL": sys.argv[2],
}
for name, value in values.items():
    prefix = f"{name}="
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = prefix + value
            break
    else:
        lines.append(prefix + value)
    text = "\n".join(lines) + "\n"
path.write_text(text)
PY
chmod 0600 .env

echo "Created .env for ${app_env} mode."
echo "Start Fakturek with: docker compose up -d --build"
echo "Then open: ${base_url}/setup?token=${setup_token}"
echo "After creating the first account, close setup with:"
echo "  sed -i '/^SETUP_TOKEN=/d' .env"
echo "  docker compose up -d --force-recreate app"
echo "Keep this setup URL private. It contains a one-time secret."
