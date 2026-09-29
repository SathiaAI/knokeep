"""Bounded structured knowledge revision journal (one store blob per knowledge id)."""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import random
import re
import time
import typing
from dataclasses import dataclass

from store import gate
from store.backend import BackendBusyError, BackendCorruptionError, StoreBackend
from store.context import create_ctx, overwrite_ctx
from store.types import ERROR, OK, STALE, EXISTS, ErrorKind, sha256_hex, WriteResult

from .identifiers import valid_id
from .knowledge_validation import (
    KnowledgeValidationError,
    parse_proposal_bytes,
    validate_proposal,
    validate_proposal_structure,
    validation_exit_code,
)
from .raw_sessions import validate_captured_timestamp, utc_captured_now

MAGIC = b"KNOKEEP-KNOWLEDGE/1\n"
DOC_TYPE = "journal"
MAX_PROPOSAL_BYTES = 64 * 1024
MAX_HEADER_BYTES = 2048
MAX_RECORDS = 32
MAX_DOCUMENT_BYTES = 3 * 1024 * 1024
MAX_ATTEMPTS = 20
RETRY_BUDGET_S = 10.0
_LEASE_TTL_S = 30.0
_MIN_LEASE_BEFORE_PERSIST_S = 2.0

_HEADER_FIELDS = frozenset(
    {
        "v",
        "project",
        "knowledge_id",
        "operation_id",
        "client",
        "revision",
        "saved_at",
        "proposal_length",
        "proposal_sha256",
        "previous_record_id",
        "base_document_sha256",
    }
)
_SAVED_AT_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_INT_DIGITS = 20

_EXIT2_REASONS = frozenset(
    {
        "invalid_project_id",
        "invalid_knowledge_id",
        "invalid_client",
        "invalid_operation_id",
        "invalid_argument",
        "secret_blocked",
        "invalid_expect_hash",
        "missing_project",
        "missing_knowledge_id",
        "missing_operation_id",
        "missing_proposal_file",
        "proposal_file_error",
        "proposal_too_large",
        "invalid_proposal_encoding",
        "precondition_conflict",
        "operation_id_conflict",
        "document_full",
        "source_reference_failed",
        "proposal_too_large",
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
        "invalid_source_sha256",
        "invalid_record_id",
        "invalid_byte_offsets",
        "invalid_quote",
        "citations_total_exceeded",
        "distinct_sources_exceeded",
    }
)

_Q17_REF_DETAIL_KEYS = frozenset({"claim_index", "citation_index"})


class KnowledgeRevisionError(Exception):
    def __init__(self, reason: str, **detail: object) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


def revision_exit_code(exc: KnowledgeRevisionError) -> int:
    if exc.reason == "source_reference_failed":
        return 2
    if exc.reason in _EXIT2_REASONS:
        return 2
    return 1


def knowledge_store_key(project: str, knowledge_id: str) -> str:
    if not valid_id(project):
        raise KnowledgeRevisionError("invalid_project_id")
    if not valid_id(knowledge_id):
        raise KnowledgeRevisionError("invalid_knowledge_id")
    return f"{project}/knowledge/{knowledge_id}"


def parse_expect_hash_literal(value: str) -> typing.Optional[str]:
    if value == "none":
        return None
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise KnowledgeRevisionError("invalid_expect_hash")
    return value


def _canonical_header_bytes(header: dict) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def record_id_for_header(header: dict) -> str:
    return hashlib.sha256(_canonical_header_bytes(header)).hexdigest()


def _gate_scan_bytes(key: str, data: bytes) -> None:
    err = gate.validate_write(key, data, doc_type=DOC_TYPE, expected_hash=None)
    if err is None:
        return
    if err.kind == ErrorKind.SECRET_BLOCKED:
        raise KnowledgeRevisionError("secret_blocked", labels=list(err.labels or ()))
    if err.kind == ErrorKind.INVALID_ARGUMENT:
        raise KnowledgeRevisionError("invalid_argument")
    raise KnowledgeRevisionError("gate_rejected", kind=err.kind.value)


