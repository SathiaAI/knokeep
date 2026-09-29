"""Citation validator application + CLI (read-only; not semantic review)."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile

import pytest

import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(store.__file__)))
sys.path.insert(0, ROOT)

from application.knowledge_validation import (
    KnowledgeValidationError,
    parse_proposal_bytes,
    validate_proposal,
    validate_proposal_structure,
    validation_exit_code,
)
from application.raw_sessions import append_raw_session, utc_captured_now
from store.fake import FakeBackend

STATE = os.path.join(ROOT, "skill", "knokeep_state.py")
PROJ = "demo"
OTHER = "otherproj"


def run_cli(store_path, *args, project=PROJ, stdin_bytes: bytes | None = None):
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + REPO
    return subprocess.run(
        [sys.executable, STATE, *args, "--store", store_path, "--project", project],
        input=stdin_bytes,
        capture_output=True,
        text=True,
        env=env,
    )


def proposal(**overrides):
    base = {
        "schema_version": 1,
        "snapshot_as_of": "2030-01-01T00:00:00.000000Z",
        "coverage": "selected_sources_only",
        "claims": [],
    }
    base.update(overrides)
    return base


def cite(session, op, receipt, start, end, quote):
    return {
        "session_id": session,
        "operation_id": op,
        "source_sha256": receipt.source_sha256,
        "record_id": receipt.record_id,
        "byte_start": start,
        "byte_end": end,
        "quote": quote,
    }


def seed_raw(fake, session, op, payload: bytes, captured=None):
    return append_raw_session(
        fake, PROJ, session, "cowork", op, payload, captured=captured
    )


class HostileWriteBackend:
    def __init__(self, inner):
        self._inner = inner
        self.read_calls = 0

    def read(self, key):
        self.read_calls += 1
        return self._inner.read(key)

    def list(self, prefix):
        return self._inner.list(prefix)

    def write(self, *a, **k):
        raise AssertionError("write forbidden")

    def lock(self, *a, **k):
        raise AssertionError("lock forbidden")

    def unlock(self, *a, **k):
        raise AssertionError("unlock forbidden")

    def renew(self, *a, **k):
        raise AssertionError("renew forbidden")

    def close(self):
        raise AssertionError("close forbidden in application layer")

    def capabilities(self):
        return self._inner.capabilities()

    def health(self):
        return self._inner.health()


def test_valid_crlf_and_unicode_citations():
    payload = "line\r\ncafé".encode("utf-8")
    fake = FakeBackend()
    rec = seed_raw(fake, "s-cite", "op1", payload, captured="2020-01-01T00:00:00.000000Z")
    quote = "café"
    start = payload.index(quote.encode("utf-8"))
    end = start + len(quote.encode("utf-8"))
    body = proposal(
        claims=[
            {
                "id": "c1",
                "text": "claim",
                "status": "current",
                "citations": [cite("s-cite", "op1", rec, start, end, quote)],
            }
        ]
    )
    raw = json.dumps(body).encode()
    out = validate_proposal(HostileWriteBackend(fake), PROJ, raw)
    assert out["ok"] is True
    assert out["citations_checked"] == 1
    assert out["semantic_support_verified"] is False
    assert out["source_authorship_verified"] is False
    assert out["authorization_verified"] is False
    assert out["project_completeness_verified"] is False
    assert out["repository_freshness_verified"] is False
    assert out["validated"]["exact_quote_bytes"] is True


def test_false_trust_flags_on_success():
    fake = FakeBackend()
    rec = seed_raw(fake, "s1", "op1", b"abc", captured="2020-01-01T00:00:00.000000Z")
    body = proposal(
        claims=[
            {
                "id": "c1",
                "text": "t",
                "status": "unresolved",
                "citations": [cite("s1", "op1", rec, 0, 3, "abc")],
            }
        ]
    )
    out = validate_proposal(fake, PROJ, json.dumps(body).encode())
    for key in (
        "semantic_support_verified",
        "source_authorship_verified",
        "authorization_verified",
        "project_completeness_verified",
        "repository_freshness_verified",
    ):
        assert out[key] is False


def test_namespace_crossing_source_not_in_other_project():
    fake = FakeBackend()
    rec = seed_raw(fake, "s1", "op1", b"x", captured="2020-01-01T00:00:00.000000Z")
    body = proposal(
        claims=[
            {
                "id": "c1",
                "text": "t",
                "status": "current",
                "citations": [cite("s1", "op1", rec, 0, 1, "x")],
            }
        ]
    )
    with pytest.raises(KnowledgeValidationError) as exc:
        validate_proposal(fake, OTHER, json.dumps(body).encode())
    assert exc.value.code == "source_not_found"


def test_hash_and_record_mismatch():
    fake = FakeBackend()
    rec = seed_raw(fake, "s1", "op1", b"abc", captured="2020-01-01T00:00:00.000000Z")
    bad_hash = proposal(
        claims=[
            {
                "id": "c1",
                "text": "t",
                "status": "current",
                "citations": [
                    {
                        **cite("s1", "op1", rec, 0, 3, "abc"),
                        "source_sha256": "0" * 64,
                    }
                ],
            }
        ]
    )
    with pytest.raises(KnowledgeValidationError) as exc:
        validate_proposal(fake, PROJ, json.dumps(bad_hash).encode())
    assert exc.value.code == "source_hash_mismatch"

    bad_rid = proposal(
        claims=[
            {
                "id": "c1",
                "text": "t",
                "status": "current",
                "citations": [
                    {**cite("s1", "op1", rec, 0, 3, "abc"), "record_id": "1" * 64}
                ],
            }
        ]
    )
    with pytest.raises(KnowledgeValidationError) as exc2:
        validate_proposal(fake, PROJ, json.dumps(bad_rid).encode())
    assert exc2.value.code == "record_id_mismatch"


def test_repeated_source_single_read():
    fake = FakeBackend()
    rec = seed_raw(fake, "s1", "op1", b"hello", captured="2020-01-01T00:00:00.000000Z")
    body = proposal(
        claims=[
            {
                "id": "c1",
                "text": "a",
                "status": "current",
                "citations": [cite("s1", "op1", rec, 0, 5, "hello")],
            },
            {
                "id": "c2",
                "text": "b",
                "status": "historical",
                "citations": [cite("s1", "op1", rec, 0, 1, "h")],
            },
        ]
    )
    hostile = HostileWriteBackend(fake)
    validate_proposal(hostile, PROJ, json.dumps(body).encode())
    assert hostile.read_calls == 1


def test_bool_schema_rejected():
    with pytest.raises(KnowledgeValidationError) as exc:
        validate_proposal_structure(
            {
                "schema_version": True,
                "snapshot_as_of": "2020-01-01T00:00:00.000000Z",
                "coverage": "selected_sources_only",
                "claims": [
                    {
                        "id": "c1",
                        "text": "t",
                        "status": "current",
                        "citations": [
                            {
                                "session_id": "s1",
                                "operation_id": "op1",
                                "source_sha256": "a" * 64,
                                "record_id": "b" * 64,
                                "byte_start": 0,
                                "byte_end": 1,
                                "quote": "x",
                            }
                        ],
                    }
                ],
            }
        )
    assert exc.value.code == "invalid_schema_version"


def test_bool_offsets_and_bad_calendar_rejected():
    with pytest.raises(KnowledgeValidationError) as exc:
        validate_proposal_structure(
            {
                "schema_version": 1,
                "snapshot_as_of": "2026-99-99T99:99:99.000000Z",
                "coverage": "selected_sources_only",
                "claims": [
                    {
                        "id": "c1",
                        "text": "t",
                        "status": "current",
                        "citations": [
                            {
                                "session_id": "s1",
                                "operation_id": "op1",
                                "source_sha256": "a" * 64,
                                "record_id": "b" * 64,
                                "byte_start": True,
                                "byte_end": 3,
                                "quote": "x",
                            }
                        ],
                    }
                ],
            }
        )
    assert exc.value.code in ("invalid_snapshot_as_of", "invalid_byte_offsets")


def test_duplicate_json_keys():
    raw = b'{"schema_version":1,"schema_version":1,"snapshot_as_of":"2020-01-01T00:00:00.000000Z","coverage":"selected_sources_only","claims":[]}'
    with pytest.raises(KnowledgeValidationError) as exc:
        parse_proposal_bytes(raw)
    assert exc.value.code == "duplicate_json_key"


def test_limits_and_unsupported_field():
    with pytest.raises(KnowledgeValidationError):
        validate_proposal_structure(
            {
                "schema_version": 1,
                "snapshot_as_of": "2020-01-01T00:00:00.000000Z",
                "coverage": "selected_sources_only",
                "claims": [],
                "extra": 1,
            }
        )
    big = "x" * 1025
    with pytest.raises(KnowledgeValidationError) as exc:
        validate_proposal_structure(
            {
                "schema_version": 1,
                "snapshot_as_of": "2020-01-01T00:00:00.000000Z",
                "coverage": "selected_sources_only",
                "claims": [{"id": "c1", "text": big, "status": "current", "citations": []}],
            }
        )
    assert exc.value.code in ("invalid_claim_text", "invalid_citations_count")


def test_quote_mismatch_and_utf8_cut():
    fake = FakeBackend()
    payload = "é".encode("utf-8")  # 2 bytes
    rec = seed_raw(fake, "s1", "op1", payload, captured="2020-01-01T00:00:00.000000Z")
    body = proposal(
        claims=[
            {
                "id": "c1",
                "text": "t",
                "status": "current",
                "citations": [cite("s1", "op1", rec, 0, 1, "é")],
            }
        ]
    )
    with pytest.raises(KnowledgeValidationError) as exc:
        validate_proposal(fake, PROJ, json.dumps(body).encode())
    assert exc.value.code == "quote_mismatch"


def test_missing_source_and_backend_permission():
    fake = FakeBackend()
    rec = seed_raw(fake, "s1", "op1", b"a", captured="2020-01-01T00:00:00.000000Z")
    body = proposal(
        claims=[
            {
                "id": "c1",
                "text": "t",
                "status": "current",
                "citations": [cite("s1", "op-missing", rec, 0, 1, "a")],
            }
        ]
    )
    with pytest.raises(KnowledgeValidationError) as exc:
        validate_proposal(fake, PROJ, json.dumps(body).encode())
    assert exc.value.code == "source_not_found"
    assert exc.value.detail.get("claim_index") == 0
    assert "session_id" not in exc.value.detail

    class DenyRead(FakeBackend):
        def read(self, key):
            raise PermissionError("nope")

    with pytest.raises(KnowledgeValidationError) as exc2:
        validate_proposal(
            DenyRead(),
            PROJ,
            json.dumps(
                proposal(
                    claims=[
                        {
                            "id": "c1",
                            "text": "t",
                            "status": "current",
                            "citations": [cite("s1", "op1", rec, 0, 1, "a")],
                        }
                    ]
                )
            ).encode(),
        )
    assert exc2.value.code == "backend_error"
    assert validation_exit_code(exc2.value) == 1


def test_future_captured_source():
    fake = FakeBackend()
    rec = seed_raw(
        fake,
        "s1",
        "op1",
        b"z",
        captured="2035-06-01T12:00:00.000000Z",
    )
    body = proposal(
        snapshot_as_of="2030-01-01T00:00:00.000000Z",
        claims=[
            {
                "id": "c1",
                "text": "t",
                "status": "current",
                "citations": [cite("s1", "op1", rec, 0, 1, "z")],
            }
        ],
    )
    with pytest.raises(KnowledgeValidationError) as exc:
        validate_proposal(fake, PROJ, json.dumps(body).encode())
    assert exc.value.code == "source_after_snapshot"


def test_cli_malformed_before_store(tmp_path):
    store_path = tmp_path / "must-not-exist"
    p = run_cli(str(store_path), "knowledge-validate", "--proposal-file", str(tmp_path / "nope.json"))
    assert p.returncode == 2
    assert json.loads(p.stdout)["blocked"] is True
    assert not store_path.exists()


def test_cli_rejects_proposal_file_on_other_commands(tmp_path):
    p = run_cli(str(tmp_path), "session-list", "--proposal-file", "x.json")
    assert p.returncode != 0
    p2 = run_cli(
        str(tmp_path),
        "raw-session-read",
        "--session-id",
        "s1",
        "--operation-id",
        "op1",
        "--proposal-file",
        "x.json",
    )
    assert p2.returncode != 0


def test_malformed_proposal_before_backend(monkeypatch, tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "skill"))
    import knokeep_state as ks

    constructed = []
    monkeypatch.setattr(
        ks,
        "_backend",
        lambda _s: constructed.append(True) or FakeBackend(),
    )
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version":1}')
    with pytest.raises(KnowledgeValidationError):
        ks.knowledge_validate("unused", PROJ, str(bad))
    assert constructed == []
    with pytest.raises(KnowledgeValidationError) as exc:
        validate_proposal_structure(
            {
                "schema_version": 1,
                "snapshot_as_of": "2020-01-01T00:00:00.000000Z",
                "coverage": "selected_sources_only",
                "claims": [
                    {
                        "id": "c1",
                        "text": "t",
                        "status": {},
                        "citations": [],
                    }
                ],
            }
        )
    assert exc.value.code == "invalid_claim_status"


def test_strict_json_rejects_nan_and_surrogate():
    with pytest.raises(KnowledgeValidationError) as exc:
        parse_proposal_bytes(
            b'{"schema_version":NaN,"snapshot_as_of":"2020-01-01T00:00:00.000000Z",'
            b'"coverage":"selected_sources_only","claims":[]}'
        )
    assert exc.value.code == "invalid_json"
    raw = (
        '{"schema_version":1,"snapshot_as_of":"2020-01-01T00:00:00.000000Z",'
        '"coverage":"selected_sources_only","claims":[{"id":"c1","text":"t",'
        '"status":"current","citations":[{"session_id":"s1","operation_id":"op1",'
        '"source_sha256":"'
        + "a" * 64
        + '","record_id":"'
        + "b" * 64
        + '","byte_start":0,"byte_end":1,"quote":"\\ud800"}]}]}'
    ).encode("utf-8")
    with pytest.raises(KnowledgeValidationError) as exc2:
        parse_proposal_bytes(raw)
    assert exc2.value.code == "invalid_json"


def test_cli_closes_backend(monkeypatch, tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "skill"))
    import knokeep_state as ks

    class Owned(FakeBackend):
        closed = False

        def close(self):
            self.closed = True

    backend = Owned()
    monkeypatch.setattr(ks, "_backend", lambda _: backend)
    prop = tmp_path / "p.json"
    prop.write_text(
        json.dumps(
            {
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
                                "session_id": "s1",
                                "operation_id": "op1",
                                "source_sha256": "a" * 64,
                                "record_id": "b" * 64,
                                "byte_start": 0,
                                "byte_end": 1,
                                "quote": "x",
                            }
                        ],
                    }
                ],
            }
        )
    )
    with pytest.raises(KnowledgeValidationError):
        ks.knowledge_validate("unused", PROJ, str(prop))
    assert backend.closed is True

    backend_ok = Owned()
    rec2 = seed_raw(backend_ok, "s2", "op2", b"ok", captured="2020-01-01T00:00:00.000000Z")
    prop2 = tmp_path / "ok.json"
    prop2.write_text(
        json.dumps(
            proposal(
                claims=[
                    {
                        "id": "c1",
                        "text": "t",
                        "status": "current",
                        "citations": [cite("s2", "op2", rec2, 0, 2, "ok")],
                    }
                ]
            )
        )
    )
    monkeypatch.setattr(ks, "_backend", lambda _: backend_ok)
    out = ks.knowledge_validate("unused", PROJ, str(prop2))
    assert out["ok"] is True
    assert backend_ok.closed is True


def test_application_never_closes_injected_backend():
    fake = FakeBackend()
    rec = seed_raw(fake, "s1", "op1", b"ok", captured="2020-01-01T00:00:00.000000Z")
    body = proposal(
        claims=[
            {
                "id": "c1",
                "text": "t",
                "status": "current",
                "citations": [cite("s1", "op1", rec, 0, 2, "ok")],
            }
        ]
    )
    validate_proposal(HostileWriteBackend(fake), PROJ, json.dumps(body).encode())


def test_cli_missing_source_exits_two(tmp_path):
    fake_root = tmp_path / "store"
    fake_root.mkdir()
    # use in-memory via subprocess with LocalBackend empty — expect source_not_found exit 2
    prop = tmp_path / "prop.json"
    prop.write_text(
        json.dumps(
            {
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
                                "session_id": "s1",
                                "operation_id": "op1",
                                "source_sha256": "a" * 64,
                                "record_id": "b" * 64,
                                "byte_start": 0,
                                "byte_end": 1,
                                "quote": "x",
                            }
                        ],
                    }
                ],
            }
        )
    )
    p = run_cli(str(fake_root), "knowledge-validate", "--proposal-file", str(prop))
    assert p.returncode == 2


def test_cli_success_exit_zero(tmp_path):
    from store.local import LocalBackend

    store_path = str(tmp_path / "store")
    fake = LocalBackend(store_path)
    try:
        rec = append_raw_session(fake, PROJ, "cli-s", "cowork", "op-cli", b"yes", captured="2020-01-01T00:00:00.000000Z")
    finally:
        fake.close()
    prop = tmp_path / "good.json"
    prop.write_text(
        json.dumps(
            proposal(
                claims=[
                    {
                        "id": "c1",
                        "text": "t",
                        "status": "current",
                        "citations": [cite("cli-s", "op-cli", rec, 0, 3, "yes")],
                    }
                ]
            )
        )
    )
    p = run_cli(store_path, "knowledge-validate", "--proposal-file", str(prop))
    assert p.returncode == 0, p.stderr
    body = json.loads(p.stdout)
    assert body["ok"] is True
    assert body["semantic_support_verified"] is False


def test_cli_rejects_explicit_client(tmp_path):
    prop = tmp_path / "p.json"
    prop.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot_as_of": "2030-01-01T00:00:00.000000Z",
                "coverage": "selected_sources_only",
                "claims": [],
            }
        )
    )
    p = run_cli(str(tmp_path), "knowledge-validate", "--proposal-file", str(prop), "--client", "cowork")
    assert p.returncode == 2


def test_deep_json_has_bounded_rejection_before_store(tmp_path):
    raw = b'{"claims":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
    with pytest.raises(KnowledgeValidationError) as err:
        parse_proposal_bytes(raw)
    assert validation_exit_code(err.value) == 2
    prop = tmp_path / 'deep.json'
    prop.write_bytes(raw)
    store_path = tmp_path / 'absent'
    result = run_cli(str(store_path), 'knowledge-validate', '--proposal-file', str(prop))
    assert result.returncode == 2
    assert json.loads(result.stdout) == {'blocked': True, 'reason': 'invalid_json'}
    assert not store_path.exists()


@pytest.mark.parametrize('raw', [None, [], 'secret-value', 42])
def test_application_rejects_nonbyte_proposal(raw):
    with pytest.raises(KnowledgeValidationError):
        validate_proposal(HostileWriteBackend(FakeBackend()), PROJ, raw)


def test_public_structure_validator_rejects_unknown_root_and_bad_types():
    fake = FakeBackend()
    rec = seed_raw(fake, 's1', 'op1', b'ok', captured='2020-01-01T00:00:00.000000Z')
    obj = proposal(claims=[{'id':'c1','text':'t','status':'current','citations':[cite('s1','op1',rec,0,2,'ok')]}])
    obj['extra'] = 'not-allowed'
    with pytest.raises(KnowledgeValidationError):
        validate_proposal_structure(obj)
    del obj['extra']
    obj['claims'][0]['text'] = '\ud800'
    with pytest.raises(KnowledgeValidationError):
        validate_proposal_structure(obj)
    with pytest.raises(KnowledgeValidationError):
        validate_proposal_structure([])
