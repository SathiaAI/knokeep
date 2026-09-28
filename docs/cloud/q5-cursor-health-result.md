# Q5 cursor health result

## Starting HEAD

Verified: `fc744c7c8d44e679582ed8ece8df49214985e53b` on `fix/cursor-health-readonly-a1`.

## Round 1 (232c81f / b808d73)

Initial read-only inspector and CI wiring. Codex review on `b808d73` found five contract gaps (see below).

## Codex failures on b808d73 (before this round)

| # | Issue | Before |
|---|--------|--------|
| 1 | Root with `data/` but no journal | `inspect ok:true`, health healthy |
| 2 | Journal appended during `_inspect_journal` | `indeterminate:true` but `ok:true`, health could stay healthy |
| 3 | `inspect_local_store` patched to `{ok:true, indeterminate:true}` | health healthy (note only) |
| 4 | Journal key `../../outside` | `ValueError` traceback from `_safe_join` |
| 5 | `.knokeep-eval/events.jsonl` containing `\xff` | `UnicodeDecodeError` in `evaluate()`, no JSON verdict |

Static review also noted: project `system_state` symlink reads, broken root-symlink check, audit/telemetry running on unsafe layout, chmod-based unreadable test, unbounded body retention in inspector.

## Round 2 change summary

- **Journal missing:** always `journal_missing` when `journal.log` absent (including data-only roots); `ok:false`.
- **Indeterminate:** `inspect_indeterminate` reason; `ok:false`; final journal size observed after published checks; health always adds `store:inspect_indeterminate` to `problems` (even if inspect reports `ok:true`).
- **Keys/paths:** gate `_valid_key_shape` before path joins; `_safe_join` failures → `store_symlink_escape` / `journal_bad_key`; no tracebacks.
- **Telemetry:** health uses safe bounded reads via `read_telemetry_bytes` / `_scorecard_from_events_raw`; corrupt UTF-8/JSON → `telemetry:telemetry_unreadable`.
- **Containment:** reject symlink store roots; `_is_safe_regular_file` for journal, data blobs, telemetry; skip audit/projects/scorecard when `store_aux_reads_allowed` is false; journal stores expected hashes only.
- **Tests:** regressions for all five Codex cases, injected `open` permission error (replaces chmod), Linux symlink cases with skip on `OSError`.

## Tests run (this round)

| Command | Exit |
|---------|------|
| `python3 tests/test_health_readonly.py` | 0 (36/36) |
| `python3 tests/test_eval.py` | 0 (10/10) |

## Commit

`e5747df` on `fix/cursor-health-readonly-a1` (round 2; prior history: `232c81f`, `b808d73`).

## Residual limits

- Layer-2 audit still optional when gitleaks is missing (disclosed in `notes`).
- Journal change detection uses file size before/after the inspection pass (documented: not a linearizability guarantee).
- Inspector targets LocalBackend on-disk layout only.
- `evaluate()` CLI behavior unchanged for malformed telemetry; only `health` fails closed.
- `store/local.py` journal hardening remains separate Codex work.