def _gate_scan_text(key: str, text: str) -> None:
    _gate_scan_bytes(key, text.encode("utf-8"))


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


def _map_proposal_input_error(exc: KnowledgeValidationError) -> KnowledgeRevisionError:
    code = exc.code
    if validation_exit_code(exc) == 2:
        if code in _EXIT2_REASONS:
            return KnowledgeRevisionError(code)
        return KnowledgeRevisionError("invalid_argument")
    return KnowledgeRevisionError("operational_error", kind="proposal_validation")


def _validate_proposal_gate_and_schema(key: str, proposal: bytes) -> None:
    if not isinstance(proposal, bytes):
        raise KnowledgeRevisionError("invalid_proposal_encoding")
    if len(proposal) > MAX_PROPOSAL_BYTES:
        raise KnowledgeRevisionError("proposal_too_large")
    _gate_scan_bytes(key, proposal)
    try:
        obj = parse_proposal_bytes(proposal)
        validate_proposal_structure(obj)
    except KnowledgeValidationError as exc:
        raise _map_proposal_input_error(exc) from exc
    _scan_json_strings(key, obj)


def _parse_bounded_int(text: str) -> int:
    if not text or len(text) > _MAX_JSON_INT_DIGITS:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="bad_int")
    try:
        return int(text, 10)
    except ValueError:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="bad_int")


def _parse_header_line(
    line: bytes, *, project: str, knowledge_id: str, expected_revision: int
) -> dict:
    if not line or line[-1:] != b"\n":
        raise KnowledgeRevisionError("knowledge_corrupt", detail="header_missing_lf")
    body = line[:-1]
    if len(body) > MAX_HEADER_BYTES:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="header_too_long")
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="header_not_ascii")

    def hook(raw_pairs: list) -> dict:
        keys = [k for k, _ in raw_pairs]
        if len(keys) != len(set(keys)):
            raise KnowledgeRevisionError("knowledge_corrupt", detail="duplicate_header_field")
        if not all(isinstance(k, str) for k in keys):
            raise KnowledgeRevisionError("knowledge_corrupt", detail="bad_header_key_type")
        return dict(raw_pairs)

    try:
        obj = json.loads(
            text,
            object_pairs_hook=hook,
            parse_int=_parse_bounded_int,
        )
    except KnowledgeRevisionError:
        raise
    except json.JSONDecodeError:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="invalid_json")
    except (TypeError, ValueError):
        raise KnowledgeRevisionError("knowledge_corrupt", detail="invalid_json_root")
    if not isinstance(obj, dict):
        raise KnowledgeRevisionError("knowledge_corrupt", detail="invalid_json_root")
    if _canonical_header_bytes(obj) != body:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="noncanonical_json")
    if set(obj) != _HEADER_FIELDS:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="unknown_or_missing_field")
    v = obj["v"]
    if type(v) is not int or isinstance(v, bool) or v != 1:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="bad_version")
    rev = obj["revision"]
    if type(rev) is not int or isinstance(rev, bool) or rev != expected_revision:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="bad_revision")
    plen = obj["proposal_length"]
    if (
        type(plen) is not int
        or isinstance(plen, bool)
        or plen < 0
        or plen > MAX_PROPOSAL_BYTES
    ):
        raise KnowledgeRevisionError("knowledge_corrupt", detail="bad_proposal_length")
    proj = obj["project"]
    kid = obj["knowledge_id"]
    if not isinstance(proj, str) or not valid_id(proj) or proj != project:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="project_mismatch")
    if not isinstance(kid, str) or not valid_id(kid) or kid != knowledge_id:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="knowledge_mismatch")
    for fld in ("operation_id", "client"):
        val = obj[fld]
        if not isinstance(val, str) or not valid_id(val):
            raise KnowledgeRevisionError("knowledge_corrupt", detail=f"bad_{fld}")
    saved = obj["saved_at"]
    try:
        validate_captured_timestamp(saved)
    except Exception:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="bad_saved_at")
    psh = obj["proposal_sha256"]
    if not isinstance(psh, str) or not _HASH_RE.fullmatch(psh):
        raise KnowledgeRevisionError("knowledge_corrupt", detail="bad_proposal_sha256")
    prev = obj["previous_record_id"]
    base = obj["base_document_sha256"]
    if expected_revision == 1:
        if prev is not None or base is not None:
            raise KnowledgeRevisionError("knowledge_corrupt", detail="bad_lineage_first")
    else:
        if not isinstance(prev, str) or not _HASH_RE.fullmatch(prev):
            raise KnowledgeRevisionError("knowledge_corrupt", detail="bad_previous_record_id")
        if not isinstance(base, str) or not _HASH_RE.fullmatch(base):
            raise KnowledgeRevisionError("knowledge_corrupt", detail="bad_base_document_sha256")
    return obj


