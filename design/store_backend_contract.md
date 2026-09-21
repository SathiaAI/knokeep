# StoreBackend — Contract v1.4 (the socket)

The single interface every KnoKeep storage backend implements. Above this line (flush engine, secret gate, reconciler, MCP, shims) nothing changes when the backend changes. Load-bearing property: **a write that would clobber a concurrent writer FAILS rather than overwrites.** Windows is the **primary host** and normative for the local adapter.

**v1.4** closes the last residual on O1: a *current* native generation identifies the current version, **not** our lost-ack write's history, so it must not be used to infer non-commit; and a reconcile-retry's `STALE` describes the retry, not the original write. **(O2)** durability rests on the **fsync'd append-only journal** (accepted by the reviewer as correct), not on a local namespace replacement (atomic but not durably persisted on Windows). History: v1.0 → v1.1 (30 fixes) → v1.2 (converge) → v1.3 (O1/O2) → v1.4 (O1 residual). Strict reviewer moved reject → approve_with_fixes with all objections addressed.

## 1. Types
```python
Blob = {"body": bytes, "version_hash": str}   # version_hash = sha256(body), lowercase 64-hex; empty-body hash pinned
Caps = {"atomic","cas","lock","durable","remote"}
```
**Gate-typed values (the write boundary — defense-in-depth, NOT a hard in-process security boundary):**
- `ScannedBody`, `ScannedKey` — **runtime-opaque** objects (not `typing.NewType`), each carrying a gate-issued `HMAC(key || body || nonce)` marker, **constructible only inside the secret-gate module**. Adapters verify the marker before any I/O. A conformance **AST/grep test** fails the suite if either is constructed/subclassed/pickled outside the gate. This is **one of the four secret layers** (threat-model §1) — a runtime+test barrier stopping accidental/casual bypass, **not** claimed to stop a determined in-process bypass; the independent gitleaks audit and references-not-values are the other layers.
- The gate exposes one write entry `persist(key, raw_bytes, *, expected_hash, doc_type)`; it scans key+bytes, enforces the doc-type allowlist (§6), wraps them, and calls the adapter. **Adapter modules are not importable by tool/application code** (private package).

```python
class ErrorKind(Enum):
    NOT_FOUND ; PERMISSION ; NETWORK ; CORRUPTION ; SCAN_FAILURE ; UNSUPPORTED
    INVALID_ARGUMENT     # malformed key/hash/args, rejected before I/O
    BUSY                 # lock-acquire timeout / Windows sharing violation
    TIMEOUT_AFTER_COMMIT ; CONFLICT_UNKNOWN   # OUTCOME-UNKNOWN

WriteResult = OK{new_hash} | STALE{current_hash} | EXISTS{current_hash} | ERROR{kind}
```
**Commit classification — every result is exactly one class:**
- **Definitely-not-committed:** STALE, EXISTS, ERROR{NOT_FOUND, PERMISSION, CORRUPTION, SCAN_FAILURE, UNSUPPORTED, INVALID_ARGUMENT, BUSY}, and `NETWORK` **when it failed before the request was fully sent**.
- **Outcome-unknown (reconcile via §7 before any further write):** ERROR{TIMEOUT_AFTER_COMMIT, CONFLICT_UNKNOWN}. A `NETWORK` failure **after the request was sent but before ack** is reported as `TIMEOUT_AFTER_COMMIT`.
- `OK` = **durable**, where durability comes from the **fsync'd append-only journal** (Durability model, §3) plus a server ACK (remote) / ref update (git) — **not** from a local namespace replacement. Never merely queued.

## 2. Methods
```python
read(key) -> Blob | None       # None ONLY for NOT_FOUND; PERMISSION/NETWORK/CORRUPTION RAISE; bytes verbatim; never follows URL/path/ref or resolves a pointer to a live secret
list(prefix) -> Iterator[str]  # paginated; adapter documents strong vs eventual consistency
write(key, body: ScannedBody, *, expected_hash: str|None) -> WriteResult   # §3
lock(key, ttl_s) -> Lock{token}  # ADVISORY; 128-bit CSPRNG token
unlock(lock) / renew(lock, ttl_s)  # succeed ONLY if this token still owns AND TTL not expired
capabilities() -> Caps ; health() -> BackendHealth
```

