"""FakeBackend — an in-memory, fault-injecting StoreBackend adapter.

Used to drive the backend-agnostic conformance suite without touching any
real storage. Atomic by construction: every read-compare-write critical
section is taken under a single process-wide lock, so concurrent callers see
a linearized sequence of operations (satisfying "a concurrent reader sees the
whole old blob or the whole new blob, never torn" for the in-memory case).
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Dict, Iterator, Optional, Tuple

from . import gate
from .backend import BackendBusyError, Lock
from .context import Overwrite
from .gate import ScannedBody, ScannedKey
from .types import (
    BackendHealth,
    Blob,
    Caps,
    EXISTS,
    ERROR,
    ErrorKind,
    OK,
    STALE,
    WriteResult,
    sha256_hex,
)


class FakeBackend:
    def __init__(self) -> None:
        self._store: Dict[str, Tuple[bytes, str]] = {}
        self._locks: Dict[str, Tuple[str, float]] = {}
        # H1 Increment 2, Phase 1: per-key durable (for the life of this
        # in-memory backend) fence-ownership state, kept SEPARATE from
        # `_locks` above on purpose. `_locks` is the transient advisory
        # mutual-exclusion bookkeeping (cleared by unlock() so a fresh
        # acquire can succeed immediately); `_fence` is the CAS-fencing
        # identity of the most recent lock()-issued lease and is NEVER
        # cleared by unlock() — a caller that acquires a lease and releases
        # it right away (the common `tests/ctx_helpers.fenced_ctx` pattern)
        # must still be able to use that lease to write until it is either
        # superseded by a later lock() on the same key or its TTL elapses.
        # Fields per key: owner_token, owner_expiry, owner_fence,
        # last_accepted_fence (the last two are pure counters, never
        # wall-clock).
        self._fence: Dict[str, Dict[str, object]] = {}
        self._mutex = threading.RLock()
        # Signalled (notify_all) whenever a key's committed state changes, so
        # a fence-losing writer parked in
        # `_settle_current_hash_after_fence_loss` wakes the moment the true
        # owner's write lands instead of polling.
        self._changed = threading.Condition(self._mutex)
        # Fault injection: each is a one-shot counter consumed by the next
        # matching write() call(s).
        self._fault_counts: Dict[str, int] = {
            "network": 0,
            "timeout_after_commit": 0,
            "conflict_unknown": 0,
        }
        self._latency_s: float = 0.0

    # -- fault injection knobs -------------------------------------------------

    def inject_network_failure(self, times: int = 1) -> None:
        """Next `times` write() calls raise ConnectionError before any state
        change (definitely-not-committed; §1: NETWORK failing before the
        request was fully sent)."""
        with self._mutex:
            self._fault_counts["network"] += times

    def inject_timeout_after_commit(self, times: int = 1) -> None:
        """Next `times` write() calls actually commit the write but report
        ERROR{TIMEOUT_AFTER_COMMIT} (outcome-unknown, §1/§7)."""
        with self._mutex:
            self._fault_counts["timeout_after_commit"] += times

    def inject_conflict_unknown(self, times: int = 1) -> None:
        """Next `times` write() calls report ERROR{CONFLICT_UNKNOWN} without
        changing state (outcome-unknown; the caller cannot tell from this
        alone whether a previous attempt landed)."""
        with self._mutex:
            self._fault_counts["conflict_unknown"] += times

    def set_latency(self, seconds: float) -> None:
        self._latency_s = max(0.0, seconds)

    def _consume_fault(self, name: str) -> bool:
        with self._mutex:
            n = self._fault_counts.get(name, 0)
            if n > 0:
                self._fault_counts[name] = n - 1
                return True
            return False

    # -- StoreBackend protocol ---------------------------------------------

    def capabilities(self) -> Caps:
        return Caps(atomic=True, cas=True, lock=True, durable=False, remote=False, fence=True)

    def health(self) -> BackendHealth:
        return BackendHealth(ok=True, detail="in-memory fake backend")

    def read(self, key: str) -> Optional[Blob]:
        with self._mutex:
            entry = self._store.get(key)
        if entry is None:
            return None
        body, version_hash = entry
        return Blob(body=body, version_hash=version_hash)

    def list(self, prefix: str) -> Iterator[str]:
        with self._mutex:
            keys = sorted(k for k in self._store if k.startswith(prefix))
        return iter(keys)

    def write(
        self,
        key: ScannedKey,
        body: ScannedBody,
        *,
        ctx,
    ) -> WriteResult:
        # Adapters must only accept gate-issued values; raw bytes/str are a
        # TypeError before any I/O (contract §1/§5).
        if not isinstance(key, ScannedKey) or not isinstance(body, ScannedBody):
            raise TypeError(
                "FakeBackend.write requires ScannedKey/ScannedBody from store.gate.persist()"
            )
        if not gate.verify(key) or not gate.verify(body):
            raise TypeError("FakeBackend.write: gate marker verification failed")

        # H1 Increment 2, Phase 0/1: ctx is required; derive expected_hash the
        # same way store/gate.py does.
        precondition_lease: Optional[Lock] = (
            ctx.precondition.lease if isinstance(ctx.precondition, Overwrite) else None
        )
        expected_hash: Optional[str] = (
            ctx.precondition.expected_hash if isinstance(ctx.precondition, Overwrite) else None
        )

        # Pre-send fault: nothing touched, definitely-not-committed.
        if self._consume_fault("network"):
            raise ConnectionError("FakeBackend: injected network failure (pre-send)")

        if self._latency_s:
            time.sleep(self._latency_s)

        raw = body.body
        new_hash = sha256_hex(raw)
        k = key.key

        with self._mutex:
            current = self._store.get(k)

            if expected_hash is None:
                # Create-only.
                if current is None:
                    def _commit_create() -> None:
                        self._store[k] = (raw, new_hash)
                        # H1 Increment 2, Phase 1 (C): a fresh create starts
                        # the key's fence floor at 0.
                        self._fence.setdefault(k, {})["last_accepted_fence"] = 0
                        self._changed.notify_all()

                    fault = self._apply_injected_fault(_commit_create)
                    if fault is not None:
                        return fault
                    _commit_create()
                    return OK(new_hash)
                _, current_hash = current
                if current_hash == new_hash:
                    # Idempotent create replay (§3).
                    return OK(new_hash)
                return EXISTS(current_hash)

            # CAS-update. Ownership/fence FIRST, then the content-hash CAS
            # (H1 Increment 2, Phase 1 (C)).
            current_hash = current[1] if current is not None else None
            if not self._fence_ok(k, precondition_lease):
                # A fence loss is PERMANENT for this lease, but the racer
                # holding the current fence may not have written yet:
                # settle (bounded) so STALE carries the true winner's hash
                # rather than this caller's own pre-race snapshot.
                settled_hash = self._settle_current_hash_after_fence_loss(k, expected_hash)
                return STALE(settled_hash, reason="FENCE")

            if current is None:
                return STALE(None)
            if current_hash != expected_hash:
                return STALE(current_hash)

            def _commit_update() -> None:
                self._store[k] = (raw, new_hash)
                # Commit last_accepted_fence atomically with the body: both
                # are updated inside this same critical section, under
                # self._mutex, before the write() call returns.
                self._fence.setdefault(k, {})["last_accepted_fence"] = precondition_lease.fence
                self._changed.notify_all()

            fault = self._apply_injected_fault(_commit_update)
            if fault is not None:
                return fault
            _commit_update()
            return OK(new_hash)

    def _apply_injected_fault(self, commit) -> Optional[WriteResult]:
        """Ambiguous-outcome faults are applied at the linearization point --
        AFTER every precondition (create-only collision, fence ownership,
        content-hash CAS) has passed, exactly where a real backend's ack
        would be lost around its commit. A write the backend refuses never
        reaches this point, so an injected fault can never turn a refused
        write into one that lands; the fault then stays armed for the next
        write that does get here. Caller holds self._mutex."""
        if self._consume_fault("timeout_after_commit"):
            # This variant DOES land — exercises the §7 "current ==
            # intended_new_hash" reconciliation branch.
            commit()
            return ERROR(ErrorKind.TIMEOUT_AFTER_COMMIT)
        if self._consume_fault("conflict_unknown"):
            # This variant does NOT land — exercises the §7 "otherwise"
            # branch, which must never be reported as STALE.
            return ERROR(ErrorKind.CONFLICT_UNKNOWN)
        return None

    # Upper bound on how long a fence-losing write() waits for the current
    # fence owner's pending write to land before reporting STALE. Only ever
    # reached when the owner never writes (it lost interest, or is slow
    # beyond this bound); the wait also ends as soon as the owner's lease
    # expires, since no fenced write can land after that.
    _FENCE_LOSS_SETTLE_MAX_S = 5.0

    def _settle_current_hash_after_fence_loss(self, key: str, expected_hash: str) -> Optional[str]:
        """Caller holds self._mutex (released while waiting). Returns the
        key's current hash once the race that superseded this caller's fence
        has SETTLED: either the hash moved away from `expected_hash` (the
        winner landed), or no fenced write is pending any more (the current
        owner's fence has been consumed by a committed write, or its lease
        expired), or the bounded wait ran out."""
        deadline = time.monotonic() + self._FENCE_LOSS_SETTLE_MAX_S
        while True:
            entry = self._store.get(key)
            current_hash = entry[1] if entry is not None else None
            if current_hash != expected_hash:
                return current_hash
            fs = self._fence.get(key) or {}
            owner_fence = int(fs.get("owner_fence", 0))
            last_accepted = int(fs.get("last_accepted_fence", 0))
            owner_expiry = float(fs.get("owner_expiry", 0.0))
            now = time.time()
            pending = owner_fence > last_accepted and owner_expiry > now
            if not pending:
                return current_hash
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return current_hash
            # Wake on the next committed change, at the owner's expiry, or at
            # the deadline -- whichever comes first.
            self._changed.wait(timeout=min(remaining, owner_expiry - now))

    def _fence_ok(self, key: str, lease: Optional[Lock]) -> bool:
        """H1 Increment 2, Phase 1 (C): the Overwrite lease must be the
        current fence owner, unexpired, and carry a fence at least as high
        as the last one that actually committed. Caller holds self._mutex."""
        if lease is None:
            return False
        fs = self._fence.get(key)
        owner_token = fs.get("owner_token") if fs else None
        owner_expiry = fs.get("owner_expiry", 0.0) if fs else 0.0
        last_accepted = fs.get("last_accepted_fence", 0) if fs else 0
        if lease.token != owner_token:
            return False
        if owner_expiry <= time.time():
            return False
        if lease.fence < last_accepted:
            return False
        return True

    def lock(self, key: str, ttl_s: float) -> Lock:
        with self._mutex:
            now = time.time()
            existing = self._locks.get(key)
            if existing is not None and existing[1] > now:
                raise BackendBusyError(f"key {key!r} is locked")
            token = secrets.token_hex(16)  # 128-bit CSPRNG token
            expiry = now + ttl_s
            self._locks[key] = (token, expiry)
            # H1 Increment 2, Phase 1 (B): every successful acquire (fresh OR
            # a stale-break takeover of an expired prior owner — both land
            # here identically) durably advances the fence past both the
            # previous owner_fence and last_accepted_fence, so two successive
            # acquisitions on the same key always return a strictly
            # increasing fence. This bookkeeping is kept in `_fence`,
            # SEPARATE from `_locks`, and is intentionally NOT cleared by
            # unlock() (see the field's docstring in __init__).
            fs = self._fence.setdefault(key, {"owner_fence": 0, "last_accepted_fence": 0})
            new_fence = max(fs.get("owner_fence", 0), fs.get("last_accepted_fence", 0)) + 1
            fs["owner_token"] = token
            fs["owner_expiry"] = expiry
            fs["owner_fence"] = new_fence
            return Lock(key=key, token=token, expiry_epoch=expiry, fence=new_fence)

    def unlock(self, lock: Lock) -> bool:
        with self._mutex:
            existing = self._locks.get(lock.key)
            if existing is None:
                return False
            token, expiry = existing
            if token != lock.token or expiry <= time.time():
                return False
            del self._locks[lock.key]
            # `_fence` bookkeeping is deliberately left untouched: unlock()
            # only releases the mutual-exclusion (advisory) side so a fresh
            # lock() can succeed immediately; the released lease remains the
            # valid fence owner (for CAS writes) until superseded by a later
            # lock() on this key or until its TTL elapses on its own.
            return True

    def renew(self, lock: Lock, ttl_s: float) -> bool:
        with self._mutex:
            existing = self._locks.get(lock.key)
            if existing is None:
                return False
            token, expiry = existing
            if token != lock.token or expiry <= time.time():
                return False
            new_expiry = time.time() + ttl_s
            self._locks[lock.key] = (token, new_expiry)
            # H1 Increment 2, Phase 1 (B): fence unchanged, but extend the
            # fence-owner's validity window to match the renewed advisory
            # lock, so a renewed lease keeps its CAS-write authorization.
            fs = self._fence.get(lock.key)
            if fs is not None and fs.get("owner_token") == token:
                fs["owner_expiry"] = new_expiry
            return True


__all__ = ["FakeBackend"]
