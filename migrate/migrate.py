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
        manifest: PASS 1 scans every single source KEY NAME *and* reads and
        scans every single source VALUE; if either fails the scan, migrate()
        aborts immediately and PASS 2 (the copy loop) never runs at all —
        so "upload nothing further" is not just "stop after the offending
        key", it is "nothing was ever uploaded, period", regardless of where
        in key order the offending key sits. Only after PASS 1 clears every
        key in full does PASS 2 write anything to `target`. Scanning the key
        NAME here (not just relying on `gate.persist()`'s own key+body scan
        in PASS 2) matters because a secret-shaped key later in the manifest
        would otherwise let earlier keys be uploaded first (review finding).
        When the offending scan hit is on a KEY NAME (the key itself is what
        the scanner flagged, not a value stored under it), the abort report
        never carries that key's raw text — only a non-reversible reference
        (`_key_ref`) — since the key IS the secret in that case.

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

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from store import gate
from store.backend import BackendBusyError, Lock, StoreBackend
from store.context import create_ctx, overwrite_ctx
from store.types import (
    Blob,
    EXISTS,
    ERROR,
    ErrorKind,
    OK,
    STALE,
    WriteResult,
)

# Hardcoded doc_type for every migrated key -- see JUDGMENT CALL A above.
_MIGRATION_DOC_TYPE = "system_state"

# H1 Increment 2, checklist #18 / D-008 (frontier-gated 2026-09-24, panel 4/4):
# migration takes a SESSION-SCOPED LEASE at startup. The PRIMARY mutex is a
# DURABLE control doc on the target at a reserved control key, written through
# the one write door: created create-only (the backend's native precondition
# is the linearization point, so two processes cannot both create it), and
# taken over from an expired/released holder only via a fenced CAS-update
# carrying a freshly acquired advisory lease on the same key (so two takeover
# attempts cannot both succeed either). The advisory lock() is the SECONDARY,
# in-process guard: on git/object-store/postgres its registry is per process,
# which is exactly why it cannot be the mutex on its own.
#
# It is a MUTEX BETWEEN MIGRATIONS only; it grants NO authority to overwrite
# any DATA key (data writes stay CreateOnly + abort-on-divergence -- #18's
# literal "Overwrite when target exists" was overruled by the gate as it would
# discard a diverging target = break the never-lose promise). The control
# doc's `expires_at` (TTL, renewed by an ELAPSED-TIME heartbeat during both
# passes) is the backstop: a crashed migration's lease expires and a later run
# takes it over (stale-owner takeover), recording the prior owner for audit.
_MIGRATION_LEASE_KEY = "knokeep-migration-control/lease"
_MIGRATION_LEASE_DOC_KIND = "knokeep-migration-lease"
_MIGRATION_LEASE_TTL_S = 120.0            # > worst-case per-key copy latency; renewed via heartbeat
# Heartbeat by ELAPSED TIME (not by key count): the lease is renewed whenever
# this much wall-clock has passed since the last renewal, checked before every
# per-key step of both passes, so it cannot lapse mid-run however slow the
# source/target are. TTL/3 leaves two further chances before expiry.
_MIGRATION_HEARTBEAT_INTERVAL_S = _MIGRATION_LEASE_TTL_S / 3.0
# In STATE_DOC_TYPES, so the control doc needs no #knokeep-gen header (gate §6).
_MIGRATION_LEASE_DOC_TYPE = "system_state"

# Clock seams (module attributes so tests can drive time deterministically).
_monotonic = time.monotonic
_wall = time.time


# The control doc's complete schema. A value under the control key is
# recognized as this tool's bookkeeping ONLY if it carries exactly these
# fields (every required one, no unknown ones) with these types -- a
# `kind` tag alone is caller-controlled text and would let an ordinary JSON
# document that happens to contain it be overwritten (target) or omitted
# (source). Docs written by earlier versions of this module carry the
# required fields and a subset of the optional ones, so they still parse.
_NUM = (int, float)
_LEASE_DOC_REQUIRED = {
    "kind": (str,), "owner_id": (str,), "write_id": (str,), "started_at": _NUM, "status": (str,),
}
_LEASE_DOC_OPTIONAL = {
    "expires_at": _NUM + (type(None),), "heartbeat_at": _NUM + (type(None),),
    "prior_owner": (str, type(None)), "took_over_from": (str, type(None)),
    "released_at": _NUM + (type(None),),
}
_LEASE_DOC_STATUSES = ("held", "released")


