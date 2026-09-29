"""Bounded citation validation for source-linked knowledge proposals (read-only).

Validates proposal schema, referenced raw-source identity, and exact quote bytes only.
Does not certify semantic truth, authorship, authorization, project completeness, or
repository freshness. Project selection is store addressing, not authorization.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import json
import math
import re
import sys
import typing
from dataclasses import dataclass

from store.backend import StoreBackend

from .identifiers import valid_id
from .raw_sessions import (
    RawSessionError,
    read_raw_session,
    validate_captured_timestamp,
)

MAX_PROPOSAL_BYTES = 64 * 1024
MAX_CLAIMS = 32
MIN_CLAIMS = 1
MAX_CITATIONS_PER_CLAIM = 16
MAX_CITATIONS_TOTAL = 128
MAX_DISTINCT_SOURCES = 8
MAX_CLAIM_TEXT_BYTES = 1024
MAX_QUOTE_BYTES = 400
MAX_JSON_DEPTH = 32
MAX_JSON_INT_DIGITS = 20

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TOP_KEYS = frozenset({"schema_version", "snapshot_as_of", "coverage", "claims"})
_CLAIM_KEYS = frozenset({"id", "text", "status", "citations"})
_CITATION_KEYS = frozenset(
    {
        "session_id",
        "operation_id",
        "source_sha256",
        "record_id",
        "byte_start",
        "byte_end",
        "quote",
    }
)
_STATUSES = frozenset({"current", "historical", "unresolved"})
_COVERAGE = "selected_sources_only"

_VALIDATION_EXIT_CODES = frozenset(
    {
        "proposal_too_large",
        "invalid_proposal_encoding",
        "invalid_json",
        "duplicate_json_key",
        "non_finite_number",
        "unsupported_field",
        "invalid_schema_version",
        "invalid_snapshot_as_of",
        "invalid_coverage",
        "invalid_claims_count",
        "invalid_claim",
        "duplicate_claim_id",
        "invalid_claim_id",
        "invalid_claim_text",
        "invalid_claim_status",
        "invalid_citations_count",
        "invalid_citation",
        "invalid_session_id",
        "invalid_operation_id",
        "invalid_source_sha256",
        "invalid_record_id",
        "invalid_byte_offsets",
        "invalid_quote",
        "citations_total_exceeded",
        "distinct_sources_exceeded",
        "source_not_found",
        "source_hash_mismatch",
        "record_id_mismatch",
        "source_after_snapshot",
        "offset_out_of_range",
        "quote_mismatch",
        "invalid_project_id",
        "missing_project",
        "missing_proposal_file",
        "proposal_file_error",
        "invalid_argument",
    }
)


class KnowledgeValidationError(Exception):
    """Bounded validation failure (stable code + positional detail only)."""

    def __init__(self, code: str, **detail: object) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)

    def to_payload(self) -> dict:
        out: dict = {"blocked": True, "reason": self.code}
        out.update(self.detail)
        return out


def validation_exit_code(exc: KnowledgeValidationError) -> int:
    return 2 if exc.code in _VALIDATION_EXIT_CODES else 1


def _reject_non_finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise KnowledgeValidationError("non_finite_number")
    return value


def _reject_json_constant(text: str) -> object:
    raise KnowledgeValidationError("invalid_json")


def _parse_bounded_int(text: str) -> int:
    if not text or len(text) > MAX_JSON_INT_DIGITS:
        raise KnowledgeValidationError("invalid_json")
    try:
        return int(text, 10)
    except ValueError:
        raise KnowledgeValidationError("invalid_json")


def _check_json_depth(obj: object, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise KnowledgeValidationError("invalid_json")
    if isinstance(obj, dict):
        for value in obj.values():
            _check_json_depth(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            _check_json_depth(value, depth + 1)


def _ensure_utf8_strings(obj: object) -> None:
    if isinstance(obj, str):
        try:
            obj.encode("utf-8")
        except UnicodeEncodeError:
            raise KnowledgeValidationError("invalid_json")
    elif isinstance(obj, dict):
        for value in obj.values():
            _ensure_utf8_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            _ensure_utf8_strings(value)


def _exact_keys(obj: dict, allowed: frozenset[str], code: str, **idx) -> None:
    keys = set(obj.keys())
    if keys != allowed:
        raise KnowledgeValidationError(code, **idx)


def parse_proposal_bytes(raw: bytes) -> dict:
    """Parse and validate proposal JSON without store I/O."""
    if not isinstance(raw, bytes):
        raise KnowledgeValidationError("invalid_proposal_encoding")
    if len(raw) > MAX_PROPOSAL_BYTES:
        raise KnowledgeValidationError("proposal_too_large", max_bytes=MAX_PROPOSAL_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise KnowledgeValidationError("invalid_proposal_encoding")

    def hook(pairs: list) -> dict:
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise KnowledgeValidationError("duplicate_json_key")
        if not all(isinstance(k, str) for k in keys):
            raise KnowledgeValidationError("invalid_json")
        return dict(pairs)

    try:
        obj = json.loads(
            text,
            object_pairs_hook=hook,
            parse_float=_reject_non_finite,
            parse_int=_parse_bounded_int,
            parse_constant=_reject_json_constant,
        )
    except KnowledgeValidationError:
        raise
    except (ValueError, RecursionError):
        raise KnowledgeValidationError("invalid_json")
    if not isinstance(obj, dict):
        raise KnowledgeValidationError("invalid_json")
    _check_json_depth(obj)
    _ensure_utf8_strings(obj)
    _exact_keys(obj, _TOP_KEYS, "unsupported_field")
    return obj


def _require_int_one(value: object, code: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise KnowledgeValidationError(code)
    if value != 1:
        raise KnowledgeValidationError(code)
    return value


def _require_snapshot(value: object) -> str:
    try:
        return validate_captured_timestamp(value)
    except RawSessionError:
        raise KnowledgeValidationError("invalid_snapshot_as_of")


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class ParsedCitation:
    session_id: str
    operation_id: str
    source_sha256: str
    record_id: str
    byte_start: int
    byte_end: int
    quote: str


@dataclass(frozen=True)
class ParsedClaim:
    claim_id: str
    text: str
    status: str
    citations: tuple[ParsedCitation, ...]


@dataclass(frozen=True)
class ParsedProposal:
    snapshot_as_of: str
    claims: tuple[ParsedClaim, ...]


def _parse_citation(raw: object, claim_index: int, citation_index: int) -> ParsedCitation:
    if not isinstance(raw, dict):
        raise KnowledgeValidationError(
            "invalid_citation", claim_index=claim_index, citation_index=citation_index
        )
    _exact_keys(
        raw,
        _CITATION_KEYS,
        "invalid_citation",
        claim_index=claim_index,
        citation_index=citation_index,
    )
    sid = raw["session_id"]
    if not isinstance(sid, str) or not valid_id(sid):
        raise KnowledgeValidationError(
            "invalid_session_id", claim_index=claim_index, citation_index=citation_index
        )
    op = raw["operation_id"]
    if not isinstance(op, str) or not valid_id(op):
        raise KnowledgeValidationError(
            "invalid_operation_id", claim_index=claim_index, citation_index=citation_index
        )
    sh = raw["source_sha256"]
    if not isinstance(sh, str) or not _HASH_RE.fullmatch(sh):
        raise KnowledgeValidationError(
            "invalid_source_sha256", claim_index=claim_index, citation_index=citation_index
        )
    rid = raw["record_id"]
    if not isinstance(rid, str) or not _HASH_RE.fullmatch(rid):
        raise KnowledgeValidationError(
            "invalid_record_id", claim_index=claim_index, citation_index=citation_index
        )
    start = raw["byte_start"]
    end = raw["byte_end"]
    if type(start) is not int or isinstance(start, bool):
        raise KnowledgeValidationError(
            "invalid_byte_offsets", claim_index=claim_index, citation_index=citation_index
        )
    if type(end) is not int or isinstance(end, bool):
        raise KnowledgeValidationError(
            "invalid_byte_offsets", claim_index=claim_index, citation_index=citation_index
        )
    if start < 0 or end <= start:
        raise KnowledgeValidationError(
            "invalid_byte_offsets", claim_index=claim_index, citation_index=citation_index
        )
    quote = raw["quote"]
    if not isinstance(quote, str) or not quote:
        raise KnowledgeValidationError(
            "invalid_quote", claim_index=claim_index, citation_index=citation_index
        )
    if _utf8_len(quote) > MAX_QUOTE_BYTES:
        raise KnowledgeValidationError(
            "invalid_quote", claim_index=claim_index, citation_index=citation_index
        )
    return ParsedCitation(sid, op, sh, rid, start, end, quote)


def validate_proposal_structure(obj: dict) -> ParsedProposal:
    """Validate version-1 proposal fields and bounds (no store I/O)."""
    if not isinstance(obj, dict):
        raise KnowledgeValidationError("invalid_json")
    _check_json_depth(obj)
    _ensure_utf8_strings(obj)
    _exact_keys(obj, _TOP_KEYS, "unsupported_field")
    _require_int_one(obj.get("schema_version"), "invalid_schema_version")
    snapshot = _require_snapshot(obj.get("snapshot_as_of"))
    coverage = obj.get("coverage")
    if not isinstance(coverage, str) or coverage != _COVERAGE:
        raise KnowledgeValidationError("invalid_coverage")
    claims_raw = obj.get("claims")
    if not isinstance(claims_raw, list):
        raise KnowledgeValidationError("invalid_claims_count")
    if not (MIN_CLAIMS <= len(claims_raw) <= MAX_CLAIMS):
        raise KnowledgeValidationError("invalid_claims_count")

    seen_ids: set[str] = set()
    parsed_claims: list[ParsedClaim] = []
    total_citations = 0
    distinct_sources: set[tuple[str, str]] = set()

    for ci, item in enumerate(claims_raw):
        if not isinstance(item, dict):
            raise KnowledgeValidationError("invalid_claim", claim_index=ci)
        _exact_keys(item, _CLAIM_KEYS, "invalid_claim", claim_index=ci)
        cid = item["id"]
        if not isinstance(cid, str) or not valid_id(cid):
            raise KnowledgeValidationError("invalid_claim_id", claim_index=ci)
        if cid in seen_ids:
            raise KnowledgeValidationError("duplicate_claim_id", claim_index=ci)
        seen_ids.add(cid)
        text = item["text"]
        if not isinstance(text, str) or not text:
            raise KnowledgeValidationError("invalid_claim_text", claim_index=ci)
        if _utf8_len(text) > MAX_CLAIM_TEXT_BYTES:
            raise KnowledgeValidationError("invalid_claim_text", claim_index=ci)
        status = item["status"]
        if not isinstance(status, str) or status not in _STATUSES:
            raise KnowledgeValidationError("invalid_claim_status", claim_index=ci)
        cites_raw = item["citations"]
        if not isinstance(cites_raw, list):
            raise KnowledgeValidationError("invalid_citations_count", claim_index=ci)
        if not (1 <= len(cites_raw) <= MAX_CITATIONS_PER_CLAIM):
            raise KnowledgeValidationError("invalid_citations_count", claim_index=ci)
        parsed_cites: list[ParsedCitation] = []
        for ki, cite in enumerate(cites_raw):
            pc = _parse_citation(cite, ci, ki)
            parsed_cites.append(pc)
            distinct_sources.add((pc.session_id, pc.operation_id))
        total_citations += len(parsed_cites)
        if total_citations > MAX_CITATIONS_TOTAL:
            raise KnowledgeValidationError("citations_total_exceeded")
        parsed_claims.append(
            ParsedClaim(cid, text, status, tuple(parsed_cites))
        )

    if len(distinct_sources) > MAX_DISTINCT_SOURCES:
        raise KnowledgeValidationError("distinct_sources_exceeded")

    return ParsedProposal(snapshot, tuple(parsed_claims))


def _snapshot_dt(snapshot: str) -> datetime.datetime:
    return datetime.datetime.strptime(snapshot, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=datetime.timezone.utc
    )


def _captured_dt(captured: str) -> datetime.datetime:
    return datetime.datetime.strptime(captured, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=datetime.timezone.utc
    )


@dataclass
class _SourceCacheEntry:
    source_bytes: bytes
    source_sha256: str
    record_id: str
    captured: str


def _map_raw_session_error(exc: RawSessionError, claim_index: int, citation_index: int) -> KnowledgeValidationError:
    reason = exc.reason
    if reason in ("invalid_operation_id",):
        return KnowledgeValidationError(
            "invalid_operation_id", claim_index=claim_index, citation_index=citation_index
        )
    if reason in ("invalid_project_id", "invalid_session_id"):
        return KnowledgeValidationError(reason, claim_index=claim_index, citation_index=citation_index)
    if reason in ("corrupt_raw_document", "document_too_large", "secret_blocked"):
        return KnowledgeValidationError("corrupt_raw_document")
    if reason == "backend_error":
        return KnowledgeValidationError("backend_error")
    return KnowledgeValidationError("backend_error")


def validate_proposal(
    backend: StoreBackend, project: str, raw_proposal: bytes
) -> dict:
    """Validate proposal bytes against raw sources (read-only; per-call source cache)."""
    if not valid_id(project):
        raise KnowledgeValidationError("invalid_project_id")
    obj = parse_proposal_bytes(raw_proposal)
    parsed = validate_proposal_structure(obj)
    snapshot_dt = _snapshot_dt(parsed.snapshot_as_of)

    cache: dict[tuple[str, str], _SourceCacheEntry] = {}
    citations_checked = 0

    def load_source(
        session_id: str, operation_id: str, claim_index: int, citation_index: int
    ) -> _SourceCacheEntry:
        key = (session_id, operation_id)
        if key in cache:
            return cache[key]
        try:
            got = read_raw_session(backend, project, session_id, operation_id)
        except RawSessionError as exc:
            raise _map_raw_session_error(exc, claim_index, citation_index) from exc
        except (OSError, RuntimeError):
            raise KnowledgeValidationError("backend_error")
        if not got.found:
            raise KnowledgeValidationError(
                "source_not_found",
                claim_index=claim_index,
                citation_index=citation_index,
            )
        assert got.source_base64 is not None
        try:
            source_bytes = base64.standard_b64decode(got.source_base64)
        except (ValueError, binascii.Error):
            raise KnowledgeValidationError("corrupt_raw_document")
        entry = _SourceCacheEntry(
            source_bytes=source_bytes,
            source_sha256=got.source_sha256 or "",
            record_id=got.record_id or "",
            captured=got.captured or "",
        )
        cache[key] = entry
        return entry

    for ci, claim in enumerate(parsed.claims):
        for ki, cite in enumerate(claim.citations):
            entry = load_source(cite.session_id, cite.operation_id, ci, ki)
            if entry.source_sha256 != cite.source_sha256:
                raise KnowledgeValidationError(
                    "source_hash_mismatch",
                    claim_index=ci,
                    citation_index=ki,
                )
            if entry.record_id != cite.record_id:
                raise KnowledgeValidationError(
                    "record_id_mismatch",
                    claim_index=ci,
                    citation_index=ki,
                )
            try:
                cap_dt = _captured_dt(entry.captured)
            except ValueError:
                raise KnowledgeValidationError("corrupt_raw_document")
            if cap_dt > snapshot_dt:
                raise KnowledgeValidationError(
                    "source_after_snapshot",
                    claim_index=ci,
                    citation_index=ki,
                )
            end = cite.byte_end
            if end > len(entry.source_bytes) or cite.byte_start >= len(entry.source_bytes):
                raise KnowledgeValidationError(
                    "offset_out_of_range",
                    claim_index=ci,
                    citation_index=ki,
                )
            slice_bytes = entry.source_bytes[cite.byte_start:end]
            quote_bytes = cite.quote.encode("utf-8")
            if slice_bytes != quote_bytes:
                raise KnowledgeValidationError(
                    "quote_mismatch",
                    claim_index=ci,
                    citation_index=ki,
                )
            citations_checked += 1

    return {
        "ok": True,
        "claims_checked": len(parsed.claims),
        "citations_checked": citations_checked,
        "distinct_sources_checked": len(cache),
        "validated": {
            "proposal_schema": True,
            "referenced_source_identity": True,
            "exact_quote_bytes": True,
        },
        "semantic_support_verified": False,
        "source_authorship_verified": False,
        "authorization_verified": False,
        "project_completeness_verified": False,
        "repository_freshness_verified": False,
        "addressing": "project selects store namespace only; not authorization or tenancy",
        "note": (
            "Exact quote bytes matched referenced raw sources at validation time; "
            "this is not semantic review or production readiness."
        ),
    }


def read_proposal_file(path: str) -> bytes:
    """Read proposal bytes with size bound (no store access)."""
    if not path:
        raise KnowledgeValidationError("missing_proposal_file")
    try:
        if path == "-":
            data = sys.stdin.buffer.read(MAX_PROPOSAL_BYTES + 1)  # type: ignore[name-defined]
        else:
            with open(path, "rb") as fh:
                data = fh.read(MAX_PROPOSAL_BYTES + 1)
    except OSError:
        raise KnowledgeValidationError("proposal_file_error")
    if len(data) > MAX_PROPOSAL_BYTES:
        raise KnowledgeValidationError("proposal_too_large", max_bytes=MAX_PROPOSAL_BYTES)
    return data


__all__ = [
    "KnowledgeValidationError",
    "parse_proposal_bytes",
    "validate_proposal_structure",
    "validate_proposal",
    "read_proposal_file",
    "validation_exit_code",
]