@dataclass
class _ParsedDoc:
    records: list[tuple[dict, bytes]]
    body: bytes


def _bounded_body(data: bytes) -> None:
    if len(data) > MAX_DOCUMENT_BYTES:
        raise KnowledgeRevisionError("document_too_large", length=len(data), max=MAX_DOCUMENT_BYTES)


def parse_knowledge_document(
    data: bytes, *, project: str, knowledge_id: str, strict_eof: bool = True
) -> _ParsedDoc:
    _bounded_body(data)
    if not data.startswith(MAGIC):
        raise KnowledgeRevisionError("knowledge_corrupt", detail="bad_magic")
    if data == MAGIC:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="magic_only")
    pos = len(MAGIC)
    records: list[tuple[dict, bytes]] = []
    seen_ops: set[str] = set()
    prefix_end = len(MAGIC)
    rev = 1
    while pos < len(data):
        line_end = data.find(b"\n", pos)
        if line_end < 0:
            raise KnowledgeRevisionError("knowledge_corrupt", detail="truncated_header")
        header_line = data[pos : line_end + 1]
        if rev == 1:
            base_expected = None
        else:
            base_expected = hashlib.sha256(data[:pos]).hexdigest()
        header = _parse_header_line(
            header_line, project=project, knowledge_id=knowledge_id, expected_revision=rev
        )
        if rev > 1:
            assert base_expected is not None
            if header["base_document_sha256"] != base_expected:
                raise KnowledgeRevisionError("knowledge_corrupt", detail="base_hash_mismatch")
            prev_id = record_id_for_header(records[-1][0])
            if header["previous_record_id"] != prev_id:
                raise KnowledgeRevisionError("knowledge_corrupt", detail="parent_mismatch")
        plen = header["proposal_length"]
        payload_start = line_end + 1
        payload_end = payload_start + plen
        if payload_end > len(data):
            raise KnowledgeRevisionError("knowledge_corrupt", detail="truncated_payload")
        payload = data[payload_start:payload_end]
        if hashlib.sha256(payload).hexdigest() != header["proposal_sha256"]:
            raise KnowledgeRevisionError("knowledge_corrupt", detail="proposal_hash_mismatch")
        if payload_end == len(data):
            raise KnowledgeRevisionError("knowledge_corrupt", detail="missing_payload_lf")
        if data[payload_end : payload_end + 1] != b"\n":
            raise KnowledgeRevisionError("knowledge_corrupt", detail="missing_payload_lf")
        op = header["operation_id"]
        if op in seen_ops:
            raise KnowledgeRevisionError("knowledge_corrupt", detail="duplicate_operation_id")
        seen_ops.add(op)
        records.append((header, payload))
        pos = payload_end + 1
        rev += 1
        if len(records) >= MAX_RECORDS and pos < len(data):
            raise KnowledgeRevisionError("knowledge_corrupt", detail="too_many_records")
        prefix_end = pos
    if strict_eof and pos != len(data):
        raise KnowledgeRevisionError("knowledge_corrupt", detail="trailing_data")
    return _ParsedDoc(records=records, body=data)