def _parse_lease_doc(blob: Optional[Blob]) -> Optional[dict]:
    """The control doc as a dict if `blob` IS a migration-lease control doc
    (this tool's own bookkeeping, validated against the complete schema
    above), else None -- meaning the control key holds a REAL data value
    that must never be repurposed or silently dropped."""
    if blob is None:
        return None
    try:
        doc = json.loads(blob.body.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(doc, dict) or doc.get("kind") != _MIGRATION_LEASE_DOC_KIND:
        return None
    keys = set(doc)
    if not set(_LEASE_DOC_REQUIRED) <= keys or not keys <= set(_LEASE_DOC_REQUIRED) | set(_LEASE_DOC_OPTIONAL):
        return None
    for field, types in {**_LEASE_DOC_REQUIRED, **_LEASE_DOC_OPTIONAL}.items():
        if field in doc:
            value = doc[field]
            if isinstance(value, bool) or not isinstance(value, types):
                return None
    if doc["status"] not in _LEASE_DOC_STATUSES:
        return None
    return doc


def _lease_doc_body(*, owner_id, write_id, started_at, status, expires_at,
                    heartbeat_at, prior_owner, took_over_from, released_at=None) -> bytes:
    return json.dumps(
        {
            "kind": _MIGRATION_LEASE_DOC_KIND,
            "owner_id": owner_id,
            "write_id": write_id,
            "started_at": started_at,
            "status": status,                 # "held" | "released"
            "expires_at": expires_at,         # wall-clock TTL; a "held" doc past this is a crashed owner
            "heartbeat_at": heartbeat_at,
            "prior_owner": prior_owner,       # previous holder, whatever how it ended (audit)
            "took_over_from": took_over_from, # set ONLY on a stale-owner takeover (prior doc still "held")
            "released_at": released_at,
        },
        sort_keys=True,
    ).encode("utf-8")


class _MigrationLease:
    """The held session lease: the durable control doc (tracked by its
    current hash for fenced CAS-updates) plus the advisory lock on the same
    key. `heartbeat_if_due()` renews by elapsed time; `release()` marks the
    doc released before dropping the advisory lock."""

    __slots__ = ("target", "lock", "doc_hash", "owner_id", "write_id", "started_at",
                 "prior_owner", "took_over_from", "last_heartbeat")

    def __init__(self, target, lock, doc_hash, owner_id, write_id, started_at,
                 prior_owner, took_over_from):
        self.target = target
        self.lock = lock
        self.doc_hash = doc_hash
        self.owner_id = owner_id
        self.write_id = write_id
        self.started_at = started_at
        self.prior_owner = prior_owner
        self.took_over_from = took_over_from
        self.last_heartbeat = _monotonic()

    # How many times a fenced control-doc write may re-acquire the fence when
    # it was refused on FENCE while the doc itself is provably still ours.
    _FENCE_REACQUIRE_ATTEMPTS = 3

    def _write_doc(self, *, status, released_at=None) -> Optional[str]:
        """Fenced CAS-update of the control doc against the hash WE last
        wrote, under the advisory lease we hold. Returns None on success,
        else the reason the lease must be considered lost.

        A refusal on FENCE does not by itself mean the lease was lost: on
        backends whose advisory registry is per process (git/object-store/
        postgres) a contender that read the control key as absent/expired
        at the same time we did calls lock() and thereby advances the
        durable fence past ours, even though it then loses the doc itself
        (its create-only gets EXISTS / its takeover CAS gets STALE) and
        never writes again. So: if the doc's current hash is still the one
        WE last wrote, nobody took the lease -- re-acquire the fence (a
        fresh lock() supersedes the loser's) and retry, bounded. A doc hash
        we did not write means another run took over -> lost."""
        for _attempt in range(self._FENCE_REACQUIRE_ATTEMPTS + 1):
            now = _wall()
            body = _lease_doc_body(
                owner_id=self.owner_id, write_id=self.write_id, started_at=self.started_at,
                status=status, expires_at=now + _MIGRATION_LEASE_TTL_S, heartbeat_at=now,
                prior_owner=self.prior_owner, took_over_from=self.took_over_from,
                released_at=released_at,
            )
            try:
                res = gate.persist(self.target, _MIGRATION_LEASE_KEY, body,
                                   ctx=overwrite_ctx(self.doc_hash, self.lock),
                                   doc_type=_MIGRATION_LEASE_DOC_TYPE)
            except Exception as exc:  # noqa: BLE001 - fail closed: treat as lost
                return f"control doc write raised: {exc!r}"
            if isinstance(res, OK):
                self.doc_hash = res.new_hash
                return None
            if not (isinstance(res, STALE) and res.reason == "FENCE"):
                if isinstance(res, STALE):
                    return ("control doc moved underneath this run (another migration took "
                            f"the lease; got {res!r})")
                return f"control doc write failed ({res!r})"
            # FENCE: is the doc still ours?
            try:
                current = self.target.read(_MIGRATION_LEASE_KEY)
            except Exception as exc:  # noqa: BLE001
                return f"control doc re-read raised after a fence refusal: {exc!r}"
            if current is None or current.version_hash != self.doc_hash:
                return ("control doc moved underneath this run (another migration took "
                        f"the lease; got {res!r})")
            if _attempt == self._FENCE_REACQUIRE_ATTEMPTS:
                break
            # Ours, but a losing contender holds the durable fence: re-acquire.
            try:
                self.target.unlock(self.lock)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.lock = self.target.lock(_MIGRATION_LEASE_KEY, _MIGRATION_LEASE_TTL_S)
            except Exception as exc:  # noqa: BLE001 - BUSY or backend failure: lost
                return f"could not re-acquire the migration fence after a fence refusal: {exc!r}"
        return ("could not re-acquire the migration fence after "
                f"{self._FENCE_REACQUIRE_ATTEMPTS} attempts")

    def heartbeat_if_due(self) -> Optional[str]:
        """Renew when `_MIGRATION_HEARTBEAT_INTERVAL_S` has elapsed since the
        last renewal: the advisory lock (in-process guard + durable fence
        expiry) AND the control doc's expires_at. Returns None if nothing
        was due or the renewal succeeded, else why ownership was lost."""
        if _monotonic() - self.last_heartbeat < _MIGRATION_HEARTBEAT_INTERVAL_S:
            return None
        try:
            self.target.renew(self.lock, _MIGRATION_LEASE_TTL_S)
        except Exception as exc:  # noqa: BLE001
            return f"heartbeat renew raised: {exc!r}"
        # A False renew means the durable fence owner is no longer this lease
        # (superseded by a contender's lock(), or lapsed). That alone does
        # not decide ownership -- the CONTROL DOC does: the fenced write below
        # is refused on FENCE, and _write_doc then either re-acquires the
        # fence (doc hash still ours: nobody took the lease) or reports the
        # loss (doc rewritten by another run). Either way no data key is
        # written under a lease whose ownership was not just re-proven.
        err = self._write_doc(status="held")
        if err is not None:
            return err
        self.last_heartbeat = _monotonic()
        return None

    def release(self) -> None:
        """Best-effort: mark the control doc released (so a later run records
        a clean handoff, not a takeover), then drop the advisory lock. The TTL
        is the backstop if either step fails (#18 item 9)."""
        try:
            self._write_doc(status="released", released_at=_wall())
        except Exception:  # noqa: BLE001
            pass
        try:
            self.target.unlock(self.lock)
        except Exception:  # noqa: BLE001
            pass


def _acquire_migration_lease(target, owner_id, write_id, started_at):
    """Acquire the session-scoped migration mutex (D-008). Returns
    (lease, error): on success (_MigrationLease, None); otherwise
    (None, <reason>) and nothing on the target was changed.

    Order matters for cross-process safety on backends whose advisory lock
    is per process (git/object-store/postgres):
      1. READ the durable control doc first. A "held" doc whose expires_at is
         in the future is another LIVE migration -> refuse WITHOUT touching
         the key (so this probe never advances the holder's fence).
         A value under the control key that is NOT a lease doc is real data
         -> refuse (never repurpose it as lease metadata).
      2. Then take the advisory lock (BUSY -> refuse).
      3. Then CREATE the doc create-only (absent), or TAKE IT OVER with a
         fenced CAS-update against the hash read in step 1 (expired/
         released). The backend's native precondition/fence makes exactly one
         of any concurrent contenders win; the rest see EXISTS/STALE and
         refuse. An expired-but-still-"held" prior doc is a crashed owner:
         its owner_id is recorded in `took_over_from` for audit."""
    try:
        prior_blob = target.read(_MIGRATION_LEASE_KEY)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not read the migration control doc: {exc!r}"
    prior_owner = None
    took_over_from = None
    if prior_blob is not None:
        prior_doc = _parse_lease_doc(prior_blob)
        if prior_doc is None:
            return None, (f"target key {_MIGRATION_LEASE_KEY!r} holds real data, not a migration "
                          "control doc -- refusing to repurpose it as the migration lease")
        prior_owner = prior_doc.get("owner_id")
        try:
            prior_expires_at = float(prior_doc.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            prior_expires_at = 0.0
        if prior_doc.get("status") == "held":
            if prior_expires_at > _wall():
                return None, ("another migration holds a live lease on "
                              f"{_MIGRATION_LEASE_KEY!r} (owner {prior_owner!r}, expires in "
                              f"{prior_expires_at - _wall():.0f}s) -- refusing to run two "
                              "migrations at once")
            took_over_from = prior_owner  # crashed/lapsed owner: stale-owner takeover

    try:
        lock = target.lock(_MIGRATION_LEASE_KEY, _MIGRATION_LEASE_TTL_S)
    except BackendBusyError:
        return None, ("another migration holds a live lease on "
                      f"{_MIGRATION_LEASE_KEY!r} -- refusing to run two migrations at once")
    except Exception as exc:  # noqa: BLE001 - lock() does I/O on git/object-store/postgres:
        # fail closed with a report, never raise out of migrate().
        return None, f"could not acquire the migration lease (lock() raised): {exc!r}"

    now = _wall()
    body = _lease_doc_body(
        owner_id=owner_id, write_id=write_id, started_at=started_at, status="held",
        expires_at=now + _MIGRATION_LEASE_TTL_S, heartbeat_at=now,
        prior_owner=prior_owner, took_over_from=took_over_from,
    )
    try:
        if prior_blob is None:
            res = gate.persist(target, _MIGRATION_LEASE_KEY, body,
                               ctx=create_ctx(), doc_type=_MIGRATION_LEASE_DOC_TYPE)
        else:
            res = gate.persist(target, _MIGRATION_LEASE_KEY, body,
                               ctx=overwrite_ctx(prior_blob.version_hash, lock),
                               doc_type=_MIGRATION_LEASE_DOC_TYPE)
    except Exception as exc:  # noqa: BLE001
        res = exc
    if not isinstance(res, OK):
        try:
            target.unlock(lock)
        except Exception:  # noqa: BLE001
            pass
        if isinstance(res, (EXISTS, STALE)):
            return None, ("another migration took the lease on "
                          f"{_MIGRATION_LEASE_KEY!r} first (control doc {type(res).__name__})")
        return None, f"could not record the migration lease control doc (got {res!r})"
    lease = _MigrationLease(target, lock, res.new_hash, owner_id, write_id, started_at,
                            prior_owner, took_over_from)
    if prior_blob is None:
        # The create-only write above carries no fence, so it cannot tell
        # whether a simultaneous contender's lock() advanced the durable
        # fence past ours before losing the create (EXISTS). Confirm
        # ownership NOW with a fenced write (re-acquiring the fence if that
        # is what happened) rather than at the first heartbeat, 40s in.
        err = lease._write_doc(status="held")
        if err is not None:
            lease.release()
            return None, f"could not confirm the migration lease after creating it: {err}"
    return lease, None


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


def _key_ref(key: str) -> str:
    """A non-reversible reference to a KEY NAME, for `reason`/`aborted_key`
    specifically when the key itself (not a value stored under it) is what
    a secret scanner flagged. Unlike a body-content hit (where the key is
    just an ordinary, non-secret path segment -- safely reportable verbatim,
    as every other abort path in this module does), putting this key's raw
    text in an abort report would disclose the very value that tripped the
    gate (review finding). Never the reverse of a real key-name secret scan
    hit's own labels, which already carry no matched text either."""
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


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


def _looks_like_same_store(source, target, target_keys: set) -> bool:
    """Read-only, adapter-agnostic aliasing hint (tool code may not import
    adapters): same class, same configured identity as each adapter reports
    it in health().detail, and the same key listing. Never raises; a False
    here is not proof of distinctness, which is why migrate() also checks,
    after acquiring the lease, whether its own control doc is visible through
    the source."""
    if type(source) is not type(target):
        return False
    try:
        hs, ht = source.health(), target.health()
        if not (hs.ok and ht.ok and hs.detail and hs.detail == ht.detail):
            return False
        return set(source.list("")) == set(target_keys)
    except Exception:  # noqa: BLE001
        return False


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

    H1 Increment 2 / D-008: acquires a session-scoped migration lease (a
    DURABLE create-only control doc on the target at a reserved control key,
    plus the advisory lock on that key) BEFORE any work and releases it in a
    finally, so two migrations cannot run at once -- including from separate
    processes against the same git/object-store/postgres target. The lease is
    NEVER authority to overwrite a data key. If another LIVE migration holds
    the lease, or the control key holds real data, this returns an aborted
    report without touching source or target.
    """
    started_at = time.time()
    owner_id = uuid.uuid4().hex
    write_id = uuid.uuid4().hex
    if source is target:
        # The lease control doc is written to `target` before the source is
        # frozen, so an aliased pair would durably modify the source while
        # the report promises it was untouched. Refuse before any write.
        return _abort(
            reason="source and target are the same backend object -- refusing to migrate a store onto itself",
            source_key_count=0, keys_scanned=0, keys_copied=0,
            keys_already_present=0, keys_verified=0, aborted_key=None,
            started_at=started_at,
        )
    # Snapshot the target's keys BEFORE acquiring the lease, so bookkeeping this
    # migration itself creates on the target (the lease control doc, and any
    # backend-internal fence sidecar the lock materializes -- e.g. git's
    # .knokeep-fence/) is never mis-reported as a foreign/unexpected target key.
    try:
        target_keys_before = set(target.list(""))
    except Exception as exc:  # noqa: BLE001 - fail closed, report, never raise
        return _abort(
            reason=f"failed to list target keys: {exc!r}",
            source_key_count=0, keys_scanned=0, keys_copied=0,
            keys_already_present=0, keys_verified=0, aborted_key=None,
            started_at=started_at,
        )
    if _looks_like_same_store(source, target, target_keys_before):
        # Read-only detection BEFORE the lease control doc is written: two
        # adapters of the same class reporting the same configured identity
        # (health().detail: root path / remote+ref / endpoint+bucket /
        # schema.table) and listing the same keys. Refusing here keeps the
        # promise that an aborted run left the source untouched.
        return _abort(
            reason=("source and target appear to be the same store (same adapter class, same "
                    "configured identity, same key listing) -- refusing to migrate a store onto itself"),
            source_key_count=0, keys_scanned=0, keys_copied=0,
            keys_already_present=0, keys_verified=0, aborted_key=None,
            started_at=started_at,
        )
    lease, lease_error = _acquire_migration_lease(target, owner_id, write_id, started_at)
    if lease is None:
        return _abort(
            reason=f"migration lease unavailable: {lease_error}",
            source_key_count=0, keys_scanned=0, keys_copied=0,
            keys_already_present=0, keys_verified=0, aborted_key=None,
            started_at=started_at,
        )
    try:
        # Distinct adapter objects over the SAME store (a second instance,
        # a different backend type on one root) cannot be told apart by
        # identity; but our own control doc, just written to `target`, is
        # now visible through `source` if and only if they alias. Fail
        # closed before the source is frozen and before any data write.
        try:
            aliased = _parse_lease_doc(source.read(_MIGRATION_LEASE_KEY))
        except Exception as exc:  # noqa: BLE001 - fail closed: without this read, aliasing
            # cannot be excluded, and a migration onto itself would report success.
            return _abort(
                reason=f"could not read the source's control key to exclude source/target aliasing: {exc!r}",
                source_key_count=0, keys_scanned=0, keys_copied=0,
                keys_already_present=0, keys_verified=0, aborted_key=None,
                started_at=started_at,
            )
        if aliased is not None and aliased.get("owner_id") == owner_id:
            return _abort(
                reason=("source and target alias the same store (this run's migration control "
                        "doc is visible through the source) -- refusing to migrate a store onto itself"),
                source_key_count=0, keys_scanned=0, keys_copied=0,
                keys_already_present=0, keys_verified=0, aborted_key=None,
                started_at=started_at,
            )
        return _migrate_body(source, target, scanner=scanner,
                             started_at=started_at, lease=lease,
                             target_keys_before=target_keys_before)
    finally:
        # Release: mark the control doc released, then drop the advisory lock
        # (ownership-conditional; the TTL is the backstop if this fails or
        # the process crashed before here -- #18 item 9).
        lease.release()


def _migrate_body(
    source: StoreBackend,
    target: StoreBackend,
    *,
    scanner: Callable[[bytes], Sequence[str]] = gate.secret_scan,
    started_at: float,
    lease: _MigrationLease,
    target_keys_before: set,
) -> MigrationReport:
    frozen_source = FrozenBackend(source)

    # -- full manifest of both sides, before any copying -------------------
    try:
        listed_source_keys = sorted(frozen_source.list(""))
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
    # The reserved control key is never migratable data (#18 item 2) -- but
    # only when what sits there IS a migration control doc (bookkeeping left
    # by an earlier migration INTO this store). A real data value under that
    # key would otherwise be silently omitted from the target while the run
    # reports success, so it is a hard abort instead (nothing written yet).
    source_keys: List[str] = [k for k in listed_source_keys if k != _MIGRATION_LEASE_KEY]
    if _MIGRATION_LEASE_KEY in listed_source_keys:
        try:
            control_blob = frozen_source.read(_MIGRATION_LEASE_KEY)
        except Exception as exc:  # noqa: BLE001
            return _abort(
                reason=f"failed reading source key {_MIGRATION_LEASE_KEY!r}: {exc!r}",
                source_key_count=len(source_keys), keys_scanned=0, keys_copied=0,
                keys_already_present=0, keys_verified=0, aborted_key=_MIGRATION_LEASE_KEY,
                started_at=started_at,
            )
        if control_blob is not None and _parse_lease_doc(control_blob) is None:
            return _abort(
                reason=(f"source key {_MIGRATION_LEASE_KEY!r} collides with the migration "
                        "control key and holds real data (not a migration control doc) -- "
                        "refusing rather than silently omitting it; rename that key first"),
                source_key_count=len(source_keys), keys_scanned=0, keys_copied=0,
                keys_already_present=0, keys_verified=0, aborted_key=_MIGRATION_LEASE_KEY,
                started_at=started_at,
            )

    # `target_keys_before` was snapshotted by migrate() BEFORE the lease was
    # acquired, so the lease control doc + any backend fence sidecar THIS run
    # creates are not in it. Still subtract the control key explicitly to cover
    # a re-run where a PRIOR run's control doc is already present on target
    # (#18 item 2).
    unexpected_target_keys = tuple(
        sorted(target_keys_before - set(source_keys) - {_MIGRATION_LEASE_KEY})
    )

    # -- PASS 1: scan EVERY source value before uploading anything ----------
    # (requirement #2). Nothing is written to `target` in this loop.
    source_blobs = {}
    keys_scanned = 0
    for key in source_keys:
        # Heartbeat the session lease by ELAPSED TIME through the pre-scan as
        # well: a slow source listing/read must not let the TTL lapse before
        # the first target write (#18 item 4).
        lost = lease.heartbeat_if_due()
        if lost is not None:
            return _abort(
                reason=f"migration lease lost during pre-scan ({lost}) -- aborting",
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=0,
                keys_already_present=0,
                keys_verified=0,
                aborted_key=key,
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )
        # Scan the KEY NAME itself, not just its body, BEFORE any target
        # write (review finding: gate.persist() also scans the key, but only
        # in PASS 2 -- a secret-shaped key sitting later in the manifest
        # would otherwise let earlier keys be uploaded first). A hit here
        # means the key ITSELF is the secret, so -- unlike every other abort
        # path below, which safely names an ordinary key -- neither `reason`
        # nor `aborted_key` may carry the raw key text; see `_key_ref`.
        try:
            key_labels = tuple(scanner(key.encode("utf-8")))
        except Exception:  # noqa: BLE001 - fail closed, never upload.
            return _abort(
                reason=f"secret scanner raised while scanning a source KEY NAME (ref {_key_ref(key)})",
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=0,
                keys_already_present=0,
                keys_verified=0,
                aborted_key=_key_ref(key),
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )
        if key_labels:
            return _abort(
                reason=(
                    f"secret scan hit on a source KEY NAME (ref {_key_ref(key)}) -- "
                    "aborting before any upload (nothing was written to target)"
                ),
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=0,
                keys_already_present=0,
                keys_verified=0,
                aborted_key=_key_ref(key),
                secret_scan_labels=key_labels,
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )

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
        # Heartbeat the session lease by ELAPSED TIME before every write so a
        # long or slow migration never lets its TTL lapse mid-run; a failed
        # renewal means ownership was lost (a takeover happened) -> fail
        # closed BEFORE this key is written, never keep writing (#18 item 4).
        lost = lease.heartbeat_if_due()
        if lost is not None:
            return _abort(
                reason=f"migration lease lost during copy ({lost}) -- aborting",
                source_key_count=len(source_keys),
                keys_scanned=keys_scanned,
                keys_copied=keys_copied,
                keys_already_present=keys_already_present,
                keys_verified=keys_verified,
                aborted_key=key,
                unexpected_target_keys=unexpected_target_keys,
                started_at=started_at,
            )
        blob = source_blobs[key]

        try:
            result: WriteResult = gate.persist(
                target, key, blob.body, ctx=create_ctx(), doc_type=_MIGRATION_DOC_TYPE
            )
        except Exception as exc:  # noqa: BLE001 - e.g. a fault-injected
            # ConnectionError from a backend simulating a network failure.
            # This is NOT necessarily "definitely-not-committed" -- the
            # underlying adapter may fsync a durability commit before a
            # later step (e.g. publish) fails, so an exception here can mean
            # the write actually landed (review finding). Resolve the TRUE
            # outcome via gate.reconcile() (contract §7's own mechanism for
            # exactly this OUTCOME-UNKNOWN situation) before treating it as a
            # hard failure.
            reconciled = gate.reconcile(
                target, key, intended_new_hash=blob.version_hash, expected_hash=None
            )
            if isinstance(reconciled, OK):
                result = reconciled
            else:
                return _abort(
                    reason=(
                        f"target write raised for key {key!r}: {exc!r} -- "
                        f"gate.reconcile() could not confirm the write landed "
                        f"(resolved as {type(reconciled).__name__}, not OK); "
                        "aborting rather than assuming either outcome"
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

        if isinstance(result, ERROR) and result.kind is ErrorKind.TIMEOUT_AFTER_COMMIT:
            # The adapter itself reports OUTCOME-UNKNOWN (an ack was lost
            # after the write may have already committed, contract §7) --
            # same treatment as the raised-exception case above: resolve via
            # gate.reconcile() before giving up on this key.
            reconciled = gate.reconcile(
                target, key, intended_new_hash=blob.version_hash, expected_hash=None
            )
            if isinstance(reconciled, OK):
                result = reconciled
            else:
                return _abort(
                    reason=(
                        f"target write for key {key!r} returned TIMEOUT_AFTER_COMMIT and "
                        f"gate.reconcile() could not confirm it landed (resolved as "
                        f"{type(reconciled).__name__}, not OK)"
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
