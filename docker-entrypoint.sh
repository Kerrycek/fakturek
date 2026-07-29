#!/bin/sh
set -eu
alembic -c /app/alembic.ini upgrade head
exec uvicorn fakturek.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips="${TRUSTED_PROXY_IPS:-127.0.0.1,::1}"