def _build_document(records: list[tuple[dict, bytes]]) -> bytes:
    if len(records) > MAX_RECORDS:
        raise KnowledgeRevisionError("document_full")
    out = bytearray(MAGIC)
    for header, payload in records:
        hb = _canonical_header_bytes(header)
        if len(hb) > MAX_HEADER_BYTES:
            raise KnowledgeRevisionError("knowledge_corrupt", detail="header_too_long")
        out.extend(hb)
        out.extend(b"\n")
        out.extend(payload)
        out.extend(b"\n")
    if len(out) > MAX_DOCUMENT_BYTES:
        raise KnowledgeRevisionError("document_too_large")
    return bytes(out)


def _verify_blob_hash(blob) -> bytes:
    if blob is None:
        raise KnowledgeRevisionError("internal", detail="missing_blob")
    _bounded_body(blob.body)
    actual = sha256_hex(blob.body)
    if actual != blob.version_hash:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="version_hash_mismatch")
    return blob.body


def _guard_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise KnowledgeRevisionError("operational_error", kind="deadline_exceeded")


def _backend_read(backend: StoreBackend, key: str, deadline: float):
    _guard_deadline(deadline)
    try:
        blob = backend.read(key)
    except BackendBusyError:
        raise KnowledgeRevisionError("operational_error", kind="backend_busy")
    except BackendCorruptionError:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="backend_corruption")
    except OSError:
        raise KnowledgeRevisionError("operational_error", kind="backend_read")
    except RuntimeError:
        raise KnowledgeRevisionError("operational_error", kind="runtime")
    _guard_deadline(deadline)
    return blob


def _unlock_quiet(backend: StoreBackend, lease) -> None:
    if lease is None:
        return
    try:
        backend.unlock(lease)
    except Exception:
        pass


def _lease_remaining_s(lease) -> float:
    return lease.expiry_epoch - time.time()


def _persist(
    backend: StoreBackend, key: str, raw_bytes: bytes, expected_hash: typing.Optional[str], lease
) -> WriteResult:
    if expected_hash is None:
        ctx = create_ctx()
    else:
        ctx = overwrite_ctx(expected_hash, lease)
    return gate.persist(backend, key, raw_bytes, ctx=ctx, doc_type=DOC_TYPE)


def _find_op(records: list[tuple[dict, bytes]], operation_id: str):
    for header, payload in records:
        if header["operation_id"] == operation_id:
            return header, payload
    return None


def _binding_match(
    header: dict,
    payload: bytes,
    *,
    client: str,
    proposal_hash: str,
    proposal_len: int,
    base_document_sha256: typing.Optional[str],
) -> bool:
    if header["client"] != client:
        return False
    if header["proposal_sha256"] != proposal_hash or header["proposal_length"] != proposal_len:
        return False
    base = header["base_document_sha256"]
    if base_document_sha256 is None:
        if base is not None:
            return False
    elif base != base_document_sha256:
        return False
    return True


@dataclass(frozen=True)
class KnowledgeSaveReceipt:
    operation_id: str
    record_id: str
    revision: int
    proposal_sha256: str
    proposal_length: int
    saved_at: str
    base_document_sha256: typing.Optional[str]
    current_document_hash: str
    duplicate: bool
    source_checks_executed_this_call: bool


@dataclass(frozen=True)
class KnowledgeReadResult:
    found: bool
    operation_id: str
    proposal_sha256: typing.Optional[str] = None
    proposal_length: typing.Optional[int] = None
    record_id: typing.Optional[str] = None
    revision: typing.Optional[int] = None
    previous_record_id: typing.Optional[str] = None
    base_document_sha256: typing.Optional[str] = None
    saved_at: typing.Optional[str] = None
    current_document_hash: typing.Optional[str] = None
    proposal_base64: typing.Optional[str] = None


