# Q0 source result: Q0-CLAUDE-CODE-20260927-A1

Job `Q0-CLAUDE-CODE-20260927-A1`, attempt generation 1, node A (Claude Code). Written by the worker inside the run. This is a Q0 mechanical smoke result. It does not establish capture reliability, fresh-session resume or a behavioral comparison.

## Run coordinates

| Item | Value |
|---|---|
| Input commit (`git rev-parse HEAD` at start) | `49e4c1cc6ad75113c97de856a069a670bf0696df` (matches required input) |
| Working branch | `q0/claude-code/20260927-a1` |
| Start / probe end (UTC) | 2026-09-27T22:50:52Z / 2026-09-27T22:54:41Z |
| Runtime | Claude Code print (headless) runtime, new session, launched by coordinator |
| Runtime version | UNAVAILABLE: `claude --version` was denied by the session permission mode |
| Model | `claude-fable-5-1` (Claude Fable 5.1), as stated in the session's system context |
| Configuration | Safe mode: global customizations, hooks, MCP and auto-memory disabled by the coordinator |
| Tools observed | Bash (restricted: pipes, `cd` compounds, `claude --version` and `gitleaks` denied), Read, Write, Edit, Glob, Grep |
| Python | 3.12.14, Windows-11-10.0.26200 (bundled Python first on PATH) |
| Usage / cost | UNAVAILABLE to the worker |

**Git ownership note.** Plain `git` failed with "detected dubious ownership" because the worktree is owned by a different Windows account. Every git command used a per-invocation `-c safe.directory=<worktree>` override. Global git config was not changed.

## Step results

| # | Step | Status | Evidence | Reason |
|---|---|---|---|---|
| 1 | Checkout at pinned input commit | PASS | this table | HEAD equals required commit |
| 2 | Synthetic input read | PASS | `inputs/`, `receipts.json` `inputs` | Input hashes recorded |
| 3 | Agent-to-KnoKeep CLI write | PASS | `commands.jsonl` labels `cli-*`, `receipts.json` `cli` | init, flush-state, flush-log and session-append all exit 0 with receipts |
| 4 | Write hash vs saved bytes | PASS | `receipts.json` `independent_read` | For all four keys the returned hash equals SHA-256 of the raw data file |
| 5 | Read from another process | PASS | `commands.jsonl` `independent-reader-process`, `cli-bootstrap-post-separate-process` | Separate Python process via LocalBackend and separate CLI bootstrap return identical hashes |
| 6 | Persisted client identity | FAIL (mismatch reported) | `receipts.json` `client_identity` | `system_state` persisted `client: cowork` although the writer was Claude Code. `skill/knokeep_state.py` hardcodes `"cowork"` in `init` and flush paths. The journal persisted `client: claude-code` because `--client` was passed. `session_log` and the MCP harness doc carry no client field |
| 7 | Persistent backend explicitly selected | PASS | `commands.jsonl` argv | CLI used `--store $REPO/.q0-run/store` (LocalBackend). MCP harness used `--backend local --root ...`, not the default fake backend |
| 8 | Secret gate negative control | PASS (rejected as expected) | `commands.jsonl` `cli-flush-state-secret-negative-control`, `receipts.json` | A synthetic AWS-shaped key built at runtime was refused, exit 1, label "AWS access key". The value is not in evidence or the store |
| 9 | Legitimate-content false positive check | PASS (no false positive observed) | `inputs/system_state.md` | Body with a 40-hex commit id and a `*_TOKEN` reference name was accepted |
| 10 | Shell-created MCP protocol harness | PASS (harness only) | `receipts.json` `mcp_harness` | initialize, tools/list, knokeep_write (status OK, committed) and knokeep_read succeeded over stdio. Server negotiated protocolVersion 2024-11-05 when 2025-06-18 was requested. This establishes neither native MCP enrollment nor automatic capture |
| 11 | Registered native MCP | NOT TESTED UNDER THIS CONFIGURATION | none | MCP disabled by safe mode. Not evidence of platform absence |
| 12 | Lifecycle hooks / automatic capture | NOT TESTED UNDER THIS CONFIGURATION | none | Hooks disabled by safe mode. No hook saved anything in this run |
| 13 | Network destinations | NOT TESTED | none | The local store path needs no network. Publication is delegated to the coordinator, so no authenticated remote call was made. No network settings were changed |
| 14 | Store export with manifest | PASS | `tests/fixtures/q0/Q0-CLAUDE-CODE-20260927-A1/manifest.json` | Nine members copied, byte-identical to the original; `probe.py verify` reports zero failures |
| 15 | Restore/read from a copy of the export | PASS (mechanical retrieval check) | this report, "Restore check" | Bootstrap from a copy returns the original hashes and resume line |
| 16 | Secret scanning of staged diff | PARTIAL | this report, "Secret review" | gitleaks invocation denied. Manual pattern grep and review done |
| 17 | Local commit | see final recap from worker | git history | Coordinator inspects the local commit |
| 18 | Branch push, draft PR, remote verification | PENDING (coordinator) | none | Worker was instructed not to push or open PRs |
| 19 | Fresh-session retrieval | PENDING | `continuation.md` | Requires a genuinely separate task. Same-session process restarts above are not fresh-session resume |

## Capability summary

