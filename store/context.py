"""OperationContext — the required write-context contract (H1 Increment 2,
Phase 0: contract foundation only).

Every write through the gate (store.gate.persist) now carries a required
OperationContext instead of a bare `expected_hash: Optional[str]`. This
module defines the context shape only; NO fence enforcement happens yet
(that is a later phase) — backends accept `ctx` and derive the same
`expected_hash` they used to receive directly, so behavior is unchanged.

Stdlib only. `Lock` is imported from `.backend` (never the reverse: `.backend`
only references `OperationContext` under `typing.TYPE_CHECKING`, so there is
no runtime import cycle between this module and `.backend`).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional, Union

from .backend import Lock


@dataclass(frozen=True)
class AuthContext:
    """Who is performing the write. `mode` defaults to "shared" (today's
    single-tenant, single-writer-class behavior); `principal`/`tenant_id`/
    `user_id` are optional identity fields for later phases."""

    mode: str = "shared"
    principal: Optional[str] = None
    tenant_id: Optional[str] = None
    user_id: Optional[str] = None


@dataclass(frozen=True)
class CreateOnly:
    """Marker precondition for a create-only write. No fields: a create
    physically cannot clobber an existing value, so there is nothing to
    fence against."""


@dataclass(frozen=True)
class Overwrite:
    """Precondition for a CAS-update: the expected current content hash,
    plus the caller's lease (advisory `Lock`). Phase 0 does NOT check the
    lease's fence against anything — it is carried structurally now so a
    later phase can enforce it without another signature change."""

    expected_hash: str
    lease: Lock


@dataclass(frozen=True)
class OperationContext:
    """The required write context. `operation_id` defaults to a fresh
    uuid4 hex per call, useful for tracing/telemetry in later phases."""

    auth: AuthContext
    precondition: Union[CreateOnly, Overwrite]
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)


def create_ctx(auth: Optional[AuthContext] = None) -> OperationContext:
    """Build a create-only OperationContext."""
    return OperationContext(auth=auth or AuthContext(), precondition=CreateOnly())


def overwrite_ctx(
    expected_hash: str, lease: Lock, auth: Optional[AuthContext] = None
) -> OperationContext:
    """Build a CAS-update OperationContext against `expected_hash`, carrying
    `lease` (the caller's advisory Lock) structurally for later fence
    enforcement."""
    return OperationContext(
        auth=auth or AuthContext(),
        precondition=Overwrite(expected_hash=expected_hash, lease=lease),
    )


__all__ = [
    "AuthContext",
    "CreateOnly",
    "Overwrite",
    "OperationContext",
    "create_ctx",
    "overwrite_ctx",
]
