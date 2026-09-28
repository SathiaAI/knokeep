# Q7 read/list freshness (Cursor A1 + A2 + A3)

## Consistency contract

- **Authority:** `journal/journal.log` after fsync is the durability source of truth; `data/` is a rebuildable materialized view only when backed by a journal record.
- **Clean replay:** Recovery publish runs only when `scan_state["end"] == scan_state["size"]`. Any trailing bytes forbid recovery publish; read/list raise `BackendCorruptionError`; write returns `ERROR(CORRUPTION)`.
- **Orphans:** A file under `data/` with no durable journal record for that key is not readable or listable; both raise `BackendCorruptionError` and leave journal and data bytes untouched. Coherent journal rollback at a clean boundary without an external anchor is not auto-detected.
- **read(key):** Bounded `cas.lock`, full scan, materialize when clean, orphan check, read published bytes **before** releasing the lock. `None` only when the key has no journal record and no data file.
- **list(prefix):** Journal keys only under prefix; any matching orphan published file raises corruption. Iterator returned after lock release.
- **Startup:** Case-insensitivity probe runs inside the same `cas.lock` critical section as `_resume()` so probe files are not visible as user orphans to concurrent adapters.

## Verification (Linux, 2026-09-28)

| Suite | Outcome |
|--------|---------|
| `tests/test_local_backend.py` | **58 passed**, **4 skipped** (Windows-only) |
| `tests/test_health_readonly.py` | **58/58 passed** |
| `tests/test_skill_mcp_shared_store.py` | **7/7 passed** |
| `conformance/suite.py` | **not run** — collection error: `boto3` not installed (no download in this VM) |

## Limits

- O(journal bytes) per read/list; no cross-key snapshot across separate calls.
- `BackendBusyError` on `cas.lock` timeout; recovery publish contention on read maps to `BackendBusyError`.
- Health inspector remains read-only and separate.
