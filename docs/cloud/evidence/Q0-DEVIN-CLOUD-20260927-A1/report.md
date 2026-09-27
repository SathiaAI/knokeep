# Q0-DEVIN-CLOUD-20260927-A1 — Devin cloud capability probe (node F)

Run date: September 27, 2026. Attempt generation: 1. Owner: Devin cloud session. Coordinator: Codex. Tracker: SathiaAI/knokeep#14.

All data in this packet is synthetic. No credential value was read, printed or stored. No product code was modified.

## Runtime facts (recorded before the probe)

| Field | Observed value |
|---|---|
| Client / surface | Devin cloud session (Devin v3 organization API launch, per dispatcher) |
| Model / provider | Devin (Cognition). Underlying model build not exposed to the session; recorded as UNAVAILABLE. |
| Repository | `SathiaAI/knokeep` |
| Input branch | `docs/cloud-handoff-2026-09-27` (platform did not rename it; no `work` branch was used) |
| Input commit (`git rev-parse HEAD`) | `49e4c1cc6ad75113c97de856a069a670bf0696df` — matches the required commit, so dependent testing proceeded |
| Host | Linux 6.8.0-1061-aws, Ubuntu, x86_64 |
| Python / Git / Node | Python 3.10.12 / git 2.34.1 / Node ABSENT |
| Authentication mode | Pre-provisioned session credentials for Git/GitHub via the platform's git proxy; no credential value was requested, pasted or printed |
| Memory settings | Repository is reported indexed by Devin and org knowledge notes were injected into this session. **This run is labelled memory-assisted.** No fresh-session isolation is claimed. |
| Store backend | `local` (explicitly selected). The MCP server's default `fake` backend was **not** used. |
| Store root | `/home/ubuntu/q0-store/Q0-DEVIN-CLOUD-20260927-A1` (isolated, disposable, synthetic) |

`max_acu_limit`: this session was launched by the dispatcher, not by this client, so the accepted `max_acu_limit` value is not observable from inside the session. Consumed ACUs and cost are **UNAVAILABLE** from within the runtime; no price is invented. Stopping/status control was likewise not exercisable from inside the session. Account-level enforcement and billing remain **UNVERIFIED**.

Elapsed time from session start to publication: approximately 15 minutes (hard limit 20).

## Result matrix

| # | Step | Status | Evidence | Reason |
|---|---|---|---|---|
| 1 | Checkout at pinned input commit | PASS | `logs/00-environment.txt` | HEAD == `49e4c1c…` |
| 2 | Repository file access / native tools (shell, fs, git) | PASS | all logs | shell, filesystem, git, Python all available |
| 3 | Agent → KnoKeep **CLI** write (`skill/knokeep_state.py`) | PASS | `logs/10-cli.log`, `logs/11-cli-attempt2.log` | `init`, `health`, `session-append`, `flush-state --expect-hash`, `bootstrap` all exit 0 |
| 4 | `flush-log` document write | **BLOCKED (2 failed attempts — diagnostic review requested)** | `logs/10-cli.log`, `logs/11-cli-attempt2.log` | attempt 1 used `--entry` (rejected: `missing --body-file`); attempt 2 used `--body-file` (rejected: `expect_hash required for log update`). Both preserved; per the brief no third attempt was made. The `session_log` key itself was written successfully by `session-append`, so this is a CLI-invocation gap, not proof of a store defect. |
| 5 | **Native MCP enrollment** for this client surface | FAIL (not available) | `logs/41-mcp-harness-attempt2.txt` | No KnoKeep MCP server is registered in this Devin session's native MCP configuration. Nothing was installed or enabled to change that. |
| 6 | Shell-created **MCP protocol harness** (stdio JSON-RPC) | PASS (harness only) | `logs/40-mcp-harness.txt` (attempt 1), `logs/41-mcp-harness-attempt2.txt` (attempt 2) | attempt 1 failed `tools/call` for a caller error (`notifications/initialized` not sent) — preserved. Attempt 2: `initialize`, `knokeep_health`, `knokeep_write` (`status OK`, `commit_class committed`), `knokeep_read`, `knokeep_list` all succeeded against the **local** backend. This establishes protocol compatibility only; it is neither native MCP enrollment nor automatic capture. |
| 7 | Write result + full content hash, verified against saved bytes | PASS | `logs/32-crossprocess-read-final.txt`, `logs/33-store-file-hashes-final.txt` | e.g. MCP write returned `new_hash df856c82…`; independent read of the stored file returns the same sha256 and 115 bytes |
| 8 | Independent cross-process read | PASS | `logs/30-crossprocess-read.txt`, `logs/32-crossprocess-read-final.txt` | separate Python process opened the store with `LocalBackend` and reproduced every key/hash |
| 9 | Persisted client identity vs actual runtime | PARTIAL | `logs/10-cli.log`, `logs/*` | Writes were made with `--client devin-cloud`; the CLI responses do not echo a stored client identity, so persisted-vs-runtime identity could not be compared from the CLI surface. Reported, not rewritten. |
| 10 | Secret gate rejects credential-shaped content | PASS | `logs/20-secret-gate.log` (attempt 1, blocked earlier by CAS), `logs/21-secret-gate-attempt2.log` | synthetic `sk-…` body refused: `{"reasons": ["openai-style key"], "blocked": true}`. Rejected attempts preserved. |
| 11 | Network destinations (authorized, observation only) | PASS (per route) | `logs/50-network.txt` | `github.com` 200, `pypi.org` 200, `example.com` 200, `api.github.com` 403 (unauthenticated GET), `api.devin.ai` 404 (root path). No network permission or service was changed. These results describe these routes/configurations only, not a vendor-wide policy. |
| 12 | Store export with manifest + byte verification | PASS | `../../../../tests/fixtures/q0/Q0-DEVIN-CLOUD-20260927-A1/MANIFEST.sha256`, `logs/60-export-diff.txt` | recursive `diff -r` of original vs export exited 0 before transient files were pruned; manifest re-generated over the pruned export |
| 13 | Publication (commit / push / draft PR / remote verification) | see “Publication status” below | — | — |
| 14 | Fresh-session retrieval / resume | PENDING (not observed) | `continuation.md` | requires a genuinely separate session dispatched by the coordinator after the export is confirmed available. Same-session process restart is not fresh-session resume. |
| 15 | Automatic capture | NOT ESTABLISHED | — | Every write in this run was explicitly invoked. Nothing here demonstrates unattended or hook-driven capture. |

