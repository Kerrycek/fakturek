#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  ./tools/safe_extract.sh <tarball.tar.gz> <dest_dir>

Hard rule:
  This script refuses to extract if <dest_dir> is NOT empty.
  Never unpack a tarball over an existing working directory.

Example:
  mkdir -p workdir
  ./tools/safe_extract.sh project.tar.gz workdir
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

tb="${1:-}"
dest="${2:-}"

if [[ -z "${tb}" || -z "${dest}" ]]; then
  usage
  exit 2
fi

if [[ ! -f "${tb}" ]]; then
  echo "ERROR: tarball not found: ${tb}" >&2
  exit 2
fi

if [[ -e "${dest}" && ! -d "${dest}" ]]; then
  echo "ERROR: destination exists but is not a directory: ${dest}" >&2
  exit 2
fi

mkdir -p "${dest}"

if find "${dest}" -mindepth 1 -maxdepth 1 | read -r _; then
  echo "ERROR: destination directory is not empty: ${dest}" >&2
  echo "Refusing to extract over existing data." >&2
  echo "Fix: delete the directory or choose a fresh empty directory." >&2
  exit 3
fi

tar --no-same-owner --no-same-permissions -xzf "${tb}" -C "${dest}"

echo "OK: extracted into ${dest}"
