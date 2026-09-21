"""migrate(source, target) — move a store from one StoreBackend to another,
safely (design/store_backend_contract.md v1.4, §5 "the one door" and §9's
mandatory "migration" conformance item).

This module is TOOL/APPLICATION code, not an adapter: like
`reconciler/reconcile.py`, it only ever imports `store.gate`, `store.backend`
(for typing) and `store.types` — never an adapter module directly (contract
§1: "Adapter modules are not importable by tool/application code"). Every
byte this module ever writes to `target` goes through `store.gate.persist()`,
the contract's one write door (§1/§5) — this module never constructs
`ScannedKey`/`ScannedBody` itself (enforced project-wide by
`tests/test_boundary.py`'s AST scan, which walks every `.py` file under the
project root, this one included).

CONTRACT §5 REQUIREMENTS, and how each is met below:

  1. Freeze source read-only for the operation.
     -> `FrozenBackend`, a thin wrapper this function puts around `source`
        for the duration of the migration. Its `write()` unconditionally
        raises `SourceFrozenError` before touching anything. This is an
        in-process guard only: it stops *this migration's own code* (and
        anything else that happens to go through the same wrapper object)
        from mutating the source. It CANNOT stop a separate process/thread
        that holds a reference to the raw `source` backend (or to the same
        underlying storage via a different backend instance) from writing to
        it concurrently. **In a live system, the operator must actually stop
        (or fence off) every writer pointed at the source before calling
        migrate(), and keep them stopped until cutover is complete** — the
        wrapper documents and locally enforces the intent, it does not
        substitute for that operational step.

  2. Scan every source value BEFORE uploading anything.
     -> Implemented as two full, separate passes over the source's key
        manifest: PASS 1 reads and scans every single source value; if any
        value fails the scan, migrate() aborts immediately and PASS 2 (the
        copy loop) never runs at all — so "upload nothing further" is not
        just "stop after the offending key", it is "nothing was ever
        uploaded, period", regardless of where in key order the offending
        key sits. Only after PASS 1 clears every key in full does PASS 2
        write anything to `target`.

  3. Copy create-only (expected_hash=None); skip already-correct keys;
     abort (never overwrite) on a genuine divergence.
     -> Delegated to the backend contract itself: every conforming adapter's
        create-only `write()` already implements exactly this (contract §3:
        "Retried create-only returning EXISTS where stored hash == new
        sha256 -> treat as OK (idempotent replay)"). So `gate.persist(target,
        key, body, expected_hash=None, ...)` naturally returns:
          - OK   -> either freshly created, or already present with the SAME
                    hash (idempotent skip) — this function tells the two
                    apart only for reporting purposes, using the target
                    manifest snapshot taken before any writes.
          - EXISTS(current_hash) -> already present with a DIFFERENT hash —
                    a genuine divergence. migrate() aborts immediately and
                    does NOT overwrite it (create-only never overwrites by
                    construction: there is no code path here, or in any
                    adapter, that turns an EXISTS into a forced update).

  4. Read-back verify by CONTENT HASH for every key.
     -> After each successful `gate.persist(...)` (OK), this function calls
        `target.read(key)` and asserts `version_hash == source_hash`. A
        missing key or a hash mismatch aborts immediately. This applies to
        EVERY key processed in PASS 2, not just newly-written ones — an
        idempotent "already correct" skip is still read back and verified,
        so a target that merely *claims* to already hold the right hash
        (e.g. metadata corruption) is still caught.

  5. Full key manifest / unexpected target keys.
     -> Both `source` and `target` are fully listed (`list("")`, which every
        adapter treats as "everything", since `str.startswith("")` is always
        true) before any copying happens. `target_keys - source_keys` is
        the "unexpected target keys" set. POLICY (explicit, contract-required
        but left to the caller to choose): these are REPORTED, never
        deleted, and never treated as fatal on their own — an extra target
        key does not, by itself, block migration or cutover, since it might
        be legitimate ambient/other data in a shared bucket/table, a
        previous partial migration's leftovers, or unrelated to this
        source's namespace. It is business-command judgment whether to
        delete these later; this module the mechanism never does.

  6. Atomic cutover / rollback honesty.
     -> `migrate()` returns before signaling anything to a caller. A
        `MigrationReport(status="success")` IS the cutover signal: the
        caller flips its config to point at `target` only after seeing this,
        and — per this same contract requirement — only while writers are
        STILL stopped (the freeze from #1 must remain in effect across the
        caller's own config flip; `migrate()` cannot enforce that beyond its
        own return). `MigrationReport.rollback_note` states plainly, on
        every report (success or abort): reverting to `source` is a valid
        rollback ONLY as long as no write has landed on `target` since
        cutover; the instant `target` has taken even one write, `source` is
        stale and getting back to a consistent state requires a REVERSE
        migration (`target` -> `source`), not simply re-pointing at `source`.

  7. Partial-failure recovery / re-runnable.
     -> Every abort happens before any write this run makes is left
        inconsistent: PASS 1 aborts before any write at all; PASS 2 aborts
        on the very key that failed, having verified every key before it.
        Nothing this function does ever deletes or reverts a key it already
        wrote — so target, after ANY abort, holds a strict, verified prefix
        (in manifest order) of the source's keys, each byte-identical to its
        source counterpart, plus whatever pre-existed there before this run.
        That is a safe, resumable state by construction: calling migrate()
        again with the same (unmodified) source and the same target simply
        re-does PASS 1 (cheap, read-only) and then, in PASS 2, gets an
        idempotent OK for every already-correct key (contract §3) and
        proceeds to copy whatever is still missing — with no operator
        intervention beyond "run it again" (see tests/test_migration.py's
        interrupted-cutover test).

JUDGMENT CALLS (see also inline):

  A. `doc_type` for the gate is hardcoded to `"system_state"` for every key,
     regardless of what doc-type the key actually is. `system_state` is in
     `store.gate.STATE_DOC_TYPES` (content-hash CAS allowed, no
     `#knokeep-gen:` header required), so this bypasses the gate's ABA/
     generation-header requirement (contract §6) that would otherwise apply
     to a lease/counter/append-log key being copied verbatim. This is safe
     specifically BECAUSE migration only ever writes `expected_hash=None`
     (create-only): every adapter's generation-monotonicity guard (the "T1
     residual" judgment call repeated in local.py/objectstore.py/postgres.py)
     is checked only on the CAS-UPDATE path, never on create-only — so no
     adapter's generation check is even reached here. Migration is a one-time
     verbatim byte-for-byte copy, not a live write against lease semantics;
     "identical bytes = identical state" (§6's rationale for the STATE
     allowlist) is exactly the invariant migration wants for every key it
     touches, independent of that key's original doc-type.

  B. The function signature is exactly
     `migrate(source, target, *, scanner=gate.secret_scan) -> MigrationReport`
     as specified — no extra parameters were added, even though a `doc_type`
     override or a manifest prefix would be natural knobs for a real tool.
     Keeping the signature exact was prioritized over that convenience;
     `judgment call A` explains why a single hardcoded doc_type is safe here.

  C. "Unexpected target keys" does not abort the migration by itself (see
     requirement #5's POLICY note) — only a per-key HASH DIVERGENCE (a key
     present on BOTH sides with different content) aborts. An operator who
     wants "any foreign object on target" to be fatal can inspect
     `report.unexpected_target_keys` and decide not to cut over; this module
     surfaces the fact rather than making that policy choice unilaterally.

  D. A source key that the manifest listing returned but that then reads back
     as `None` (vanished between `list()` and `read()`) is treated as an
     abort, not a silent skip — under the "source is frozen" invariant this
     should never happen from this migration's own code, so if it does, it
     means the freeze was violated from outside (exactly the live-writer
     scenario `judgment call`/requirement #1 warns about) and failing loudly
     is safer than silently producing an incomplete target.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from store import gate
from store.backend import Lock, StoreBackend
from store.types import (
    Blob,
    EXISTS,
    ERROR,
    OK,
    STALE,
    WriteResult,
)

# Hardcoded doc_type for every migrated key -- see JUDGMENT CALL A above.
_MIGRATION_DOC_TYPE = "system_state"

_ROLLBACK_NOTE_SUCCESS = (
    "Cutover signaled: source was frozen read-only and left fully intact "
    "and unmodified for the entire migration. Reverting to source (pointing "
    "the application back at it) is a SAFE rollback ONLY for as long as "
    "NO write has landed on target since this cutover. The moment target "
    "takes even one write post-cutover, source is stale and a simple "
    "revert is no longer correct -- getting back to a consistent state at "
    "that point requires a REVERSE migration (target -> source), not "
    "re-pointing at source."
)

_ROLLBACK_NOTE_ABORTED = (
    "Migration aborted before cutover: source was frozen read-only and was "
    "never written to by this migration, so it remains fully intact -- "
    "simply continuing to use source is safe with no rollback required. "
    "Target may hold a partial, but internally verified and non-corrupt, "
    "prefix of already-copied keys (see keys_copied/keys_already_present); "
    "re-running migrate() against the same source/target is safe and will "
    "resume from where this attempt stopped (contract §3 idempotent "
    "create-only replay)."
)


class SourceFrozenError(Exception):
    """Raised by `FrozenBackend.write()` -- see module docstring requirement
    #1. Never raised for any other reason."""