## 3. CAS + Durability — the load-bearing method
- `expected_hash=<hash>` → write iff current stored hash == expected_hash, else **STALE**.
- `expected_hash=None` → create-only: write iff absent, else **EXISTS**.
- `expected_hash=<hash>` but key **absent** → **STALE{current_hash=None}**; a CAS-update NEVER creates.
- Retried create-only returning EXISTS where stored hash == new sha256 → treat as **OK** (idempotent replay).
- Malformed `expected_hash` → **ERROR{INVALID_ARGUMENT}** before any I/O.
- **Atomic:** a concurrent reader sees the whole old blob or the whole new blob, never torn. **Crash scope:** no torn or unscanned bytes **visible at the key**; staging artifacts live only in a dedicated staging dir, removed by a startup **scavenger** by TTL.
- **Linearization is per-backend:** native precondition where it exists (object-store/git/postgres) is the linearization point and is mandatory on every write; where the store **is** a filesystem (local), a held **cross-process lock** around read-compare-replace is the mechanism. Advisory `lock()` is never the CAS mechanism for networked backends.

**Durability model (O2 fix — the key point):** the **fsync'd append-only journal is the durability source of truth**; the published key blob is a **rebuildable materialized view** of it. A write returns `OK` only after its journal record is durably persisted (**append + `FlushFileBuffers` on an already-open journal file** — an append to an existing directory entry, which *is* well-defined on Windows, sidestepping the non-durable namespace-replacement problem). The atomic publish of the key blob (`os.replace`) provides **reader-consistency**, not durability. **On resume, the journal is replayed to re-materialize any key whose published copy was lost or torn** by a crash/power-loss after the journal record but before/around the publish. Remote backends derive durability from their server ACK; git from the ref update.

## 4. Per-backend mapping

### 4.1 local (default) — Windows-NORMATIVE
- **Cross-process lock:** non-blocking `msvcrt.locking(LK_NBLCK)` on a dedicated `.lock` file on Windows; `fcntl.flock` on POSIX (`fcntl` forbidden on Windows). Acquire-timeout / sharing violation → **ERROR{BUSY}**, never block indefinitely. Held across read-compare-replace.
- **Order per write:** (1) append the record to the journal + `FlushFileBuffers` → this is the durability commit; (2) publish the key blob atomically: body to `mkstemp` in a dedicated staging dir on the **same volume** (refuse cross-volume via `st_dev`), `FlushFileBuffers`, then **atomic no-overwrite rename for create** (`O_EXCL` reserves the key name; publish by rename) / `os.replace` for update; parent-dir fsync where supported (POSIX; no-op Windows). **Never** `O_EXCL`-create the key then stream bytes into it.
- Binary-only; keys case-sensitive → **refuse case-collision** on case-insensitive volumes; `os.replace` sharing violation → bounded retry then ERROR{BUSY}, never copy/move fallback. Conformance: **two concurrent processes on NTFS** + a crash-after-journal-before-publish replay test.

### 4.2 object-store (primary remote) — metadata-hash + native-token two-step
- **Every PUT stores `sha256(body)` in custom object metadata** (`x-amz-meta-knokeep-sha256` / GCS metadata) — a hash, not content.
- **CAS-update:** a single `HEAD` reads the `knokeep-sha256` metadata **and** captures the native token (S3 `ETag`, GCS `generation`) **from the same object version**. If metadata-hash != `expected_hash` → **STALE** (no write). Else `PUT` (new body + new metadata hash) conditioned on that captured token: `If-Match: <ETag>` (S3) / `x-goog-if-generation-match: <generation>` (GCS). **ETag is NOT the content hash**; sha256 in `If-Match` forbidden; unconditional PUT = spec violation.
- **Create-only:** S3 `If-None-Match: *`; **GCS `x-goog-if-generation-match: 0`**.
- **Map by OPERATION not status:** create-only precondition fail → EXISTS; CAS-update precondition fail → STALE; unclassifiable/5xx/after-send timeout → CONFLICT_UNKNOWN / TIMEOUT_AFTER_COMMIT, fail-closed. Multipart disabled; pinned endpoint; TLS verify; no off-host redirects; env-only creds; default SDK cred chain OFF; IMDS/telemetry OFF. Signed-HTTP core (zero dep); boto3/gcs optional extras under the same rules.

