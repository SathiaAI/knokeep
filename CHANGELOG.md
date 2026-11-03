# Changelog

All notable changes to KnoKeep are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added
- **Version history for the postgres backend** (contract v1.5 §10, optional
  capability `Caps.versions`). Every committed write is kept as an immutable,
  readable version, written in the same transaction as the write itself.
  `PostgresBackend.versions()` lists a key's history; `read_version()` returns
  the exact bytes. History is append-only, enforced by database triggers.
  Restoring an old version is an ordinary gated write.
- **Fuzzy search for the postgres backend** (contract v1.6 §11, optional
  capability `Caps.fuzzy`). `PostgresBackend.search(space, query)` finds
  entries by full-text match inside one space and returns pointers (key plus
  the exact version), never text. The index is derived and rebuildable
  (`reindex_fuzzy()`), updated in the same transaction as each write without
  ever blocking it, and a stale entry is never returned. No embeddings.
- **Automatic verifier for conclusions** (`conclusions/verify.py`). Decides
  whether a proposed conclusion (a derived statement citing exact entries)
  may be accepted with no person in the loop. Deterministic checks first:
  one space only, verbatim grounded quotes at an exact version, two
  independent sessions or an owner statement, no untrusted source, no
  secrets, nothing instruction-shaped. A judge model from a different family
  runs last; a missing, erroring or unsure judge means reject. Verdicts carry
  codes and indexes only, never text or a matched secret. Pure: no I/O.
- **Conclusion store** (`conclusions/store.py`). Where conclusions live and
  how they move: `proposed` -> `accepted` (only when the verifier passes) ->
  `stale` (evidence gone) or `superseded` (a newer one accepted). One record
  per conclusion under a reserved `.conclusions/` segment of its own space,
  written through the one gate (so a secret is refused, not stored) as an
  ordinary hash-CAS document, with compare-and-swap status updates. Nothing is
  deleted and exact entries are never touched. A conclusion cannot cite
  another conclusion. `recall()` returns only accepted ones, labelled derived,
  with their sources. `backend_resolver()` finds cited versions through
  version history when the backend keeps it. No gate change needed.
- **Jev judge** (`conclusions/jev.py`). TypeSafe's Jev as the verifier's
  judge: two yes/no questions (support, overreach) become true, false or
  unsure; any error, timeout, malformed answer or spent budget fails closed.
  Every call, including retries, is counted against a hard call and token
  `Budget` before it is made. https only; the key is optional (a proxy may
  inject it), never logged, never in an error or `repr()`. Calibrated live on
  37 labelled cases (`docs/JEV-JUDGE-EVAL.md`): 0 false accepts, 21 of 21
  unsupported rejected including 3 prompt-injection attempts.
- **Conclusion generator and end-to-end runner** (`conclusions/generator.py`,
  `conclusions/runner.py`). The generator proposes candidate conclusions from
  exact entries through OpenRouter; the model, its family and its price are
  settings. Every call is reserved against a hard dollar budget BEFORE it is
  sent (pessimistic worst case, then settled from real usage; an unknown
  outcome is charged in full). Requests deny data collection and can pin
  hosting providers; entry text is wrapped in a per-request random boundary;
  reasoning is off by default (a reasoning model can spend its whole allowance
  and return nothing). Its output is untrusted. The runner reads exact
  entries, proposes, verifies and promotes, skips duplicates on re-run, and
  stops with a reason when the budget is spent or the judge keeps failing.
  `store.list_records()` added. Model choice and two live end-to-end runs:
  `docs/GENERATOR-BAKEOFF.md`.

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
