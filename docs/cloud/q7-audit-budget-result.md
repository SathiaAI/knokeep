# Q7-DEVIN-AUDIT-FIX-20260928-A1 — bootstrap audit secret-bypass close and walk budgets

- Base: `fix/q7-read-freshness-reviewed` at `3821cdc048f593725e25ba1b86045d84c525179c`.
- Output branch: `fix/q7-audit-budget-a1`.
- Files changed:
  - `store/health_inspect.py`
  - `skill/knokeep_state.py`
  - `tests/test_audit_budget.py` (new)
  - this file
- `store/local.py` (LocalBackend) and existing tests are unchanged.

## Changes
- **Secret bypass closed.**
  - `enumerate_project_published_files_for_audit` no longer skips `.DS_Store`, `Thumbs.db` or `desktop.ini` (any case, any depth). It returns their raw bytes like any other file.
  - `_audit_store` runs `gate.secret_scan` on every file's raw bytes first.
  - OS-metadata names are exempt **only** from the non-text/binary check, through `is_os_metadata_basename`, so benign binary OS metadata stays compatible. They are never exempt from secret scanning.
  - Unreadable or oversized files still produce `unreadable blob in store`.
  - `secret_scan` is heuristic, not a universal guarantee.
- **Bounded walks.**
  - A single iterative `_bounded_tree_walk` (no recursion) now drives both the prerequisite data-tree safety walk (`_data_tree_safe_for_audit`, which is also used by health) and the project audit walk.
  - Limits:
    - directory depth below `data/` ≤ 64 (`_AUDIT_MAX_DEPTH`);
    - entries ≤ 4096 (`_AUDIT_MAX_ENTRIES`);
    - aggregate regular-file `st_size` ≤ 64 MiB (`_AUDIT_MAX_TOTAL_BYTES`);
    - the existing per-file 8 MiB scanner bound is kept.
  - When a limit is exceeded the walk fails closed with `audit_depth_budget_exceeded`, `audit_entry_budget_exceeded` or `audit_byte_budget_exceeded`, and returns no entries.
- **Link refusal kept.** Symlinks and reparse points still give `store_symlink_escape`, and non-regular files give `data_layout_unsafe`.
- **Bootstrap ordering unchanged.** `bootstrap()` still constructs a recovery-capable `LocalBackend` before the audit. That startup can replay journal records and remove aged staging, so bootstrap is not read-only. Only the audit helper is.

## Tests (Linux, Python 3.10.12, nothing installed)
| Command | Result |
|---|---|
| `python3 tests/test_audit_budget.py` | 35/35 passed, exit 0 |
| `python3 tests/test_health_readonly.py` | 61/61 passed, exit 0 |
| `python3 tests/test_v1.py` | 33/33 passed, exit 0 |
| `python3 tests/test_eval.py` | 10/10 passed, exit 0 |
| `python3 tests/test_skill_mcp_shared_store.py` | 7/7 passed, exit 0 |
| `python3 tests/test_concurrency.py` | all PASS, exit 0 |
| `python3 tests/test_audit.py` | exit 0, `SKIP: gitleaks not installed`. **UNVERIFIED.** |
| `python3 -m pytest -q tests/test_local_backend.py` | 59 passed, 4 skipped (Windows-only) |
| `pyflakes store/health_inspect.py skill/knokeep_state.py tests/test_audit_budget.py` | clean |

`test_audit_budget.py` covers:
- A secret under each reserved name, including nested mixed-case names, a binary-wrapped secret and a UTF-16 secret. Each is flagged, `bootstrap` refuses, and the store bytes are unchanged.
- Benign binary `.DS_Store` and `Thumbs.db` are allowed, and `bootstrap` proceeds. Binary content under an ordinary name is still refused.
- A 1100-deep tree gives `audit_depth_budget_exceeded` with no `RecursionError`, and `bootstrap` refuses. Depth 64 is allowed and depth 65 is refused.
- Depth limits are enforced by the prerequisite walk (on another project) and by the project walk on its own.
- The entry and byte limits are exercised with patched constants. Both fail closed and leave bytes unchanged.
- A symlink named `Thumbs.db` is still refused.

## Limits
- The limits apply to the whole `data/` tree in the prerequisite walk. A store over 4096 entries or 64 MiB now makes both bootstrap and the health audit refuse, with an explicit reason. There is no override.
- The safety model assumes a cooperative filesystem and is not a hostile-filesystem security promise:
  - an lstat-then-open TOCTOU window remains;
  - hard links are accepted as regular files;
  - `st_size` can change between the walk and the read. Each read is still capped at 8 MiB + 1 bytes.
- `staging/` and journal bodies are not content-scanned by this audit.
- Windows was not run here.
