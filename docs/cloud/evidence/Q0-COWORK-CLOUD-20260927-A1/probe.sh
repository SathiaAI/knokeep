#!/usr/bin/env bash
# Q0-COWORK-CLOUD-20260927-A1 reproducible probe.
# Usage (from repo root at input commit 49e4c1cc6ad75113c97de856a069a670bf0696df):
#   bash docs/cloud/evidence/Q0-COWORK-CLOUD-20260927-A1/probe.sh /path/to/new/empty/store
# Writes per-step logs to $E/logs/NN-name.{cmd,out,err,exit}. Never reads env/credentials.
set -u
E=docs/cloud/evidence/Q0-COWORK-CLOUD-20260927-A1
STORE="${1:?store path required}"
P=q0-cowork-cloud-a1-plan
PY=python3
unset KNOKEEP_STORE
mkdir -p "$E/logs"
[ -e "$STORE" ] && { echo "refusing: store path exists: $STORE" >&2; exit 2; }

run() { # run NN-name command...
  local n="$1"; shift
  printf '%q ' "$@" | sed "s#$STORE#\$STORE#g" > "$E/logs/$n.cmd"; echo >> "$E/logs/$n.cmd"
  "$@" > "$E/logs/$n.out" 2> "$E/logs/$n.err"; echo $? > "$E/logs/$n.exit"
  echo "$n exit=$(cat "$E/logs/$n.exit")"
}

# --- agent-to-KnoKeep CLI path (skill/knokeep_state.py, LocalBackend) ---
run 01-cli-init            $PY skill/knokeep_state.py init --store "$STORE" --project $P
# flush requires CAS: read current hashes via bootstrap (attempt 1 omitted --expect-hash and was rejected; see logs-attempt1/)
run 01b-cli-bootstrap-pre  $PY skill/knokeep_state.py bootstrap --store "$STORE" --project $P
VH=$($PY -c 'import json,sys;print(json.load(open(sys.argv[1]))["version_hash"])' "$E/logs/01b-cli-bootstrap-pre.out")
LH=$($PY -c 'import json,sys;print(json.load(open(sys.argv[1]))["log_hash"])' "$E/logs/01b-cli-bootstrap-pre.out")
run 02-cli-flush-state     $PY skill/knokeep_state.py flush-state --store "$STORE" --project $P --body-file $E/inputs/system_state.md --session-id q0-a1-s1 --expect-hash "$VH"
run 03-cli-flush-log       $PY skill/knokeep_state.py flush-log --store "$STORE" --project $P --body-file $E/inputs/session_log.md --session-id q0-a1-s1 --expect-hash "$LH"
run 04-cli-session-append  $PY skill/knokeep_state.py session-append --store "$STORE" --project $P --session-id q0-a1-s1 --client cowork-cloud --entry-file $E/inputs/journal_entry.txt
run 05-cli-fp-probe        $PY skill/knokeep_state.py session-append --store "$STORE" --project $P --session-id q0-a1-fp --client cowork-cloud --entry-file $E/inputs/fp_probe_entry.txt
run 06-cli-bootstrap       $PY skill/knokeep_state.py bootstrap --store "$STORE" --project $P

# --- shell-created MCP protocol harness (mcp/server.py over stdio, --backend local) ---
run 07-mcp-harness         $PY $E/mcp_harness.py "$STORE" $P $E/inputs/mcp_harness_note.txt

# --- independent verification in a separate process ---
run 08-verify              $PY $E/verify.py "$STORE" $E/logs
