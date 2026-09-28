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

## Round 3 change summary (50a8b34 follow-up)

- **Telemetry shape validation:** health rejects non-object rows, bad `findings`/`reasons` types; no silent skip or zero scorecard on corrupt lines; no tracebacks.
- **Optional paths via `lexists`/`lstat`:** dangling or symlink `events.jsonl` (and unsafe `.knokeep-eval` dirs) → `telemetry_unreadable`; missing file still OK.
- **Audit containment:** recursive pre-scan of `data/` (symlinks/reparse/non-regular) before external scan; problems prefixed `audit:`; `gitleaks:none` treated as unavailable (not clean).
- **Layout:** empty journal with missing `data/` → `data_layout_missing`; cooperative offline snapshot limits documented in result residual limits.

## Tests run (round 3)

| Command | Exit |
|---------|------|
| `python3 tests/test_health_readonly.py` | 0 (46/46) |
| `python3 tests/test_eval.py` | 0 (10/10) |

## Round 4 (4379892 follow-up)

Fixed `_scorecard_from_events_raw` malformed error-`ts` path returning a four-tuple; added health CLI regression for invalid/numeric error timestamps.

## Commit

`c64fc794416ba50c2292b3ebdaac1ef95e8563fa` on `fix/cursor-health-readonly-a1` (round 4; prior rounds unchanged in history).

## Residual limits

- Layer-2 audit optional when gitleaks missing or returns `gitleaks:none` (disclosed in `notes`).
- Journal change detection uses file size before/after inspection (not a linearizability guarantee).
- Path and tree checks assume a cooperative offline snapshot; adversarial replacement during health is not claimed safe.
- Inspector targets LocalBackend on-disk layout only.
- `evaluate()` CLI behavior unchanged for malformed telemetry; only `health` fails closed.
- `store/local.py` journal hardening remains separate work.
