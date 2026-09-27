# Q0 report — Cursor Cloud (Q0-CURSOR-CLOUD-20260927-A1)

| Field | Value |
|---|---|
| Job ID | `Q0-CURSOR-CLOUD-20260927-A1` |
| Attempt | 1 |
| Client node | D (Cursor cloud) |
| Input commit | `49e4c1cc6ad75113c97de856a069a670bf0696df` |
| Output branch | `q0/cursor-cloud/20260927-a1` |
| Provider run | `bc-6d5ec32a-49dd-4673-9444-b5ba89a0533f` |
| Model | `composer-2.5` |
| Elapsed (approx.) | ~12 minutes (launch ~22:47 UTC, evidence complete ~22:49 UTC) |
| Usage / cost | UNAVAILABLE |

## Checkout

- `git rev-parse HEAD` → `49e4c1cc6ad75113c97de856a069a670bf0696df` (**PASS**, matches required input)
- Working branch → `q0/cursor-cloud/20260927-a1`

Environment details: `logs/environment_record.json`.

## Integration paths (scored separately)

| Path | Status | Evidence | Notes |
|---|---|---|---|
| Agent → KnoKeep **CLI** (`skill/knokeep_state.py`) | **PASS** | `logs/cli_*.json`, `probe_q0.py` | `init`, `bootstrap`, `flush-state` on disposable `LocalBackend` root |
| **Registered native** KnoKeep MCP on Cursor Cloud | **BLOCKED** | `logs/environment_record.json` | Host exposes `KnoTrack` (`kt_*`) MCP; no `knokeep_read` / `knokeep_write` tools in catalog |
| **Shell stdio** MCP protocol harness (`python -m mcp.server`) | **PASS** | `probe_stdio_mcp.sh`, `logs/stdio_mcp_harness.log` | Newline-delimited JSON-RPC subprocess |
| In-process MCP harness (Python `KnoKeepServer`) | **PASS** | `logs/mcp_harness_*.json` | Protocol harness only; not native enrollment |
| **Automatic capture** | **NOT TESTED** | — | Out of Q0 scope |

Backend: **LocalBackend** at isolated temp root (not MCP default `FakeBackend`). See `probe_summary.json` step `fake_backend_not_selected`.

## Durability and read-back

| Step | Status | Evidence |
|---|---|---|
| Write receipt (CLI `version_hash`) | **PASS** | `logs/cli_flush_state.json` |
| Independent process read (same hash + marker) | **PASS** | `logs/independent_read_cli.json` |
| MCP harness write/read | **PASS** | `logs/mcp_harness_write.json`, `logs/mcp_harness_read.json` |
| Secret gate (synthetic `ghs_` look-alike) | **PASS** (blocked as expected) | `logs/secret_gate_probe.json` — label: possible false positive if legitimate content matched |

Content hash (CLI `system_state`): see `logs/probe_summary.json` → `cli_system_state_hash`.

## Network (authorized destinations only)

| Host | Method | Result | Log |
|---|---|---|---|
| `api.github.com` | GET `/zen` | HTTP 200 | `logs/network_probe.log` |
| `github.com` | GET repo page | HTTP 200 | `logs/network_probe.log` |

Egress: unrestricted per `cursor-cloud` `environment-info`.

## Store export

| Step | Status | Path |
|---|---|---|
| Quiesce writers / copy bytes | **PASS** | `export_store.sh` |
| SHA-256 manifest | **PASS** | `tests/fixtures/q0/Q0-CURSOR-CLOUD-20260927-A1/MANIFEST.sha256` |
| Export tree | **PASS** | `tests/fixtures/q0/Q0-CURSOR-CLOUD-20260927-A1/store-export/` |

Original probe store path (ephemeral): `receipts/probe_store_path.txt` (under `/tmp`; export is the preserved copy).

## Fresh session / publication tracks

| Track | Status |
|---|---|
| Fresh-session retrieval | **PENDING** — see `continuation.md` |
| Store transfer via git fixtures | **PASS** (committed export intended) |
| Commit | *pending this push* |
| Branch push | *pending this push* |
| Draft PR | **PENDING** — dispatcher disabled auto-PR; coordinator opens draft |
| Remote manifest verification | *pending post-push* |

## Secret scanning

Manual review of staged synthetic evidence only (no live credentials). Automated `gitleaks` binary **UNAVAILABLE** in environment; scope recorded in `logs/secret_scan.txt`. Manifest member `.knokeep-eval/events.jsonl` is force-added (`git add -f`) because `.gitignore` normally excludes telemetry.

## Reproducibility

```bash
cd /workspace  # or repo root at input commit
python3 docs/cloud/evidence/Q0-CURSOR-CLOUD-20260927-A1/probe_q0.py
docs/cloud/evidence/Q0-CURSOR-CLOUD-20260927-A1/probe_stdio_mcp.sh
docs/cloud/evidence/Q0-CURSOR-CLOUD-20260927-A1/export_store.sh
```

## Client identity

Persisted store metadata reflects the cloud agent runtime executing the CLI. No mismatch rewrite performed; successor should compare exported frontmatter `client` fields against its own runtime.