### 4.3 git (solo / fallback)
- Atomic ref update of the captured HEAD SHA (no force/rebase); only the target key's blob mutated. Non-FF → STALE; existing-on-create → EXISTS; **reject a stale per-key hash even if the push would fast-forward**. **Scan the entire to-be-uploaded object set incl. history BEFORE any network write**; failed scan → no local durable unscanned copy, upload nothing.

### 4.4 postgres (adapter only — no service/auth/multi-tenant layer)
- Parameterized SQL, connection **READ COMMITTED**. Create = `INSERT ... ON CONFLICT (key) DO NOTHING` (0 rows → EXISTS). Update = `UPDATE store SET body=?, version_hash=? WHERE key=? AND version_hash=?` (rowcount 1 → OK). **0-row update → follow-up `SELECT version_hash WHERE key=?`:** absent → **STALE{None}**, present → **STALE{actual_hash}**.

## 5. The "one door" (all egress via the gate)
Covered: blob body; journal/mkstemp/repair/import/migration; lock & marker file contents (**only** the 128-bit token + expiry int); git blobs/trees/commits/notes/ref names; object user-metadata/tags/headers (incl. the sha256 metadata, a hash); error strings (**sha256 prefix only** — never body bytes or key values); local telemetry. **Key names** → `ScannedKey`: charset allowlist `[A-Za-z0-9._/-]`; reject empty segments, relative paths, NTFS reserved names, trailing whitespace, case-insensitive collisions — before I/O; never derived from blob bytes.

## 6. version_hash, doc-type allowlist & ABA (enforced in persist())
- `version_hash = sha256(body)`, lowercase 64-hex; empty-body hash pinned.
- **Content-hash CAS only for the named STATE/snapshot allowlist** (`system_state`, `session_log`, `HANDOFF`, per-session journal) where identical bytes = identical state.
- **Lease/counter/append-log/lock-like types MUST carry a monotonic `uint64` generation in the scanned body**; `persist()` refuses them on content-hash CAS alone.

## 7. Uncertain-outcome reconciliation (O1 — no false definitely-not-committed)
After TIMEOUT_AFTER_COMMIT / CONFLICT_UNKNOWN / cancel, read once and compare:
- current == intended new sha256 → **OK** (our write landed; do not rewrite).
- current == expected_hash → optionally retry the same CAS **once** under backoff. **The retry's result describes the RETRY only:** if it returns OK, that is a fresh commit; if it returns STALE/EXISTS/uncertain, the ORIGINAL operation's outcome is **unchanged and remains `CONFLICT_UNKNOWN`** — never report the retry's STALE as the original's result.
- **otherwise → `CONFLICT_UNKNOWN` (still outcome-unknown), NEVER `STALE`.**

Under content-hash CAS a lost ack cannot distinguish "our write committed then was superseded" from "our write never committed," so the contract must **not** assert definitely-not-committed. A **native monotonic generation resolves the original write's fate ONLY if the writer captured its own write's target generation before the ack was lost** (e.g. the conditional-PUT's assigned generation is known to us); a merely-*current* generation identifies the current version, **not our write's history, and MUST NOT be used to infer non-commit** (doing so recreates the false-not-committed bug). When our own generation was not captured, the result stays `CONFLICT_UNKNOWN`. Callers on content-hash-only paths must be **idempotent** and tolerate `CONFLICT_UNKNOWN`. Cancellation leaves either the old or the new visible blob — no third value, no unscanned temp at the key.

## 8. Load-time capability probe (invariant)
On load, against reserved prefix `.knokeep/probe/` (cleaned up): create-only → OK, duplicate → EXISTS, failed CAS → STALE. **Wrong mapping or missing native precondition → REFUSE TO START. No degraded mode.**

## 9. Conformance suite — mandatory for ALL four adapters before merge
CAS-reject (incl. races) · atomicity + crash leaves nothing torn/unscanned at the key · **journal-replay durability (crash after journal record, before/around publish)** · secret scan incl. side paths · no-egress · lock TTL+token · uncertain-outcome reconcile (incl. the committed-then-superseded case → CONFLICT_UNKNOWN, never false STALE) · **Windows two-process on NTFS** · migration. Secret-gate + CAS diffs additionally get a frontier review of the diff (the G1 verification).