| Route | Status |
|---|---|
| CLI support (agent runs KnoKeep CLI) | PASS, observed in this run |
| Native MCP support | NOT TESTED UNDER THIS CONFIGURATION |
| MCP protocol harness (shell-created) | PASS as a harness only |
| Automatic capture | NOT TESTED UNDER THIS CONFIGURATION |
| Store transfer | Export PASS locally; remote transfer PENDING coordinator publication |
| Publication | PENDING coordinator |
| Fresh-session retrieval | PENDING |

## Receipts

| Key | SHA-256 (returned hash = file bytes) | Bytes |
|---|---|---|
| `q0-claude-code-a1/system_state` | `8c4197fde5bbf0a62b9d8124b8d27f2983986d934b120602fafc2c0bdf7f5a29` | 686 |
| `q0-claude-code-a1/session_log` | `3e9baf6bafaf23c50037d747378e24633e28267a761af8fb45cb34add97d046e` | 385 |
| `q0-claude-code-a1/sessions/q0-cc-a1-s1` | `1b0934c7618a946bbabbc2e152653fef8a50a63912711646de7f8f0b4a4f43b6` | 327 |
| `q0-claude-code-a1-mcp/system_state` | `85d9804cc08014a99e8ba8bee381419842e8e2e10c34b8bcc9225b72e5127bf4` | 573 |

## Export contents

The export at `tests/fixtures/q0/Q0-CLAUDE-CODE-20260927-A1/store/` contains `data/`, `journal/journal.log`, `locks/fence-alloc/*.fence` and `.knokeep-eval/events.jsonl`. The fence-alloc files are durable fence-ownership metadata and are required. The journal is the durability source of truth. Omitted transient files are `locks/cas.lock` and `locks/advisory.lock` (one byte each, recreated on open). No `staging/` or `locks/advisory/` files existed at export. All writers were short-lived subprocesses that had exited before export.

`.gitattributes` gains a `-text` rule limited to the export store directory, to preserve bytes. `.knokeep-eval/events.jsonl` is matched by the repository ignore rule. It was reviewed (labels, timestamps and decisions only) and is added by exact path.

## Restore check

```
python -c "import shutil; shutil.copytree('tests/fixtures/q0/Q0-CLAUDE-CODE-20260927-A1/store', '.q0-run/restore-check')"
python skill/knokeep_state.py bootstrap --store .q0-run/restore-check --project q0-claude-code-a1
```

Output: `version_hash 8c4197fd…5a29`, `log_hash 3e9baf6b…046e`, `revision 2`, `conflict_count 0`, resume line "resuming: - Synthetic: total() rewrite to integer cents is in progress; uncommitted in the fixture. / next: - Synthetic: add a test for negative receipt amounts, then finish total(). / v8c4197fde5bb".

Bootstrap appends telemetry to the store it opens, so always restore into a copy rather than reading the committed export in place.

## Secret review

gitleaks could not be run because the permission mode denied it. A manual grep across the worktree for the negative-control pattern found the value only in the uncommitted scratch file under `.q0-run/`. The probe script builds the value from fragments. All inputs and store contents are synthetic. Paths are sanitized to `$REPO` in logs. No environment variables, credentials or account data were recorded. The coordinator should run gitleaks on the commit before publication.

## Reproduction

From the repository root at the input commit plus this job's files, with no existing `.q0-run/` and no existing export directory:

```
python docs/cloud/evidence/Q0-CLAUDE-CODE-20260927-A1/probe.py run
python docs/cloud/evidence/Q0-CLAUDE-CODE-20260927-A1/probe.py export
python docs/cloud/evidence/Q0-CLAUDE-CODE-20260927-A1/probe.py verify
```

`probe.py run` refuses to run if `.q0-run/` exists, and `export` refuses to overwrite the export. A rerun therefore produces a new store with new timestamps and hashes. It does not reproduce the original bytes, which are preserved only in the export.

Console output of the original run was not captured to a file because shell pipes were denied. The per-command stdout, stderr, exit codes and timings are in `commands.jsonl`.

## Appended after commit (2026-09-27, worker)

- **Local commit: PASS.** Evidence commit `873e4f2b8bd0df288e09fa92fd53ded93cfa95fa` on `q0/claude-code/20260927-a1`, parent `49e4c1cc6ad75113c97de856a069a670bf0696df`. This report addendum is a follow-up commit on top of it.
- **Verification from committed objects: PASS.** `git archive HEAD` of the evidence commit was extracted to a separate directory. Running the archived `probe.py verify` against the archived export checked nine members with zero failures and no unexpected files.
- **Branch push, draft PR creation and remote verification: PENDING.** These belong to the coordinator. The remote size/hash check must be repeated from a clean checkout of the pushed commit.

## Coordinator publication note

The worker's original local commits are preserved privately. This public branch is a sanitized snapshot of their evidence, created by Codex from the pinned base; account/authentication metadata was omitted before the first public commit. Original fixture bytes, receipts and command results were not rewritten. The local commit identifiers above identify the private source and are not advertised as fetchable GitHub commits.

The coordinator independently observed installed Claude Code version 2.1.283 before launch and runtime model `claude-fable-5-1` in the stream. Process wall time was 388.33 seconds, exit 0; these are coordinator observations, not worker measurements. The safe-mode limitations in this report still apply. Publication and fresh-task retrieval are separate milestones; the latter remains pending. Synthetic output and secret scanning are checked before push, then the coordinator repeats manifest verification against a clean archive of the remote commit.
