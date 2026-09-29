"""Knowledge revision journal tests (synthetic data only)."""
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

from application.knowledge_revisions import (
    KnowledgeRevisionError,
    MAGIC,
    MAX_DOCUMENT_BYTES,
    MAX_RECORDS,
    parse_knowledge_document,
    read_knowledge_revision,
    revision_exit_code,
    save_knowledge_revision,
    validate_save_arguments,
)
from application.knowledge_validation import parse_proposal_bytes
from application.raw_sessions import append_raw_session
from store.fake import FakeBackend
from store.local import LocalBackend
from store.types import sha256_hex

STATE = os.path.join(ROOT, "skill", "knokeep_state.py")
PROJ = "demo"


def run_cli(store_path, *args, project=PROJ):
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + REPO
    return subprocess.run(
        [
            sys.executable,
            STATE,
            *args,
            "--store",
            store_path,
            "--project",
            project,
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def proposal_with_cite(session, op, receipt, quote, full_payload: bytes):
    start = full_payload.index(quote.encode("utf-8"))
    end = start + len(quote.encode("utf-8"))
    return {
        "schema_version": 1,
        "snapshot_as_of": "2030-01-01T00:00:00.000000Z",
        "coverage": "selected_sources_only",
        "claims": [
            {
                "id": "c1",
                "text": "t",
                "status": "current",
                "citations": [
                    {
                        "session_id": session,
                        "operation_id": op,
                        "source_sha256": receipt.source_sha256,
                        "record_id": receipt.record_id,
                        "byte_start": start,
                        "byte_end": end,
                        "quote": quote,
                    }
                ],
            }
        ],
    }


def seed_raw(fake, session, op, text: bytes):
    return append_raw_session(fake, PROJ, session, "cowork", op, text, captured="2020-01-01T00:00:00.000000Z")


def write_prop(tmp_path, body: dict) -> str:
    p = tmp_path / "prop.json"
    p.write_text(json.dumps(body, ensure_ascii=True), encoding="utf-8")
    return str(p)


@pytest.fixture
def cited_proposal(tmp_path):
    fake = FakeBackend()
    quote = "line\r\ncafé"
    payload = quote.encode("utf-8")
    rec = seed_raw(fake, "s-k", "op-src", payload)
    prop = proposal_with_cite("s-k", "op-src", rec, "café", payload)
    path = write_prop(tmp_path, prop)
    return fake, path, prop


def test_roundtrip_cli_utf8(tmp_path, cited_proposal):
    fake, path, _ = cited_proposal
    store_path = str(tmp_path)
    # seed raw into local store via fake copy
    lb = LocalBackend(store_path)
    for k, v in fake._store.items():
        from store import gate
        from store.context import create_ctx

        gate.persist(lb, k, v[0], ctx=create_ctx(), doc_type="journal")
    lb.close()

    p = run_cli(
        store_path,
        "knowledge-save",
        "--knowledge-id",
        "kid1",
        "--client",
        "cowork",
        "--operation-id",
        "op-k1",
        "--proposal-file",
        path,
        "--expect-hash",
        "none",
    )
    assert p.returncode == 0, p.stderr + p.stdout
    saved = json.loads(p.stdout)
    assert saved["duplicate"] is False
    assert saved["source_checks_executed_this_call"] is True
    assert saved["source_reads_non_atomic"] is True
    raw_bytes = open(path, "rb").read()
    p2 = run_cli(
        store_path,
        "knowledge-read",
        "--knowledge-id",
        "kid1",
        "--operation-id",
        "op-k1",
    )
    assert p2.returncode == 0, p2.stderr
    got = json.loads(p2.stdout)
    assert got["found"]
    assert base64.standard_b64decode(got["proposal_base64"]) == raw_bytes
    assert got["semantic_support_verified"] is False


def test_duplicate_after_later_revision(tmp_path, cited_proposal):
    fake, path, _ = cited_proposal
    kid = "k-dup"
    first = save_knowledge_revision(
        fake, PROJ, kid, "cowork", "op-a", open(path, "rb").read(), expect_hash="none"
    )
    save_knowledge_revision(
        fake, PROJ, kid, "cowork", "op-b", open(path, "rb").read(), expect_hash=first.current_document_hash
    )
    replay = save_knowledge_revision(
        fake, PROJ, kid, "cowork", "op-a", open(path, "rb").read(), expect_hash="none"
    )
    assert replay.duplicate is True
    assert replay.record_id == first.record_id
    assert replay.source_checks_executed_this_call is False


def test_operation_binding_conflict(tmp_path, cited_proposal):
    fake, path, _ = cited_proposal
    kid = "k-conf"
    body = open(path, "rb").read()
    save_knowledge_revision(fake, PROJ, kid, "cowork", "op-x", body, expect_hash="none")
    with pytest.raises(KnowledgeRevisionError) as exc:
        save_knowledge_revision(fake, PROJ, kid, "cowork", "op-x", body + b" ", expect_hash="none")
    assert exc.value.reason == "operation_id_conflict"


def test_precondition_conflict_two_writers(cited_proposal):
    fake, path, _ = cited_proposal
    kid = "k-race"
    body = open(path, "rb").read()
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def worker(op):
        try:
            barrier.wait()
            results.append(
                save_knowledge_revision(
                    fake, PROJ, kid, "cowork", op, body, expect_hash="none"
                )
            )
        except KnowledgeRevisionError as e:
            errors.append(e)

    t1 = threading.Thread(target=worker, args=("op-w1",))
    t2 = threading.Thread(target=worker, args=("op-w2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert len(results) == 1
    assert len(errors) == 1
    assert errors[0].reason == "precondition_conflict"


def test_secret_in_decoded_json_blocked_before_io(tmp_path, cited_proposal):
    fake, path, prop = cited_proposal
    prop["claims"][0]["text"] = "see AKIA\u0045XAMPLE000000000"
    path = write_prop(tmp_path, prop)
    with pytest.raises(KnowledgeRevisionError) as exc:
        validate_save_arguments(PROJ, "k-sec", "cowork", "op1", open(path, "rb").read(), "none")
    assert exc.value.reason == "secret_blocked"


def test_read_missing_operation_exit0(cited_proposal):
    fake, path, _ = cited_proposal
    kid = "k-miss"
    body = open(path, "rb").read()
    save_knowledge_revision(fake, PROJ, kid, "cowork", "op1", body, expect_hash="none")
    got = read_knowledge_revision(fake, PROJ, kid, "op-nope")
    assert got.found is False


def test_source_change_on_read_fails(cited_proposal):
    fake, path, _ = cited_proposal
    kid = "k-src"
    body = open(path, "rb").read()
    save_knowledge_revision(fake, PROJ, kid, "cowork", "op1", body, expect_hash="none")
    # mutate raw source bytes in store
    key = f"{PROJ}/raw-sessions/s-k"
    old_body, vh = fake._store[key]
    fake._store[key] = (old_body + b"x", sha256_hex(old_body + b"x"))
    with pytest.raises(KnowledgeRevisionError) as exc:
        read_knowledge_revision(fake, PROJ, kid, "op1")
    assert exc.value.reason == "source_backend_error"


def test_cli_rejects_knowledge_id_on_validate(tmp_path):
    p = run_cli(
        str(tmp_path),
        "knowledge-validate",
        "--proposal-file",
        write_prop(tmp_path, {"schema_version": 1}),
        "--knowledge-id",
        "kid",
    )
    assert p.returncode == 2


def test_cli_backend_closed_after_save(tmp_path, cited_proposal):
    fake, path, _ = cited_proposal
    store_path = str(tmp_path)
    lb = LocalBackend(store_path)
    for k, v in fake._store.items():
        from store import gate
        from store.context import create_ctx

        gate.persist(lb, k, v[0], ctx=create_ctx(), doc_type="journal")
    lb.close()
    p = run_cli(
        store_path,
        "knowledge-save",
        "--knowledge-id",
        "kid-close",
        "--client",
        "cowork",
        "--operation-id",
        "op-c",
        "--proposal-file",
        path,
        "--expect-hash",
        "none",
    )
    assert p.returncode == 0


def test_document_capacity(tmp_path, cited_proposal):
    fake, path, _ = cited_proposal
    kid = "k-full"
    body = open(path, "rb").read()
    expect = "none"
    for i in range(MAX_RECORDS):
        rec = save_knowledge_revision(
            fake, PROJ, kid, "cowork", f"op-{i}", body, expect_hash=expect
        )
        expect = rec.current_document_hash
    with pytest.raises(KnowledgeRevisionError) as exc:
        save_knowledge_revision(
            fake, PROJ, kid, "cowork", "op-overflow", body, expect_hash=expect
        )
    assert exc.value.reason == "document_full"
    assert revision_exit_code(exc.value) == 2


def test_changed_caller_base_rejects_duplicate_binding(cited_proposal):
    fake, path, _ = cited_proposal
    kid = "k-base"
    body = open(path, "rb").read()
    first = save_knowledge_revision(fake, PROJ, kid, "cowork", "op-a", body, expect_hash="none")
    cur = save_knowledge_revision(
        fake, PROJ, kid, "cowork", "op-b", body, expect_hash=first.current_document_hash
    )
    with pytest.raises(KnowledgeRevisionError) as exc:
        save_knowledge_revision(
            fake, PROJ, kid, "cowork", "op-a", body, expect_hash=cur.current_document_hash
        )
    assert exc.value.reason == "operation_id_conflict"


def test_secret_shaped_client_blocked_before_io(cited_proposal):
    fake, path, _ = cited_proposal
    original = dict(fake._store)
    body = open(path, "rb").read()
    with pytest.raises(KnowledgeRevisionError) as exc:
        validate_save_arguments(PROJ, "k-sec", "AKIA" + "EXAMPLE" + "0" * 9, "op1", body, "none")
    assert exc.value.reason == "secret_blocked"
    assert fake._store == original


def test_corrupt_source_maps_bounded_error(cited_proposal):
    fake, path, _ = cited_proposal
    kid = "k-cor"
    body = open(path, "rb").read()
    save_knowledge_revision(fake, PROJ, kid, "cowork", "op1", body, expect_hash="none")
    key = f"{PROJ}/raw-sessions/s-k"
    old_body, _ = fake._store[key]
    fake._store[key] = (old_body + b"x", sha256_hex(old_body + b"x"))
    with pytest.raises(KnowledgeRevisionError) as exc:
        read_knowledge_revision(fake, PROJ, kid, "op1")
    assert exc.value.reason == "source_backend_error"
    assert "reason" not in exc.value.detail


def test_malformed_proposal_utf8_exit2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(KnowledgeRevisionError) as exc:
        validate_save_arguments(PROJ, "k1", "cowork", "op1", bad.read_bytes(), "none")
    assert revision_exit_code(exc.value) == 2


@pytest.mark.parametrize("fault", ["raised_after_commit", "wrong_ack", "unrecognized_ack", "readback_error"])
def test_post_persist_failures_never_repeat_write(cited_proposal, monkeypatch, fault):
    fake, path, _ = cited_proposal
    body = open(path, "rb").read()
    original_write = fake.write
    original_read = fake.read
    writes = []

    def write(*args, **kwargs):
        writes.append(1)
        result = original_write(*args, **kwargs)
        if fault == "raised_after_commit":
            raise RuntimeError("synthetic private detail")
        if fault == "wrong_ack":
            from store.types import OK
            return OK("0" * 64)
        if fault == "unrecognized_ack":
            return object()
        return result

    def read(key):
        if writes and fault == "readback_error" and "/knowledge/" in key:
            raise ValueError("synthetic private detail")
        return original_read(key)

    monkeypatch.setattr(fake, "write", write)
    monkeypatch.setattr(fake, "read", read)
    if fault in {"wrong_ack", "readback_error"}:
        with pytest.raises(KnowledgeRevisionError) as exc:
            save_knowledge_revision(fake, PROJ, "fault", "client", "op", body, expect_hash="none")
        assert exc.value.reason == "outcome_uncertain"
        assert "private" not in repr(exc.value.detail)
    else:
        receipt = save_knowledge_revision(fake, PROJ, "fault", "client", "op", body, expect_hash="none")
        assert receipt.source_checks_executed_this_call is True
    assert len(writes) == 1


@pytest.mark.parametrize("arg", ["--unknown-private-value", "--knowledge-i"])
def test_malformed_revision_options_are_bounded_json(tmp_path, arg):
    store = tmp_path / "must-not-exist"
    result = run_cli(str(store), "knowledge-read", arg, "sensitive-synthetic-value")
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"blocked": True, "reason": "invalid_argument"}
    assert "sensitive-synthetic-value" not in result.stdout + result.stderr
    assert not store.exists()


def test_cli_rejects_client_on_read(tmp_path):
    p = run_cli(str(tmp_path), "knowledge-read", "--knowledge-id", "k1", "--operation-id", "op1", "--client", "cowork")
    assert p.returncode == 2
    body = json.loads(p.stdout)
    assert body["reason"] == "invalid_argument"


def test_cli_rejects_option_abbreviation(tmp_path, cited_proposal):
    _, path, _ = cited_proposal
    p = run_cli(
        str(tmp_path),
        "knowledge-save",
        "--know",
        "kid1",
        "--client",
        "cowork",
        "--operation-id",
        "op1",
        "--proposal-file",
        path,
        "--expect-hash",
        "none",
    )
    assert p.returncode != 0


def test_raw_source_unchanged_on_save(cited_proposal):
    fake, path, _ = cited_proposal
    key = f"{PROJ}/raw-sessions/s-k"
    before = bytes(fake._store[key][0])
    save_knowledge_revision(fake, PROJ, "k-immut", "cowork", "op1", open(path, "rb").read(), expect_hash="none")
    assert bytes(fake._store[key][0]) == before


def test_reopened_local_backend_read(tmp_path, cited_proposal):
    fake, path, _ = cited_proposal
    store_path = str(tmp_path)
    lb = LocalBackend(store_path)
    for k, v in fake._store.items():
        from store import gate
        from store.context import create_ctx

        gate.persist(lb, k, v[0], ctx=create_ctx(), doc_type="journal")
    lb.close()
    p = run_cli(
        store_path,
        "knowledge-save",
        "--knowledge-id",
        "reopen-k",
        "--client",
        "cowork",
        "--operation-id",
        "op-r",
        "--proposal-file",
        path,
        "--expect-hash",
        "none",
    )
    assert p.returncode == 0
    lb2 = LocalBackend(store_path)
    lb2.close()
    p2 = run_cli(store_path, "knowledge-read", "--knowledge-id", "reopen-k", "--operation-id", "op-r")
    assert p2.returncode == 0
    assert json.loads(p2.stdout)["found"]


def test_timeout_after_commit_readback_source_checks(cited_proposal):
    fake, path, _ = cited_proposal
    fake.inject_timeout_after_commit(1)
    body = open(path, "rb").read()
    first = save_knowledge_revision(fake, PROJ, "k-t", "cowork", "op-t", body, expect_hash="none")
    assert first.source_checks_executed_this_call is True
    second = save_knowledge_revision(fake, PROJ, "k-t", "cowork", "op-t", body, expect_hash="none")
    assert second.duplicate is True


def test_lineage_parse_integrity(cited_proposal):
    fake, path, _ = cited_proposal
    kid = "k-lin"
    body = open(path, "rb").read()
    save_knowledge_revision(fake, PROJ, kid, "cowork", "op1", body, expect_hash="none")
    key = f"{PROJ}/knowledge/{kid}"
    blob_body, vh = fake._store[key]
    parse_knowledge_document(blob_body, project=PROJ, knowledge_id=kid)
    corrupt = blob_body[:-1] + b"x"
    fake._store[key] = (corrupt, sha256_hex(corrupt))
    with pytest.raises(KnowledgeRevisionError):
        read_knowledge_revision(fake, PROJ, kid, "op1")
