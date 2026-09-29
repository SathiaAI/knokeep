"""Explicit knowledge operation handoff (read-only; selected citations only)."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
import typing
from dataclasses import dataclass

from store import gate
from store.backend import BackendBusyError, BackendCorruptionError, StoreBackend
from store.types import Blob, ErrorKind

from .identifiers import valid_id
from .knowledge_revisions import (
    DOC_TYPE as KNOWLEDGE_DOC_TYPE,
    KnowledgeRevisionError,
    knowledge_store_key,
    read_knowledge_revision,
    revision_exit_code,
)
from .knowledge_validation import (
    KnowledgeValidationError,
    parse_proposal_bytes,
    validate_proposal_structure,
    validation_exit_code,
)
from .raw_sessions import (
    DOC_TYPE as RAW_DOC_TYPE,
    RawSessionError,
    raw_session_store_key,
    read_raw_session,
)

MAX_KNOWLEDGE_DOCUMENT_BYTES = 3 * 1024 * 1024
MAX_RAW_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_CACHED_KEYS = 9
MAX_CACHED_BODY_BYTES = 12 * 1024 * 1024
MAX_PROPOSAL_BYTES = 64 * 1024
MAX_SOURCE_AGGREGATE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_JSON_BYTES = 3 * 1024 * 1024
MAX_UNDERLYING_READS = 18
BUDGET_SECONDS = 10.0

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_EXIT2 = frozenset(
    {
        "invalid_project_id",
        "invalid_knowledge_id",
        "invalid_operation_id",
        "invalid_expect_hash",
        "missing_project",
        "missing_knowledge_id",
        "missing_operation_id",
        "invalid_argument",
        "selection_missing",
        "knowledge_snapshot_mismatch",
        "snapshot_changed",
        "source_reference_failed",
        "content_blocked",
        "bundle_too_large",
    }
)

_Q17_REF_KEYS = frozenset({"claim_index", "citation_index", "q17_reason"})


class KnowledgeHandoffError(Exception):
    def __init__(self, reason: str, **detail: object) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason)

    def to_payload(self) -> dict:
        out: dict = {"blocked": True, "reason": self.reason}
        for key, value in self.detail.items():
            if key == "labels" and isinstance(value, (list, tuple)):
                out["labels"] = list(value)
            elif key in _Q17_REF_KEYS and isinstance(value, (str, int)) and not isinstance(value, bool):
                out[key] = value
        return out


def handoff_exit_code(exc: KnowledgeHandoffError) -> int:
    return 2 if exc.reason in _EXIT2 else 1


def parse_required_document_hash(value: str) -> str:
    if not isinstance(value, str) or value == "none" or not _HASH_RE.fullmatch(value):
        raise KnowledgeHandoffError("invalid_expect_hash")
    return value


def _gate_scan_bytes(key: str, data: bytes, *, doc_type: str) -> None:
    err = gate.validate_write(key, data, doc_type=doc_type, expected_hash=None)
    if err is None:
        return
    if err.kind == ErrorKind.SECRET_BLOCKED:
        raise KnowledgeHandoffError("content_blocked", labels=list(err.labels or ()))
    raise KnowledgeHandoffError("content_blocked")


def _gate_scan_text(key: str, text: str, *, doc_type: str) -> None:
    _gate_scan_bytes(key, text.encode("utf-8"), doc_type=doc_type)


def _scan_json_strings(key: str, obj: object, *, doc_type: str) -> None:
    if isinstance(obj, str):
        _gate_scan_text(key, obj, doc_type=doc_type)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                _gate_scan_text(key, k, doc_type=doc_type)
            _scan_json_strings(key, v, doc_type=doc_type)
    elif isinstance(obj, list):
        for item in obj:
            _scan_json_strings(key, item, doc_type=doc_type)


@dataclass(frozen=True)
class _CachedBlob:
    key: str
    body: bytes
    document_sha256: str


class _HandoffReadCache:
    """Read-only adapter: bounded keys, cooperative budget, immutable cache."""

    def __init__(
        self,
        inner: StoreBackend,
        *,
        project: str,
        knowledge_id: str,
        knowledge_key: str,
        deadline: float,
    ) -> None:
        self._inner = inner
        self._project = project
        self._knowledge_key = knowledge_key
        self._prefix = f"{project}/raw-sessions/"
        self._deadline = deadline
        self._read_count = 0
        self._total_body_bytes = 0
        self._cache: dict[str, _CachedBlob] = {}
        self._first_read_order: list[str] = []
        self._expected_hash = ""
        self._knowledge_hash_verified = False
        self.failure: typing.Optional[KnowledgeRevisionError] = None

    def set_expected_hash(self, value: str) -> None:
        self._expected_hash = value

    @property
    def first_read_order(self) -> tuple[str, ...]:
        return tuple(self._first_read_order)

    @property
    def cached_entries(self) -> tuple[_CachedBlob, ...]:
        return tuple(self._cache[k] for k in self._first_read_order)

    def _check_budget(self) -> None:
        if time.monotonic() > self._deadline:
            raise KnowledgeRevisionError("operational_error", kind="timeout")

    def _allowed_key(self, key: str) -> bool:
        if key == self._knowledge_key:
            return True
        if not key.startswith(self._prefix):
            return False
        session_id = key[len(self._prefix) :]
        return valid_id(session_id)

    def _store_cached(self, key: str, body: bytes, document_sha256: str) -> _CachedBlob:
        if key in self._cache:
            existing = self._cache[key]
            if existing.body != body or existing.document_sha256 != document_sha256:
                raise KnowledgeRevisionError("operational_error", kind="cache_inconsistent")
            return existing
        if len(self._cache) >= MAX_CACHED_KEYS:
            raise KnowledgeRevisionError("bundle_too_large")
        body_copy = bytes(body)
        new_total = self._total_body_bytes + len(body_copy)
        if new_total > MAX_CACHED_BODY_BYTES:
            raise KnowledgeRevisionError("bundle_too_large")
        entry = _CachedBlob(key=key, body=body_copy, document_sha256=document_sha256)
        self._cache[key] = entry
        self._first_read_order.append(key)
        self._total_body_bytes = new_total
        return entry

    def read(self, key: str) -> typing.Optional[Blob]:
        try:
            return self._read_impl(key)
        except KnowledgeRevisionError as exc:
            self.failure = exc
            raise

    def _read_impl(self, key: str) -> typing.Optional[Blob]:
        if not self._allowed_key(key):
            raise KnowledgeRevisionError("operational_error", kind="key_denied")
        if key in self._cache:
            entry = self._cache[key]
            return Blob(body=entry.body, version_hash=entry.document_sha256)
        self._check_budget()
        if self._read_count >= MAX_UNDERLYING_READS:
            raise KnowledgeRevisionError("operational_error", kind="read_budget")
        self._read_count += 1
        try:
            blob = self._inner.read(key)
        except BackendCorruptionError:
            raise KnowledgeRevisionError("operational_error", kind="backend_corruption")
        except BackendBusyError:
            raise KnowledgeRevisionError("operational_error", kind="backend_busy")
        except OSError:
            raise KnowledgeRevisionError("operational_error", kind="backend_read")
        except RuntimeError:
            raise KnowledgeRevisionError("operational_error", kind="runtime")
        except Exception:
            raise KnowledgeRevisionError("operational_error", kind="unexpected")
        self._check_budget()
        if blob is None:
            if key == self._knowledge_key:
                return None
            return None
        try:
            if not isinstance(blob.body, (bytes, bytearray)):
                raise TypeError()
            body = bytes(blob.body)
            if hashlib.sha256(body).hexdigest() != blob.version_hash:
                raise ValueError()
        except Exception:
            raise KnowledgeRevisionError("operational_error", kind="blob_integrity") from None
        if key == self._knowledge_key:
            if len(body) > MAX_KNOWLEDGE_DOCUMENT_BYTES:
                raise KnowledgeRevisionError("bundle_too_large")
            if blob.version_hash != self._expected_hash:
                raise KnowledgeRevisionError("knowledge_snapshot_mismatch")
            self._knowledge_hash_verified = True
        else:
            if not self._knowledge_hash_verified:
                raise KnowledgeRevisionError("operational_error", kind="knowledge_order")
            if len(body) > MAX_RAW_DOCUMENT_BYTES:
                raise KnowledgeRevisionError("bundle_too_large")
        self._store_cached(key, body, blob.version_hash)
        return Blob(body=body, version_hash=blob.version_hash)

    def recheck_snapshot(self) -> None:
        for key in self._first_read_order:
            entry = self._cache[key]
            if time.monotonic() > self._deadline:
                raise KnowledgeHandoffError("operational_error", kind="timeout")
            if self._read_count >= MAX_UNDERLYING_READS:
                raise KnowledgeHandoffError("operational_error", kind="read_budget")
            self._read_count += 1
            try:
                blob = self._inner.read(key)
            except BackendCorruptionError:
                raise KnowledgeHandoffError("operational_error", kind="backend_corruption")
            except BackendBusyError:
                raise KnowledgeHandoffError("operational_error", kind="backend_busy")
            except OSError:
                raise KnowledgeHandoffError("operational_error", kind="backend_read")
            except RuntimeError:
                raise KnowledgeHandoffError("operational_error", kind="runtime")
            except Exception:
                raise KnowledgeHandoffError("operational_error", kind="unexpected")
            if time.monotonic() > self._deadline:
                raise KnowledgeHandoffError("operational_error", kind="timeout")
            if blob is None:
                raise KnowledgeHandoffError("snapshot_changed", phase="recheck_missing")
            try:
                if not isinstance(blob.body, (bytes, bytearray)):
                    raise TypeError()
                body = bytes(blob.body)
                if hashlib.sha256(body).hexdigest() != blob.version_hash:
                    raise ValueError()
            except Exception:
                raise KnowledgeHandoffError("operational_error", kind="blob_integrity") from None
            if blob.version_hash != entry.document_sha256 or blob.body != entry.body:
                raise KnowledgeHandoffError("snapshot_changed", phase="recheck_mismatch")


def _map_revision_error(exc: KnowledgeRevisionError) -> KnowledgeHandoffError:
    reason = exc.reason
    if reason == "secret_blocked":
        labels = exc.detail.get("labels", ())
        return KnowledgeHandoffError("content_blocked", labels=list(labels) if labels else [])
    if reason == "source_reference_failed":
        detail = {"q17_reason": exc.detail.get("q17_reason", "unknown")}
        for k in ("claim_index", "citation_index"):
            v = exc.detail.get(k)
            if type(v) is int and not isinstance(v, bool):
                detail[k] = v
        return KnowledgeHandoffError("source_reference_failed", **detail)
    if reason in _EXIT2:
        return KnowledgeHandoffError(reason, **exc.detail)
    if reason == "knowledge_snapshot_mismatch":
        return KnowledgeHandoffError("knowledge_snapshot_mismatch", **exc.detail)
    if reason == "bundle_too_large":
        return KnowledgeHandoffError("bundle_too_large", **exc.detail)
    if revision_exit_code(exc) == 2 and reason not in ("knowledge_corrupt", "source_backend_error"):
        return KnowledgeHandoffError(reason, **exc.detail)
    return KnowledgeHandoffError("operational_error", kind=reason)


def _collect_distinct_sources(parsed) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for claim in parsed.claims:
        for cite in claim.citations:
            pair = (cite.session_id, cite.operation_id)
            if pair in seen:
                continue
            seen.add(pair)
            ordered.append(pair)
    return ordered


def _knowledge_payload(got) -> dict:
    return {
        "found": True,
        "operation_id": got.operation_id,
        "proposal_sha256": got.proposal_sha256,
        "proposal_length": got.proposal_length,
        "record_id": got.record_id,
        "revision": got.revision,
        "previous_record_id": got.previous_record_id,
        "base_document_sha256": got.base_document_sha256,
        "saved_at": got.saved_at,
        "current_document_hash": got.current_document_hash,
        "proposal_base64": got.proposal_base64,
    }


def _source_payload(session_id: str, got) -> dict:
    return {
        "found": True,
        "session_id": session_id,
        "operation_id": got.operation_id,
        "source_sha256": got.source_sha256,
        "source_length": got.source_length,
        "captured": got.captured,
        "record_id": got.record_id,
        "current_version_hash": got.current_version_hash,
        "source_base64": got.source_base64,
    }


def validate_handoff_arguments(
    project: str,
    knowledge_id: str,
    operation_id: str,
    expected_document_hash: str,
) -> str:
    if not project:
        raise KnowledgeHandoffError("missing_project")
    if not knowledge_id:
        raise KnowledgeHandoffError("missing_knowledge_id")
    if not operation_id:
        raise KnowledgeHandoffError("missing_operation_id")
    if not valid_id(project):
        raise KnowledgeHandoffError("invalid_project_id")
    if not valid_id(knowledge_id):
        raise KnowledgeHandoffError("invalid_knowledge_id")
    if not valid_id(operation_id):
        raise KnowledgeHandoffError("invalid_operation_id")
    expect = parse_required_document_hash(expected_document_hash)
    knowledge_key = knowledge_store_key(project, knowledge_id)
    _gate_scan_text(knowledge_key, project, doc_type=KNOWLEDGE_DOC_TYPE)
    _gate_scan_text(knowledge_key, knowledge_id, doc_type=KNOWLEDGE_DOC_TYPE)
    _gate_scan_text(knowledge_key, operation_id, doc_type=KNOWLEDGE_DOC_TYPE)
    return expect


def export_knowledge_handoff(backend: StoreBackend, project: str, knowledge_id: str,
                             operation_id: str, expected_document_hash: str) -> dict:
    try:
        return _export_knowledge_handoff(backend, project, knowledge_id, operation_id, expected_document_hash)
    except KnowledgeHandoffError:
        raise
    except KnowledgeRevisionError as exc:
        raise _map_revision_error(exc) from None
    except Exception:
        raise KnowledgeHandoffError("operational_error") from None


def _export_knowledge_handoff(backend: StoreBackend, project: str, knowledge_id: str,
                              operation_id: str, expected_document_hash: str) -> dict:
    expect = validate_handoff_arguments(project, knowledge_id, operation_id, expected_document_hash)
    knowledge_key = knowledge_store_key(project, knowledge_id)

    deadline = time.monotonic() + BUDGET_SECONDS
    cache = _HandoffReadCache(
        backend,
        project=project,
        knowledge_id=knowledge_id,
        knowledge_key=knowledge_key,
        deadline=deadline,
    )
    cache.set_expected_hash(expect)

    try:
        got = read_knowledge_revision(cache, project, knowledge_id, operation_id)
    except KnowledgeRevisionError as exc:
        raise _map_revision_error(cache.failure or exc) from exc

    if not got.found:
        if knowledge_key not in cache._cache:
            raise KnowledgeHandoffError("selection_missing", phase="knowledge")
        raise KnowledgeHandoffError("selection_missing", phase="operation")

    assert got.proposal_base64 is not None
    try:
        proposal_bytes = base64.standard_b64decode(got.proposal_base64)
    except (ValueError, binascii.Error):
        raise KnowledgeHandoffError("operational_error", kind="proposal_decode")

    if len(proposal_bytes) > MAX_PROPOSAL_BYTES:
        raise KnowledgeHandoffError("bundle_too_large")

    _gate_scan_bytes(knowledge_key, proposal_bytes, doc_type=KNOWLEDGE_DOC_TYPE)
    try:
        proposal_obj = parse_proposal_bytes(proposal_bytes)
        parsed = validate_proposal_structure(proposal_obj)
    except KnowledgeValidationError as exc:
        if validation_exit_code(exc) == 2:
            raise KnowledgeHandoffError("source_reference_failed", q17_reason=exc.code) from exc
        raise KnowledgeHandoffError("operational_error", kind=exc.code) from exc
    _scan_json_strings(knowledge_key, proposal_obj, doc_type=KNOWLEDGE_DOC_TYPE)

    cite_pairs = _collect_distinct_sources(parsed)
    sources_out: list[dict] = []
    source_bytes_total = 0
    for session_id, src_op in cite_pairs:
        raw_key = raw_session_store_key(project, session_id)
        try:
            src = read_raw_session(cache, project, session_id, src_op)
        except RawSessionError:
            raise KnowledgeHandoffError("operational_error", kind="raw_read")
        if not src.found:
            raise KnowledgeHandoffError("source_reference_failed", q17_reason="source_not_found")
        assert src.source_base64 is not None
        try:
            decoded = base64.standard_b64decode(src.source_base64)
        except (ValueError, binascii.Error):
            raise KnowledgeHandoffError("operational_error", kind="source_decode")
        _gate_scan_bytes(raw_key, decoded, doc_type=RAW_DOC_TYPE)
        source_bytes_total += len(decoded)
        if source_bytes_total > MAX_SOURCE_AGGREGATE_BYTES:
            raise KnowledgeHandoffError("bundle_too_large")
        sources_out.append(_source_payload(session_id, src))

    cache.recheck_snapshot()

    documents = [
        {"key": entry.key, "document_sha256": entry.document_sha256}
        for entry in cache.cached_entries
    ]

    payload = {
        "schema_version": 1,
        "kind": "knokeep_explicit_handoff",
        "project": project,
        "knowledge_id": knowledge_id,
        "operation_id": operation_id,
        "coverage": "selected_sources_only",
        "selection": "explicit_operation",
        "knowledge": _knowledge_payload(got),
        "sources": sources_out,
        "documents": documents,
        "proposal_snapshot_as_of": parsed.snapshot_as_of,
        "semantic_support_verified": False,
        "source_authorship_verified": False,
        "authorization_verified": False,
        "project_completeness_verified": False,
        "repository_freshness_verified": False,
        "atomic_snapshot_verified": False,
        "native_client_integration_verified": False,
        "semantic_search_evaluated": False,
        "source_reads_non_atomic": True,
        "observed_documents_rechecked": True,
        "content_is_untrusted_data": True,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("utf-8")) > MAX_OUTPUT_JSON_BYTES:
        raise KnowledgeHandoffError("bundle_too_large")
    return payload


__all__ = [
    "KnowledgeHandoffError",
    "export_knowledge_handoff",
    "handoff_exit_code",
    "parse_required_document_hash",
    "validate_handoff_arguments",
]
