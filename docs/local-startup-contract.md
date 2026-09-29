# Local backend startup serialization

The local filesystem adapter treats the append-only `journal/journal.log` as the durability source of truth. Published blobs under `data/` are a materialized view that must agree with the journal.

## Startup under `cas.lock`

When a `LocalBackend` is constructed, it acquires the existing cross-process `locks/cas.lock` once (with the same bounded timeout used elsewhere) and holds that lock while it:

1. Observes whether `journal.log` exists and whether `data/` already contains published files.
2. Opens or creates `journal.log` (create-only when the store is genuinely empty).
3. Runs journal replay (`resume`), the case-insensitivity probe, and the staging scavenger.

The lock is released before the constructor returns. Startup does not acquire `cas.lock` a second time and does not change advisory-lock ordering.

This serialization prevents two fresh processes from interleaving so that one observes “no journal” and later “published data” and falsely refuses the store as corrupt while another process legitimately created the journal and published.

## Missing journal with published data

If `journal.log` is absent but `data/` already contains published blobs, construction fails with `BackendCorruptionError`. The adapter does not recreate, truncate, or repair the journal in that situation.

## Corrupt or incomplete journals

If the journal cannot be replayed to end-of-file without ambiguity, startup marks the journal ambiguous, does not publish recovery materialization, and preserves journal bytes and staging evidence. Read and list fail closed until an operator repairs the store out of band. This correction does not add new recovery behavior beyond serializing the existing startup decision.

## Lock timeout on startup

If `cas.lock` cannot be acquired within the configured timeout during construction, construction fails without creating `journal.log` on an otherwise empty store. Any journal handle opened during a failed attempt is closed on the exception path.
