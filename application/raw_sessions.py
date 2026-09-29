"""Bounded raw-session capture: one store blob per session under raw-sessions/."""
from __future__ import annotations

import base64
import contextlib
import datetime
import hashlib
import json
import random
import re
import time
import typing
from dataclasses import dataclass

from store import gate
from store.backend import BackendBusyError, StoreBackend
from store.context import create_ctx, overwrite_ctx
from store.types import ERROR, OK, STALE, EXISTS, ErrorKind, sha256_hex, WriteResult

from .identifiers import valid_id

MAGIC = b"KNOKEEP-RAW/1\n"
DOC_TYPE = "journal"
MAX_SOURCE_BYTES = 1024 * 1024
MAX_SOURCE_READ = MAX_SOURCE_BYTES + 1
MAX_HEADER_BYTES = 1024
MAX_RECORDS = 256
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_ATTEMPTS = 20
RETRY_BUDGET_S = 10.0
_LEASE_TTL_S = 30.0

_HEADER_FIELDS = frozenset({"v", "project", "session", "op", "client", "captured", "len", "sha256"})
_CAPTURED_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class RawSessionError(Exception):
    def __init__(self, reason: str, **detail: object) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


@dataclass(frozen=True)
class RawAppendReceipt:
    operation_id: str
    source_sha256: str
    source_length: int
    captured: str
    record_id: str
    current_version_hash: str
    duplicate: bool


@dataclass(frozen=True)
class RawReadResult:
    found: bool
    operation_id: str
    source_sha256: typing.Optional[str] = None
    source_length: typing.Optional[int] = None
    captured: typing.Optional[str] = None
    record_id: typing.Optional[str] = None
    current_version_hash: typing.Optional[str] = None
    source_base64: typing.Optional[str] = None


def raw_session_store_key(project: str, session_id: str) -> str:
    if not valid_id(project):
        raise RawSessionError("invalid_project_id", value=project)
    if not valid_id(session_id):
        raise RawSessionError("invalid_session_id", value=session_id)
    return f"{project}/raw-sessions/{session_id}"


