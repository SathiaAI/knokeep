#!/usr/bin/env bash
# Q0-DEVIN-CLOUD-20260927-A1 capability probe (Devin cloud session, node F).
# Synthetic data only. No credentials are read, printed or stored.
#
# Usage:
#   REPO=/path/to/knokeep STORE=/path/to/disposable/store bash probe.sh
# Defaults match the recorded run.

set -u

REPO="${REPO:-$HOME/repos/knokeep}"
STORE="${STORE:-$HOME/q0-store/Q0-DEVIN-CLOUD-20260927-A1}"
JOB="Q0-DEVIN-CLOUD-20260927-A1"
PROJECT="q0-devin-cloud-20260927-a1"
OUT="${OUT:-$REPO/docs/cloud/evidence/$JOB/logs}"

mkdir -p "$OUT" "$STORE"
cd "$REPO" || exit 1

log() { echo "=== $* ===" ; }
run() { # run <logfile> <label> <cmd...>
  local f="$OUT/$1"; shift
  local label="$1"; shift
  { echo "\$ $*"; "$@" 2>&1; echo "[exit=$?]"; } >>"$f"
  echo "$label logged -> $f"
}

######################################################################
# 0. Runtime facts
######################################################################
{
  echo "job_id=$JOB"
  echo "utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "repo=$REPO"
  echo "branch=$(git rev-parse --abbrev-ref HEAD)"
  echo "head=$(git rev-parse HEAD)"
  echo "uname=$(uname -a)"
  echo "python=$(python3 -V 2>&1)"
  echo "git=$(git --version)"
  echo "node=$(node -v 2>/dev/null || echo ABSENT)"
  echo "store=$STORE"
  echo "store_backend=local (explicitly selected; the MCP default 'fake' backend is NOT used)"
} >"$OUT/00-environment.txt"

######################################################################
# 1. KnoKeep agent CLI path (shell-invoked skill entrypoint)
######################################################################
export KNOKEEP_STORE="$STORE"
CLI=(python3 skill/knokeep_state.py --store "$STORE" --project "$PROJECT" \
     --session-id "$JOB" --client devin-cloud)

run 10-cli.log "cli:init"    "${CLI[@]}" init
run 10-cli.log "cli:health"  "${CLI[@]}" health

cat >"$OUT/state-body.md" <<'BODY'
# Q0 synthetic system state (Devin cloud, node F)

- Fixture project: disposable synthetic store, no product code touched.
- Constraint under test: a successor client must resume from stored bytes alone.
- Decision changed during this run: object-store backend deferred; local backend selected for durability evidence.
- Unresolved: native MCP enrollment for this client surface is not demonstrated.
BODY

run 10-cli.log "cli:flush-state"     "${CLI[@]}" flush-state --body-file "$OUT/state-body.md"
run 10-cli.log "cli:flush-log"       "${CLI[@]}" flush-log --entry "Completed: CLI write probe. Active: MCP harness probe. Next: export store + manifest."
run 10-cli.log "cli:session-append"  "${CLI[@]}" session-append --entry "synthetic journal entry from Devin cloud session"
run 10-cli.log "cli:bootstrap"       "${CLI[@]}" bootstrap

######################################################################
# 2. Secret gate: legitimate-looking credential material must be refused
######################################################################
cat >"$OUT/secret-body.md" <<'BODY'
# Q0 secret-gate probe (synthetic, not a real credential)
export EXAMPLE_API_KEY="sk-lIvE000000000000000000000000000000000000000000000q0synthetic"
BODY
run 20-secret-gate.log "gate:refuse" "${CLI[@]}" flush-state --body-file "$OUT/secret-body.md"

######################################################################
# 3. Independent cross-process read + content hashes
######################################################################
python3 - "$STORE" "$PROJECT" >"$OUT/30-crossprocess-read.txt" 2>&1 <<'PY'
import hashlib, sys
from store.local import LocalBackend
root, project = sys.argv[1], sys.argv[2]
b = LocalBackend(root)
for key in sorted(b.list("")):
    blob = b.read(key)
    if blob is None:
        print(f"{key}\tNOT_FOUND"); continue
    body = blob.body if isinstance(blob.body, (bytes, bytearray)) else str(blob.body).encode()
    print(f"{key}\tbytes={len(body)}\tsha256={hashlib.sha256(body).hexdigest()}")
PY

find "$STORE" -type f -print0 | sort -z | xargs -0 sha256sum >"$OUT/31-store-file-hashes.txt" 2>&1

######################################################################
# 4. MCP protocol harness (shell-created; NOT native client enrollment)
######################################################################
python3 - "$STORE" >"$OUT/40-mcp-harness.txt" 2>&1 <<'PY'
import json, subprocess, sys
root = sys.argv[1]
p = subprocess.Popen(
    [sys.executable, "-m", "mcp.server", "--backend", "local", "--root", root],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

def rpc(method, params=None, _id=[0]):
    _id[0] += 1
    req = {"jsonrpc": "2.0", "id": _id[0], "method": method}
    if params is not None:
        req["params"] = params
    p.stdin.write(json.dumps(req, separators=(",", ":")) + "\n"); p.stdin.flush()
    line = p.stdout.readline()
    print(f"-> {method}\n<- {line.strip()[:800]}\n")
    return json.loads(line) if line.strip() else None

rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "q0-devin-cloud-harness", "version": "1"}})
r = rpc("tools/list", {})
print("tools:", [t["name"] for t in r["result"]["tools"]], "\n")
rpc("tools/call", {"name": "knokeep_health", "arguments": {}})
rpc("tools/call", {"name": "knokeep_write", "arguments": {
    "key": "projects/q0-devin-cloud-20260927-a1/journal/mcp-harness.md",
    "body": "# MCP harness write (synthetic)\n\nWritten through the bundled KnoKeep MCP server over stdio JSON-RPC.\n",
    "doc_type": "journal"}})
rpc("tools/call", {"name": "knokeep_read", "arguments": {
    "key": "projects/q0-devin-cloud-20260927-a1/journal/mcp-harness.md"}})
rpc("tools/call", {"name": "knokeep_list", "arguments": {"prefix": "projects/"}})
p.stdin.close(); p.wait(timeout=20)
print("[server exit]", p.returncode)
err = p.stderr.read()
if err:
    print("[stderr]", err[:2000])
PY

######################################################################
# 5. Network destination probe (observation only; no settings changed)
######################################################################
: >"$OUT/50-network.txt"
for host in https://github.com https://api.github.com https://pypi.org https://api.devin.ai https://example.com; do
  code=$(curl -s -o /dev/null -m 8 -w '%{http_code}' "$host" 2>>"$OUT/50-network.txt")
  echo "GET $host -> http_code=$code curl_exit=$?" >>"$OUT/50-network.txt"
done

######################################################################
# 6. Export the ORIGINAL saved store (quiesced: no writers left running)
######################################################################
EXPORT="$REPO/tests/fixtures/q0/$JOB/store"
rm -rf "$EXPORT"; mkdir -p "$EXPORT"
cp -a "$STORE/." "$EXPORT/"
( cd "$EXPORT" && find . -type f -print0 | sort -z | xargs -0 sha256sum ) >"$REPO/tests/fixtures/q0/$JOB/MANIFEST.sha256"
( cd "$EXPORT" && find . -type f -printf '%s\t%p\n' | sort -k2 ) >"$REPO/tests/fixtures/q0/$JOB/MANIFEST.sizes"

echo "probe complete: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
