"""Shared OperationContext test helpers (H1 Increment 2, Phase 0).

Re-exports create_ctx/overwrite_ctx from store.context, and adds
fenced_ctx(), the one shared way conformance/test call sites build a
CAS-update context: it acquires a REAL advisory lease via backend.lock(),
then releases it immediately. Phase 0 has no fence enforcement (the lease's
fence value is never checked, and no backend write() path touches the
advisory-lock bookkeeping at all), and holding the lease across the rest of
the test would make a second CAS-update to the same key within the same
test spuriously BUSY on the advisory lock -- a different, already-enforced
mechanism unrelated to the CAS content-hash check under test.

JUDGMENT CALL: several conformance tests race many threads' gate.persist()
calls against the SAME key with a shared threading.Barrier (e.g.
test_c1_stale_write_reject_race). Since building a CAS ctx now means every
racing thread also calls backend.lock()/unlock() on that same key right
before its persist() call, two threads' lock()+unlock() windows can
genuinely interleave and raise BackendBusyError from lock() itself --
unrelated to the CAS race the test is actually exercising. fenced_ctx
absorbs that contention with a short bounded retry (never indefinite,
consistent with the contract's own "lock() never blocks indefinitely" rule)
rather than letting an unrelated advisory-lock collision fail a CAS test.
"""
from __future__ import annotations

import time
from typing import Optional

from store.backend import BackendBusyError, StoreBackend
from store.context import (
    AuthContext,
    CreateOnly,
    OperationContext,
    Overwrite,
    create_ctx,
    overwrite_ctx,
)


def fenced_ctx(
    backend: StoreBackend,
    key: str,
    expected_hash: str,
    *,
    ttl_s: float = 30.0,
    auth: Optional[AuthContext] = None,
    _retry_budget_s: float = 5.0,
) -> OperationContext:
    """Build a CAS-update OperationContext for `expected_hash`, carrying a
    real lease acquired (and immediately released) from `backend`. Retries
    a bounded amount on BackendBusyError (see JUDGMENT CALL above) rather
    than propagating an advisory-lock collision unrelated to the CAS check
    under test."""
    deadline = time.monotonic() + _retry_budget_s
    while True:
        try:
            lease = backend.lock(key, ttl_s=ttl_s)
        except BackendBusyError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.001)
            continue
        backend.unlock(lease)
        return overwrite_ctx(expected_hash, lease, auth=auth)


__all__ = [
    "create_ctx",
    "overwrite_ctx",
    "fenced_ctx",
    "AuthContext",
    "CreateOnly",
    "Overwrite",
    "OperationContext",
]
