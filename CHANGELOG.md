# Changelog

All notable changes to KnoKeep are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/).

## [0.1.0] — first public release

The "one engine" release. KnoKeep now has a single memory engine end to end:
the skill and the bundled MCP server write through the same store gate into the
same store, so memory is shared across tools instead of split per tool.

### Added
- **Bundled MCP server.** `.claude-plugin/plugin.json` now declares the
  `knokeep` MCP server, launched cross-OS via `shims/launch_mcp.mjs`
  (finds `py -3` / `python` / `python3`, runs `python -m mcp.server --backend local`).
- **Tool-independent store root.** `store/config.py:default_store_root()` resolves
  a per-user store — `$KNOKEEP_STORE` → `%LOCALAPPDATA%\KnoKeep\store` (Windows)
  → `$XDG_DATA_HOME/knokeep/store` else `~/.local/share/knokeep/store`. The skill
  and the MCP server default to the same path, so every tool shares one memory.
- Test coverage proving the skill (with no `--store`) and the MCP server resolve
  to and read/write the same store.

### Changed
- **Skill unified onto the V2 store engine.** `skill/knokeep_state.py`'s write,
  lock, and hash-CAS layer now runs through `store.gate.persist(...)` and
  `LocalBackend`, replacing the skill's former self-contained write path.
  Higher-level behavior users rely on is unchanged: bootstrap → resume line,
  sectioned flushes, journals, rollup, telemetry, health, and eval.
- **Concurrency guard now uses the store's content hash.** `--expect-hash`
  takes the store's 64-hex `version_hash` (previously a 12-char skill hash).
  A stale hash is still rejected.
- **`--store` is now optional.** It defaults to the tool-independent store root;
  pass `--store` or set `KNOKEEP_STORE` to override.
- `bootstrap` and `health` now read the store via `backend.list()` / `read()`
  instead of walking raw directories, so they respect the store's own layout.

### Security
- **Single secret gate.** The skill's separate `knokeep_secretgate.py` is retired;
  `store/gate.py` is the one write door. Because this changes the single boundary,
  the new write path was put through an adversarial review by a non-Anthropic
  model panel and passed before this release. The gate remains defense-in-depth,
  backed by a resume-time store scan and gitleaks in CI.

### Migration
- No data migration is required for a fresh install. If you were running the
  pre-unify skill with an explicit `--store`, either keep passing it or set
  `KNOKEEP_STORE` to that path so the unified skill and MCP server use it.

[0.1.0]: https://github.com/SathiaAI/knokeep/releases/tag/v0.1.0
