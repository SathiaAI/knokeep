# KnoKeep

**Portable, secret-safe memory that lets any AI coding agent resume exactly where the last session — on any tool — left off.**

*Knosky routes to the right file; KnoKeep remembers the work.* Part of the Kno suite. AGPL-3.0.

## What it does

Long agent sessions lose their thread. Context gets compacted, a new chat starts cold, or you switch from one tool to another — and the agent no longer knows the architecture, the paths, the decisions already made, or what it was in the middle of.

KnoKeep fixes that. It flushes your project's exact state — architecture, path and variable directory, hard constraints, what's done, the active task, and the next step — to a durable, first-party store that survives context compaction, new threads, and tool switches. On the next session, in **any** tool, the agent bootstraps from that store and states its resume line before doing anything else.

It never writes a secret. Every write passes a fail-closed gate that refuses secret-like values, so what lands on disk is references (`OPENROUTER_API_KEY (in .env, not stored)`), never the values themselves. The store is a plain local directory you own — self-hostable and airgappable.

## One engine, one store, one gate

KnoKeep is a single memory engine. The skill your agent runs and the bundled MCP server write through the **same** store gate (`store/gate.py`) into the **same** `LocalBackend` store — there is no split-brain, no "skill memory" separate from "MCP memory." A flush written by the skill in Cowork is readable by the MCP server in the next tool, and vice versa.

The store is **tool-independent and per-user by default**, so Cursor, Codex, Claude Code, and Cowork all resume from one memory rather than fragmenting it per tool:

- Default: `%LOCALAPPDATA%\KnoKeep\store` on Windows; `$XDG_DATA_HOME/knokeep/store` (else `~/.local/share/knokeep/store`) on macOS/Linux.
- Override anywhere with the `KNOKEEP_STORE` environment variable.

## Install (Claude Code / Cowork)

```
/plugin marketplace add SathiaAI/knokeep
/plugin install knokeep@kno
```

That installs the `knokeep` skill and starts the bundled MCP server (`knokeep`), both pointed at your default store. Requires Python 3 and Node on the machine.

## Use it in other tools

The same schema and the same commands work everywhere — only the entry point differs:

- **Cursor** — the rule at `shims/cursor/knokeep.mdc` tells the agent to bootstrap and flush.
- **Codex** — `shims/codex/AGENTS.md` does the same.
- **Any tool with a shell** — call the helper directly: `python skill/knokeep_state.py bootstrap --store <STORE> --project <PROJECT>`.

Point every tool at the same store (the default, or one `KNOKEEP_STORE`) and they share one memory.

## How your agent uses it

At session start it bootstraps and states the resume line — `resuming: … / next: … / v<hash>` — before acting. It flushes after each verified milestone, before compaction, and at session end:

- `flush-state` — invariants (Architecture / Path & Variable Directory / Hard Constraints), guarded by the content hash you last saw (a stale hash is rejected, so concurrent sessions can't clobber each other).
- `flush-log` — Completed & Verified / Active State / Next Step.
- `session-append` — this session's own journal (append-only, parallel-safe).

A resume only recovers what was flushed. For handing off across tools, `session-handshake` writes a portable `HANDOFF.md`.

## Secret safety

The gate is the one write door, and it fails closed: a write is refused if the content looks like a credential (known token prefixes, high-entropy values, credential-shaped assignments and URIs), across the raw bytes and normalized views of the text. It is backed up by a store-scan on resume and by gitleaks in CI. The design is defense-in-depth — treat it as the last line, not the only one — and it means the store is safe to keep in a synced folder or push to a private remote. What may and may not be stored is spelled out in `docs/DATA-CLASSIFICATION.md`; recalled memory is always treated as data, never as instructions.

## Self-hostable

The default store is a local directory. KnoKeep also ships additional backends (git, object store, Postgres) behind the same gate and contract if you want shared or remote memory. Nothing phones home.

## Docs

- `docs/SCHEMA.md` — the frozen, client-neutral memory contract
- `docs/DATA-CLASSIFICATION.md` — what may and may not be stored
- `docs/THREAT-MODEL.md` — the security model
- `design/store_backend_contract.md` — the backend contract every store implements

## Status

Version 0.1.0 — first public release of the unified engine. Interfaces are stable but young; issues and feedback are welcome.

## License

GNU AGPL-3.0 (see `LICENSE` and `NOTICE`). Contributions (later) via CLA.
