#!/usr/bin/env python3
"""T-1 success criterion: the skill and the MCP server share ONE store.

  Direction A (skill -> MCP): a skill `flush-state` is byte-identical, with the
      SAME version_hash, when read back through the MCP `knokeep_read` tool.
  Direction B (MCP -> skill): an MCP `knokeep_write` journal is seen by the skill
      (`rollup` counts it).
  Single gate: an MCP `knokeep_write` carrying a secret is blocked too — proving
      the ONE write door (store.gate) guards the MCP path, not just the skill.

Both sides open a LocalBackend at the SAME root (skill via --store, MCP via
--root), each constructed fresh per call exactly as the shipped CLIs do.
Run: python tests/test_skill_mcp_shared_store.py"""
import subprocess, sys, os, json, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "skill", "knokeep_state.py")
sys.path.insert(0, ROOT)
from store.local import LocalBackend
from mcp.server import KnoKeepServer

store = tempfile.mkdtemp(prefix="knokeep_share_")
proj = "demo"
results = []

def check(name, cond):
    results.append((name, cond)); print(("PASS " if cond else "FAIL ") + name)

def skill(*args, project=proj):
    p = subprocess.run([sys.executable, STATE, *args, "--store", store, "--project", project],
                       capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()

def bf(text):
    f = os.path.join(store, "_body.md"); open(f, "w", encoding="utf-8").write(text); return f

def mcp_tool(name, arguments):
    """One MCP tools/call over a fresh LocalBackend at the shared root
    (constructed + closed per call, as a real MCP process would)."""
    be = LocalBackend(store)
    try:
        srv = KnoKeepServer(be)
        srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments}})
        return json.loads(r["result"]["content"][0]["text"]), r["result"]["isError"]
    finally:
        be.close()

# --- Direction A: skill writes, MCP reads the SAME bytes + hash ---
skill("init")
_, out, _ = skill("bootstrap"); h0 = json.loads(out)["version_hash"]
MARKER = "SHARED-STORE-PROOF-Alpha-42"
rc, out, err = skill("flush-state", "--body-file", bf("## Architecture\n" + MARKER), "--expect-hash", h0)
new_hash = json.loads(out)["version_hash"] if rc == 0 else None
check("skill flush ok", rc == 0 and bool(new_hash))
payload, is_err = mcp_tool("knokeep_read", {"key": f"{proj}/system_state"})
check("MCP reads skill's write (found)", payload.get("found") is True and not is_err)
check("MCP sees identical body (skill->MCP)", MARKER in payload.get("text", ""))
check("MCP version_hash == skill hash", payload.get("version_hash") == new_hash)

# --- Direction B: MCP writes a journal, the skill sees it (rollup) ---
payload, is_err = mcp_tool("knokeep_write", {"key": f"{proj}/sessions/mcpwriter",
                           "doc_type": "journal", "body": "## Journal\n[2026-09-21T00:00:00Z] from MCP\n"})
check("MCP journal write OK", payload.get("status") == "OK" and not is_err)
rc, out, err = skill("rollup")
entries = json.loads(out).get("session_entries", 0) if rc == 0 else -1
check("skill rollup sees MCP's journal (MCP->skill)", rc == 0 and entries >= 1)

# --- Single gate on BOTH paths: an MCP write carrying a secret is blocked ---
payload, is_err = mcp_tool("knokeep_write", {"key": f"{proj}/sessions/leak",
                           "doc_type": "journal", "body": "token ghs_EXAMPLE0000000000000000000000"})
check("MCP write blocks secret (single gate)",
      payload.get("status") == "ERROR" and payload.get("kind") == "SECRET_BLOCKED" and is_err)

shutil.rmtree(store, ignore_errors=True)
passed = sum(1 for _, c in results if c)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
