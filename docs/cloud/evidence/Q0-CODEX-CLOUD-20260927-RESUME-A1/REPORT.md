# Q0-CODEX-CLOUD-20260927-RESUME-A1

## Scope and ordering

This was a bounded, mechanical restore of the committed synthetic KnoKeep store for GitHub issue #8. Before dependent work, `git rev-parse HEAD` returned the required input SHA `5637ba7e6f5b980368cdc80a77562c832df0a3d6`. I then read only `CONTINUE.md`, copied the fixture to a newly created disposable directory, and ran the documented CLI bootstrap. I recorded the result below before reading `README.md`, either source-input file, `MANIFEST.sha256`, or `verify_export.py`.

The checkout branch supplied by the platform was `work`, not the requested input branch `codex/run-q0-capability-smoke-test-for-codex-cloud`; the exact required commit was checked out. The platform also did not provide the requested output branch `q0/codex-cloud/resume-20260927-a1`, so this report records that branch-name exception rather than silently claiming either requested branch.

## Results

| Check | Result | Evidence |
|---|---|---|
| Input identity | **PASS** | HEAD exactly matched `5637ba7e6f5b980368cdc80a77562c832df0a3d6`; platform branch-name exception: `work`. |
| Store restore | **PASS** | Fixture copied with `cp -a` into `/tmp/knokeep-q0-codex-resume.m9C9T3`; all 10 manifest-listed store files independently compared byte-for-byte equal. |
| Bootstrap | **PASS** | Existing CLI exited 0, emitted one JSON object on stdout, and emitted no stderr. |
| Receipt match | **PASS** | Retrieved state `87164697d7b3b601b59df5c38c30dc4ea3dacc336f122c864231bc37c990f53f`, log `fa80e5a2262bca138a5ff159cb548e30c84a654c361a09acea0b42fc6d83384e`, and probe `a6f9d7a732af6adb07cc99e59691da0f771c69d71d3177824bff610377a2b48b` matched the later-read manifest/verifier receipts. |
| Unchanged fixture | **PASS** | `git diff --exit-code` and `git status --short` for the fixture were clean after the run. |

After the pre-disclosure capture, an independent hash-and-size loop matched all 12 manifest entries. A separate `cmp` loop matched all 10 store entries in the restored copy. The repository's existing verifier also reported `PASS manifest+receipts+restore: 12 files`.

## Pre-disclosure bootstrap capture

Full invocation (the documented bootstrap command and arguments are unchanged; redirections preserve stdout, stderr, and exit code separately):

```bash
cd /workspace/knokeep
RESTORE="$(mktemp -d /tmp/knokeep-q0-codex-resume.XXXXXX)"
cp -a tests/fixtures/q0/codex-cloud-source-20260927/store/. "$RESTORE/"
python skill/knokeep_state.py bootstrap \
  --store "$RESTORE" \
  --project q0-codex-cloud-synthetic \
  >"$RESTORE/bootstrap.stdout" 2>"$RESTORE/bootstrap.stderr"
rc=$?
printf '%s\n' "$rc" >"$RESTORE/bootstrap.exit-code"
```

Stdout:

```json
{"version_hash": "87164697d7b3b601b59df5c38c30dc4ea3dacc336f122c864231bc37c990f53f", "revision": 1, "log_hash": "fa80e5a2262bca138a5ff159cb548e30c84a654c361a09acea0b42fc6d83384e", "active": "Q0 Codex cloud smoke test evidence collection.", "next": "In a genuinely separate Codex cloud session, bootstrap this exact store and report marker violet-orbit-7319 before reading this evidence report.", "conflicts": [], "conflict_count": 0, "settled_count": 0, "resume_line": "resuming: Q0 Codex cloud smoke test evidence collection. / next: In a genuinely separate Codex cloud session, bootstrap this exact store and report marker violet-orbit-7319 before reading this evidence report. / v87164697d7b3"}
```

Stderr was empty. Exit code: `0`.

Retrieved state SHA-256: `87164697d7b3b601b59df5c38c30dc4ea3dacc336f122c864231bc37c990f53f`.

Retrieved log SHA-256: `fa80e5a2262bca138a5ff159cb548e30c84a654c361a09acea0b42fc6d83384e`.

Active state: `Q0 Codex cloud smoke test evidence collection.`

Next step: `In a genuinely separate Codex cloud session, bootstrap this exact store and report marker violet-orbit-7319 before reading this evidence report.`

Retrieved marker, observed in that captured next step before source evidence was opened: `violet-orbit-7319`.

## Runtime and identity

- Model/runtime identity reported by the active assistant: **GPT-5.6 Sol, a model created by OpenAI**.
- Available cloud thread identity: `CODEX_THREAD_ID=01a0e4af-daee-7cb2-8b61-e9a795a0dca0`.
- Available execution identity: `CODEX_CI=1`; `CODEX_INTERNAL_ORIGINATOR_OVERRIDE=codex_web_agent`.
- Runtime versions exposed by the session: Python 3.12, Node 20, Go 1.24.3, Java 21, Rust 1.89.0, Ruby 3.4.4, PHP 8.4, Swift 6.1, and Bun 1.2.14.
- No separate run ID was exposed. The coordinator must externally verify that this thread is a genuinely new cloud task rather than a restart of the source task.

## Limitations and claims not made

The synthetic answers and marker were publicly readable in the committed repository after the bootstrap capture. This is therefore **not** blind evaluation or unprompted capture. The run used the existing local CLI and a repository snapshot; it does **not** demonstrate native MCP enrollment, autonomous publication, cross-client continuity, or authorization by stored content. No product code, source fixture, permissions, service, paid API, other client, or later stage was changed or launched.

No Git remote was configured. Publication therefore requires the coordinator/platform Create PR flow or application of the local commit; no network publication retries were attempted.
