#!/usr/bin/env node
// KnoKeep MCP launcher (T-4). Claude Code / Cowork run this via `node` (always
// present in that runtime); it finds a Python 3 across OSes and execs the server
// as `python -m mcp.server --backend local` with cwd = plugin root (so `store`,
// `reconciler` and the local `mcp` package all import cleanly). The store root
// defaults to the shared cross-tool path (store/config.py:default_store_root),
// so the MCP server and the skill (run from an agent's bash) resolve the SAME
// store with no env var having to propagate between them. Set KNOKEEP_STORE to
// override the location for every tool at once.
import { spawnSync, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname } from "node:path";

const pluginRoot =
  process.env.CLAUDE_PLUGIN_ROOT ||
  dirname(dirname(fileURLToPath(import.meta.url))); // shims/launch_mcp.mjs -> plugin root

function findPython() {
  const candidates =
    process.platform === "win32"
      ? [["py", ["-3"]], ["python", []], ["python3", []]]
      : [["python3", []], ["python", []]];
  for (const [cmd, pre] of candidates) {
    try {
      const r = spawnSync(cmd, [...pre, "--version"], { stdio: "ignore" });
      if (r.status === 0) return [cmd, pre];
    } catch (_) {
      /* try next */
    }
  }
  console.error("KnoKeep MCP: no Python 3 interpreter found (tried py -3 / python / python3).");
  process.exit(1);
}

const [cmd, pre] = findPython();
const child = spawn(cmd, [...pre, "-m", "mcp.server", "--backend", "local"], {
  cwd: pluginRoot,
  stdio: "inherit",
  env: process.env,
});
child.on("exit", (code) => process.exit(code == null ? 0 : code));
child.on("error", (err) => {
  console.error("KnoKeep MCP: failed to launch python -m mcp.server:", err.message);
  process.exit(1);
});