def _receipt(
    header: dict,
    payload: bytes,
    doc_hash: str,
    *,
    duplicate: bool,
    source_checks: bool,
    base_at_save: typing.Optional[str],
) -> KnowledgeSaveReceipt:
    return KnowledgeSaveReceipt(
        operation_id=header["operation_id"],
        record_id=record_id_for_header(header),
        revision=header["revision"],
        proposal_sha256=header["proposal_sha256"],
        proposal_length=header["proposal_length"],
        saved_at=header["saved_at"],
        base_document_sha256=base_at_save,
        current_document_hash=doc_hash,
        duplicate=duplicate,
        source_checks_executed_this_call=source_checks,
    )


def _load_binding_receipt(
    backend: StoreBackend,
    key: str,
    project: str,
    knowledge_id: str,
    operation_id: str,
    client: str,
    proposal: bytes,
    base_document_sha256: typing.Optional[str],
) -> typing.Optional[KnowledgeSaveReceipt]:
    blob = backend.read(key)
    if blob is None:
        return None
    body = _verify_blob_hash(blob)
    parsed = parse_knowledge_document(body, project=project, knowledge_id=knowledge_id)
    hit = _find_op(parsed.records, operation_id)
    if hit is None:
        return None
    header, payload = hit
    ph = hashlib.sha256(proposal).hexdigest()
    stored_base = header["base_document_sha256"]
    if payload != proposal:
        raise KnowledgeRevisionError("operation_id_conflict")
    if not _binding_match(
        header,
        payload,
        client=client,
        proposal_hash=ph,
        proposal_len=len(proposal),
        base_document_sha256=base_document_sha256,
    ):
        raise KnowledgeRevisionError("operation_id_conflict")
    base_at = header["base_document_sha256"]
    return _receipt(
        header,
        payload,
        blob.version_hash,
        duplicate=True,
        source_checks=False,
        base_at_save=base_at,
    )


def _sleep_bounded(deadline: float, busy_waits: int) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    delay = random.uniform(0.01, 0.05) * min(busy_waits, 10)
    time.sleep(min(delay, remaining))


def validate_save_arguments(
    project: str,
    knowledge_id: str,
    client: str,
    operation_id: str,
    proposal: bytes,
    expect_hash: str,
) -> str:
    key = knowledge_store_key(project, knowledge_id)
    if not valid_id(client):
        raise KnowledgeRevisionError("invalid_client")
    if not valid_id(operation_id):
        raise KnowledgeRevisionError("invalid_operation_id")
    _gate_scan_text(key, client)
    _gate_scan_text(key, operation_id)
    parse_expect_hash_literal(expect_hash)
    _validate_proposal_gate_and_schema(key, proposal)
    return key


def _map_q17_read_error(exc: KnowledgeValidationError) -> KnowledgeRevisionError:
    code = exc.code
    if validation_exit_code(exc) == 2:
        detail: dict[str, object] = {"q17_reason": code}
        for k in _Q17_REF_DETAIL_KEYS:
            v = exc.detail.get(k)
            if type(v) is int and not isinstance(v, bool):
                detail[k] = v
        return KnowledgeRevisionError("source_reference_failed", **detail)
    return KnowledgeRevisionError("source_backend_error", q17_reason=code)


