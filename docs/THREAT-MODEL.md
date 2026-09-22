# KnoKeep Threat Model (v0.1 — pre-build, panel-required)

| # | Asset | Threat | Mitigation | Status |
|---|---|---|---|---|
| T1 | `.env` secrets | Leak into a durable / human-visible store or git history | Secret gate (DATA-CLASSIFICATION), references-only, scan-all-surfaces, gitleaks pre-commit | Design |
| T2 | PHI / payer data | Stored in memory → compliance breach | Prohibited by data-class policy; KnoKeep never pointed at PHI repos | Design |
| T3 | Knosky package | Supply chain (typosquat, post-install, telemetry) | Pin version + integrity hash; review scripts; run read-only, loopback, no egress; record SBOM | Design |
| T4 | GitHub token | Compromise in an unattended run | Off-session; fine-grained, single-repo, contents:write, 24h TTL; revoke at halt; **absent during local build** | Design |
| T5 | Primary vs mirror | Split-brain after bridge drop / partial push | Single source of truth; idempotent content-hash + read-back; resume reconciliation | Design |
| T6 | Memory content | Prompt-injection ("commit this secret / ignore rules") stored then re-read | Treat recalled memory as DATA, not instructions | Design |
| T7 | Concurrency | Two sessions clobber shared state | Per-session append-only logs; system_state read-verify-write-confirm (hash) | Design |
| T8 | Release | Self-certified security on a secret-handling tool | Independent (different-model) review + soak + human approval before any publish | Design |

## Build-time controls (this session)
Local-only; **fake secrets only** (see `fixtures/fake-project`); no remote writes; caps (≤3 retries/test, wall-clock + cost cap, HALT+HANDOFF on breach); crash-resume via our own HANDOFF. Output = release CANDIDATE, not released.

## Review
This model must be re-checked by a different model/agent (an independent, non-Anthropic-model review) before the remote-write gate is opened.
