# SPDX-License-Identifier: Elastic-2.0 OR LicenseRef-Viaknox-Commercial
# Copyright (c) 2026 Viaknox Holding LLC
"""Where conclusions live, and how they move from `proposed` to `accepted`.

A conclusion is a derived statement that cites exact memory. It is ADDITIVE
context: nothing here ever reads-then-changes, replaces or deletes an exact
entry. Every write goes through the one gate (`store.gate.persist`) into a
reserved `.conclusions/` segment inside the conclusion's own space, so the
secret gate scans it like everything else and a conclusion containing a secret
is refused, not stored.

One record per conclusion, `<space>.conclusions/<id>.json`, moving through:

    proposed ──(verifier accepts)──▶ accepted ──┬─(evidence gone)──▶ stale
        ▲  failed verification stays proposed    └─(a newer one accepted)─▶ superseded

Nothing is ever deleted. Status changes are compare-and-swap updates, so two
workers racing on one record cannot both win. Failure reasons are stored as
codes only, never claim text, quotes or matched secrets.

No gate change is needed: a conclusion record is an ordinary hash-CAS state
document, so it uses the gate's existing `system_state` document type.
"""
from __future__ import annotations

import datetime
import json
import re
import time
import uuid
from typing import Callable, Dict, List, Optional, Sequence

from store import gate
from store.backend import BackendBusyError
from store.context import create_ctx, overwrite_ctx
from store.types import ERROR, ErrorKind, OK, STALE

from . import verify as v

SCHEMA_VERSION = 1
LABEL = "derived, auto-verified, not exact memory"

PROPOSED = "proposed"
ACCEPTED = "accepted"
STALE_STATUS = "stale"
SUPERSEDED = "superseded"

_DOC_TYPE = "system_state"  # an ordinary hash-CAS state document (contract §6)
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_LEASE_TTL_S = 30.0
_MAX_ATTEMPTS = 8
_MAX_VERSION_PAGES = 50


