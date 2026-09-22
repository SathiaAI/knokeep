"""Shared store-root resolution (T-4).

KnoKeep is *cross-tool* memory: Cursor, Codex, Claude Code and Cowork must all
resume from ONE store. So the default store root is TOOL-INDEPENDENT and per-user
— every tool resolves the same path with no dependency on any tool-specific env
var reaching the process. This is what makes the skill (run via an agent's bash)
and the MCP server (launched by the plugin) share one store without having to
propagate a plugin data-dir into the agent's shell.

Precedence:
  1. $KNOKEEP_STORE            — explicit override (a user who wants a custom or
                                 shared-team location sets this in their profile,
                                 so it is present in every tool's environment).
  2. platform per-user default — Windows %LOCALAPPDATA%\\KnoKeep\\store;
                                 else $XDG_DATA_HOME/knokeep/store or
                                 ~/.local/share/knokeep/store.

Deliberately NOT ${CLAUDE_PLUGIN_DATA}: it is Claude-only and per-plugin-install,
so using it would fragment memory per tool — the opposite of the product goal.
Stdlib only; no imports from the rest of the store package (safe to import early)."""
import os


def default_store_root() -> str:
    override = os.environ.get("KNOKEEP_STORE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    home = os.path.expanduser("~")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "KnoKeep", "store")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "knokeep", "store")


__all__ = ["default_store_root"]
