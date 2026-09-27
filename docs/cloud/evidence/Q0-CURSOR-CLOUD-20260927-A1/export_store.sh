#!/usr/bin/env bash
# Copy probe store to tracked fixture export and write SHA-256 manifest.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
JOB="Q0-CURSOR-CLOUD-20260927-A1"
SRC="$(cat "$ROOT/docs/cloud/evidence/$JOB/receipts/probe_store_path.txt" | tr -d '\n')"
DEST="$ROOT/tests/fixtures/q0/$JOB/store-export"
MANIFEST="$ROOT/tests/fixtures/q0/$JOB/MANIFEST.sha256"

if [[ ! -d "$SRC" ]]; then
  echo "BLOCKED: probe store missing at $SRC" >&2
  exit 2
fi

rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
cp -a "$SRC/." "$DEST/"

{
  echo "# Q0 store export manifest — $JOB"
  echo "# generated $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  cd "$DEST"
  find . -type f ! -path './MANIFEST.sha256' -print0 | sort -z | while IFS= read -r -d '' f; do
    f="${f#./}"
    sz=$(wc -c <"$f" | tr -d ' ')
    hash=$(sha256sum "$f" | awk '{print $1}')
    printf '%s  %s  bytes=%s\n' "$hash" "$f" "$sz"
  done
} > "$MANIFEST"

echo "Exported $SRC -> $DEST"
echo "Manifest: $MANIFEST"
