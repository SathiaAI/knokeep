"""Core types for the StoreBackend contract (contract v1.4, §1).

Stdlib-only. No network. No third-party imports.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Tuple, Union


def sha256_hex(body: bytes) -> str:
    """sha256(body) as lowercase 64-hex, per contract §1/§6."""
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("sha256_hex requires bytes")
    return hashlib.sha256(bytes(body)).hexdigest()


# Pin the empty-body hash (contract §1: "empty-body hash pinned"). This guards
# against an accidental change of hash algorithm/encoding ever silently
# altering what "empty body" hashes to.
EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
assert sha256_hex(b"") == EMPTY_BODY_SHA256, "sha256_hex empty-body pin violated"


@dataclass(frozen=True)
class Blob:
    """A stored value: raw bytes plus its content hash."""

    body: bytes
    version_hash: str


@dataclass(frozen=True)
class Caps:
    """Backend capability flags, per contract §1/§8."""

    atomic: bool
    cas: bool
    lock: bool
    durable: bool
    remote: bool


@dataclass(frozen=True)
class BackendHealth:
    """Result of StoreBackend.health()."""

    ok: bool
    detail: str = ""


class ErrorKind(Enum):
    NOT_FOUND = "NOT_FOUND"
    PERMISSION = "PERMISSION"
    NETWORK = "NETWORK"
    CORRUPTION = "CORRUPTION"
    SCAN_FAILURE = "SCAN_FAILURE"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"  # malformed key/hash/args, rejected before I/O
    BUSY = "BUSY"  # lock-acquire timeout / Windows sharing violation
    TIMEOUT_AFTER_COMMIT = "TIMEOUT_AFTER_COMMIT"  # OUTCOME-UNKNOWN
    CONFLICT_UNKNOWN = "CONFLICT_UNKNOWN"  # OUTCOME-UNKNOWN
    # JUDGMENT CALL: the contract's §1 WriteResult union does not list a
    # secret-block outcome (the gate boundary is described separately, in the
    # prose around persist()). The task instructions explicitly say to add a
    # dedicated ErrorKind for a scan hit (as opposed to a scanner *failure*,
    # which is SCAN_FAILURE). SECRET_BLOCKED is definitely-not-committed: the
    # gate refuses to call backend.write() at all when the scanner finds a hit.
    SECRET_BLOCKED = "SECRET_BLOCKED"


@dataclass(frozen=True)
class OK:
    new_hash: str


@dataclass(frozen=True)
class STALE:
    current_hash: Optional[str]  # None when the key was absent (§3)


@dataclass(frozen=True)
class EXISTS:
    current_hash: str


@dataclass(frozen=True)
class ERROR:
    kind: ErrorKind
    # JUDGMENT CALL: "labels" is populated ONLY for kind=SECRET_BLOCKED, to
    # carry secret_scan()'s label list (never matched values) back to the
    # caller for logging/telemetry. Empty for every other ErrorKind. This is
    # an additive field on top of the plain "ERROR{kind}" the contract
    # describes; it does not change the tagged-union shape for any other kind.
    labels: Tuple[str, ...] = field(default_factory=tuple)


WriteResult = Union[OK, STALE, EXISTS, ERROR]


def commit_class(result: WriteResult) -> str:
    """Classify a WriteResult per contract §1.

    JUDGMENT CALL: the task text asks for a function typed as returning only
    "definitely_not_committed" | "outcome_unknown", but then says in the same
    breath "OK is committed" — a third value outside that declared type. The
    contract itself (§1) is unambiguous that there are exactly three commit
    classes (definitely-not-committed, outcome-unknown, and OK=committed/
    durable) and that "every result is exactly one class". Collapsing OK into
    "definitely_not_committed" would be actively wrong for a CAS/durability
    contract (it would tell a caller a durable, acknowledged write never
    happened). So this returns one of three strings: "committed",
    "definitely_not_committed", "outcome_unknown" — resolving the
    contradiction in favor of contract correctness over the literal (and
    self-contradictory) two-value signature in the task text.
    """
    if isinstance(result, OK):
        return "committed"
    if isinstance(result, (STALE, EXISTS)):
        return "definitely_not_committed"
    if isinstance(result, ERROR):
        if result.kind in (ErrorKind.TIMEOUT_AFTER_COMMIT, ErrorKind.CONFLICT_UNKNOWN):
            return "outcome_unknown"
        return "definitely_not_committed"
    raise TypeError(f"commit_class: not a WriteResult: {result!r}")


__all__ = [
    "Blob",
    "Caps",
    "BackendHealth",
    "ErrorKind",
    "OK",
    "STALE",
    "EXISTS",
    "ERROR",
    "WriteResult",
    "commit_class",
    "sha256_hex",
    "EMPTY_BODY_SHA256",
]