class FrozenBackend:
    """A read-only wrapper around a `StoreBackend`, used by `migrate()` to
    freeze `source` for the duration of the operation (contract §5,
    requirement #1). Delegates every method except `write()`, which always
    raises `SourceFrozenError` without touching the wrapped backend at all.

    This is deliberately NOT a subclass of the wrapped backend's own class --
    it is a plain composition wrapper implementing the same
    `store.backend.StoreBackend` protocol shape, so it works uniformly across
    every adapter (fake/local/git/objectstore/postgres) without needing any
    adapter-specific knowledge.
    """

    __slots__ = ("_backend",)

    def __init__(self, backend: StoreBackend) -> None:
        self._backend = backend

    @property
    def frozen(self) -> bool:
        """Always True for this wrapper -- exposed for callers/tests that
        want an explicit flag rather than inferring freeze from write()
        raising."""
        return True

    def read(self, key: str) -> Optional[Blob]:
        return self._backend.read(key)

    def list(self, prefix: str):
        return self._backend.list(prefix)

    def write(self, *args, **kwargs) -> WriteResult:
        raise SourceFrozenError(
            "migrate(): source is frozen read-only for the duration of the "
            "migration -- write() is refused. This wrapper only prevents "
            "code that goes through it (this migration) from mutating the "
            "source; in a live system the caller must independently stop "
            "any writer pointed directly at the source's underlying storage "
            "before calling migrate(), and keep it stopped through cutover."
        )

    def lock(self, key: str, ttl_s: float) -> Lock:
        return self._backend.lock(key, ttl_s)

    def unlock(self, lock: Lock) -> bool:
        return self._backend.unlock(lock)

    def renew(self, lock: Lock, ttl_s: float) -> bool:
        return self._backend.renew(lock, ttl_s)

    def capabilities(self):
        return self._backend.capabilities()

    def health(self):
        return self._backend.health()


