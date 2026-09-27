#!/usr/bin/env bash
# Q0-DEVIN-CLOUD-20260927-A1 -- attempt 2, corrections appended to attempt 1.
# Attempt 1 (probe.sh) failed three steps for caller error, not product defect:
#   * flush-state requires --expect-hash (CAS guard) for an update,
#   * flush-log takes --body-file (not --entry),
#   * MCP tools/call requires the 'notifications/initialized' notification first.
# Attempt 1 logs are preserved; this script re-runs only those steps.
# Synthetic data only. No credentials are read, printed or stored.

set -u
REPO="${REPO:-$HOME/repos/knokeep}"
STORE="${STORE:-$HOME/q0-store/Q0-DEVIN-CLOUD-20260927-A1}"
JOB="Q0-DEVIN-CLOUD-20260927-A1"
PROJECT="q0-devin-cloud-20260927-a1"
OUT="${OUT:-$REPO/docs/cloud/evidence/$JOB/logs}"
cd "$REPO" || exit 1
export KNOKEEP_STORE="$STORE"

CLI=(python3 skill/knokeep_state.py --store "$STORE" --project "$PROJECT" \
     --session-id "$JOB" --client devin-cloud)
run() { local f="$OUT/$1"; shift; { echo "\$ $*"; "$@" 2>&1; echo "[exit=$?]"; } >>"$f"; }

HASH=$("${CLI[@]}" bootstrap | python3 -c 'import json,sys; print(json.load(sys.stdin)["version_hash"])')
echo "expect_hash=$HASH" >>"$OUT/11-cli-attempt2.log"

run 11-cli-attempt2.log "${CLI[@]}" flush-state --body-file "$OUT/state-body.md" --expect-hash "$HASH"

cat >"$OUT/log-body.md" <<'BODY'
## Completed
- CLI write probe through skill/knokeep_state.py on an isolated synthetic store.

## Active
- MCP stdio protocol harness probe (shell-created; not native client enrollment).

## Next Step
- Export the store with a SHA-256 manifest and publish evidence on the output branch.
BODY
run 11-cli-attempt2.log "${CLI[@]}" flush-log --body-file "$OUT/log-body.md"
run 11-cli-attempt2.log "${CLI[@]}" bootstrap

# Secret gate, now reaching the gate (CAS satisfied).
HASH2=$("${CLI[@]}" bootstrap | python3 -c 'import json,sys; print(json.load(sys.stdin)["version_hash"])')
run 21-secret-gate-attempt2.log "${CLI[@]}" flush-state --body-file "$OUT/secret-body.md" --expect-hash "$HASH2"

# MCP harness, with the initialized notification.
python3 - "$STORE" >"$OUT/41-mcp-harness-attempt2.txt" 2>&1 <<'PY'
import json, subprocess, sys
root = sys.argv[1]
p = subprocess.Popen([sys.executable, "-m", "mcp.server", "--backend", "local", "--root", root],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
def send(msg):
    p.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n"); p.stdin.flush()
def rpc(method, params=None, _id=[0]):
    _id[0] += 1
    req = {"jsonrpc": "2.0", "id": _id[0], "method": method}
    if params is not None: req["params"] = params
    send(req)
    line = p.stdout.readline()
    print(f"-> {method} {json.dumps(params)[:200] if params else ''}\n<- {line.strip()[:900]}\n")
    return json.loads(line) if line.strip() else None

rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "q0-devin-cloud-harness", "version": "1"}})
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
rpc("tools/call", {"name": "knokeep_health", "arguments": {}})
rpc("tools/call", {"name": "knokeep_write", "arguments": {
    "key": "q0-devin-cloud-20260927-a1/mcp_harness_note",
    "body": "# MCP harness write (synthetic)\nWritten through the bundled KnoKeep MCP server over stdio JSON-RPC, local backend.\n",
    "doc_type": "journal"}})
rpc("tools/call", {"name": "knokeep_read", "arguments": {"key": "q0-devin-cloud-20260927-a1/mcp_harness_note"}})
rpc("tools/call", {"name": "knokeep_list", "arguments": {"prefix": "q0-devin-cloud"}})
p.stdin.close(); p.wait(timeout=20)
print("[server exit]", p.returncode)
err = p.stderr.read()
if err: print("[stderr]", err[:2000])
PY

# Re-verify store contents from an independent process, then re-export.
python3 - "$STORE" >"$OUT/32-crossprocess-read-final.txt" 2>&1 <<'PY'
import hashlib, sys
from store.local import LocalBackend
b = LocalBackend(sys.argv[1])
for key in sorted(b.list("")):
    blob = b.read(key)
    body = blob.body if isinstance(blob.body, (bytes, bytearray)) else str(blob.body).encode()
    print(f"{key}\tbytes={len(body)}\tsha256={hashlib.sha256(body).hexdigest()}")
PY
find "$STORE" -type f -print0 | sort -z | xargs -0 sha256sum >"$OUT/33-store-file-hashes-final.txt" 2>&1

EXPORT="$REPO/tests/fixtures/q0/$JOB/store"
rm -rf "$EXPORT"; mkdir -p "$EXPORT"
cp -a "$STORE/." "$EXPORT/"
( cd "$EXPORT" && find . -type f -print0 | sort -z | xargs -0 sha256sum ) >"$REPO/tests/fixtures/q0/$JOB/MANIFEST.sha256"
( cd "$EXPORT" && find . -type f -printf '%s\t%p\n' | sort -k2 ) >"$REPO/tests/fixtures/q0/$JOB/MANIFEST.sizes"

# Byte-fidelity check: exported copy vs the original store.
diff -r "$STORE" "$EXPORT" >"$OUT/60-export-diff.txt" 2>&1
echo "[diff exit=$?]" >>"$OUT/60-export-diff.txt"
echo "corrections complete: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
