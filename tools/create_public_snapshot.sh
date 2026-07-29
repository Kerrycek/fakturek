#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT=${1:-}

if [[ -z "$OUTPUT" ]]; then
  echo "Usage: $0 /new/path/fakturek-public-snapshot" >&2
  exit 2
fi
if [[ -e "$OUTPUT" ]]; then
  echo "Output already exists: $OUTPUT" >&2
  exit 2
fi
if [[ -n "$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=all)" ]]; then
  echo "Source checkout is dirty" >&2
  exit 1
fi

"$SOURCE_ROOT/tools/verify_public_release.py"
mkdir -p "$OUTPUT"
git -C "$SOURCE_ROOT" archive HEAD | tar -x -C "$OUTPUT"
git -C "$OUTPUT" init -q -b main
git -C "$OUTPUT" add -A
git -C "$OUTPUT" \
  -c user.name="Fakturek Release" \
  -c user.email="release@fakturek.cz" \
  -c commit.gpgsign=false \
  commit -qm "Initial open-source release"
"$OUTPUT/tools/verify_public_release.py" --require-single-root

printf 'snapshot=%s\ncommit=%s\ntree=%s\n' \
  "$OUTPUT" \
  "$(git -C "$OUTPUT" rev-parse HEAD)" \
  "$(git -C "$OUTPUT" rev-parse 'HEAD^{tree}')"