Tracked separately, as required: native MCP support = FAIL; CLI support = PASS; publication = see below; store transfer = PASS (export + manifest); fresh-session retrieval = PENDING; automatic capture = NOT ESTABLISHED. Q0 does not establish capture reliability or a blind behavioral comparison.

## Secret scanning

`gitleaks` is **not installed** on this machine (`which gitleaks` → not found; the KnoKeep `health` command also reported `audit_unavailable (gitleaks not found)`). Fallback scanner: the repository's own `store.gate.secret_scan`, run over every file staged for this job. Scope and full output: `logs/70-secret-scan.txt`.

Four findings, all reviewed and all synthetic:

- `probe.sh` and `logs/secret-body.md` — `openai-style key`: the deliberately fabricated `sk-…q0synthetic` string used to exercise the gate. Not a real credential.
- `logs/00-environment.txt` and `logs/41-mcp-harness-attempt2.txt` — `high-entropy token`: SHA-256 digests and a base64 blob body of synthetic Markdown. **Possible false positives** by the gate's entropy heuristic on legitimate content, recorded as such.

Coordinator correction: account and private session identifiers were removed from this public snapshot; see coordinator-review.md. Saved fixture content is synthetic.

## Store export and restore

- Export directory: `tests/fixtures/q0/Q0-DEVIN-CLOUD-20260927-A1/store/`
- Manifest: `tests/fixtures/q0/Q0-DEVIN-CLOUD-20260927-A1/MANIFEST.sha256` and `MANIFEST.sizes`
- The exported bytes are the **original** saved store, copied with `cp -a` after all of this job's writers exited (quiesced). The store was not reconstructed from this report.

Deliberately omitted from the export, with reasons:

- `locks/advisory.lock`, `locks/cas.lock`, `locks/fence-alloc/*.fence` — transient lease/fence state of a finished run; not required to read the documents.
- `.knokeep-eval/events.jsonl` — runtime telemetry, excluded by the repository's own `.gitignore` (`git check-ignore` confirms). Not force-added; global store ignores were not weakened.

Required metadata that *is* preserved: the document bodies themselves (each carries its generation header), the per-session journal document and the store journal log.

Restore and read (exact commands):

```bash
git clone https://github.com/SathiaAI/knokeep.git && cd knokeep
git checkout q0/devin-cloud/20260927-a1
cd tests/fixtures/q0/Q0-DEVIN-CLOUD-20260927-A1/store && sha256sum -c <(sed 's|  ./|  |' ../MANIFEST.sha256) ; cd -
RESTORED=$(mktemp -d)
cp -a tests/fixtures/q0/Q0-DEVIN-CLOUD-20260927-A1/store/. "$RESTORED"/
python3 skill/knokeep_state.py --store "$RESTORED" --project q0-devin-cloud-20260927-a1 bootstrap
python3 - "$RESTORED" <<'PY'
import sys; from store.local import LocalBackend
b = LocalBackend(sys.argv[1])
for k in sorted(b.list("")): print(k, b.read(k).version_hash)
PY
```

A `.gitattributes` rule scoped to this job's export directory (`-text`) preserves the exported bytes across line-ending conversion. That evidence-only configuration change appears in the diff.

Because these fixtures and this report are published in a public repository, any later restore from them is a **mechanical retrieval check only**. Q2+ behavioral comparisons need separate access-controlled fixtures and answer keys.

## Publication status

Recorded separately, and filled in from observed results at publication time in `publication.md` (same directory): local commit, branch push, draft PR creation and remote verification. A local commit or a PR-metadata response alone is not publication.

## Reproduce

```bash
REPO=$PWD STORE=$(mktemp -d) bash docs/cloud/evidence/Q0-DEVIN-CLOUD-20260927-A1/probe.sh
REPO=$PWD STORE=<same store> bash docs/cloud/evidence/Q0-DEVIN-CLOUD-20260927-A1/probe-corrections.sh
```

`probe.sh` is attempt 1 verbatim, including the three steps that failed; `probe-corrections.sh` is the appended attempt-2 correction. Original failures were kept rather than edited away.

Coordinator publication: this branch was rebuilt from the pinned base with a sanitized evidence snapshot. Original worker commits are preserved privately. Replacing the branch does not guarantee removal of previously exposed versions from GitHub caches or PR references. Original retained fixture bytes are unchanged; omitted original metadata remains pending.
