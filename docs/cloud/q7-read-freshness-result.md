# Q7 read/list freshness (Cursor A1 + A2 correction)

## Consistency contract

- **Authority:** `journal/journal.log` after fsync is the durability source of truth; `data/` is a rebuildable materialized view.
- **Clean replay:** Recovery publish (startup, read, list materialization, write-time recover) runs only when a sequential journal scan reaches EOF (`scan_state["end"] == scan_state["size"]`). There is no torn-tail exception and no suffix re-parse to guess whether trailing bytes are harmless.
- **Incomplete journal (`end != size`):** No recovery publish. `_journal_ambiguous` is set at startup. `read`/`list` raise `BackendCorruptionError`. `write` returns `ERROR(CORRUPTION)`. All journal and published bytes are left untouched (no truncation, salvage, or repair).
- **read(key):** Bounded `cas.lock`, full scan, optional materialize when replay is clean, lock released, then read `data/`. O(journal bytes) per call; no size/mtime cache. Separate calls are not a cross-key snapshot.
- **list(prefix):** Same replay rules; iterator returned only after lock release.
- **Recovery publish contention:** `_ReplaceBusy` / `_ReservationRace` during read recovery map to `BackendBusyError`, not raw internal exceptions.
- **Missing journal with nonempty `data/`:** construction raises `BackendCorruptionError`; journal is not created.
- **Runtime journal deletion:** `read`/`list`/`write` fail closed; `data/` alone is never authoritative.
- **Constructor failure after journal open:** `close()` runs so handles are not leaked.

## Verification (Linux)

```text
PATH="$HOME/.local/bin:$PATH" python3 -m pytest tests/test_local_backend.py -q
python3 tests/test_health_readonly.py
```

Conformance (`conformance/suite.py`) skipped here if `boto3` is not installed.

## Limits

- Per-read/list full journal scan cost is intentional.
- `BackendBusyError` when `cas.lock` cannot be acquired within the configured timeout.
- Health inspector remains read-only and separate from this path.
