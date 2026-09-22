# KnoKeep

**Portable, secret-safe memory that lets any AI coding agent pick up exactly where the last session left off — even in a different tool.**

[![CI](https://github.com/SathiaAI/knokeep/actions/workflows/ci.yml/badge.svg)](https://github.com/SathiaAI/knokeep/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/SathiaAI/knokeep)](https://github.com/SathiaAI/knokeep/releases)

*Part of the Kno suite — Knosky routes to the right file; KnoKeep remembers the work.*

---

## The problem

Long AI coding sessions forget. The context window fills up and gets **compacted** into a lossy summary — exact paths, versions, the bug you just squashed, the constraint you agreed on — all dropped. So the agent reintroduces a bug it already fixed. Start a **new chat**, or switch from **Cursor to Codex to Claude Code**, and it resets to zero.

The workarounds each have a catch: they work in only one tool, or they need a local daemon that can't run in a cloud sandbox, or they mean copy-pasting context by hand. And **none of them scrub secrets** — a real risk, because these agents are reading your `.env`.

KnoKeep fixes continuity **without** any of those catches.

## See it work

End a session in one tool, resume in another with the **exact** state — not a fuzzy recap:

```text
# Session 1 — in Cursor. You flush before the context fills up.
✔ flushed  system_state + session_log   (v 8a1c…)

# Later — a brand-new session, in Claude Code (different tool).
# KnoKeep bootstraps automatically and states the resume line first:

resuming:  Stripe webhook retry — idempotency key done, backoff left to do
next:      add jitter to the backoff, then fix the 3 tests in test_webhooks.py
rules:     never log the signing secret · webhook handler stays under 200ms
v 8a1c…
```

No re-explaining. No re-reading the diff. It states where you were and what's next, then keeps going.

<!-- TODO: replace with a short terminal GIF of the resume moment (the single highest-impact asset for this README). -->

## Quickstart

**Claude Code / Cowork** (requires Python 3 and Node):

```
/plugin marketplace add SathiaAI/knokeep
/plugin install knokeep@kno
```

**Cursor** → add the rule at `shims/cursor/knokeep.mdc`.
**Codex** → drop in `shims/codex/AGENTS.md`.
**Any tool with a shell** → `python skill/knokeep_state.py bootstrap --project <name>`.

Point every tool at the same store (the default is per-user and shared automatically) and they all resume from one memory.

## What makes it different

Four things, and no single existing tool does all four (`~` = partial / varies):

| | Vendor-native memory | Single-tool memory | Fuzzy "memory layer" services | **KnoKeep** |
|---|:---:|:---:|:---:|:---:|
| Works across *different* tools (Cursor ↔ Codex ↔ Claude) | ❌ | ❌ | ~ | ✅ |
| Runs in a cloud sandbox — no local daemon | ✅ | ❌ | ✅ | ✅ |
| Recalls **exact** state, not a fuzzy summary | ~ | ~ | ❌ | ✅ |
| **Never writes a secret** to the store | ❌ | ❌ | ❌ | ✅ |
| Self-hostable / airgappable — you own the data | ❌ | ~ | ❌ | ✅ |

## Secret-safe by design

KnoKeep is built to read your project — including files that sit next to secrets — and **never write a secret down**. Every write passes through a single, fail-closed gate. If the content looks like a credential — a known token prefix, a high-entropy value, a credential-shaped assignment or URL — the write is **refused**, and a reference is stored instead:

```
OPENROUTER_API_KEY (in .env, not stored)
```

It's defense-in-depth, not one check: the write gate is backed by a store scan on every resume and by an independent `gitleaks` pass in CI. The gate is also the part we hardened hardest — its design was put through an **independent adversarial review by non-Anthropic models** before release, and the findings are pinned as regression tests.

## Works across your tools

One schema, one store, one gate — every tool writes and reads the same memory:

| Tool | How it plugs in |
|---|---|
| Claude Code / Cowork | the plugin (skill + bundled MCP server) |
| Cursor | `shims/cursor/knokeep.mdc` rule |
| Codex | `shims/codex/AGENTS.md` |
| Any tool with a shell | call `skill/knokeep_state.py` directly |

**Sharing with a team?** You don't need a hosted service. Point KnoKeep's Postgres backend at a database your team already runs, and everyone shares one memory. (Everyone with the connection string has full access — it's built for a *trusted* team; per-user logins and isolation are a later, opt-in tier.)

## What KnoKeep is *not*

Being honest about the edges:

- **Not a code-search / RAG engine.** It remembers *your working state*, not your whole codebase.
- **Not for PHI or payer data.** Storing that is prohibited by design — don't point it at it.
- **Secret detection on freeform prose is best-effort, not a guarantee.** The gate is one of several layers (store-scan + gitleaks back it up), but a cleverly disguised secret in plain text can still slip a single heuristic — which is exactly why there's more than one.
- **v0.1.0.** The engine is built, tested, and independently reviewed, but it's young. Expect some rough edges and file issues.

## How it works (one screen)

At the start of a session the agent **bootstraps** — reads the store and states its resume line before doing anything. As work is verified it **flushes**: `system_state` (architecture, paths, hard constraints, changes rarely) and `session_log` (done / active / next step, changes often). Concurrent sessions are safe — each writes its own append-only journal; only `system_state` is shared and it's guarded by a content hash, so two sessions can't clobber each other. Under the hood it's a single store engine with pluggable backends (local folder by default; git, S3/GCS-style object stores, and Postgres also supported) — all behind the same secret gate.

## Docs

- [`docs/SCHEMA.md`](docs/SCHEMA.md) — the frozen, client-neutral memory contract
- [`docs/DATA-CLASSIFICATION.md`](docs/DATA-CLASSIFICATION.md) — what may and may not be stored
- [`docs/THREAT-MODEL.md`](docs/THREAT-MODEL.md) — the security model
- [`design/store_backend_contract.md`](design/store_backend_contract.md) — the contract every backend implements
- [`CHANGELOG.md`](CHANGELOG.md) — release notes

## Security

Found a vulnerability? Please open an issue (or email the maintainer) rather than a public PoC. The secret gate is the one write door and is deliberately conservative; it's backed by a resume-time store scan and an independent `gitleaks` engine in CI, and its design passed an independent non-Anthropic-model adversarial review.

## Status & license

**v0.1.0**, actively maintained, single maintainer. Contributions aren't open yet (a contributor agreement is being sorted first) — issues and feedback are very welcome.

Licensed under **GNU AGPL-3.0** (see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE)). AGPL is copyleft **including over a network** — if you run a modified KnoKeep as a service, you must share your source. Use it freely; just keep it open.
