#!/usr/bin/env bash
# Appended correction (run after probe.sh). Step 03 stored non-schema headings (## Active / ## Next step),
# so bootstrap's resume_line was empty. This re-flushes the log with schema headings via CAS; original bytes stay in journal/ and logs/.
set -u
E=docs/cloud/evidence/Q0-COWORK-CLOUD-20260927-A1
STORE="${1:?store path required}"; P=q0-cowork-cloud-a1-plan; PY=python3; unset KNOKEEP_STORE
run() { local n="$1"; shift; printf '%q ' "$@" | sed "s#$STORE#\$STORE#g" > "$E/logs/$n.cmd"; echo >> "$E/logs/$n.cmd"
  "$@" > "$E/logs/$n.out" 2> "$E/logs/$n.err"; echo $? > "$E/logs/$n.exit"; echo "$n exit=$(cat "$E/logs/$n.exit")"; }
LH=$($PY -c 'import json,sys;print(json.load(open(sys.argv[1]))["log_hash"])' "$E/logs/06-cli-bootstrap.out")
run 09-cli-flush-log-corrected $PY skill/knokeep_state.py flush-log --store "$STORE" --project $P --body-file $E/inputs/session_log_corrected.md --session-id q0-a1-s1 --expect-hash "$LH"
run 10-cli-bootstrap-final     $PY skill/knokeep_state.py bootstrap --store "$STORE" --project $P
run 11-verify-final            $PY $E/verify.py "$STORE" $E/logs
