"""Scoped lexical source retrieval (read-only manifest closure)."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import typing
from dataclasses import dataclass

from store import gate
from store.backend import BackendBusyError, BackendCorruptionError, StoreBackend
from store.types import Blob, ErrorKind

from .identifiers import valid_id
from .raw_sessions import (
    DOC_TYPE as RAW_DOC_TYPE,
    RawSessionError,
    raw_session_store_key,
    parse_raw_document,
    record_id_for_header,
)

MAX_MANIFEST_BYTES = 32 * 1024
MAX_REFS = 32
MAX_SESSIONS = 8
MAX_QUERY_BYTES = 512
MAX_QUERY_TOKENS = 32
MAX_CASEFOLD_TOKEN_BYTES = 128
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_CACHED_BODY_BYTES = 16 * 1024 * 1024
MAX_DECODED_SOURCE_BYTES = 2 * 1024 * 1024
MAX_UNDERLYING_READS = 16
MAX_OUTPUT_JSON_BYTES = 64 * 1024
MAX_EXCERPT_AGGREGATE_BYTES = 4000
MAX_EXCERPT_BYTES = 400
EXCERPT_PREFIX_BYTES = 160
BUDGET_SECONDS = 10.0
DEADLINE_CHECK_EVERY_CP = 4096
MAX_JSON_DEPTH = 8
MAX_JSON_INT_DIGITS = 20

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TOP_KEYS = frozenset({"schema_version", "manifest_id", "project", "refs"})
_REF_KEYS = frozenset(
    {
        "ref_id",
        "session_id",
        "operation_id",
        "document_sha256",
        "record_id",
        "source_sha256",
    }
)

_ALLOWED_REASONS = frozenset(
    {
        "invalid_request",
        "manifest_mismatch",
        "evidence_missing",
        "evidence_pin_mismatch",
        "evidence_corrupt",
        "content_blocked",
        "snapshot_changed",
        "limit_exceeded",
        "budget_exceeded",
        "operational_error",
    }
)

_QUALIFICATIONS = {
    "content_is_untrusted_data": True,
    "observed_documents_rechecked": True,
    "source_reads_non_atomic": True,
    "semantic_support_verified": False,
    "authorship_verified": False,
    "authorization_verified": False,
    "project_completeness_verified": False,
    "repository_freshness_verified": False,
    "atomic_snapshot_verified": False,
    "native_integration_verified": False,
    "semantic_search_evaluated": False,
    "full_product_qualified": False,
}


class SourceRetrievalError(Exception):
    def __init__(self, reason: str, **detail: object) -> None:
        if reason not in _ALLOWED_REASONS:
            raise ValueError("disallowed reason")
        clean: dict[str, int] = {}
        for key, value in detail.items():
            if key not in ("ref_index", "document_index"):
                continue
            if type(value) is not int or isinstance(value, bool):
                continue
            clean[key] = value
        self.reason = reason
        self.detail = clean
        super().__init__(reason)


def _reject_non_finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise SourceRetrievalError("invalid_request")
    return value


def _reject_json_constant(text: str) -> object:
    raise SourceRetrievalError("invalid_request")


def _parse_bounded_int(text: str) -> int:
    if not text or len(text) > MAX_JSON_INT_DIGITS:
        raise SourceRetrievalError("invalid_request")
    try:
        return int(text, 10)
    except ValueError:
        raise SourceRetrievalError("invalid_request")


def _check_json_depth(obj: object, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise SourceRetrievalError("invalid_request")
    if isinstance(obj, dict):
        for value in obj.values():
            _check_json_depth(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            _check_json_depth(value, depth + 1)


def _parse_manifest_json(raw: bytes) -> dict:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise SourceRetrievalError("invalid_request")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SourceRetrievalError("invalid_request")
    if text.startswith("\ufeff"):
        raise SourceRetrievalError("invalid_request")

    def hook(pairs: list) -> dict:
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise SourceRetrievalError("invalid_request")
        if not all(isinstance(k, str) for k in keys):
            raise SourceRetrievalError("invalid_request")
        return dict(pairs)

    try:
        obj = json.loads(
            text,
            object_pairs_hook=hook,
            parse_float=_reject_non_finite,
            parse_int=_parse_bounded_int,
            parse_constant=_reject_json_constant,
        )
    except SourceRetrievalError:
        raise
    except (ValueError, RecursionError, json.JSONDecodeError):
        raise SourceRetrievalError("invalid_request")
    if not isinstance(obj, dict):
        raise SourceRetrievalError("invalid_request")
    _check_json_depth(obj)
    return obj


def _require_hash(value: object) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise SourceRetrievalError("invalid_request")
    return value


def _require_id(value: object) -> str:
    if not isinstance(value, str) or not valid_id(value):
        raise SourceRetrievalError("invalid_request")
    return value


def _require_int_one(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value != 1:
        raise SourceRetrievalError("invalid_request")
    return 1


@dataclass(frozen=True)
class _ManifestRef:
    ref_id: str
    session_id: str
    operation_id: str
    document_sha256: str
    record_id: str
    source_sha256: str


@dataclass(frozen=True)
class _ParsedManifest:
    manifest_id: str
    project: str
    refs: tuple[_ManifestRef, ...]


def _validate_manifest_obj(obj: dict, project: str) -> _ParsedManifest:
    if set(obj.keys()) != _TOP_KEYS:
        raise SourceRetrievalError("invalid_request")
    _require_int_one(obj["schema_version"])
    manifest_id = _require_id(obj["manifest_id"])
    manifest_project = _require_id(obj["project"])
    if manifest_project != project:
        raise SourceRetrievalError("invalid_request")
    refs_raw = obj["refs"]
    if not isinstance(refs_raw, list) or not (1 <= len(refs_raw) <= MAX_REFS):
        raise SourceRetrievalError("invalid_request")
    _check_json_depth(refs_raw, 1)
    seen_ref: set[str] = set()
    seen_op: set[tuple[str, str]] = set()
    session_pins: dict[str, str] = {}
    sessions: set[str] = set()
    refs: list[_ManifestRef] = []
    for idx, item in enumerate(refs_raw):
        if not isinstance(item, dict):
            raise SourceRetrievalError("invalid_request", ref_index=idx)
        if set(item.keys()) != _REF_KEYS:
            raise SourceRetrievalError("invalid_request", ref_index=idx)
        ref_id = _require_id(item["ref_id"])
        session_id = _require_id(item["session_id"])
        operation_id = _require_id(item["operation_id"])
        document_sha256 = _require_hash(item["document_sha256"])
        record_id = _require_hash(item["record_id"])
        source_sha256 = _require_hash(item["source_sha256"])
        if ref_id in seen_ref:
            raise SourceRetrievalError("invalid_request", ref_index=idx)
        seen_ref.add(ref_id)
        pair = (session_id, operation_id)
        if pair in seen_op:
            raise SourceRetrievalError("invalid_request", ref_index=idx)
        seen_op.add(pair)
        prev_pin = session_pins.get(session_id)
        if prev_pin is not None and prev_pin != document_sha256:
            raise SourceRetrievalError("invalid_request", ref_index=idx)
        session_pins[session_id] = document_sha256
        sessions.add(session_id)
        if len(sessions) > MAX_SESSIONS:
            raise SourceRetrievalError("invalid_request", ref_index=idx)
        refs.append(
            _ManifestRef(
                ref_id=ref_id,
                session_id=session_id,
                operation_id=operation_id,
                document_sha256=document_sha256,
                record_id=record_id,
                source_sha256=source_sha256,
            )
        )
    return _ParsedManifest(manifest_id=manifest_id, project=project, refs=tuple(refs))


def _gate_scan_bytes(key: str, data: bytes, invalid_reason: str = "evidence_corrupt") -> None:
    err = gate.validate_write(key, data, doc_type=RAW_DOC_TYPE, expected_hash=None)
    if err is None:
        return
    if err.kind == ErrorKind.SECRET_BLOCKED:
        raise SourceRetrievalError("content_blocked")
    if err.kind == ErrorKind.INVALID_ARGUMENT:
        raise SourceRetrievalError(invalid_reason)
    raise SourceRetrievalError("operational_error") from None


def _gate_scan_text(key: str, text: str) -> None:
    _gate_scan_bytes(key, text.encode("utf-8"), "invalid_request")


def _scan_json_strings(key: str, obj: object) -> None:
    if isinstance(obj, str):
        _gate_scan_text(key, obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                _gate_scan_text(key, k)
            _scan_json_strings(key, v)
    elif isinstance(obj, list):
        for item in obj:
            _scan_json_strings(key, item)


def _parse_manifest_hash(value: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise SourceRetrievalError("invalid_request")
    return value


def _validate_top_k(top_k: object) -> int:
    if type(top_k) is not int or isinstance(top_k, bool) or not (1 <= top_k <= 10):
        raise SourceRetrievalError("invalid_request")
    return top_k


def _validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise SourceRetrievalError("invalid_request")
    try:
        qb = query.encode("utf-8")
    except UnicodeEncodeError:
        raise SourceRetrievalError("invalid_request")
    if not (1 <= len(qb) <= MAX_QUERY_BYTES):
        raise SourceRetrievalError("invalid_request")
    return query


def _iter_alnum_token_spans(text: str, deadline: typing.Optional[float] = None) -> typing.Iterator[tuple[int, int, str]]:
    i = 0
    n = len(text)
    while i < n:
        if deadline is not None and i % DEADLINE_CHECK_EVERY_CP == 0 and time.monotonic() > deadline:
            raise SourceRetrievalError("budget_exceeded")
        if text[i].isalnum():
            start = i
            i += 1
            while i < n and text[i].isalnum():
                if deadline is not None and i % DEADLINE_CHECK_EVERY_CP == 0 and time.monotonic() > deadline:
                    raise SourceRetrievalError("budget_exceeded")
                i += 1
            yield start, i, text[start:i]
        else:
            i += 1


def _char_span_to_byte_span(text: str, c0: int, c1: int) -> tuple[int, int]:
    return len(text[:c0].encode("utf-8")), len(text[:c1].encode("utf-8"))


def _next_utf8_boundary(data: bytes, pos: int) -> int:
    n = len(data)
    if pos <= 0:
        return 0
    if pos >= n:
        return n
    while pos < n and (data[pos] & 0xC0) == 0x80:
        pos += 1
    return min(pos, n)


def _prev_utf8_boundary(data: bytes, pos: int) -> int:
    n = len(data)
    pos = min(max(pos, 0), n)
    while pos > 0 and pos < n and (data[pos] & 0xC0) == 0x80:
        pos -= 1
    return pos


def _prepare_query_tokens(query: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    count = 0
    for _c0, _c1, tok in _iter_alnum_token_spans(query):
        count += 1
        if count > MAX_QUERY_TOKENS:
            raise SourceRetrievalError("invalid_request")
        folded = tok.casefold()
        if len(folded.encode("utf-8")) > MAX_CASEFOLD_TOKEN_BYTES:
            raise SourceRetrievalError("invalid_request")
        if folded in seen:
            continue
        seen.add(folded)
        ordered.append(folded)
    if not ordered:
        raise SourceRetrievalError("invalid_request")
    return ordered


@dataclass(frozen=True)
class _CachedDoc:
    key: str
    session_id: str
    body: bytes
    document_sha256: str


class _RetrievalReadCache:
    """Maps backend exceptions to operational_error before any raw helper use."""

    def __init__(
        self,
        inner: StoreBackend,
        *,
        project: str,
        allowed_keys: frozenset[str],
        session_pins: dict[str, str],
        deadline: float,
    ) -> None:
        self._inner = inner
        self._project = project
        self._allowed = allowed_keys
        self._session_pins = session_pins
        self._deadline = deadline
        self._read_count = 0
        self._total_body_bytes = 0
        self._cache: dict[str, _CachedDoc] = {}
        self._order: list[str] = []
        self._doc_index: dict[str, int] = {}

    @property
    def cached_docs(self) -> tuple[_CachedDoc, ...]:
        return tuple(self._cache[k] for k in self._order)

    def _check_deadline(self) -> None:
        if time.monotonic() > self._deadline:
            raise SourceRetrievalError("budget_exceeded")

    def _backend_read(self, key: str) -> typing.Optional[Blob]:
        self._check_deadline()
        if self._read_count >= MAX_UNDERLYING_READS:
            raise SourceRetrievalError("budget_exceeded")
        self._read_count += 1
        try:
            blob = self._inner.read(key)
        except (BackendCorruptionError, BackendBusyError, OSError, RuntimeError):
            raise SourceRetrievalError("operational_error") from None
        except Exception as exc:
            if isinstance(exc, (SourceRetrievalError, RawSessionError)):
                raise SourceRetrievalError("operational_error") from None
            raise SourceRetrievalError("operational_error") from None
        self._check_deadline()
        return blob

    def ensure_loaded(self, key: str, document_index: int) -> _CachedDoc:
        if key in self._cache:
            return self._cache[key]
        if key not in self._allowed:
            raise SourceRetrievalError("operational_error") from None
        session_id = key.rsplit("/", 1)[-1]
        document_pin = self._session_pins[session_id]
        blob = self._backend_read(key)
        if blob is None:
            raise SourceRetrievalError("evidence_missing", document_index=document_index)
        try:
            if not isinstance(blob.body, (bytes, bytearray)):
                raise TypeError()
            body = bytes(blob.body)
            actual = hashlib.sha256(body).hexdigest()
            if actual != blob.version_hash:
                raise ValueError()
        except Exception:
            raise SourceRetrievalError("evidence_corrupt", document_index=document_index) from None
        if len(body) > MAX_DOCUMENT_BYTES:
            raise SourceRetrievalError("limit_exceeded", document_index=document_index)
        if actual != document_pin:
            raise SourceRetrievalError("evidence_pin_mismatch", document_index=document_index)
        new_total = self._total_body_bytes + len(body)
        if new_total > MAX_CACHED_BODY_BYTES:
            raise SourceRetrievalError("limit_exceeded", document_index=document_index)
        entry = _CachedDoc(
            key=key, session_id=session_id, body=body, document_sha256=actual
        )
        self._cache[key] = entry
        self._order.append(key)
        self._doc_index[key] = document_index
        self._total_body_bytes = new_total
        return entry

    def read(self, key: str) -> typing.Optional[Blob]:
        if key not in self._allowed:
            raise SourceRetrievalError("operational_error") from None
        if key not in self._cache:
            raise SourceRetrievalError("operational_error") from None
        doc = self._cache[key]
        return Blob(body=doc.body, version_hash=doc.document_sha256)

    def recheck_all(self) -> None:
        for key in self._order:
            doc_index = self._doc_index[key]
            entry = self._cache[key]
            self._check_deadline()
            blob = self._backend_read(key)
            if blob is None:
                raise SourceRetrievalError("snapshot_changed", document_index=doc_index) from None
            try:
                if not isinstance(blob.body, (bytes, bytearray)):
                    raise TypeError()
                body = bytes(blob.body)
                actual = hashlib.sha256(body).hexdigest()
                if actual != blob.version_hash:
                    raise ValueError()
            except Exception:
                raise SourceRetrievalError("snapshot_changed", document_index=doc_index) from None
            if body != entry.body or actual != entry.document_sha256:
                raise SourceRetrievalError("snapshot_changed", document_index=doc_index) from None


def _map_raw_session_error(exc: RawSessionError, *, ref_index: int) -> SourceRetrievalError:
    reason = exc.reason
    if reason in ("secret_blocked", "source_rejected"):
        return SourceRetrievalError("content_blocked", ref_index=ref_index)
    if reason in ("corrupt_raw_document", "hash_mismatch", "invalid_captured_timestamp"):
        return SourceRetrievalError("evidence_corrupt", ref_index=ref_index)
    if reason == "document_too_large":
        return SourceRetrievalError("limit_exceeded", ref_index=ref_index)
    return SourceRetrievalError("operational_error", ref_index=ref_index)


@dataclass(frozen=True)
class _LoadedRef:
    ref: _ManifestRef
    captured: str
    source_text: str
    source_bytes: bytes


def _load_ref_evidence(
    cache: _RetrievalReadCache,
    project: str,
    ref: _ManifestRef,
    ref_index: int,
    document_index: int,
    seen_ops: set[tuple[str, str]],
    decoded_total: int,
    deadline: float,
    cp_budget: list[int],
) -> tuple[_LoadedRef, int]:
    key = raw_session_store_key(project, ref.session_id)
    document = cache.ensure_loaded(key, document_index)
    try:
        parsed = parse_raw_document(document.body, project=project, session_id=ref.session_id)
    except RawSessionError as exc:
        raise _map_raw_session_error(exc, ref_index=ref_index) from None
    except Exception:
        raise SourceRetrievalError("operational_error", ref_index=ref_index) from None
    hit = next(((header, payload) for header, payload in parsed.records if header["op"] == ref.operation_id), None)
    if hit is None:
        raise SourceRetrievalError("evidence_missing", ref_index=ref_index)
    header, source_bytes = hit
    if header["sha256"] != ref.source_sha256:
        raise SourceRetrievalError("evidence_pin_mismatch", ref_index=ref_index)
    if record_id_for_header(header) != ref.record_id:
        raise SourceRetrievalError("evidence_pin_mismatch", ref_index=ref_index)
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise SourceRetrievalError("evidence_corrupt", ref_index=ref_index)
    _gate_scan_bytes(key, source_bytes)
    op_key = (ref.session_id, ref.operation_id)
    new_total = decoded_total
    if op_key not in seen_ops:
        new_total += len(source_bytes)
        if new_total > MAX_DECODED_SOURCE_BYTES:
            raise SourceRetrievalError("limit_exceeded", ref_index=ref_index)
        seen_ops.add(op_key)
    cp_budget[0] += len(source_text)
    if cp_budget[0] >= DEADLINE_CHECK_EVERY_CP:
        cp_budget[0] = 0
        if time.monotonic() > deadline:
            raise SourceRetrievalError("budget_exceeded")
    return (
        _LoadedRef(
            ref=ref,
            captured=header["captured"],
            source_text=source_text,
            source_bytes=source_bytes,
        ),
        new_total,
    )


def _score_source(
    source_text: str,
    query_tokens: list[str],
    deadline: float,
    cp_budget: list[int],
) -> tuple[int, list[str], int, int]:
    query_set = set(query_tokens)
    seen_hits: set[str] = set()
    earliest_b0: typing.Optional[int] = None
    earliest_b1: typing.Optional[int] = None
    for c0, c1, tok in _iter_alnum_token_spans(source_text, deadline):
        folded = tok.casefold()
        if folded not in query_set:
            continue
        seen_hits.add(folded)
        if earliest_b0 is None:
            earliest_b0, earliest_b1 = _char_span_to_byte_span(source_text, c0, c1)
    if not seen_hits:
        return 0, [], 0, 0
    matched_ordered = [qt for qt in query_tokens if qt in seen_hits]
    assert earliest_b0 is not None and earliest_b1 is not None
    return len(seen_hits), matched_ordered, earliest_b0, earliest_b1


def _build_excerpt(source_bytes: bytes, m0: int, m1: int) -> tuple[str, int, int]:
    if m1 - m0 > MAX_EXCERPT_BYTES:
        raise SourceRetrievalError("limit_exceeded")
    n = len(source_bytes)
    start = max(0, m0 - EXCERPT_PREFIX_BYTES)
    start = _next_utf8_boundary(source_bytes, start)
    end = min(n, start + MAX_EXCERPT_BYTES)
    end = _prev_utf8_boundary(source_bytes, end)
    if end < m1:
        end = m1
        start = max(0, end - MAX_EXCERPT_BYTES)
        start = _next_utf8_boundary(source_bytes, start)
    if not (start <= m0 < m1 <= end):
        raise SourceRetrievalError("operational_error") from None
    chunk = source_bytes[start:end]
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        raise SourceRetrievalError("operational_error") from None
    if not text:
        raise SourceRetrievalError("operational_error") from None
    return text, start, end


def retrieve_sources(
    backend: StoreBackend,
    project: str,
    manifest_bytes: bytes,
    expected_manifest_sha256: str,
    query: str,
    *,
    top_k: int = 5,
) -> dict:
    try:
        return _retrieve_sources_impl(
            backend, project, manifest_bytes, expected_manifest_sha256, query, top_k=top_k
        )
    except SourceRetrievalError:
        raise
    except Exception:
        raise SourceRetrievalError("operational_error") from None


def _retrieve_sources_impl(
    backend: StoreBackend,
    project: str,
    manifest_bytes: bytes,
    expected_manifest_sha256: str,
    query: str,
    *,
    top_k: int,
) -> dict:
    deadline = time.monotonic() + BUDGET_SECONDS
    cp_budget = [0]

    if not valid_id(project):
        raise SourceRetrievalError("invalid_request")
    top_k = _validate_top_k(top_k)
    query = _validate_query(query)
    pin = _parse_manifest_hash(expected_manifest_sha256)
    if not isinstance(manifest_bytes, bytes):
        raise SourceRetrievalError("invalid_request")
    manifest_bytes = bytes(manifest_bytes)
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise SourceRetrievalError("invalid_request")
    actual_manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_hash != pin:
        raise SourceRetrievalError("manifest_mismatch")
    manifest_obj = _parse_manifest_json(manifest_bytes)
    parsed = _validate_manifest_obj(manifest_obj, project)
    scan_key = raw_session_store_key(project, parsed.refs[0].session_id)
    _gate_scan_text(scan_key, query)
    _scan_json_strings(scan_key, manifest_obj)
    query_tokens = _prepare_query_tokens(query)

    session_order: list[str] = []
    session_pin: dict[str, str] = {}
    for ref in parsed.refs:
        if ref.session_id not in session_pin:
            session_order.append(ref.session_id)
            session_pin[ref.session_id] = ref.document_sha256
    allowed = frozenset(raw_session_store_key(project, sid) for sid in session_order)
    session_to_doc_index = {sid: i for i, sid in enumerate(session_order)}

    cache = _RetrievalReadCache(
        backend,
        project=project,
        allowed_keys=allowed,
        session_pins=session_pin,
        deadline=deadline,
    )

    loaded: list[_LoadedRef] = []
    decoded_total = 0
    seen_ops: set[tuple[str, str]] = set()
    for ref_index, ref in enumerate(parsed.refs):
        doc_index = session_to_doc_index[ref.session_id]
        key = raw_session_store_key(project, ref.session_id)
        cache.ensure_loaded(key, doc_index)
        item, decoded_total = _load_ref_evidence(
            cache,
            project,
            ref,
            ref_index,
            doc_index,
            seen_ops,
            decoded_total,
            deadline,
            cp_budget,
        )
        loaded.append(item)

    candidates: list[dict] = []
    for item in loaded:
        score, matched, b0, b1 = _score_source(
            item.source_text, query_tokens, deadline, cp_budget
        )
        if score < 1:
            continue
        excerpt, byte_start, byte_end = _build_excerpt(item.source_bytes, b0, b1)
        candidates.append(
            {
                "ref": item.ref,
                "captured": item.captured,
                "score": score,
                "matched_query_tokens": matched,
                "byte_start": byte_start,
                "byte_end": byte_end,
                "excerpt_utf8": excerpt,
            }
        )

    candidates.sort(
        key=lambda c: (
            -c["score"],
            c["ref"].ref_id,
            c["ref"].session_id,
            c["ref"].operation_id,
        )
    )
    matching_total = len(candidates)
    hits_out: list[dict] = []
    excerpt_bytes = 0
    for rank, cand in enumerate(candidates[:top_k], start=1):
        ref = cand["ref"]
        excerpt_bytes += len(cand["excerpt_utf8"].encode("utf-8"))
        if excerpt_bytes > MAX_EXCERPT_AGGREGATE_BYTES:
            raise SourceRetrievalError("limit_exceeded")
        hits_out.append(
            {
                "rank": rank,
                "ref_id": ref.ref_id,
                "session_id": ref.session_id,
                "operation_id": ref.operation_id,
                "document_sha256": ref.document_sha256,
                "record_id": ref.record_id,
                "source_sha256": ref.source_sha256,
                "captured": cand["captured"],
                "matched_query_tokens": cand["matched_query_tokens"],
                "score": cand["score"],
                "byte_start": cand["byte_start"],
                "byte_end": cand["byte_end"],
                "excerpt_utf8": cand["excerpt_utf8"],
            }
        )

    cache.recheck_all()

    documents = [
        {"session_id": doc.session_id, "document_sha256": doc.document_sha256}
        for doc in cache.cached_docs
    ]
    outcome = "matches" if matching_total else "no_match"
    payload = {
        "schema_version": 1,
        "kind": "knokeep_lexical_source_retrieval",
        "project": project,
        "manifest_id": parsed.manifest_id,
        "manifest_sha256": pin,
        "query_tokens": query_tokens,
        "outcome": outcome,
        "hits": hits_out,
        "documents": documents,
        "coverage": {
            "kind": "declared_manifest_only",
            "refs_declared": len(parsed.refs),
            "refs_checked": len(parsed.refs),
            "matching_refs_total": matching_total,
            "hits_returned": len(hits_out),
            "truncated": matching_total > len(hits_out),
        },
        "qualifications": dict(_QUALIFICATIONS),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("utf-8")) > MAX_OUTPUT_JSON_BYTES:
        raise SourceRetrievalError("limit_exceeded")
    if time.monotonic() > deadline:
        raise SourceRetrievalError("budget_exceeded")
    return payload


__all__ = ["SourceRetrievalError", "retrieve_sources"]
