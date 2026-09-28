# recovery_export_v1 (experimental)

Offline, read-only **evidence export** for a local store tree. This does **not**
repair the store, switch service, install a candidate, or construct
`LocalBackend` on the source.

## Scope

- **export**: hold `locks/cas.lock` (non-blocking), copy every regular file into
  an archive under a new output directory, derive a **non-authoritative**
  journal-prefix candidate (safe keys only), write `MANIFEST.json`.
- **verify**: re-hash the archive, re-derive prefix/candidate checks, require
  `completeness_proven: false` and `candidate_is_authoritative: false`.

## Limits (explicit)

| Limit | Value |
| --- | --- |
| Total regular-file bytes inventoried | 64 MiB |
| Max regular files | 10,000 |
| Max relative path depth | 64 |
| Copy/hash chunk size | 64 KiB |

Refuses symlinks, hard links (`st_nlink > 1`), special files, output/store
overlap (after `realpath` + `normcase`), busy lock, missing/unreadable journal
or lock, budget exceed, store mutation between inventory passes.

## Journal

Strict prefix classification uses `store.health_inspect._iter_journal_records`
(the product read-only parser). Bytes after the prefix stop offset are reported
as an **unexamined/ambiguous** tail; no quadratic resync scan. Invalid keys and
case-fold collisions are listed and omitted from the candidate.

**Always** `completeness_proven: false` and `candidate_is_authoritative: false`.
Without an external anchor, completeness cannot be proven (coherent rollback/truncation
is indistinguishable from an honest store).

## Durability (honest)

- Regular files are flushed and `fsync`'d before the export directory is renamed
  complete.
- **POSIX**: directory entry durability may require `fsync` on the parent
  directory; this experiment does not claim full directory fsync on all platforms.
- **Windows**: Python does not expose directory `fsync`; directory metadata
  durability is **not** claimed.
- Final `rename` to the completed export name is a name swap only — **not** a
  power-loss proof.
- `manifest_sha256` is a deterministic self-check only; it is **not**
  authentication and does not prove absence of coherent malicious editing.

## Incomplete attempts

Each run creates a unique `.incomplete-recovery-export-…` directory with
`ATTEMPT.json`. Old attempts are never removed or reused. Failed runs retain
evidence; completed exports are never overwritten (`REFUSE_OUTPUT_EXISTS`).

## Usage

From a checkout root (imports `store` from the tree; **does not modify** it):

```bash
export PYTHONPATH="/path/to/checkout:/path/to/pkg-root"
python3 -m experiments.recovery_export_v1.export export /path/to/store /path/to/out
python3 -m experiments.recovery_export_v1.export verify /path/to/out/recovery-export-<id>
python3 -m unittest discover -s experiments/recovery_export_v1 -p 'test_*.py' -v
```

## Platform testing

Linux results are produced in CI/coordinator runs. **Windows: UNVERIFIED** until
reproduced (held-lock byte read, overlap, junction/reparse rules).