def utc_captured_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def validate_captured_timestamp(captured: object) -> str:
    if not isinstance(captured, str) or not _CAPTURED_RE.fullmatch(captured):
        raise RawSessionError("invalid_captured_timestamp", value=captured)
    try:
        datetime.datetime.strptime(captured, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        raise RawSessionError("invalid_captured_timestamp", value=captured)
    return captured


def _canonical_header_bytes(header: dict) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def record_id_for_header(header: dict) -> str:
    return hashlib.sha256(_canonical_header_bytes(header)).hexdigest()


def _validate_source_for_key(key: str, source: bytes) -> None:
    if not isinstance(source, (bytes, bytearray)):
        raise RawSessionError("invalid_source")
    source = bytes(source)
    if len(source) > MAX_SOURCE_BYTES:
        raise RawSessionError("source_too_large", length=len(source), max=MAX_SOURCE_BYTES)
    err = gate.validate_write(key, source, doc_type=DOC_TYPE, expected_hash=None)
    if err is not None:
        if err.kind == ErrorKind.SECRET_BLOCKED:
            raise RawSessionError("secret_blocked", labels=list(err.labels or ()))
        raise RawSessionError("source_rejected", kind=err.kind.value)


def _validate_envelope_for_key(key: str, blob: bytes, expected_hash: typing.Optional[str]) -> None:
    if len(blob) > MAX_DOCUMENT_BYTES:
        raise RawSessionError("document_too_large")
    err = gate.validate_write(key, blob, doc_type=DOC_TYPE, expected_hash=expected_hash)
    if err is not None:
        if err.kind == ErrorKind.SECRET_BLOCKED:
            raise RawSessionError("secret_blocked", labels=list(err.labels or ()))
        raise RawSessionError("envelope_rejected", kind=err.kind.value)


def _require_id_string(value: object, detail: str) -> str:
    if not isinstance(value, str) or not valid_id(value):
        raise RawSessionError("corrupt_raw_document", detail=detail)
    return value


def _parse_header_line(line: bytes, *, project: str, session_id: str) -> dict:
    if not line or line[-1:] != b"\n":
        raise RawSessionError("corrupt_raw_document", detail="header_missing_lf")
    body = line[:-1]
    if len(body) > MAX_HEADER_BYTES:
        raise RawSessionError("corrupt_raw_document", detail="header_too_long")
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError:
        raise RawSessionError("corrupt_raw_document", detail="header_not_ascii")

    def hook(raw_pairs: list) -> dict:
        keys = [k for k, _ in raw_pairs]
        if len(keys) != len(set(keys)):
            raise RawSessionError("corrupt_raw_document", detail="duplicate_header_field")
        if not all(isinstance(k, str) for k in keys):
            raise RawSessionError("corrupt_raw_document", detail="bad_header_key_type")
        return dict(raw_pairs)

    try:
        obj = json.loads(text, object_pairs_hook=hook)
    except RawSessionError:
        raise
    except json.JSONDecodeError:
        raise RawSessionError("corrupt_raw_document", detail="invalid_json")
    except (TypeError, ValueError):
        raise RawSessionError("corrupt_raw_document", detail="invalid_json_root")
    if not isinstance(obj, dict):
        raise RawSessionError("corrupt_raw_document", detail="invalid_json_root")
    if _canonical_header_bytes(obj) != body:
        raise RawSessionError("corrupt_raw_document", detail="noncanonical_json")
    if set(obj) != _HEADER_FIELDS:
        raise RawSessionError("corrupt_raw_document", detail="unknown_or_missing_field")
    v = obj["v"]
    if type(v) is not int or isinstance(v, bool) or v != 1:
        raise RawSessionError("corrupt_raw_document", detail="bad_version")
    ln = obj["len"]
    if type(ln) is not int or isinstance(ln, bool) or ln < 0 or ln > MAX_SOURCE_BYTES:
        raise RawSessionError("corrupt_raw_document", detail="bad_len")
    proj = _require_id_string(obj["project"], "project_mismatch")
    sess = _require_id_string(obj["session"], "session_mismatch")
    if proj != project or sess != session_id:
        raise RawSessionError("corrupt_raw_document", detail="scope_mismatch")
    _require_id_string(obj["op"], "bad_op")
    _require_id_string(obj["client"], "bad_client")
    cap = obj["captured"]
    validate_captured_timestamp(cap)
    sh = obj["sha256"]
    if not isinstance(sh, str) or not _HASH_RE.fullmatch(sh):
        raise RawSessionError("corrupt_raw_document", detail="bad_sha256")
    return obj


@dataclass
class _ParsedDoc:
    records: list[tuple[dict, bytes]]
    trailing: bytes


def _bounded_body(data: bytes) -> None:
    if len(data) > MAX_DOCUMENT_BYTES:
        raise RawSessionError("document_too_large", length=len(data), max=MAX_DOCUMENT_BYTES)


def parse_raw_document(
    data: bytes, *, project: str, session_id: str, strict_eof: bool = True
) -> _ParsedDoc:
    _bounded_body(data)
    if not data.startswith(MAGIC):
        raise RawSessionError("corrupt_raw_document", detail="bad_magic")
    pos = len(MAGIC)
    records: list[tuple[dict, bytes]] = []
    seen_ops: set[str] = set()
    while pos < len(data):
        line_end = data.find(b"\n", pos)
        if line_end < 0:
            raise RawSessionError("corrupt_raw_document", detail="truncated_header")
        header_line = data[pos : line_end + 1]
        header = _parse_header_line(header_line, project=project, session_id=session_id)
        ln = header["len"]
        payload_start = line_end + 1
        payload_end = payload_start + ln
        if payload_end > len(data) or payload_end > MAX_DOCUMENT_BYTES:
            raise RawSessionError("corrupt_raw_document", detail="truncated_payload")
        payload = data[payload_start:payload_end]
        if hashlib.sha256(payload).hexdigest() != header["sha256"]:
            raise RawSessionError("corrupt_raw_document", detail="payload_hash_mismatch")
        if payload_end == len(data):
            raise RawSessionError("corrupt_raw_document", detail="missing_payload_lf")
        if data[payload_end : payload_end + 1] != b"\n":
            raise RawSessionError("corrupt_raw_document", detail="missing_payload_lf")
        op = header["op"]
        if op in seen_ops:
            raise RawSessionError("corrupt_raw_document", detail="duplicate_operation_id")
        seen_ops.add(op)
        records.append((header, payload))
        pos = payload_end + 1
        if len(records) > MAX_RECORDS:
            raise RawSessionError("corrupt_raw_document", detail="too_many_records")
    if strict_eof and pos != len(data):
        raise RawSessionError("corrupt_raw_document", detail="trailing_data")
    return _ParsedDoc(records=records, trailing=b"")


def _build_document(records: list[tuple[dict, bytes]]) -> bytes:
    if len(records) > MAX_RECORDS:
        raise RawSessionError("too_many_records")
    out = bytearray(MAGIC)
    for header, payload in records:
        hb = _canonical_header_bytes(header)
        if len(hb) > MAX_HEADER_BYTES:
            raise RawSessionError("header_too_long")
        out.extend(hb)
        out.extend(b"\n")
        out.extend(payload)
        out.extend(b"\n")
    if len(out) > MAX_DOCUMENT_BYTES:
        raise RawSessionError("document_too_large")
    return bytes(out)


def _verify_blob_hash(blob) -> bytes:
    if blob is None:
        raise RawSessionError("internal", detail="missing_blob")
    _bounded_body(blob.body)
    actual = sha256_hex(blob.body)
    if actual != blob.version_hash:
        raise RawSessionError(
            "hash_mismatch",
            version_hash=blob.version_hash,
            actual_hash=actual,
        )
    return blob.body


@contextlib.contextmanager
def _hold(backend: StoreBackend, key: str):
    lease = backend.lock(key, ttl_s=_LEASE_TTL_S)
    try:
        yield lease
    finally:
        try:
            backend.unlock(lease)
        except Exception:
            pass


def _persist(
    backend: StoreBackend, key: str, raw_bytes: bytes, expected_hash: typing.Optional[str], lease
) -> WriteResult:
    if expected_hash is None:
        ctx = create_ctx()
    else:
        ctx = overwrite_ctx(expected_hash, lease)
    return gate.persist(backend, key, raw_bytes, ctx=ctx, doc_type=DOC_TYPE)


def _find_op_record(
    records: list[tuple[dict, bytes]], operation_id: str
) -> typing.Optional[tuple[dict, bytes]]:
    for header, payload in records:
        if header["op"] == operation_id:
            return header, payload
    return None


def _receipt_from_record(
    header: dict, payload: bytes, version_hash: str, duplicate: bool
) -> RawAppendReceipt:
    rid = record_id_for_header(header)
    return RawAppendReceipt(
        operation_id=header["op"],
        source_sha256=header["sha256"],
        source_length=header["len"],
        captured=header["captured"],
        record_id=rid,
        current_version_hash=version_hash,
        duplicate=duplicate,
    )


def _load_op_receipt(
    backend: StoreBackend,
    key: str,
    project: str,
    session_id: str,
    operation_id: str,
    client: str,
    source_hash: str,
    source_len: int,
    *,
    duplicate: bool,
) -> typing.Optional[RawAppendReceipt]:
    blob = backend.read(key)
    if blob is None:
        return None
    body = _verify_blob_hash(blob)
    parsed = parse_raw_document(body, project=project, session_id=session_id)
    existing = _find_op_record(parsed.records, operation_id)
    if existing is None:
        return None
    eh, ep = existing
    if eh["client"] != client or eh["sha256"] != source_hash or eh["len"] != source_len:
        raise RawSessionError("operation_id_conflict", operation_id=operation_id)
    return _receipt_from_record(eh, ep, blob.version_hash, duplicate)


def _sleep_bounded(deadline: float, busy_waits: int) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    delay = random.uniform(0.01, 0.05) * min(busy_waits, 10)
    time.sleep(min(delay, remaining))


def append_raw_session(
    backend: StoreBackend,
    project: str,
    session_id: str,
    client: str,
    operation_id: str,
    source: bytes,
    *,
    captured: typing.Optional[str] = None,
) -> RawAppendReceipt:
    if not valid_id(client):
        raise RawSessionError("invalid_client", value=client)
    if not valid_id(operation_id):
        raise RawSessionError("invalid_operation_id", value=operation_id)
    key = raw_session_store_key(project, session_id)
    _validate_source_for_key(key, source)
    if captured is None:
        captured = utc_captured_now()
    else:
        validate_captured_timestamp(captured)
    source = bytes(source)
    source_hash = hashlib.sha256(source).hexdigest()
    source_len = len(source)

    deadline = time.monotonic() + RETRY_BUDGET_S
    attempt = 0
    busy_waits = 0
    last_res: typing.Optional[WriteResult] = None
    intended_hash: typing.Optional[str] = None
    expect: typing.Optional[str] = None
    header: dict = {}

    while attempt < MAX_ATTEMPTS and time.monotonic() < deadline:
        attempt += 1
        new_body = b""
        try:
            with _hold(backend, key) as lease:
                blob = backend.read(key)
                if blob is None:
                    records: list[tuple[dict, bytes]] = []
                    expect = None
                else:
                    body = _verify_blob_hash(blob)
                    parsed = parse_raw_document(body, project=project, session_id=session_id)
                    records = list(parsed.records)
                    expect = blob.version_hash

                existing = _find_op_record(records, operation_id)
                if existing is not None:
                    eh, ep = existing
                    if eh["client"] != client or eh["sha256"] != source_hash or eh["len"] != source_len:
                        raise RawSessionError("operation_id_conflict", operation_id=operation_id)
                    return _receipt_from_record(eh, ep, blob.version_hash, True)

                header = {
                    "v": 1,
                    "project": project,
                    "session": session_id,
                    "op": operation_id,
                    "client": client,
                    "captured": captured,
                    "len": source_len,
                    "sha256": source_hash,
                }
                new_records = records + [(header, source)]
                new_body = _build_document(new_records)
                _validate_envelope_for_key(key, new_body, expect)
                intended_hash = sha256_hex(new_body)
                last_res = _persist(
                    backend,
                    key,
                    new_body,
                    expect,
                    lease if expect is not None else None,
                )
        except BackendBusyError:
            busy_waits += 1
            _sleep_bounded(deadline, busy_waits)
            continue

        if isinstance(last_res, OK):
            confirmed = _load_op_receipt(
                backend,
                key,
                project,
                session_id,
                operation_id,
                client,
                source_hash,
                source_len,
                duplicate=False,
            )
            if confirmed is None:
                raise RawSessionError("outcome_uncertain", kind="write_unverified")
            return RawAppendReceipt(
                operation_id=confirmed.operation_id,
                source_sha256=confirmed.source_sha256,
                source_length=confirmed.source_length,
                captured=confirmed.captured,
                record_id=confirmed.record_id,
                current_version_hash=last_res.new_hash,
                duplicate=False,
            )

        if isinstance(last_res, ERROR) and last_res.kind == ErrorKind.SECRET_BLOCKED:
            raise RawSessionError("secret_blocked", labels=list(last_res.labels or ()))

        if isinstance(last_res, (STALE, EXISTS)):
            continue

        if isinstance(last_res, ERROR) and last_res.kind in (
            ErrorKind.TIMEOUT_AFTER_COMMIT,
            ErrorKind.CONFLICT_UNKNOWN,
        ):
            if intended_hash is None:
                raise RawSessionError("outcome_uncertain", kind=last_res.kind.value)
            rec = gate.reconcile(
                backend,
                key,
                intended_new_hash=intended_hash,
                expected_hash=expect,
            )
            if isinstance(rec, OK):
                confirmed = _load_op_receipt(
                    backend,
                    key,
                    project,
                    session_id,
                    operation_id,
                    client,
                    source_hash,
                    source_len,
                    duplicate=False,
                )
                if confirmed is None:
                    raise RawSessionError("outcome_uncertain", kind="reconcile_unverified")
                return confirmed
            confirmed = _load_op_receipt(
                backend,
                key,
                project,
                session_id,
                operation_id,
                client,
                source_hash,
                source_len,
                duplicate=True,
            )
            if confirmed is not None:
                return confirmed
            if isinstance(rec, ERROR) and rec.kind == ErrorKind.CONFLICT_UNKNOWN:
                raise RawSessionError("outcome_uncertain", kind=rec.kind.value)
            continue

        if isinstance(last_res, ERROR):
            raise RawSessionError("write_error", kind=last_res.kind.value)

    confirmed = _load_op_receipt(
        backend,
        key,
        project,
        session_id,
        operation_id,
        client,
        source_hash,
        source_len,
        duplicate=True,
    )
    if confirmed is not None:
        return confirmed
    if isinstance(last_res, ERROR) and last_res.kind in (
        ErrorKind.TIMEOUT_AFTER_COMMIT,
        ErrorKind.CONFLICT_UNKNOWN,
    ):
        raise RawSessionError("outcome_uncertain", kind=last_res.kind.value)
    raise RawSessionError("append_retry_exhausted")


def read_raw_session(
    backend: StoreBackend, project: str, session_id: str, operation_id: str
) -> RawReadResult:
    if not valid_id(operation_id):
        raise RawSessionError("invalid_operation_id", value=operation_id)
    key = raw_session_store_key(project, session_id)
    try:
        blob = backend.read(key)
    except OSError as exc:
        raise RawSessionError("backend_error", error=type(exc).__name__) from exc
    if blob is None:
        return RawReadResult(found=False, operation_id=operation_id)
    body = _verify_blob_hash(blob)
    parsed = parse_raw_document(body, project=project, session_id=session_id)
    hit = _find_op_record(parsed.records, operation_id)
    if hit is None:
        return RawReadResult(found=False, operation_id=operation_id)
    header, payload = hit
    return RawReadResult(
        found=True,
        operation_id=operation_id,
        source_sha256=header["sha256"],
        source_length=header["len"],
        captured=header["captured"],
        record_id=record_id_for_header(header),
        current_version_hash=blob.version_hash,
        source_base64=base64.standard_b64encode(payload).decode("ascii"),
    )


def read_source_bytes(path: str, stdin: typing.BinaryIO) -> bytes:
    """Read at most MAX_SOURCE_READ bytes without newline transformation."""
    try:
        if path == "-":
            data = stdin.read(MAX_SOURCE_READ)
        else:
            with open(path, "rb") as fh:
                data = fh.read(MAX_SOURCE_READ)
    except OSError as exc:
        raise RawSessionError("source_file_error", error=type(exc).__name__) from exc
    if len(data) > MAX_SOURCE_BYTES:
        raise RawSessionError("source_too_large", length=len(data), max=MAX_SOURCE_BYTES)
    return data


def validate_append_arguments(
    project: str,
    session_id: str,
    client: str,
    operation_id: str,
    source: bytes,
) -> str:
    """Pure validation before any store I/O; returns the raw session key."""
    key = raw_session_store_key(project, session_id)
    if not valid_id(client):
        raise RawSessionError("invalid_client", value=client)
    if not valid_id(operation_id):
        raise RawSessionError("invalid_operation_id", value=operation_id)
    _validate_source_for_key(key, source)
    return key
