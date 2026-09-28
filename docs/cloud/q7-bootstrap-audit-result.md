# Q7 bootstrap audit raw published walk (Cursor A1)

## Problem

After Q7 read/list freshness (A3), `LocalBackend.list()` / `read()` correctly refuse **orphan** files under `data/` that have no durable journal record. Bootstrap’s out-of-band audit previously enumerated via those APIs, so a gate-bypass write (direct file under `data/`) made `list()` raise `BackendCorruptionError` before any secret scan — surfaced to the CLI as `internal error` instead of a blocked resume.

## Fix

- Added `enumerate_project_published_files_for_audit()` in `store/health_inspect.py`: a **read-only** walk of `data/{project}/` using the same symlink/reparse and bounded-read guards as `safe_data_dir_for_audit`, without constructing `LocalBackend` and without treating blobs as committed records.
- `skill/knokeep_state._audit_store()` now uses that walk and re-scans raw bytes through `store.gate` (same content-based secret / non-text rules; benign `.DS_Store`, `Thumbs.db`, `desktop.ini` skipped).
- Journal / layout problems from `inspect_local_store` are attached as **uncertainty** findings (not a silent all-clear). Unsafe layout blocks audit with explicit `audit blocked:` reasons.
- Journal-authoritative `LocalBackend` read/list behavior is unchanged.

## Verification (Linux, 2026-09-28)

| Suite | Outcome |
|--------|---------|
| `python3 tests/test_v1.py` | **33/33 passed** |
| `python3 tests/test_health_readonly.py` | **61/61 passed** |
| `python3 tests/test_eval.py` | **10/10 passed** |
| `python3 tests/test_skill_mcp_shared_store.py` | **7/7 passed** |
| `tests/test_local_backend.py` (pytest) | **not run** — `pytest` not installed in this VM (no dependency install per job bounds) |

## Limits

- Bootstrap audit is O(files under project) with bounded reads; it does not replay or mutate the journal.
- Uncertainty findings block resume even when no secret is found (by design — incomplete journal is not a clean verdict).
