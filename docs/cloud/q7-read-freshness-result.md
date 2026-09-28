# Q7 read/list freshness (Cursor A1)

## Consistency contract

- **Authority:** `journal/journal.log` after fsync is the durability source of truth; `data/` is a rebuildable materialized view.
- **read(key):** Under a bounded `cas.lock` wait, performs a full sequential journal scan (O(journal bytes), no size/mtime cache), materializes any committed-but-unpublished record for that key, releases the lock, then returns the published blob. `None` only when no durable record exists. Separate calls do not form a cross-key transactional snapshot.
- **list(prefix):** Same journal replay rules; union of journal-derived keys and files under `data/` matching the prefix. The iterator is returned only after `cas.lock` is released.
- **Ambiguous journal:** If replay stops before EOF on unparsable bytes, startup recovery publishes nothing, `_journal_ambiguous` is set, and read/list raise `BackendCorruptionError`. No truncation, salvage, or automatic repair.

## Defects addressed

| Area | Before | After |
|------|--------|--------|
| Long-lived `read` after child journal-only crash | Returned stale `data/` bytes | Replays journal under lock; returns committed body |
| `list` after unpublished create | Omitted new key | Includes journal-visible keys |
| Startup with prefix + opaque suffix + later record | Prefix-only replay overwrote newer published blob | Full-scan gate; ambiguous → no publish, bytes preserved |

## Verification (Linux)

```text
PATH="$HOME/.local/bin:$PATH" python3 -m pytest tests/test_local_backend.py -q
```

48 passed, 4 skipped (Windows-only), including subprocess crash regressions and the opaque-suffix startup guard.

## Limits

- Per-read/list full journal scan cost is intentional (no size-only caching).
- `BackendBusyError` if `cas.lock` cannot be acquired within the configured timeout.
- Health inspector remains read-only and separate from this path.