def _verify_readback_receipt(
    backend: StoreBackend,
    key: str,
    project: str,
    knowledge_id: str,
    operation_id: str,
    client: str,
    proposal: bytes,
    proposal_hash: str,
    proposal_len: int,
    caller_base: typing.Optional[str],
    expected_new_hash: str,
    q17_ran: bool,
    deadline: float,
) -> KnowledgeSaveReceipt:
    try:
        rb = _backend_read(backend, key, deadline)
    except Exception:
        raise KnowledgeRevisionError("outcome_uncertain", kind="readback_failed")
    if rb is None:
        raise KnowledgeRevisionError("outcome_uncertain", kind="readback_missing")
    try:
        rb_body = _verify_blob_hash(rb)
        parsed_rb = parse_knowledge_document(rb_body, project=project, knowledge_id=knowledge_id)
        hit = _find_op(parsed_rb.records, operation_id)
        if hit is None:
            raise KnowledgeRevisionError("outcome_uncertain", kind="binding_missing")
        eh, ep = hit
        if ep != proposal or not _binding_match(
            eh,
            ep,
            client=client,
            proposal_hash=proposal_hash,
            proposal_len=proposal_len,
            base_document_sha256=caller_base,
        ):
            raise KnowledgeRevisionError("outcome_uncertain", kind="binding_mismatch")
        return _receipt(
            eh,
            ep,
            rb.version_hash,
            duplicate=False,
            source_checks=q17_ran,
            base_at_save=caller_base,
        )
    except KnowledgeRevisionError as exc:
        if exc.reason == "knowledge_corrupt":
            raise KnowledgeRevisionError("outcome_uncertain", kind="readback_corrupt") from exc
        raise
    except Exception:
        raise KnowledgeRevisionError("outcome_uncertain", kind="readback_failed") from None


def save_knowledge_revision(
    backend: StoreBackend,
    project: str,
    knowledge_id: str,
    client: str,
    operation_id: str,
    proposal: bytes,
    *,
    expect_hash: str,
    saved_at: typing.Optional[str] = None,
) -> KnowledgeSaveReceipt:
    try:
        return _save_knowledge_revision_impl(
            backend,
            project,
            knowledge_id,
            client,
            operation_id,
            proposal,
            expect_hash=expect_hash,
            saved_at=saved_at,
        )
    except KnowledgeRevisionError:
        raise
    except Exception:
        raise KnowledgeRevisionError("operational_error", kind="unexpected")