class ConclusionRefused(Exception):
    """A conclusion operation was refused. `code` is from a closed vocabulary
    and never contains claim text, a quote or a secret."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# Codes raised here (verifier codes live in conclusions.verify).
SECRET = "SECRET"
WRITE_FAILED = "WRITE_FAILED"
BUSY = "BUSY"
CORRUPT = "CORRUPT"
NOT_FOUND = "NOT_FOUND"
BAD_ID = "BAD_ID"
SOURCE_CHANGED = "SOURCE_CHANGED"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _prefix(space: str) -> str:
    return space + v.DERIVED_SEGMENT


def _key(space: str, cid: str) -> str:
    if not _ID_RE.match(cid):
        raise ConclusionRefused(BAD_ID)
    return f"{_prefix(space)}{cid}.json"


def _encode(record: Dict) -> bytes:
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _load(backend, space: str, cid: str):
    """(record, version_hash) for one conclusion, or None if absent."""
    blob = backend.read(_key(space, cid))
    if blob is None:
        return None
    try:
        record = json.loads(bytes(blob.body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ConclusionRefused(CORRUPT)
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != SCHEMA_VERSION
        or record.get("id") != cid
        or record.get("space") != space
    ):
        raise ConclusionRefused(CORRUPT)
    return record, blob.version_hash


def _write(backend, key: str, record: Dict, expected_hash: Optional[str]):
    # Internal invariant: this module only ever writes inside a space's
    # reserved conclusions segment, never to an exact entry.
    if v.DERIVED_SEGMENT not in key:
        raise ConclusionRefused(WRITE_FAILED)
    raw = _encode(record)
    if expected_hash is None:
        return gate.persist(backend, key, raw, ctx=create_ctx(), doc_type=_DOC_TYPE)
    lease = backend.lock(key, ttl_s=_LEASE_TTL_S)
    try:
        return gate.persist(
            backend, key, raw, ctx=overwrite_ctx(expected_hash, lease), doc_type=_DOC_TYPE
        )
    finally:
        backend.unlock(lease)


def _update(backend, space: str, cid: str, mutate: Callable[[Dict], Optional[Dict]]) -> Dict:
    """Read-modify-write one record with compare-and-swap, retrying when
    another writer got there first. `mutate` returns the new record, or None
    for "nothing to change". Returns the record as it ended up."""
    key = _key(space, cid)
    for attempt in range(_MAX_ATTEMPTS):
        loaded = _load(backend, space, cid)
        if loaded is None:
            raise ConclusionRefused(NOT_FOUND)
        record, current_hash = loaded
        changed = mutate(json.loads(json.dumps(record)))
        if changed is None:
            return record
        changed["updated"] = _now_iso()
        try:
            result = _write(backend, key, changed, current_hash)
        except BackendBusyError:
            time.sleep(0.01 * (attempt + 1))
            continue
        if isinstance(result, OK):
            return changed
        if isinstance(result, STALE):
            continue  # someone else changed it: re-read and decide again
        if isinstance(result, ERROR) and result.kind is ErrorKind.BUSY:
            time.sleep(0.01 * (attempt + 1))
            continue
        raise ConclusionRefused(WRITE_FAILED)
    raise ConclusionRefused(BUSY)


# -- public API ---------------------------------------------------------------


def propose(
    backend,
    space: str,
    claims: Sequence[v.Claim],
    generator_family: str,
    *,
    supersedes: Optional[str] = None,
) -> str:
    """Stage a new conclusion as `proposed` and return its id. It is not yet
    trusted for anything. Refuses (storing nothing) if it is malformed or the
    gate finds a secret in it."""
    conclusion = v.Conclusion(space, tuple(claims), generator_family)
    if v._shape_failures(conclusion):
        raise ConclusionRefused(v.SHAPE)
    if supersedes is not None and not _ID_RE.match(supersedes):
        raise ConclusionRefused(BAD_ID)
    cid = uuid.uuid4().hex
    now = _now_iso()
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": cid,
        "space": space,
        "status": PROPOSED,
        "generator_family": generator_family,
        "judge_family": None,
        "claims": [
            {
                "text": c.text,
                "citations": [
                    {"key": ci.key, "version_hash": ci.version_hash, "quote": ci.quote}
                    for ci in c.citations
                ],
            }
            for c in claims
        ],
        "failures": [],
        "tainted": False,
        "supersedes": supersedes,
        "created": now,
        "updated": now,
    }
    result = _write(backend, _key(space, cid), record, None)
    if isinstance(result, OK):
        return cid
    if isinstance(result, ERROR) and result.kind in (ErrorKind.SECRET_BLOCKED, ErrorKind.SCAN_FAILURE):
        raise ConclusionRefused(SECRET)  # a found secret, or a scanner that could not decide: both fail closed
    raise ConclusionRefused(WRITE_FAILED)


def _as_conclusion(record: Dict) -> v.Conclusion:
    return v.Conclusion(
        space=record["space"],
        claims=tuple(
            v.Claim(
                c["text"],
                tuple(v.Citation(ci["key"], ci["version_hash"], ci["quote"]) for ci in c["citations"]),
            )
            for c in record["claims"]
        ),
        generator_family=record["generator_family"],
    )


def promote(
    backend,
    space: str,
    cid: str,
    *,
    resolve: v.Resolve,
    source_info: v.SourceInfoFn,
    judge: Optional[v.Judge] = None,
    judge_family: Optional[str] = None,
) -> v.Verdict:
    """Run the automatic verifier on a `proposed` conclusion. If it passes the
    conclusion becomes `accepted`; if not it stays `proposed` with the failure
    CODES recorded (and `tainted` set when an untrusted source was the only
    problem). Idempotent: an already-accepted conclusion returns an accepting
    verdict without re-running anything."""
    loaded = _load(backend, space, cid)
    if loaded is None:
        raise ConclusionRefused(NOT_FOUND)
    record, _ = loaded
    if record["status"] == ACCEPTED:
        return v.Verdict(True)
    if record["status"] != PROPOSED:
        raise ConclusionRefused("NOT_PROPOSED")

    verdict = v.verify(
        _as_conclusion(record),
        resolve=resolve, source_info=source_info, judge=judge, judge_family=judge_family,
    )

    def mutate(rec: Dict) -> Optional[Dict]:
        if rec["status"] != PROPOSED:
            return None  # another worker already moved it
        rec["failures"] = [
            {"code": f.code, "claim_index": f.claim_index} for f in verdict.failures
        ]
        rec["tainted"] = bool(verdict.tainted)
        if verdict.accepted:
            rec["status"] = ACCEPTED
            rec["judge_family"] = judge_family
            rec["accepted_at"] = _now_iso()
        return rec

    final = _update(backend, space, cid, mutate)

    # A newer accepted conclusion demotes the one it supersedes; the old one is
    # kept (status only), never overwritten or deleted.
    if verdict.accepted and final.get("supersedes") and final["status"] == ACCEPTED:
        old_id = final["supersedes"]
        if old_id != cid and _ID_RE.match(old_id):
            def demote(rec: Dict) -> Optional[Dict]:
                if rec["status"] != ACCEPTED:
                    return None
                rec["status"] = SUPERSEDED
                rec["superseded_by"] = cid
                return rec
            try:
                _update(backend, space, old_id, demote)
            except ConclusionRefused as exc:
                if exc.code != NOT_FOUND:
                    raise
    return verdict


def recheck(backend, space: str, cid: str) -> str:
    """Mark an `accepted` conclusion `stale` if any of its evidence is gone:
    the cited entry no longer exists, or no longer contains the quoted text
    verbatim. An entry that merely changed elsewhere does not matter. Returns
    the conclusion's status afterwards."""
    loaded = _load(backend, space, cid)
    if loaded is None:
        raise ConclusionRefused(NOT_FOUND)
    record, _ = loaded
    if record["status"] != ACCEPTED:
        return record["status"]

    def evidence_present(rec: Dict) -> bool:
        for claim in rec["claims"]:
            for ci in claim["citations"]:
                blob = backend.read(ci["key"])
                if blob is None:
                    return False
                try:
                    text = bytes(blob.body).decode("utf-8")
                except UnicodeDecodeError:
                    return False
                if ci["quote"] not in text:
                    return False
        return True

    def mutate(rec: Dict) -> Optional[Dict]:
        if rec["status"] != ACCEPTED or evidence_present(rec):
            return None
        rec["status"] = STALE_STATUS
        rec["stale_reason"] = SOURCE_CHANGED
        return rec

    return _update(backend, space, cid, mutate)["status"]


