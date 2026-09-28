# Q5 cursor health result

## Starting HEAD

Verified: `fc744c7c8d44e679582ed8ece8df49214985e53b` on `fix/cursor-health-readonly-a1`.

## Reproduction (before fix)

Synthetic store with one good KKJ2 record plus a trailing frame declaring `UINT64_MAX` body length:

- `LocalBackend(...)` → `OverflowError`
- `python skill/knokeep_state.py health --store <store>` → exit 0, `"verdict": "healthy"` (backend failure swallowed in project enumeration)

## Change summary

- Added `store/health_inspect.py`: read-only journal framing/digest walk with bounded body lengths, published-blob consistency checks, symlink containment, and machine-readable `reasons` (no keys, bodies, or absolute paths in diagnostics).
- `health()` in `skill/knokeep_state.py` runs inspection first; store problems become `store:<reason>` entries in `problems`, nonzero exit via existing `attention` verdict. Project metadata uses `read_project_summaries()` instead of `LocalBackend`.
- Added `tests/test_health_readonly.py` and wired it into CI `SKILL_TESTS`.

## Tests run

| Command | Exit |
|---------|------|
| `python3 tests/test_health_readonly.py` | 0 (19/19) |
| `python3 tests/test_eval.py` | 0 (10/10) |

Initial failure during development: unreadable-store case raised uncaught `PermissionError` from `Path.exists()`; fixed by catching `OSError` on path probes.

## Commit

`232c81f` on `fix/cursor-health-readonly-a1` (pushed).

## Residual limits

- Health still reports Layer-2 audit as unavailable when gitleaks is missing (disclosed in `notes`; not a secret-free guarantee).
- Concurrent journal growth during inspection sets `indeterminate` note; verdict still fails closed if any reason is found.
- Project listing reads published `data/<project>/system_state` files only (not keys absent from disk).
- Inspector targets LocalBackend on-disk layout only (no Postgres/S3 health).
- `store/local.py` frame-length hardening remains out of scope for this branch (separate Codex work).
