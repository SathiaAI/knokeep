"""T-4: the skill (run from an agent's bash) and the MCP server resolve the SAME
store root by DEFAULT, so `/plugin install` yields one shared memory with no
tool-specific env var having to propagate between the two processes.

`default_store_root()` precedence: $KNOKEEP_STORE, else a per-user platform path.
These tests pin the resolver and prove end-to-end sharing with NO --store passed."""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from store.config import default_store_root
from store.local import LocalBackend
from mcp.server import KnoKeepServer

STATE = os.path.join(ROOT, "skill", "knokeep_state.py")


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOKEEP_STORE", str(tmp_path / "custom"))
    assert default_store_root() == os.path.abspath(str(tmp_path / "custom"))


def test_platform_default_shape(monkeypatch):
    monkeypatch.delenv("KNOKEEP_STORE", raising=False)
    r = default_store_root()
    # tool-independent per-user path; never a plugin-data dir
    assert r.endswith(os.path.join("KnoKeep", "store")) or r.endswith(os.path.join("knokeep", "store"))
    assert "CLAUDE_PLUGIN_DATA" not in r


def test_skill_and_mcp_share_the_default_root(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOKEEP_STORE", str(tmp_path / "shared"))

    def skill(*args):
        return subprocess.run(
            [sys.executable, "-X", "utf8", STATE, *args, "--project", "demo"],
            capture_output=True, text=True, cwd=ROOT, env=os.environ.copy(),
        )

    assert skill("init").returncode == 0
    h0 = json.loads(skill("bootstrap").stdout)["version_hash"]
    body = tmp_path / "body.md"
    body.write_text("## Architecture\nSHARED-DEFAULT-MARKER-T4", encoding="utf-8")
    r = skill("flush-state", "--body-file", str(body), "--expect-hash", h0)
    assert r.returncode == 0, r.stderr

    # MCP opens a LocalBackend at the SAME default root and reads the skill's write.
    be = LocalBackend(default_store_root())
    try:
        srv = KnoKeepServer(be)
        srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        res = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "knokeep_read", "arguments": {"key": "demo/system_state"}}})
        payload = json.loads(res["result"]["content"][0]["text"])
        assert payload.get("found") is True
        assert "SHARED-DEFAULT-MARKER-T4" in payload.get("text", "")
    finally:
        be.close()
