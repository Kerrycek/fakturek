#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="$PYTHON"
elif [[ -x .venv/bin/python ]]; then
  PYTHON_BIN=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "Python 3 is required." >&2
  echo "Create a virtual environment with: python3 -m venv .venv" >&2
  exit 1
fi

export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

"$PYTHON_BIN" -m compileall -q fakturek tests tools
"$PYTHON_BIN" -m ruff check fakturek tests tools \
  --select B011,B904,F401,F601,F811,F821,F823,F841
"$PYTHON_BIN" -m pytest --disable-warnings
