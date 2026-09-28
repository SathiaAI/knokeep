# Q7-DEVIN-BOOTSTRAP-AUDIT-20260928-A1: independent review of 3821cdc

- Input: `fix/q7-read-freshness-reviewed` at `3821cdc048f593725e25ba1b86045d84c525179c`. Its parent is `9cd040c943219d7bd65133b6f9c7de5c55561bb6`, confirmed with `git rev-parse HEAD HEAD^`.
- Evidence branch: `q7/devin-bootstrap-review-a1`, created from that head. The only files added are in this directory; product code and existing tests are unchanged.
- Reviewed diff, `git diff 9cd040c HEAD`:
  - `store/health_inspect.py`: new `enumerate_project_published_files_for_audit`.
  - `skill/knokeep_state.py`: `_audit_store(store, project)` now uses the raw walk instead of `backend.list/read`.
  - `tests/test_health_readonly.py`: 3 new checks.
  - Two docs files.

## Commands and results (Linux, Python 3.10.12, nothing installed)
| Command | Result |
|---|---|
| `python3 tests/test_health_readonly.py` | 61/61 passed, exit 0 |
| `python3 tests/test_v1.py` | 33/33 passed, exit 0 |
| `python3 tests/test_eval.py` | 10/10 passed, exit 0 |
| `python3 tests/test_skill_mcp_shared_store.py` | 7/7 passed, exit 0 |
| `python3 tests/test_concurrency.py` | PASS: 2-process no lost update; PASS: store not wedged after contention. Exit 0 |
| `python3 tests/test_audit.py` | exit 0 but `SKIP: gitleaks not installed`. The actual-scanner audit is **UNVERIFIED** here. |
| `python3 -m pytest -q tests/test_local_backend.py` | 59 passed, 4 skipped (Windows-only), exit 0 |
| `python3 docs/cloud/evidence/Q7-DEVIN-BOOTSTRAP-AUDIT-20260928-A1/probe_bootstrap_audit.py` | exit 0; output in `probe_results.txt` |

The coordinator's Windows results were not reproduced; this was a Linux-only run.

## Independent probe results (`probe_bootstrap_audit.py`)
| # | Case | Verdict |
|---|---|---|
| 1 | Secret in an unjournaled `data/p/leak.md` (orphan) | PASS: flagged by secret scan |
| 2 | Binary orphan | PASS: flagged as `non-text/binary` |
| 3 | Secret in `data/p/Thumbs.db` and binary in `data/p/sub/DESKTOP.INI` | **FINDING F1**: no findings, `bootstrap` exit 0 |
| 4 | Sparse orphan of 8 MiB + 1 byte (no real disk use) | PASS: fails closed as `unreadable blob in store` |
| 5 | Symlink in the project to an outside file holding a secret | PASS: `audit blocked: store_symlink_escape` |
| 6 | Hard link (nlink=2) to an outside file holding a secret | PASS: the content is scanned and flagged. The hard link itself is not classified as unsafe. |
| 7 | Torn `KKJ2` journal tail | PASS: `store audit uncertain: journal_torn_tail`, not a silent all-clear |
| 8 | Helper run over staging, orphan and journal | PASS: store tree (mode, size, sha256) unchanged |
| 9 | `bootstrap` CLI on a store with aged staging and an orphan secret | **LIMIT L1**: refuses (exit 1), but `staging/old.tmp` was already removed |
| 10 | 1100 nested directories under `data/p` | **FINDING F2**: `_audit_store` raises `RecursionError`; `bootstrap` exit 1 |
| 11 | 2000 files of 1 KiB | INFO: 1.28 s; no cap on file count or total bytes |

## Findings
- **F1 (medium): basename allowlist bypass.** Any file named `.DS_Store`, `Thumbs.db` or `desktop.ini`, in any letter case and at any depth, is skipped before its content is read. A secret or binary written under one of those names passes the audit, and `bootstrap` resumes. This contradicts the docstring's claim that the audit is "content-based … regardless of its file name". The old `backend.list` path also skipped these names, so this is not a regression, but the raw walk now reaches these files and still skips them. Possible fix to evaluate: still gate-scan benign names, or only skip them when the content is non-text, never when it matches a secret.
- **F2 (low): unbounded recursion.** `walk()` recurses once per directory level, and only `OSError` is caught. Deep nesting raises `RecursionError` instead of returning a block reason. `bootstrap` still exits non-zero, so it fails closed, but without a machine code. An iterative walk or a depth limit that returns a block reason would fix this.
- **L1 (by design, but must be stated): bootstrap is not read-only.** `bootstrap()` calls `_backend(store)`, which constructs `LocalBackend`, *before* `_audit_store`. Constructing it runs `_resume` under `cas.lock`, which can materialize journal records and scavenge aged staging. Probe 9 shows staging removed even though bootstrap then refuses. The helper itself is read-only (probe 8); bootstrap as a whole is not.

## Limits
- The helper's safety assumes a cooperative filesystem; it is not a security promise against a hostile filesystem:
  - `_read_bounded_regular_file` does an `lstat` and then an `open` that follows links, a TOCTOU window. `_entry_contained` uses `resolve()`.
  - Hard links are accepted as regular files.
  - Windows reparse-point handling was not exercised here.
- Aggregate cost: each file can be read up to 8 MiB, plus a full journal scan in `inspect_local_store`, with no budget on file count or total bytes.
- The audit covers only `data/{project}/`. `staging/` and the journal bodies are not content-scanned. An ambiguous journal still refuses (probe 7).
- `bootstrap` output is capped at 10 findings (`findings[:10]`). The refusal is preserved, but the full list is truncated.
