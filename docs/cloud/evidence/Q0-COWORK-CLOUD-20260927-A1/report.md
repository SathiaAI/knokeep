# Q0-COWORK-CLOUD-20260927-A1 — Claude Cowork Cloud source probe (generation 1)

Mechanical Q0 capability test. Synthetic data only. Not a behavioral or blind test. Automatic capture and capture reliability: **NOT TESTED**.

## Run record

| Item | Value |
|---|---|
| Issue | https://github.com/SathiaAI/knokeep/issues/12 |
| Surface | Claude Cowork, cloud sandbox task (web-browser client), new task outside the KnoKeep review project |
| Input branch / commit | `docs/cloud-handoff-2026-09-27` @ `49e4c1cc6ad75113c97de856a069a670bf0696df` (`git rev-parse HEAD` matched; remote branch head also matched) |
| Output branch | `q0/cowork-cloud/20260927-a1` (created locally from the input commit) |
| Brief | fetched from raw.githubusercontent.com @ `f913d9e9…` — HTTP 200, 8520 bytes, sha256 `fbefed88245afd0d060d80f4f3df89d3a4b85462f0bb5371af36ef76c19e865e` |
| Runtime | Linux 6.18.44 x86_64 container; Python 3.11.15; Node v22.22.2; git 2.43.0; `gh` not installed |
| Model | configured `claude-opus-5-5` (platform notes the serving model may differ) |
| Tools exposed | Bash, Read/Write/Edit, Artifact, SendUserFile, subagents, memory tools, many third-party connectors (unused). **No `knokeep` MCP tools registered.** |
| Memory | Account-level global memory is present and a snapshot is injected by the platform. Not consulted for this task; no project memory (not in a project). Global memory isolation: **NOT ESTABLISHED / UNVERIFIED**. Settings not changed. |
| Desktop bridge | Bridge tool names are present in the tool list; **not invoked**. All steps ran in the cloud container → CLI path is **bridge-independent**. |
| Auth mode | git over HTTPS via the platform egress proxy (proxy-injected git config). No credential files read, no new credentials obtained. |
| Start / finish (UTC) | 2026-09-27T22:58:59Z / see Publication section |
| Usage / cost | UNAVAILABLE (not exposed to the task) |

Docs read at the input commit: `docs/cloud/README.md`, `orchestration.md`, `continuity-acceptance.md`, `design-constraints.md`.

## Store and project

- Backend: **LocalBackend**, explicit `--store`/`--root` (never the MCP default `FakeBackend`). `KNOKEEP_STORE` unset; per-user default path did not exist before the run.
- Store path during run: disposable container path (`$STORE`, redacted in logs). Project ID: **`q0-cowork-cloud-a1-plan`** (synthetic "Orchard Library volunteer rota" plan with a changed decision, an obsolete claim and an unresolved item).
- Inputs: `inputs/`. Scripts: `probe.sh` (main), `probe_correction.sh` (appended correction), `mcp_harness.py`, `verify.py`. Logs: `logs/NN-*.{cmd,out,err,exit}`; attempt 1: `logs-attempt1/`.

## Results per step

| # | Step | Result | Evidence |
|---|---|---|---|
| 0 | Checkout at exact input commit | PASS | this table |
| 1 | Brief retrieval (pinned URL) | PASS | sha256 above |
| 2 | CLI `init` | PASS | `logs/01-*` |
| 3a | CLI `flush-state`/`flush-log` — attempt 1 | FAIL (probe error: omitted `--expect-hash`; store correctly refused non-CAS update, exit 1) | `logs-attempt1/02-*`, `03-*` |
| 3b | CLI `flush-state`/`flush-log` — attempt 2 with CAS hash | PASS | `logs/02-*`, `03-*` |
| 4 | CLI `session-append` (`--client cowork-cloud`) | PASS | `logs/04-*` |
| 5 | Secret-gate false-positive probe (40-hex commit SHA + 64-hex hash in prose) | PASS — accepted, **no false positive**. No rejection occurred in this run, so no rejected content exists to preserve. Credential-shaped rejection was deliberately not tested (would put credential-like strings in public evidence). | `logs/05-*` |
| 6 | CLI `bootstrap` read (separate process) | PASS mechanically; resume line empty — see finding F1 | `logs/06-*` |
| 7 | Shell-created MCP stdio harness (`mcp.server --backend local`): initialize, tools/list, read, write, list | PASS — **harness only**; proves neither native enrollment nor automatic capture | `logs/07-*` |
| 8 | Independent verify (fresh process, `LocalBackend.read` + raw sha256) | PASS | `logs/08-*` |
| 9 | Correction: `flush-log` with schema headings (CAS) | PASS | `logs/09-*` |
| 10 | Final `bootstrap` — resume line populated | PASS | `logs/10-*` |
| 11 | Final independent verify: every key's `version_hash == sha256(on-disk bytes)` | PASS | `logs/11-*`, `receipts.txt` |
| 12 | Export original store + manifest; export == original byte-for-byte | PASS | `tests/fixtures/q0/Q0-COWORK-CLOUD-20260927-A1/` |
| 13 | Restore-read from a copy of the export (separate process) | PASS — same hashes, resume line intact | `logs/12-*` |
| 14 | GitHub endpoints | see Network | `logs/13-network.out` |
| 15 | Publication | see Publication | — |

