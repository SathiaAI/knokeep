#!/usr/bin/env python3
"""Q0-CURSOR-CLOUD-20260927-A1 reproducible capability probe (synthetic only).

Runs CLI, in-process MCP protocol harness, and independent read-back checks
against a disposable LocalBackend store. Does not modify product code.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import shutil
from datetime import datetime, timezone

ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
)
STATE = os.path.join(ROOT, "skill", "knokeep_state.py")
sys.path.insert(0, ROOT)

from store.local import LocalBackend
from mcp.server import KnoKeepServer

JOB = "Q0-CURSOR-CLOUD-20260927-A1"
PROJECT = "q0cursor"
MARKER = "q0-cursor-cloud-probe-marker-alpha"
EVIDENCE = os.path.join(ROOT, "docs", "cloud", "evidence", JOB)
LOG_DIR = os.path.join(EVIDENCE, "logs")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def log(name: str, payload: dict) -> None:
    path = os.path.join(LOG_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def skill(store: str, *args: str) -> tuple[int, str, str]:
    cmd = [sys.executable, STATE, *args, "--store", store, "--project", PROJECT]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def mcp_call(store: str, tool: str, arguments: dict) -> tuple[dict, bool]:
    be = LocalBackend(store)
    try:
        srv = KnoKeepServer(be)
        srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = srv.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        text = r["result"]["content"][0]["text"]
        return json.loads(text), r["result"]["isError"]
    finally:
        be.close()


def independent_read(store: str, key: str) -> dict:
    """Read blob from a fresh process via LocalBackend only (no skill cache)."""
    script = (
        "import json, sys; "
        f"sys.path.insert(0, {json.dumps(ROOT)}); "
        "from store.local import LocalBackend; "
        f"b=LocalBackend({json.dumps(store)}); "
        f"r=b.read({json.dumps(key)}); b.close(); "
        f"marker={json.dumps(MARKER)}; "
        "out={'found': r is not None, 'version_hash': r.version_hash if r else None, "
        "'body_len': len(r.body) if r else 0}; "
        "out['marker_present']=bool(r and marker in r.body.decode('utf-8', errors='replace')); "
        "print(json.dumps(out))"
    )
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT)
    return {"exit_code": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip()}


def main() -> int:
    os.makedirs(LOG_DIR, exist_ok=True)
    store = tempfile.mkdtemp(prefix="q0_cursor_probe_")
    results: list[dict] = []

    def record(step: str, status: str, detail: dict, reason: str = "") -> None:
        results.append({"step": step, "status": status, "reason": reason, **detail})

    # --- CLI path ---
    rc, out, err = skill(store, "init")
    log("cli_init.json", {"cmd": "init", "exit_code": rc, "stdout": out, "stderr": err})
    record("cli_init", "PASS" if rc == 0 else "FAIL", {"exit_code": rc})

    rc, out, err = skill(store, "bootstrap")
    log("cli_bootstrap.json", {"cmd": "bootstrap", "exit_code": rc, "stdout": out, "stderr": err})
    if rc != 0:
        record("cli_bootstrap", "FAIL", {"exit_code": rc}, err or out)
        shutil.rmtree(store, ignore_errors=True)
        write_summary(results, store, None)
        return 1
    h0 = json.loads(out)["version_hash"]
    record("cli_bootstrap", "PASS", {"version_hash": h0})

    body_file = os.path.join(store, "_probe_body.md")
    with open(body_file, "w", encoding="utf-8") as f:
        f.write(f"## Q0 probe\n{MARKER}\n")

    rc, out, err = skill(store, "flush-state", "--body-file", body_file, "--expect-hash", h0)
    log("cli_flush_state.json", {"cmd": "flush-state", "exit_code": rc, "stdout": out, "stderr": err})
    if rc != 0:
        record("cli_flush_state", "FAIL", {"exit_code": rc}, err or out)
        shutil.rmtree(store, ignore_errors=True)
        write_summary(results, store, None)
        return 1
    new_hash = json.loads(out)["version_hash"]
    record("cli_flush_state", "PASS", {"version_hash": new_hash})

    key = f"{PROJECT}/system_state"
    ind = independent_read(store, key)
    log("independent_read_cli.json", ind)
    ind_payload = json.loads(ind["stdout"]) if ind["exit_code"] == 0 and ind["stdout"] else {}
    record(
        "independent_read_after_cli",
        "PASS"
        if ind_payload.get("version_hash") == new_hash and ind_payload.get("marker_present")
        else "FAIL",
        {"independent": ind_payload, "expected_hash": new_hash},
    )

    # --- MCP protocol harness (not Cursor-native enrollment) ---
    mcp_body = f"## Journal\n[{utc_now()}] {MARKER} via mcp harness\n"
    mcp_key = f"{PROJECT}/sessions/q0harness"
    payload, is_err = mcp_call(
        store,
        "knokeep_write",
        {"key": mcp_key, "doc_type": "journal", "body": mcp_body},
    )
    log("mcp_harness_write.json", {"payload": payload, "isError": is_err})
    mcp_hash = payload.get("new_hash") or payload.get("version_hash")
    record(
        "mcp_protocol_harness_write",
        "PASS" if payload.get("status") == "OK" and not is_err else "FAIL",
        {"version_hash": mcp_hash, "key": mcp_key},
    )

    read_payload, read_err = mcp_call(store, "knokeep_read", {"key": mcp_key})
    log("mcp_harness_read.json", {"payload": read_payload, "isError": read_err})
    body_match = MARKER in (read_payload.get("text") or "")
    record(
        "mcp_protocol_harness_read",
        "PASS" if body_match and read_payload.get("version_hash") == mcp_hash else "FAIL",
        {"found": read_payload.get("found"), "version_hash": read_payload.get("version_hash")},
    )

    # --- Secret gate (synthetic look-alike; label possible false positive if blocked) ---
    secret_body = "synthetic test token ghs_SYNTHETIC00000000000000000000"
    sec_payload, sec_err = mcp_call(
        store,
        "knokeep_write",
        {"key": f"{PROJECT}/sessions/secretprobe", "doc_type": "journal", "body": secret_body},
    )
    log("secret_gate_probe.json", {"payload": sec_payload, "isError": sec_err})
    blocked = sec_payload.get("kind") == "SECRET_BLOCKED" or sec_payload.get("status") == "ERROR"
    record(
        "secret_gate_synthetic",
        "PASS" if blocked else "FAIL",
        {"blocked": blocked, "note": "legitimate synthetic marker may be false positive if rejected"},
    )

    # --- Fake backend contrast (not durable evidence) ---
    from store.fake import FakeBackend

    fake = FakeBackend()
    fake_payload, _ = mcp_call_on_backend(
        fake, "knokeep_write", {"key": "x/y", "doc_type": "journal", "body": "ephemeral"}
    )
    fake_durable = fake_payload.get("status") == "OK"
    record(
        "fake_backend_not_selected",
        "PASS",
        {"fake_write_ok": fake_durable, "selected_backend": "LocalBackend", "store_root": store},
    )

    write_summary(results, store, new_hash)
    # Leave store path in summary for export step (caller copies before rmtree)
    with open(os.path.join(EVIDENCE, "receipts", "probe_store_path.txt"), "w") as f:
        f.write(store + "\n")
    return 0


def mcp_call_on_backend(backend, tool: str, arguments: dict):
    srv = KnoKeepServer(backend)
    srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = srv.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    text = r["result"]["content"][0]["text"]
    return json.loads(text), r["result"]["isError"]


def write_summary(results: list[dict], store: str, cli_hash: str | None) -> None:
    summary = {
        "job": JOB,
        "probe_finished_at": utc_now(),
        "store_root": store,
        "cli_system_state_hash": cli_hash,
        "results": results,
    }
    log("probe_summary.json", summary)


if __name__ == "__main__":
    raise SystemExit(main())