def _save_knowledge_revision_impl(
    backend: StoreBackend,
    project: str,
    knowledge_id: str,
    client: str,
    operation_id: str,
    proposal: bytes,
    *,
    expect_hash: str,
    saved_at: typing.Optional[str],
) -> KnowledgeSaveReceipt:
    key = validate_save_arguments(project, knowledge_id, client, operation_id, proposal, expect_hash)
    caller_base = parse_expect_hash_literal(expect_hash)
    proposal = bytes(proposal)
    proposal_hash = hashlib.sha256(proposal).hexdigest()
    proposal_len = len(proposal)
    if saved_at is None:
        saved_at = utc_captured_now()
    else:
        try:
            validate_captured_timestamp(saved_at)
        except Exception:
            raise KnowledgeRevisionError("invalid_argument") from None

    deadline = time.monotonic() + RETRY_BUDGET_S
    attempt = 0
    busy_waits = 0
    q17_ran = False

    while attempt < MAX_ATTEMPTS:
        _guard_deadline(deadline)
        attempt += 1
        lease = None
        try:
            try:
                lease = backend.lock(key, ttl_s=_LEASE_TTL_S)
            except BackendBusyError:
                busy_waits += 1
                _sleep_bounded(deadline, busy_waits)
                continue
            _guard_deadline(deadline)

            blob = _backend_read(backend, key, deadline)
            if blob is None:
                records: list[tuple[dict, bytes]] = []
                expect = None
                base_for_header: typing.Optional[str] = None
                doc_hash: typing.Optional[str] = None
            else:
                body = _verify_blob_hash(blob)
                parsed = parse_knowledge_document(body, project=project, knowledge_id=knowledge_id)
                records = list(parsed.records)
                expect = blob.version_hash
                doc_hash = expect
                base_for_header = expect

            existing = _find_op(records, operation_id)
            if existing is not None:
                eh, ep = existing
                stored_base = eh["base_document_sha256"]
                if ep != proposal:
                    raise KnowledgeRevisionError("operation_id_conflict")
                if not _binding_match(
                    eh,
                    ep,
                    client=client,
                    proposal_hash=proposal_hash,
                    proposal_len=proposal_len,
                    base_document_sha256=caller_base,
                ):
                    raise KnowledgeRevisionError("operation_id_conflict")
                return _receipt(
                    eh,
                    ep,
                    doc_hash or sha256_hex(_build_document(records)),
                    duplicate=True,
                    source_checks=False,
                    base_at_save=stored_base,
                )

            if blob is None:
                if caller_base is not None:
                    raise KnowledgeRevisionError("precondition_conflict", detail="missing")
            else:
                if caller_base is None:
                    raise KnowledgeRevisionError("precondition_conflict", detail="exists")
                if caller_base != expect:
                    raise KnowledgeRevisionError("precondition_conflict", detail="hash_mismatch")

            if len(records) >= MAX_RECORDS:
                raise KnowledgeRevisionError("document_full")

            _guard_deadline(deadline)
            try:
                validate_proposal(backend, project, proposal)
            except KnowledgeValidationError as exc:
                raise _map_q17_read_error(exc) from exc
            q17_ran = True
            _guard_deadline(deadline)

            prev_id = None
            if records:
                prev_id = record_id_for_header(records[-1][0])
            revision = len(records) + 1
            header = {
                "v": 1,
                "project": project,
                "knowledge_id": knowledge_id,
                "operation_id": operation_id,
                "client": client,
                "revision": revision,
                "saved_at": saved_at,
                "proposal_length": proposal_len,
                "proposal_sha256": proposal_hash,
                "previous_record_id": prev_id,
                "base_document_sha256": base_for_header,
            }
            for fld in (
                header["operation_id"],
                header["client"],
                header["saved_at"],
                project,
                knowledge_id,
            ):
                _gate_scan_text(key, fld)
            if prev_id is not None:
                _gate_scan_text(key, prev_id)
            if base_for_header is not None:
                _gate_scan_text(key, base_for_header)
            _gate_scan_text(key, proposal_hash)

            new_records = records + [(header, proposal)]
            new_body = _build_document(new_records)
            err = gate.validate_write(key, new_body, doc_type=DOC_TYPE, expected_hash=expect)
            if err is not None:
                if err.kind == ErrorKind.SECRET_BLOCKED:
                    raise KnowledgeRevisionError("secret_blocked", labels=list(err.labels or ()))
                raise KnowledgeRevisionError("envelope_rejected", kind=err.kind.value)
            _guard_deadline(deadline)
            if _lease_remaining_s(lease) < _MIN_LEASE_BEFORE_PERSIST_S:
                raise KnowledgeRevisionError("operational_error", kind="lease_short")
            _guard_deadline(deadline)
            try:
                last_res = _persist(backend, key, new_body, expect, lease if expect else None)
            except Exception:
                # The port cannot certify whether a raised call committed.
                # Reconcile once and never issue another persist in this call.
                last_res = ERROR(ErrorKind.CONFLICT_UNKNOWN)
            if not isinstance(last_res, (OK, ERROR, STALE, EXISTS)):
                last_res = ERROR(ErrorKind.CONFLICT_UNKNOWN)

            if isinstance(last_res, OK):
                intended = last_res.new_hash
                if intended != sha256_hex(new_body):
                    raise KnowledgeRevisionError("outcome_uncertain", kind="wrong_new_hash")
                return _verify_readback_receipt(
                    backend,
                    key,
                    project,
                    knowledge_id,
                    operation_id,
                    client,
                    proposal,
                    proposal_hash,
                    proposal_len,
                    caller_base,
                    intended,
                    q17_ran,
                    deadline,
                )

            if isinstance(last_res, ERROR) and last_res.kind == ErrorKind.SECRET_BLOCKED:
                raise KnowledgeRevisionError("secret_blocked", labels=list(last_res.labels or ()))

            if isinstance(last_res, (STALE, EXISTS)):
                raise KnowledgeRevisionError("precondition_conflict", detail=type(last_res).__name__)

            if isinstance(last_res, ERROR) and last_res.kind in (
                ErrorKind.TIMEOUT_AFTER_COMMIT,
                ErrorKind.CONFLICT_UNKNOWN,
            ):
                try:
                    confirmed = _load_binding_receipt(
                        backend,
                        key,
                        project,
                        knowledge_id,
                        operation_id,
                        client,
                        proposal,
                        caller_base,
                    )
                except Exception:
                    raise KnowledgeRevisionError("outcome_uncertain", kind="readback_failed") from None
                if confirmed is not None:
                    return KnowledgeSaveReceipt(
                        operation_id=confirmed.operation_id,
                        record_id=confirmed.record_id,
                        revision=confirmed.revision,
                        proposal_sha256=confirmed.proposal_sha256,
                        proposal_length=confirmed.proposal_length,
                        saved_at=confirmed.saved_at,
                        base_document_sha256=confirmed.base_document_sha256,
                        current_document_hash=confirmed.current_document_hash,
                        duplicate=False,
                        source_checks_executed_this_call=q17_ran,
                    )
                raise KnowledgeRevisionError("outcome_uncertain", kind=last_res.kind.value)

            if isinstance(last_res, ERROR):
                raise KnowledgeRevisionError("write_error", kind=last_res.kind.value)

        finally:
            _unlock_quiet(backend, lease)

    raise KnowledgeRevisionError("save_retry_exhausted")


