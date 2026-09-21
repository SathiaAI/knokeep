"""StoreBackend protocol — the socket every adapter implements (contract §2)."""
from __future__ import annotations

import typing
from dataclasses import dataclass

from .types import BackendHealth, Blob, Caps, WriteResult

if typing.TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from .gate import ScannedBody, ScannedKey


@dataclass(frozen=True)
class Lock:
    key: str
    token: str
    expiry_epoch: float


class BackendBusyError(Exception):
    """Raised by lock() on acquire-timeout / sharing violation (maps to ERROR{BUSY}
    at the caller layer). JUDGMENT CALL: contract §2 types lock() as returning a
    bare `Lock`, with no room in that return type for a failure value, while §3
    says an acquire-timeout "-> ERROR{BUSY}, never block indefinitely" (that
    sentence is written for write()/CAS, but the same non-blocking-timeout
    requirement is stated for lock() too). Since lock()'s declared return type
    has no error slot, failure to acquire is signaled by raising rather than by
    inventing a new return shape not in the contract.
    """


class StoreBackend(typing.Protocol):
    def read(self, key: str) -> typing.Optional[Blob]:
        """None ONLY for NOT_FOUND. PERMISSION/NETWORK/CORRUPTION raise.
        Bytes verbatim; never follows a URL/path/ref or resolves a pointer.
        """
        ...

    def list(self, prefix: str) -> typing.Iterator[str]:
        """Paginated; adapter documents strong vs eventual consistency."""
        ...

    def write(
        self,
        key: "ScannedKey",
        body: "ScannedBody",
        *,
        expected_hash: typing.Optional[str],
    ) -> WriteResult:
        """CAS write. See contract §3. `key`/`body` MUST be gate-issued
        ScannedKey/ScannedBody; adapters verify the marker and raise TypeError
        for anything else before any I/O.
        """
        ...

    def lock(self, key: str, ttl_s: float) -> Lock:
        """ADVISORY; 128-bit CSPRNG token. Raises BackendBusyError if not
        acquirable within a bounded time (never blocks indefinitely)."""
        ...

    def unlock(self, lock: Lock) -> bool:
        """Succeeds ONLY if this token still owns AND TTL not expired."""
        ...

    def renew(self, lock: Lock, ttl_s: float) -> bool:
        """Succeeds ONLY if this token still owns AND TTL not expired."""
        ...

    def capabilities(self) -> Caps: ...

    def health(self) -> BackendHealth: ...


__all__ = ["StoreBackend", "Lock", "BackendBusyError"]
