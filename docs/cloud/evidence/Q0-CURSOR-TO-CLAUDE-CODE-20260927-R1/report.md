# Q0-CURSOR-TO-CLAUDE-CODE-20260927-R1 — cross-client retrieval report

**Result: all 9 checks passed.** Claude Code read back the store that Cursor Cloud exported
(`Q0-CURSOR-CLOUD-20260927-A1`), byte for byte. The committed fixture is unchanged.

## Setup

| Item | Value |
|---|---|
| Source commit (checked-out HEAD) | `be48e86408ff93290e2e750ac04ee2a5bd49e094` (matches the required commit) |
| Branch | `q0/cursor-to-claude-code/20260927-r1` |
| Input | `$REPO/tests/fixtures/q0/Q0-CURSOR-CLOUD-20260927-A1/store-export/` + `MANIFEST.sha256` |
| Project ID | `q0cursor` |
| Reader | Claude Code runtime, model `claude-fable-5-1` (as reported by the runtime; CLI version not observable from inside the session) |
| CLI | `skill/knokeep_state.py` (docstring: "KnoKeep state helper v2.1"), run with bundled Python 3.12.14 |
| Writer (source) | Cursor Cloud, which is a different client from this reader |

## Reproduce

```
python docs/cloud/evidence/Q0-CURSOR-TO-CLAUDE-CODE-20260927-R1/verify_retrieval.py
```

The script checks every manifest line (sha256 **and** byte count) and runs the strict verifier.
It then copies the export into a disposable scratch directory (`$SCRATCH`, created inside the
checkout and deleted afterwards) and runs `bootstrap` plus an in-process `LocalBackend`
read/list, both against that copy. Finally it re-hashes the committed fixture and runs
`git diff`/`git status` on it. The full sanitized command log, with exit codes, stdout and
stderr, is in `logs/command_log.md`. The machine-readable results are in `logs/results.json`.

## Checks

| Check | Result |
|---|---|
| HEAD == required commit | pass |
| Manifest: 9/9 members, hash + size match, no unmanifested files | pass |
| Strict verifier `verify_export.py` (exit 0, `failures: []`) | pass |
| `bootstrap` on scratch copy (exit 0) | pass |
| Independent `LocalBackend.read` bytes == export bytes (3 keys) | pass |
| bootstrap `version_hash` == sha256(export `system_state`) `49303af1…30a3` | pass |
| bootstrap `log_hash` == sha256(export `session_log`) `1a51ab6d…3453` | pass |
| Fixture bytes unchanged (pre/post hash of every file) | pass |
| Fixture `git diff --exit-code` / `git status --porcelain` clean | pass |

As expected, bootstrap appended one telemetry line to `.knokeep-eval/events.jsonl` in the
**scratch copy** (`scratch_telemetry_appended: true`). It never touched the committed fixture.

## Retrieved content (as returned)

`q0cursor/system_state` (LocalBackend read, 151 bytes):

```
---
schema_version: 1
project_id: q0cursor
client: cowork
revision: 2
updated: 2026-09-27T22:48:38Z
---
## Q0 probe
q0-cursor-cloud-probe-marker-alpha
```

`q0cursor/sessions/q0harness` (85 bytes, no frontmatter):

```
## Journal
[2026-09-27T22:48:38Z] q0-cursor-cloud-probe-marker-alpha via mcp harness
```

`q0cursor/session_log` (144 bytes): frontmatter plus empty `Completed & Verified`,
`Active State` and `Next Step` sections.

Bootstrap resume fields, exactly as returned:

```
"revision": 2, "active": "## Next Step", "next": "", "conflict_count": 0, "settled_count": 0,
"resume_line": "resuming: ## Next Step / next: (none) / v49303af13596"
```

**Unexpected values:** `active` is the literal text `"## Next Step"`, but the saved Active State
section is empty. This appears to be a parsing artifact in `_section()`: `\s*` after the
heading swallows the blank line, so the next heading gets captured. `next` is empty, which
matches the saved data. The probe marker is **not** part of the resume fields. It appears only
in the `system_state` body and in the journal. I did not change any product code.

## Client metadata: saved value vs. actual writer

- The source writer was **Cursor Cloud**. However, the saved `system_state` frontmatter says
  `client: cowork`, which is the CLI's default `--client` value. It does not say "cursor".
- The `q0harness` journal has no frontmatter, so it records no client identity. Its only
  identity text is the entry's "via mcp harness".
- The saved metadata therefore does not identify the writer as Cursor. Claude Code, as the
  reader, wrote nothing to the committed store.

## Limitations

- The run was dispatched manually. The input was a git-carried snapshot, not a live shared store.
- Hooks, MCP and automatic memory were disabled (safe-mode). The only paths exercised were
  the CLI `bootstrap` and a direct `LocalBackend` read.
- No network access, package installation, agent dispatch, product-code edits or fixture edits.
- This proves **only the retrieval observed here**. It does not prove automatic saving, blind
  or behavioral performance, or the safety of future writes.
- I used the original A1 report only indirectly: the strict verifier compares against its
  `logs/probe_summary.json`. None of the results above come from that report.
- Git needed a one-off `-c safe.directory=…` because the checkout is owned by a different
  OS account. I did not change any global git config.