def read_knowledge_revision(
    backend: StoreBackend, project: str, knowledge_id: str, operation_id: str
) -> KnowledgeReadResult:
    try:
        return _read_knowledge_revision_impl(backend, project, knowledge_id, operation_id)
    except KnowledgeRevisionError:
        raise
    except Exception:
        raise KnowledgeRevisionError("operational_error", kind="unexpected")


def _read_knowledge_revision_impl(
    backend: StoreBackend, project: str, knowledge_id: str, operation_id: str
) -> KnowledgeReadResult:
    if not valid_id(project):
        raise KnowledgeRevisionError("invalid_project_id")
    if not valid_id(knowledge_id):
        raise KnowledgeRevisionError("invalid_knowledge_id")
    if not valid_id(operation_id):
        raise KnowledgeRevisionError("invalid_operation_id")
    key = knowledge_store_key(project, knowledge_id)
    try:
        blob = backend.read(key)
    except BackendCorruptionError:
        raise KnowledgeRevisionError("knowledge_corrupt", detail="backend_corruption")
    except BackendBusyError:
        raise KnowledgeRevisionError("operational_error", kind="backend_busy")
    except OSError:
        raise KnowledgeRevisionError("operational_error", kind="backend_read")
    except RuntimeError:
        raise KnowledgeRevisionError("operational_error", kind="runtime")
    if blob is None:
        return KnowledgeReadResult(found=False, operation_id=operation_id)
    try:
        body = _verify_blob_hash(blob)
        parsed = parse_knowledge_document(body, project=project, knowledge_id=knowledge_id)
    except KnowledgeRevisionError as exc:
        if exc.reason == "document_too_large":
            raise KnowledgeRevisionError("knowledge_corrupt", detail="document_too_large") from exc
        raise
    hit = _find_op(parsed.records, operation_id)
    if hit is None:
        return KnowledgeReadResult(found=False, operation_id=operation_id)
    header, payload = hit
    try:
        validate_proposal(backend, project, payload)
    except KnowledgeValidationError as exc:
        raise _map_q17_read_error(exc) from exc
    rid = record_id_for_header(header)
    return KnowledgeReadResult(
        found=True,
        operation_id=operation_id,
        proposal_sha256=header["proposal_sha256"],
        proposal_length=header["proposal_length"],
        record_id=rid,
        revision=header["revision"],
        previous_record_id=header["previous_record_id"],
        base_document_sha256=header["base_document_sha256"],
        saved_at=header["saved_at"],
        current_document_hash=blob.version_hash,
        proposal_base64=base64.standard_b64encode(payload).decode("ascii"),
    )


def read_disclaimers() -> dict:
    return {
        "semantic_support_verified": False,
        "source_authorship_verified": False,
        "authorization_verified": False,
        "project_completeness_verified": False,
        "repository_freshness_verified": False,
        "source_reads_non_atomic": True,
    }
