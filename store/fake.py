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
        self._mutex = threading.RLock()
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
        return Caps(atomic=True, cas=True, lock=True, durable=False, remote=False)

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
        expected_hash: Optional[str],
    ) -> WriteResult:
        # Adapters must only accept gate-issued values; raw bytes/str are a
        # TypeError before any I/O (contract §1/§5).
        if not isinstance(key, ScannedKey) or not isinstance(body, ScannedBody):
            raise TypeError(
                "FakeBackend.write requires ScannedKey/ScannedBody from store.gate.persist()"
            )
        if not gate.verify(key) or not gate.verify(body):
            raise TypeError("FakeBackend.write: gate marker verification failed")

        # Pre-send fault: nothing touched, definitely-not-committed.
        if self._consume_fault("network"):
            raise ConnectionError("FakeBackend: injected network failure (pre-send)")

        if self._latency_s:
            time.sleep(self._latency_s)

        raw = body.body
        new_hash = sha256_hex(raw)
        k = key.key

        with self._mutex:
            # Ambiguous-outcome faults are applied at the linearization point:
            # the write may or may not have actually landed, mirroring a real
            # backend whose ack was lost after (or around) the commit.
            if self._consume_fault("timeout_after_commit"):
                # This variant DOES land — exercises the §7 "current ==
                # intended_new_hash" reconciliation branch.
                self._store[k] = (raw, new_hash)
                return ERROR(ErrorKind.TIMEOUT_AFTER_COMMIT)
            if self._consume_fault("conflict_unknown"):
                # This variant does NOT land — exercises the §7 "otherwise"
                # branch, which must never be reported as STALE.
                return ERROR(ErrorKind.CONFLICT_UNKNOWN)

            current = self._store.get(k)

            if expected_hash is None:
                # Create-only.
                if current is None:
                    self._store[k] = (raw, new_hash)
                    return OK(new_hash)
                _, current_hash = current
                if current_hash == new_hash:
                    # Idempotent create replay (§3).
                    return OK(new_hash)
                return EXISTS(current_hash)

            # CAS-update.
            if current is None:
                return STALE(None)
            _, current_hash = current
            if current_hash != expected_hash:
                return STALE(current_hash)
            self._store[k] = (raw, new_hash)
            return OK(new_hash)

    def lock(self, key: str, ttl_s: float) -> Lock:
        with self._mutex:
            now = time.time()
            existing = self._locks.get(key)
            if existing is not None and existing[1] > now:
                raise BackendBusyError(f"key {key!r} is locked")
            token = secrets.token_hex(16)  # 128-bit CSPRNG token
            expiry = now + ttl_s
            self._locks[key] = (token, expiry)
            return Lock(key=key, token=token, expiry_epoch=expiry)

    def unlock(self, lock: Lock) -> bool:
        with self._mutex:
            existing = self._locks.get(lock.key)
            if existing is None:
                return False
            token, expiry = existing
            if token != lock.token or expiry <= time.time():
                return False
            del self._locks[lock.key]
            return True

    def renew(self, lock: Lock, ttl_s: float) -> bool:
        with self._mutex:
            existing = self._locks.get(lock.key)
            if existing is None:
                return False
            token, expiry = existing
            if token != lock.token or expiry <= time.time():
                return False
            self._locks[lock.key] = (token, time.time() + ttl_s)
            return True


__all__ = ["FakeBackend"]