@dataclass(frozen=True)
class MigrationReport:
    """The result of one `migrate()` call. Never partially trustworthy: a
    `status == "success"` report means every source key was scanned clean,
    copied-or-already-correct, AND read-back verified on target; a
    `status == "aborted"` report means nothing beyond what `keys_copied` +
    `keys_already_present` describes was ever written, and `source` was
    never touched (it was frozen).

    NOTE on `keys_copied`/`keys_already_present` vs `keys_verified`:
    the former two count keys for which `gate.persist()` itself returned
    `OK` (the write genuinely landed, or was already correct) -- they can be
    momentarily one AHEAD of `keys_verified` on an aborted report whose abort
    was specifically a read-back mismatch/failure for that same key (see
    tests/test_migration.py::test_read_back_hash_mismatch_aborts): the write
    landed, but this function could not confirm it reads back correctly, so
    it aborted rather than counting it verified. `status == "success"`
    always implies `keys_verified == keys_copied + keys_already_present`."""

    status: str  # "success" | "aborted"
    reason: Optional[str]
    source_key_count: int
    keys_scanned: int
    keys_copied: int
    keys_already_present: int
    keys_verified: int
    aborted_key: Optional[str]
    secret_scan_labels: Tuple[str, ...]
    unexpected_target_keys: Tuple[str, ...]
    cutover_signaled: bool
    rollback_note: str
    started_at: float
    finished_at: float

    @property
    def ok(self) -> bool:
        return self.status == "success"


