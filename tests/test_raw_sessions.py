"""Raw session capture slice tests."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import threading

import pytest

import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(store.__file__)))
sys.path.insert(0, ROOT)

from application.raw_sessions import (
    MAGIC,
    MAX_DOCUMENT_BYTES,
    MAX_SOURCE_BYTES,
    RawSessionError,
    append_raw_session,
    parse_raw_document,
    read_raw_session,
    utc_captured_now,
    validate_captured_timestamp,
)
from store.fake import FakeBackend
from store.local import LocalBackend
from store.types import sha256_hex

STATE = os.path.join(ROOT, "skill", "knokeep_state.py")
PROJ = "demo"


def run_cli(store_path, *args, project=PROJ, stdin_bytes: bytes | None = None):
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + REPO
    p = subprocess.run(
        [sys.executable, STATE, *args, "--store", store_path, "--project", project],
        input=stdin_bytes,
        capture_output=True,
        env=env,
    )
    return p.returncode, p.stdout.decode(), p.stderr.decode()


def data_files(store_path: str) -> set[str]:
    data = os.path.join(store_path, "data")
    out: set[str] = set()
    if not os.path.isdir(data):
        return out
    for root, _, files in os.walk(data):
        for f in files:
            out.add(os.path.relpath(os.path.join(root, f), store_path))
    return out


@pytest.mark.parametrize(
    "payload",
    [
        b"line\r\n",
        b"only\r",
        b"trail ",
        b"",
        "café".encode("utf-8"),
        ("e" + "\u0301").encode("utf-8"),
        b"KNOKEEP-RAW/1\nnot a real frame",
    ],
    ids=["crlf", "lonecr", "trail", "empty", "multibyte", "combining", "delimiter"],
)
def test_cli_byte_equality_roundtrip(tmp_path, payload):
    sid, op = "sess-raw-1", "op-bytes"
    src = tmp_path / "in.bin"
    src.write_bytes(payload)
    rc, out, err = run_cli(
        str(tmp_path),
        "raw-session-append",
        "--session-id",
        sid,
        "--operation-id",
        op,
        "--source-file",
        str(src),
    )
    assert rc == 0, err
    body = json.loads(out)
    assert body["ok"] and body["duplicate"] is False
    assert body["source_length"] == len(payload)
    assert body["source_sha256"] == hashlib.sha256(payload).hexdigest()
    rc2, out2, err2 = run_cli(
        str(tmp_path),
        "raw-session-read",
        "--session-id",
        sid,
        "--operation-id",
        op,
    )
    assert rc2 == 0, err2
    got = json.loads(out2)
    assert got["found"]
    assert base64.standard_b64decode(got["source_base64"]) == payload


def test_rejected_secret_no_payload_write(tmp_path):
    secret = b"AKIAEXAMPLE000000000"
    src = tmp_path / "s.txt"
    src.write_bytes(secret)
    before = data_files(str(tmp_path))
    rc, out, err = run_cli(
        str(tmp_path),
        "raw-session-append",
        "--session-id",
        "s1",
        "--operation-id",
        "op1",
        "--source-file",
        str(src),
    )
    assert rc != 0
    assert data_files(str(tmp_path)) == before


def test_duplicate_retry_without_rewrite(tmp_path):
    fake = FakeBackend()
    src = b"alpha"
    first = append_raw_session(fake, PROJ, "s1", "cowork", "op-a", src)
    second = append_raw_session(fake, PROJ, "s1", "cowork", "op-a", src)
    assert first.duplicate is False and second.duplicate is True
    assert first.record_id == second.record_id
    assert first.captured == second.captured


def test_duplicate_after_later_append_and_reopen(tmp_path):
    fake = FakeBackend()
    append_raw_session(fake, PROJ, "s1", "cowork", "op-a", b"one")
    append_raw_session(fake, PROJ, "s1", "cowork", "op-b", b"two")
    replay = append_raw_session(fake, PROJ, "s1", "cowork", "op-a", b"one")
    assert replay.duplicate is True
    fake2 = FakeBackend()
    key = f"{PROJ}/raw-sessions/s1"
    fake2._store[key] = fake._store[key]
    again = append_raw_session(fake2, PROJ, "s1", "cowork", "op-a", b"one")
    assert again.duplicate is True


def test_conflicting_operation_id_fails(tmp_path):
    fake = FakeBackend()
    append_raw_session(fake, PROJ, "s1", "cowork", "op-a", b"one")
    with pytest.raises(RawSessionError) as exc:
        append_raw_session(fake, PROJ, "s1", "cowork", "op-a", b"two")
    assert exc.value.reason == "operation_id_conflict"


def test_invalid_captured_rejected_before_backend_write():
    fake = FakeBackend()
    with pytest.raises(RawSessionError) as exc:
        append_raw_session(
            fake, PROJ, "s1", "cowork", "op-a", b"x", captured="not-a-timestamp"
        )
    assert exc.value.reason == "invalid_captured_timestamp"
    assert fake._store == {}


def test_validate_captured_rejects_impossible_calendar():
    with pytest.raises(RawSessionError):
        validate_captured_timestamp("2026-99-99T99:99:99.000000Z")


def test_parse_rejects_null_header_root():
    blob = MAGIC + b"null\n"
    with pytest.raises(RawSessionError) as exc:
        parse_raw_document(blob, project=PROJ, session_id="s1")
    assert exc.value.reason == "corrupt_raw_document"


def test_parse_rejects_numeric_op_and_client():
    header = (
        b'{"captured":"2026-01-15T12:00:00.000000Z","client":123,"len":1,'
        b'"op":123,"project":"demo","session":"s1","sha256":"'
        + hashlib.sha256(b"x").hexdigest().encode()
        + b'","v":1}\n'
    )
    blob = MAGIC + header + b"x\n"
    with pytest.raises(RawSessionError):
        parse_raw_document(blob, project=PROJ, session_id="s1")


def test_parse_rejects_oversized_payload_length():
    header = (
        b'{"captured":"2026-01-15T12:00:00.000000Z","client":"cowork","len":'
        + str(MAX_SOURCE_BYTES + 1).encode()
        + b',"op":"op1","project":"demo","session":"s1","sha256":"'
        + b"0" * 64
        + b'","v":1}\n'
    )
    blob = MAGIC + header
    with pytest.raises(RawSessionError) as exc:
        parse_raw_document(blob, project=PROJ, session_id="s1")
    assert exc.value.detail.get("detail") in ("bad_len", "truncated_payload")


def test_read_rejects_document_over_limit():
    fake = FakeBackend()
    key = f"{PROJ}/raw-sessions/big"
    body = b"x" * (MAX_DOCUMENT_BYTES + 1)
    fake._store[key] = (body, sha256_hex(body))
    with pytest.raises(RawSessionError) as exc:
        read_raw_session(fake, PROJ, "big", "op1")
    assert exc.value.reason == "document_too_large"


def test_timeout_after_commit_reconciles_without_double_append():
    fake = FakeBackend()
    fake.inject_timeout_after_commit(1)
    first = append_raw_session(fake, PROJ, "s1", "cowork", "op-t", b"payload")
    assert first.duplicate is False
    second = append_raw_session(fake, PROJ, "s1", "cowork", "op-t", b"payload")
    assert second.duplicate is True
    got = read_raw_session(fake, PROJ, "s1", "op-t")
    assert got.found
    assert base64.standard_b64decode(got.source_base64) == b"payload"


def test_concurrent_appends_same_session_both_land(tmp_path):
    fake = FakeBackend()
    errors: list[Exception] = []
    results: list = []

    def worker(op: str, text: bytes):
        try:
            results.append(
                append_raw_session(fake, PROJ, "s1", "cowork", op, text)
            )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(f"op-{i}", f"v{i}".encode()))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(results) == 4
    for i in range(4):
        got = read_raw_session(fake, PROJ, "s1", f"op-{i}")
        assert got.found
        assert base64.standard_b64decode(got.source_base64) == f"v{i}".encode()


def test_legacy_session_append_rejects_source_file(tmp_path):
    rc, out, err = run_cli(
        str(tmp_path),
        "session-append",
        "--session-id",
        "s1",
        "--entry",
        "hi",
        "--source-file",
        "-",
    )
    assert rc == 2
    assert "source-file" in err or "source-file" in out


def test_stdin_parity(tmp_path):
    payload = b"stdin-bytes\n"
    rc, out, err = run_cli(
        str(tmp_path),
        "raw-session-append",
        "--session-id",
        "stdin-s",
        "--operation-id",
        "op-stdin",
        "--source-file",
        "-",
        stdin_bytes=payload,
    )
    assert rc == 0, err
    rc2, out2, _ = run_cli(
        str(tmp_path),
        "raw-session-read",
        "--session-id",
        "stdin-s",
        "--operation-id",
        "op-stdin",
    )
    got = json.loads(out2)
    assert base64.standard_b64decode(got["source_base64"]) == payload


def test_legacy_session_read_unchanged(tmp_path):
    backend = LocalBackend(str(tmp_path))
    key = f"{PROJ}/sessions/legacy1"
    body = b"---\nschema_version: 1\n---\n## Journal\n"
    from store import gate
    from store.context import create_ctx

    gate.persist(backend, key, body, ctx=create_ctx(), doc_type="journal")
    backend.close()
    rc, out, err = run_cli(
        str(tmp_path),
        "session-read",
        "--session-id",
        "legacy1",
    )
    assert rc == 0, err
    assert json.loads(out)["found"]


def test_raw_blob_survives_generic_copy(tmp_path):
    fake = FakeBackend()
    src = b"copy-me"
    append_raw_session(fake, PROJ, "copy-s", "cowork", "op1", src)
    key = f"{PROJ}/raw-sessions/copy-s"
    body, vhash = fake._store[key]
    fake2 = FakeBackend()
    fake2._store[key] = (bytes(body), vhash)
    got = read_raw_session(fake2, PROJ, "copy-s", "op1")
    assert got.found
    assert base64.standard_b64decode(got.source_base64) == src
