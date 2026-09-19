# KnoKeep Data Classification (enforced before every write)

## FORBIDDEN in any KnoKeep store (block the write, fail closed)
- **Secret values:** API keys, tokens, passwords, private keys, session cookies. Detectors: prefix denylist (`sk-`, `sk-ant-`, `sk-or-`, `ghp_`, `github_pat_`, `AKIA`, `xox`, `pypi-`, `-----BEGIN … PRIVATE KEY`), credential-bearing URIs (`scheme://user:pass@host`), `.env`-dump shapes, high-entropy strings (Shannon > ~4.0 over len ≥ 20).
- **PHI / payer data:** member IDs, claim numbers, MRNs, EHR hostnames, patient names/DOBs. (Separate rule — secret scanning ≠ PHI detection.)
- Full raw `.env` file contents.

## ALLOWED (the point of KnoKeep)
- Architecture, module/file paths, variable NAMES, decisions + rationale, task state, next steps, non-sensitive config, references to secrets (name + where it lives, never the value).

## Scan scope = every egress, not just file writes
State docs, git payloads, logs, sub-agent prompts, tool output, MCP stdio. If a secret/PHI pattern is detected anywhere on the way out → HALT the write, quarantine a redacted note locally, surface. Never rely on a prompt instruction alone.

## Reference form (how a secret is recorded)
`OPENROUTER_API_KEY — value in .env (not stored)`. The name + location, never the value.

## Scope & limitations (honest claim — read before relying on this)
KnoKeep's gate is **best-effort defense-in-depth, not a mathematical guarantee.** A regex/entropy scanner cannot prove arbitrary freeform prose is secret-free. Design decision (Option C, owner-approved 2026-09-19): keep freeform memory + this layered gate + the model-side rule to never write secrets, rather than restrict memory to opaque reference IDs.
- **Enforced boundary:** every write goes through `safe_write()`, which scans the FULL serialized content (frontmatter + body) before an atomic write. The helper is the ONLY legal write path; direct edits to the store are a bug. `bootstrap` re-scans the whole store and refuses to resume if it finds a hit (catches bypass writes).
- **Residual:** a short secret in freeform prose with no nearby keyword and no known shape can still pass. Mitigated by the model-side rule, not eliminated.
- **PHI:** KnoKeep is **not for PHI/payer workflows.** There is no clinical-grade PHI detector; do not point it at PHI. (Conservative SSN/MRN/account patterns may be added as defense-in-depth, never as a compliance claim.)
- **Not** a claim of "secrets never stored." The claim is: layered scanning + enforced write path + model policy.