def list_records(backend, space: str) -> List[Dict]:
    """Every conclusion record of ONE space, any status, oldest first. Corrupt
    or unreadable derived records are skipped: a derived layer must never break
    reading memory."""
    if not isinstance(space, str) or not space.endswith("/") or space == "/":
        raise ConclusionRefused(v.SHAPE)
    records = []
    for key in backend.list(_prefix(space)):
        name = key[len(_prefix(space)):]
        if not name.endswith(".json"):
            continue
        try:
            loaded = _load(backend, space, name[: -len(".json")])
        except ConclusionRefused:
            continue
        if loaded is not None:
            records.append(loaded[0])
    return sorted(records, key=lambda r: (r["created"], r["id"]))


def recall(backend, space: str) -> List[Dict]:
    """The accepted conclusions of ONE space, oldest first, each labelled as
    derived and carrying its sources."""
    return [
        {
            "id": rec["id"],
            "label": LABEL,
            "created": rec["created"],
            "claims": [
                {
                    "text": c["text"],
                    "sources": [
                        {"key": ci["key"], "version_hash": ci["version_hash"], "quote": ci["quote"]}
                        for ci in c["citations"]
                    ],
                }
                for c in rec["claims"]
            ],
        }
        for rec in list_records(backend, space)
        if rec["status"] == ACCEPTED
    ]


def backend_resolver(backend) -> v.Resolve:
    """A `resolve(key, version_hash)` for the verifier, built on a backend.
    Uses the current entry when it still has that exact version, and otherwise
    looks through the version history when the backend keeps one."""

    def resolve(key: str, version_hash: str) -> Optional[bytes]:
        blob = backend.read(key)
        if blob is not None and blob.version_hash == version_hash:
            return bytes(blob.body)
        versions = getattr(backend, "versions", None)
        read_version = getattr(backend, "read_version", None)
        if not (callable(versions) and callable(read_version)):
            return None
        before = None
        for _ in range(_MAX_VERSION_PAGES):
            page = versions(key, limit=100, before_seq=before)
            if not page:
                return None
            for rec in page:
                if rec.version_hash == version_hash:
                    old = read_version(key, rec.seq)
                    return bytes(old.body) if old is not None else None
            before = page[-1].seq
        return None

    return resolve


__all__ = [
    "ConclusionRefused", "propose", "promote", "recheck", "recall", "list_records", "backend_resolver",
    "PROPOSED", "ACCEPTED", "STALE_STATUS", "SUPERSEDED", "LABEL",
]