Integration scoring (separate):

| Path | Result |
|---|---|
| Agent → KnoKeep CLI (`skill/knokeep_state.py`, LocalBackend) | **PASS** (observed; bridge-independent) |
| Registered native KnoKeep MCP in Cowork | **NOT PRESENT** — no KnoKeep MCP tools/plugin in this task. Not enrolled. |
| Shell-created MCP protocol harness | **PASS (harness only)** |
| Automatic capture / capture reliability | **NOT TESTED** |
| Fresh-session retrieval | **PENDING** — requires a different task started by Codex after publication is verified; same-session subprocess reads do not count |

## Findings

- **F1 (fixture/usability):** `flush-log` accepted a body with non-schema headings (`## Active`, `## Next step`) and returned ok; `bootstrap` then produced an empty resume line (it parses `## Active State` / `## Next Step`). Corrected by an appended CAS flush (step 9). Original bytes of step 3 remain in `journal/journal.log`, and the original log is preserved. No product change made.
- **F2 (client identity):** actual runtime = Claude Cowork **Cloud**. `system_state` persists `client: cowork` (hard-coded in `init`/flush, not settable, does not distinguish cloud vs desktop) → **partial mismatch**. Journals persist `client: cowork-cloud` (from explicit `--client`; the CLI default would also be `cowork`). The MCP-harness blob has no client field (raw body). Original bytes not rewritten.
- **F3 (receipts):** CLI `session-append` returns `{ok, log}` without a content hash; its bytes were verified only by independent read-back. `flush-*` and MCP `knokeep_write` return full sha256 receipts that match the saved bytes.
- **F4 (telemetry):** `bootstrap` is not read-only on disk — it appends to `.knokeep-eval/events.jsonl`. Restore/read must therefore run against a copy of the export.

## Network (observed route/configuration only)

| Host | Method | Path | Result |
|---|---|---|---|
| github.com | git clone/fetch (smart HTTP) | SathiaAI/knokeep | OK |
| github.com | git ls-remote | heads | OK |
| raw.githubusercontent.com | GET | pinned brief | 200 |
| api.github.com | GET | /repos/SathiaAI/knokeep | 403 — proxy message: repository access not enabled for this session (would need an `add_repo` grant; not available/not requested) |
| api.github.com | GET | /repos/SathiaAI/knokeep/issues/12 | 403 (same) |
| api.github.com | GET | /user | 200 (body not recorded) |
| github.com | GET | gitleaks v8.21.2 release tarball (scanner tooling) | 200 |
| pypi.org | pip install | detect-secrets 1.5.0 (scanner tooling) | OK |

Draft PR creation via API is therefore not available on this route; not attempted.

## Store export

- Path: `tests/fixtures/q0/Q0-COWORK-CLOUD-20260927-A1/store/` + `MANIFEST.sha256-size.txt` (sha256, bytes, path).
- Writers quiesced (all subprocesses exited; no running writer) before copy with `cp -a`. **No files omitted**: 0-byte `locks/*.lock` and `locks/fence-alloc/*.fence` are included verbatim for fidelity; they are transient and not needed for reads.
- `.gitattributes` gained one evidence-only rule: `tests/fixtures/q0/Q0-COWORK-CLOUD-20260927-A1/** -text`.
- `store/.knokeep-eval/events.jsonl` is matched by the repo `.gitignore` (`.knokeep-eval/`); it was reviewed (synthetic telemetry only) and added with exact-path `git add -f`. Ignore rules not changed.
- Restore/read:
  ```
  git fetch origin q0/cowork-cloud/20260927-a1 && git checkout <commit>
  cd tests/fixtures/q0/Q0-COWORK-CLOUD-20260927-A1 && while read h s p; do echo "$h  store/$p"; done < MANIFEST.sha256-size.txt | sha256sum -c
  cd - && cp -a tests/fixtures/q0/Q0-COWORK-CLOUD-20260927-A1/store "$RESTORE"
  python3 skill/knokeep_state.py bootstrap --store "$RESTORE" --project q0-cowork-cloud-a1-plan
  ```

## Secret review and scanning

- Manual review of every staged file: synthetic content, sha256 hashes, base64 of synthetic bodies, `$STORE` placeholder. No credentials, account data, private session identifiers or personal data. Logs sanitized only by replacing the absolute container store path with `$STORE` (`logs/SANITIZATION.txt`).
- Scope: exactly the 115 staged files (exported from the index, not the working tree).
- gitleaks 8.21.2 with repo `gitleaks.toml`, `--no-git` over staged content: **no leaks found** (exit 0).
- detect-secrets 1.5.0 `--all-files`: 12 files flagged, all `Hex High Entropy String` (sha256 receipts/hashes) or `Base64 High Entropy String` (`body_b64` of the synthetic `system_state` in the MCP read response). Reviewed: **false positives**.

## Publication

(results appended after commit/push)
