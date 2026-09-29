"""Bounded session read/list queries over StoreBackend (read-only at app layer).

Project selection is addressing (which namespace to query), not authorization.
No tenancy or access control is implied by these helpers.
"""
from __future__ import annotations

import base64
import typing
from dataclasses import dataclass

from store.backend import StoreBackend
from store.types import sha256_hex

from .identifiers import valid_id

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
MAX_LIST_SCAN_KEYS = 5000


class SessionQueryError(Exception):
    """Machine-readable query failure (caller maps to JSON + exit code)."""

    def __init__(self, reason: str, **detail: object) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


def session_store_key(project: str, session_id: str) -> str:
    """Validated store key for one session journal (`{project}/sessions/{session}`)."""
    if not valid_id(project):
        raise SessionQueryError("invalid_project_id", value=project)
    if not valid_id(session_id):
        raise SessionQueryError("invalid_session_id", value=session_id)
    return f"{project}/sessions/{session_id}"


def _sessions_prefix(project: str) -> str:
    if not valid_id(project):
        raise SessionQueryError("invalid_project_id", value=project)
    return f"{project}/sessions/"


def _session_id_from_key(project: str, key: str) -> typing.Optional[str]:
    """Return session id when key is exactly `{project}/sessions/{id}`; else None."""
    prefix = _sessions_prefix(project)
    if not key.startswith(prefix):
        return None
    suffix = key[len(prefix) :]
    if not suffix or "/" in suffix:
        return None
    if not valid_id(suffix):
        return None
    return suffix


@dataclass(frozen=True)
class SessionReadResult:
    found: bool
    session_id: str
    content_hash: typing.Optional[str] = None
    body_base64: typing.Optional[str] = None
    body_utf8: typing.Optional[str] = None


@dataclass(frozen=True)
class SessionListResult:
    session_ids: tuple[str, ...]
    truncated: bool
    next_after: typing.Optional[str] = None


def read_session(
    backend: StoreBackend, project: str, session_id: str
) -> SessionReadResult:
    key = session_store_key(project, session_id)
    blob = backend.read(key)
    if blob is None:
        return SessionReadResult(found=False, session_id=session_id)
    actual_hash = sha256_hex(blob.body)
    if actual_hash != blob.version_hash:
        raise SessionQueryError(
            "hash_mismatch",
            key=key,
            version_hash=blob.version_hash,
            actual_hash=actual_hash,
        )
    body_utf8: typing.Optional[str] = None
    try:
        body_utf8 = blob.body.decode("utf-8")
    except UnicodeDecodeError:
        pass
    return SessionReadResult(
        found=True,
        session_id=session_id,
        content_hash=actual_hash,
        body_base64=base64.standard_b64encode(blob.body).decode("ascii"),
        body_utf8=body_utf8,
    )


def list_sessions(
    backend: StoreBackend,
    project: str,
    *,
    limit: int = DEFAULT_LIST_LIMIT,
    after: typing.Optional[str] = None,
) -> SessionListResult:
    prefix = validate_list_arguments(project, limit=limit, after=after)
    seen: set[str] = set()
    scanned = 0
    for key in backend.list(prefix):
        scanned += 1
        if scanned > MAX_LIST_SCAN_KEYS:
            raise SessionQueryError(
                "scan_limit_exceeded",
                scanned=scanned,
                max_scan=MAX_LIST_SCAN_KEYS,
            )
        sid = _session_id_from_key(project, key)
        if sid is None:
            raise SessionQueryError("invalid_session_key", key=key)
        seen.add(sid)

    ordered = sorted(seen)
    if after is not None:
        ordered = [s for s in ordered if s > after]

    page = tuple(ordered[:limit])
    truncated = len(ordered) > limit
    next_after = page[-1] if truncated and page else None
    return SessionListResult(
        session_ids=page,
        truncated=truncated,
        next_after=next_after,
    )


def validate_list_arguments(project: str, *, limit: int = DEFAULT_LIST_LIMIT,
                            after: typing.Optional[str] = None) -> str:
    """Validate before an adapter acquires backend resources; return the prefix."""
    if type(limit) is not int or limit < 1 or limit > MAX_LIST_LIMIT:
        raise SessionQueryError(
            "invalid_limit", limit=limit, max_limit=MAX_LIST_LIMIT
        )
    if after is not None and not valid_id(after):
        raise SessionQueryError("invalid_after", value=after)

    return _sessions_prefix(project)