def _abort(
    *,
    reason: str,
    source_key_count: int,
    keys_scanned: int,
    keys_copied: int,
    keys_already_present: int,
    keys_verified: int,
    aborted_key: Optional[str],
    secret_scan_labels: Tuple[str, ...] = (),
    unexpected_target_keys: Tuple[str, ...] = (),
    started_at: float,
) -> MigrationReport:
    return MigrationReport(
        status="aborted",
        reason=reason,
        source_key_count=source_key_count,
        keys_scanned=keys_scanned,
        keys_copied=keys_copied,
        keys_already_present=keys_already_present,
        keys_verified=keys_verified,
        aborted_key=aborted_key,
        secret_scan_labels=secret_scan_labels,
        unexpected_target_keys=unexpected_target_keys,
        cutover_signaled=False,
        rollback_note=_ROLLBACK_NOTE_ABORTED,
        started_at=started_at,
        finished_at=time.time(),
    )


def migrate(
    source: StoreBackend,
    target: StoreBackend,
    *,
    scanner: Callable[[bytes], Sequence[str]] = gate.secret_scan,
) -> MigrationReport:
    """Move every key from `source` to `target`, safely. See module
    docstring for the full contract-§5 mapping. Never mutates `source`
    (frozen for the duration); never overwrites a diverging key on `target`;
    never deletes anything on `target`.
    """
    started_at = time.time()
    frozen_source = FrozenBackend(source)

    # -- full manifest of both sides, before any copying -------------------
    try:
        source_keys: List[str] = sorted(frozen_source.list(""))
    except Exception as exc:  # noqa: BLE001 - fail closed, report, never raise
        return _abort(
            reason=f"failed to list source keys: {exc!r}",
            source_key_count=0,
            keys_scanned=0,
            keys_copied=0,
            keys_already_present=0,
            keys_verified=0,
            aborted_key=None,
            started_at=started_at,
        )

    try:
        target_keys_before = set(target.list(""))
    except Exception as exc:  # noqa: BLE001
        return _abort(
            reason=f"failed to list target keys: {exc!r}",
            source_key_count=len(source_keys),
            keys_scanned=0,
            keys_copied=0,
            keys_already_present=0,
            keys_verified=0,
            aborted_key=None,
            started_at=started_at,
        )

    unexpected_target_keys = tuple(sorted(target_keys_before - set(source_keys)))

    # -- PASS 1: scan EVERY source value before uploading anything ----------
    # (requirement #2). Nothing is written to `target` in this loop.
    source_blobs = {}
    keys_scanned = 0
    for key in source_keys:
        try:
            blob = frozen_source.read(key)
        except Exception as exc:  # noqa: BLE001
            return _abort(
                reason=f"failed reading source key {key!r} during pre-scan: {exc!r}",
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=0,
                keys_already_present=0,
                keys_verified=0,
                aborted_key=key,
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )
        if blob is None:
            # JUDGMENT CALL D: listed but unreadable -- the freeze was
            # violated from outside, or the backend is inconsistent. Fail
            # closed rather than silently produce an incomplete target.
            return _abort(
                reason=f"source key {key!r} was listed but read() returned None "
                "(vanished under a frozen source -- freeze may have been "
                "violated by an out-of-band writer)",
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=0,
                keys_already_present=0,
                keys_verified=0,
                aborted_key=key,
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )

        try:
            labels = tuple(scanner(blob.body))
        except Exception as exc:  # noqa: BLE001 - a scanner failure is itself
            # a defense-in-depth failure: fail closed, never upload.
            return _abort(
                reason=f"secret scanner raised on source key {key!r}: {exc!r}",
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=0,
                keys_already_present=0,
                keys_verified=0,
                aborted_key=key,
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )
        if labels:
            return _abort(
                reason=f"secret scan hit on source key {key!r} -- aborting before "
                "any upload (nothing was written to target)",
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=0,
                keys_already_present=0,
                keys_verified=0,
                aborted_key=key,
                secret_scan_labels=labels,
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )

        source_blobs[key] = blob
        keys_scanned += 1

    # -- PASS 2: copy create-only, read-back verify each ---------------------
    keys_copied = 0
    keys_already_present = 0
    keys_verified = 0
    for key in source_keys:
        blob = source_blobs[key]

        try:
            result: WriteResult = gate.persist(
                target, key, blob.body, expected_hash=None, doc_type=_MIGRATION_DOC_TYPE
            )
        except Exception as exc:  # noqa: BLE001 - e.g. a fault-injected
            # ConnectionError from a backend simulating a pre-send network
            # failure. Definitely-not-committed for THIS key; abort here,
            # leaving every prior key's copy intact and verified.
            return _abort(
                reason=f"target write raised for key {key!r}: {exc!r}",
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=keys_copied,
                keys_already_present=keys_already_present,
                keys_verified=keys_verified,
                aborted_key=key,
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )

        if isinstance(result, OK):
            if key in target_keys_before:
                keys_already_present += 1
            else:
                keys_copied += 1
        elif isinstance(result, EXISTS):
            # Create-only against a key already present with a DIFFERENT
            # hash -- a genuine divergence (requirement #3). Abort; never
            # overwrite.
            return _abort(
                reason=(
                    f"target key {key!r} diverges from source: target holds "
                    f"hash {result.current_hash!r}, source holds "
                    f"{blob.version_hash!r} -- aborting without overwriting"
                ),
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=keys_copied,
                keys_already_present=keys_already_present,
                keys_verified=keys_verified,
                aborted_key=key,
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )
        elif isinstance(result, STALE):
            # Not reachable for a create-only write (expected_hash=None) per
            # contract §3, but handled explicitly rather than falling through
            # to a generic branch, in case an adapter ever misbehaves.
            return _abort(
                reason=f"unexpected STALE from a create-only write for key {key!r}: {result!r}",
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=keys_copied,
                keys_already_present=keys_already_present,
                keys_verified=keys_verified,
                aborted_key=key,
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )
        else:
            assert isinstance(result, ERROR)
            return _abort(
                reason=f"target write for key {key!r} failed: {result!r}",
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=keys_copied,
                keys_already_present=keys_already_present,
                keys_verified=keys_verified,
                aborted_key=key,
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )

        # Read-back verify by CONTENT HASH (requirement #4) -- for every key,
        # whether freshly copied or already-correct.
        try:
            verify_blob = target.read(key)
        except Exception as exc:  # noqa: BLE001
            return _abort(
                reason=f"read-back verify raised for key {key!r}: {exc!r}",
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=keys_copied,
                keys_already_present=keys_already_present,
                keys_verified=keys_verified,
                aborted_key=key,
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )
        if verify_blob is None or verify_blob.version_hash != blob.version_hash:
            return _abort(
                reason=(
                    f"read-back verify MISMATCH for key {key!r}: expected hash "
                    f"{blob.version_hash!r}, target now reads "
                    f"{(verify_blob.version_hash if verify_blob else None)!r}"
                ),
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=keys_copied,
                keys_already_present=keys_already_present,
                keys_verified=keys_verified,
                aborted_key=key,
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )
        keys_verified += 1

    # -- every key scanned, copied-or-already-correct, and verified ---------
    # This return value itself IS the cutover signal (requirement #6): the
    # caller flips its config to target only after seeing status=="success",
    # and only while writers remain stopped through that flip.
    return MigrationReport(
        status="success",
        reason=None,
        source_key_count=len(source_keys),
        keys_scanned=keys_scanned,
        keys_copied=keys_copied,
        keys_already_present=keys_already_present,
        keys_verified=keys_verified,
        aborted_key=None,
        secret_scan_labels=(),
        unexpected_target_keys=unexpected_target_keys,
        cutover_signaled=True,
        rollback_note=_ROLLBACK_NOTE_SUCCESS,
        started_at=started_at,
        finished_at=time.time(),
    )


__all__ = [
    "migrate",
    "MigrationReport",
    "FrozenBackend",
    "SourceFrozenError",
]
