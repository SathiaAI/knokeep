#!/usr/bin/env bash
# Shell-driven newline JSON-RPC probe against `python -m mcp.server` (stdio transport).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
JOB="Q0-CURSOR-CLOUD-20260927-A1"
LOG="$ROOT/docs/cloud/evidence/$JOB/logs/stdio_mcp_harness.log"
STORE="$(mktemp -d /tmp/q0_stdio_mcp_XXXXXX)"
export KNOKEEP_STORE="$STORE"
export PYTHONPATH="$ROOT"

python3 <<'PY' | tee "$LOG"
import json, os, subprocess, sys, tempfile

ROOT = os.environ["PYTHONPATH"]
store = os.environ["KNOKEEP_STORE"]
proc = subprocess.Popen(
    [sys.executable, "-m", "mcp.server", "--backend", "local", "--root", store],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
    cwd=ROOT,
)

def send(obj):
    line = json.dumps(obj, separators=(",", ":"))
    assert "\n" not in line
    proc.stdin.write(line + "\n")
    proc.stdin.flush()

def recv():
    line = proc.stdout.readline()
    if not line:
        raise SystemExit("no response line")
    return json.loads(line)

send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
init = recv()
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
        "name": "knokeep_write",
        "arguments": {
            "key": "stdio_probe/journal",
            "doc_type": "journal",
            "body": "## Journal\nstdio subprocess probe line\n",
        },
    },
})
write_resp = recv()
payload = json.loads(write_resp["result"]["content"][0]["text"])
send({
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {"name": "knokeep_read", "arguments": {"key": "stdio_probe/journal"}},
})
read_resp = recv()
read_payload = json.loads(read_resp["result"]["content"][0]["text"])
proc.stdin.close()
proc.wait(timeout=5)
print(json.dumps({
    "initialize_server": init.get("result", {}).get("serverInfo", {}),
    "write_status": payload.get("status"),
    "write_new_hash": payload.get("new_hash"),
    "read_found": read_payload.get("found"),
    "read_hash": read_payload.get("version_hash"),
    "store_root": store,
    "exit_code": proc.returncode,
}, indent=2))
PY

echo "stdio probe store: $STORE" >> "$ROOT/docs/cloud/evidence/$JOB/receipts/stdio_store_path.txt"
