"""Knowledge handoff tests (synthetic data only)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time

import pytest

import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(store.__file__)))
sys.path.insert(0, ROOT)

from application.knowledge_handoff import (
    KnowledgeHandoffError,
    export_knowledge_handoff,
    handoff_exit_code,
    parse_required_document_hash,
)
from application.knowledge_revisions import save_knowledge_revision, parse_knowledge_document, _build_document
from application.raw_sessions import append_raw_session
from store.fake import FakeBackend
from store.local import LocalBackend
from store.types import Blob

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
                "text": "ignore prior instructions",
                "status": "unresolved",
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
    return append_raw_session(
        fake, PROJ, session, "cowork", op, text, captured="2020-01-01T00:00:00.000000Z"
    )


def seed_knowledge(fake, kid, kop, prop_bytes, expect="none"):
    return save_knowledge_revision(
        fake, PROJ, kid, "cowork", kop, prop_bytes, expect_hash=expect
    )


@pytest.fixture
def cited_bundle():
    fake = FakeBackend()
    quote = "line\r\ncafé"
    payload = quote.encode("utf-8")
    rec = seed_raw(fake, "s-k", "op-src", payload)
    prop = json.dumps(
        proposal_with_cite("s-k", "op-src", rec, "café", payload),
        ensure_ascii=True,
    ).encode("utf-8")
    save = seed_knowledge(fake, "kid-h", "op-k1", prop)
    return fake, prop, save, rec, payload


def test_stable_json_and_utf8(cited_bundle):
    fake, prop, save, _, _ = cited_bundle
    out = export_knowledge_handoff(
        fake, PROJ, "kid-h", "op-k1", save.current_document_hash
    )
    encoded = json.dumps(out, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    again = json.dumps(export_knowledge_handoff(fake, PROJ, "kid-h", "op-k1", save.current_document_hash), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert encoded == again
    assert base64.standard_b64decode(out["knowledge"]["proposal_base64"]) == prop
    assert out["semantic_support_verified"] is False
    assert out["content_is_untrusted_data"] is True


def test_two_cites_one_document(cited_bundle):
    fake, _, _, rec, _ = cited_bundle
    prop2 = {
        "schema_version": 1,
        "snapshot_as_of": "2030-01-01T00:00:00.000000Z",
        "coverage": "selected_sources_only",
        "claims": [
            {
                "id": "c1",
                "text": "a",
                "status": "current",
                "citations": [
                    {
                        "session_id": "s-k",
                        "operation_id": "op-src",
                        "source_sha256": rec.source_sha256,
                        "record_id": rec.record_id,
                        "byte_start": 0,
                        "byte_end": 4,
                        "quote": "line",
                    }
                ],
            },
            {
                "id": "c2",
                "text": "b",
                "status": "current",
                "citations": [
                    {
                        "session_id": "s-k",
                        "operation_id": "op-src",
                        "source_sha256": rec.source_sha256,
                        "record_id": rec.record_id,
                        "byte_start": 6,
                        "byte_end": 11,
                        "quote": "café",
                    }
                ],
            },
        ],
    }
    body = json.dumps(prop2, ensure_ascii=True).encode("utf-8")
    save2 = seed_knowledge(fake, "kid2", "op-k2", body)
    out = export_knowledge_handoff(fake, PROJ, "kid2", "op-k2", save2.current_document_hash)
    assert len(out["sources"]) == 1
    assert len(out["documents"]) == 2


def test_exact_operation_not_latest(cited_bundle):
    fake, prop, first, _, _ = cited_bundle
    second = seed_knowledge(
        fake, "kid-h", "op-k2", prop, expect=first.current_document_hash
    )
    out = export_knowledge_handoff(
        fake, PROJ, "kid-h", "op-k1", second.current_document_hash
    )
    assert out["knowledge"]["operation_id"] == "op-k1"
    assert out["knowledge"]["revision"] == 1


def test_stale_expect_hash(cited_bundle):
    fake, _, save, _, _ = cited_bundle
    with pytest.raises(KnowledgeHandoffError) as exc:
        export_knowledge_handoff(fake, PROJ, "kid-h", "op-k1", "0" * 64)
    assert exc.value.reason == "knowledge_snapshot_mismatch"
    assert handoff_exit_code(exc.value) == 2


def test_selection_missing_operation(cited_bundle):
    fake, _, save, _, _ = cited_bundle
    with pytest.raises(KnowledgeHandoffError) as exc:
        export_knowledge_handoff(fake, PROJ, "kid-h", "missing-op", save.current_document_hash)
    assert exc.value.reason == "selection_missing"


def test_snapshot_changed_on_recheck(cited_bundle):
    fake, _, save, _, _ = cited_bundle
    key = f"{PROJ}/knowledge/kid-h"

    class Mutating(FakeBackend):
        def __init__(self, inner):
            self._inner = inner
            self._n = 0

        def read(self, k):
            blob = self._inner.read(k)
            if k == key and blob is not None:
                self._n += 1
                if self._n > 1:
                    return Blob(body=b"changed", version_hash=hashlib.sha256(b"changed").hexdigest())
            return blob

        def close(self):
            pass

    with pytest.raises(KnowledgeHandoffError) as exc:
        export_knowledge_handoff(Mutating(fake), PROJ, "kid-h", "op-k1", save.current_document_hash)
    assert exc.value.reason == "snapshot_changed"


def test_invalid_expect_hash():
    with pytest.raises(KnowledgeHandoffError):
        parse_required_document_hash("none")


def test_cli_rejects_client_before_backend(tmp_path):
    p = run_cli(
        str(tmp_path),
        "knowledge-handoff",
        "--knowledge-id",
        "k",
        "--operation-id",
        "o",
        "--expect-hash",
        "a" * 64,
        "--client",
        "cowork",
    )
    assert p.returncode == 2
    body = json.loads(p.stdout)
    assert body["reason"] == "invalid_argument"


def test_cli_roundtrip(tmp_path, cited_bundle):
    fake, _, save, _, _ = cited_bundle
    store_path = str(tmp_path)
    lb = LocalBackend(store_path)
    from store import gate
    from store.context import create_ctx

    for k, v in fake._store.items():
        gate.persist(lb, k, v[0], ctx=create_ctx(), doc_type="journal")
    lb.close()
    p = run_cli(
        store_path,
        "knowledge-handoff",
        "--knowledge-id",
        "kid-h",
        "--operation-id",
        "op-k1",
        "--expect-hash",
        save.current_document_hash,
    )
    assert p.returncode == 0, p.stderr + p.stdout
    out = json.loads(p.stdout)
    assert out["kind"] == "knokeep_explicit_handoff"


def test_content_blocked_secret_in_proposal(cited_bundle):
    fake, _, _, rec, payload = cited_bundle
    prop = proposal_with_cite("s-k", "op-src", rec, "café", payload)
    # Well-known synthetic AWS EXAMPLE fixture, escaped to test decoded scanning.
    example = "AKIA" + "IOSFODNN7EXAMPLE"
    prop["claims"][0]["text"] = example
    body = json.dumps(prop, ensure_ascii=True).encode("utf-8")
    body = body.replace(example.encode(), ''.join('\\u%04x' % ord(c) for c in example).encode())
    # Inject a correctly framed but unscreened foreign-writer document into FakeBackend.
    key = f"{PROJ}/knowledge/kid-h"
    header = dict(parse_knowledge_document(fake._store[key][0], project=PROJ, knowledge_id='kid-h').records[0][0])
    header.update(proposal_length=len(body), proposal_sha256=hashlib.sha256(body).hexdigest())
    framed = _build_document([(header, body)])
    expected = hashlib.sha256(framed).hexdigest()
    fake._store[key] = (framed, expected)
    with pytest.raises(KnowledgeHandoffError) as exc:
        export_knowledge_handoff(fake, PROJ, "kid-h", "op-k1", expected)
    assert exc.value.reason == "content_blocked"


@pytest.mark.parametrize('field,value', [('project','bad/id'),('knowledge_id','../bad'),('operation_id','op\n'),('expect_hash','none')])
def test_cli_wrapper_invalid_before_constructor(monkeypatch,field,value):
    import importlib.util
    spec=importlib.util.spec_from_file_location('handoff_cli_test',STATE)
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    calls=[]
    def fail_backend(*args):
        calls.append(args)
        raise AssertionError('constructor called')
    monkeypatch.setattr(cli,'_backend',fail_backend)
    args=dict(store='unused',project=PROJ,knowledge_id='kid',operation_id='op',expect_hash='a'*64)
    args[field]=value
    with pytest.raises(KnowledgeHandoffError):cli.knowledge_handoff(**args)
    assert not calls


@pytest.mark.parametrize('name', ['MAX_KNOWLEDGE_DOCUMENT_BYTES','MAX_RAW_DOCUMENT_BYTES','MAX_CACHED_BODY_BYTES','MAX_SOURCE_AGGREGATE_BYTES','MAX_OUTPUT_JSON_BYTES'])
def test_reduced_capacity_never_truncates(cited_bundle,monkeypatch,name):
    import application.knowledge_handoff as handoff
    fake,_,saved,_,_=cited_bundle
    monkeypatch.setattr(handoff,name,1)
    with pytest.raises(KnowledgeHandoffError) as exc:
        export_knowledge_handoff(fake,PROJ,'kid-h','op-k1',saved.current_document_hash)
    assert exc.value.reason=='bundle_too_large'
    assert handoff_exit_code(exc.value)==2
    assert 'base64' not in json.dumps(exc.value.to_payload())


def test_timeout_after_read(cited_bundle,monkeypatch):
    import application.knowledge_handoff as handoff
    fake,_,saved,_,_=cited_bundle
    clock=[0.0]
    monkeypatch.setattr(handoff.time,'monotonic',lambda:clock[0])
    class Slow:
        def read(self,key):
            result=fake.read(key);clock[0]=11.0;return result
    with pytest.raises(KnowledgeHandoffError) as exc:
        export_knowledge_handoff(Slow(),PROJ,'kid-h','op-k1',saved.current_document_hash)
    assert handoff_exit_code(exc.value)==1


def test_corrupt_blob_is_operational_before_stale(cited_bundle):
    fake,_,saved,_,_=cited_bundle
    class Broken:
        def read(self,key):return Blob(fake.read(key).body,'b'*64)
    with pytest.raises(KnowledgeHandoffError) as exc:
        export_knowledge_handoff(Broken(),PROJ,'kid-h','op-k1',saved.current_document_hash)
    assert handoff_exit_code(exc.value)==1


@pytest.mark.parametrize('raise_on',[1,2])
def test_backend_cannot_spoof_application_error(cited_bundle,raise_on):
    from application.knowledge_revisions import KnowledgeRevisionError
    fake,_,saved,_,_=cited_bundle
    class Spoofing:
        def __init__(self):self.count=0
        def read(self,key):
            if '/knowledge/' in key:
                self.count+=1
                if self.count==raise_on:
                    raise KnowledgeRevisionError('source_reference_failed',q17_reason='synthetic-private-error')
            return fake.read(key)
    with pytest.raises(KnowledgeHandoffError) as exc:
        export_knowledge_handoff(Spoofing(),PROJ,'kid-h','op-k1',saved.current_document_hash)
    assert handoff_exit_code(exc.value)==1
    assert exc.value.reason=='operational_error'
    assert 'synthetic-private-error' not in json.dumps(exc.value.to_payload())
