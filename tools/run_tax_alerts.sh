#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

env_file="${FAKTUREK_ENV_FILE:-.env}"
if [[ -f "$env_file" ]]; then
  set -a
  . "$env_file"
  set +a
fi

base_url="${INTERNAL_BASE_URL:-http://127.0.0.1:${APP_PORT:-8000}}"
job_token="${INTERNAL_JOB_TOKEN:-}"

if [[ -z "${job_token}" ]]; then
  echo "INTERNAL_JOB_TOKEN is not configured" >&2
  exit 1
fi

curl --fail --silent --show-error \
  -H "X-Internal-Job-Token: ${job_token}" \
  -X POST "${base_url%/}/internal/jobs/tax-alerts"
