"""Q11 session query API + CLI adapters."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile

import pytest
import store

DELIVERY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(store.__file__)))
sys.path.insert(0, DELIVERY_ROOT)

from application.identifiers import valid_id
from application.session_queries import (
    MAX_LIST_LIMIT,
    MAX_LIST_SCAN_KEYS,
    SessionQueryError,
    list_sessions,
    read_session,
)
from store import gate
from store.backend import BackendBusyError, BackendCorruptionError
from store.fake import FakeBackend
from store.local import LocalBackend
from store.types import OK, sha256_hex
from tests.ctx_helpers import create_ctx

STATE = os.path.join(DELIVERY_ROOT, "skill", "knokeep_state.py")
PROJ = "demo"


class HostileWriteBackend:
    """Fails if any mutating StoreBackend method is invoked."""

    def __init__(self, inner):
        self._inner = inner

    def read(self, key):
        return self._inner.read(key)

    def list(self, prefix):
        return self._inner.list(prefix)

    def write(self, *a, **k):
        raise AssertionError("write must not be called from query layer")

    def lock(self, *a, **k):
        raise AssertionError("lock must not be called from query layer")

    def unlock(self, *a, **k):
        raise AssertionError("unlock must not be called from query layer")

    def renew(self, *a, **k):
        raise AssertionError("renew must not be called from query layer")

    def capabilities(self):
        return self._inner.capabilities()

    def health(self):
        return self._inner.health()


def run_cli(store, *args, project=PROJ):
    env = os.environ.copy()
    env["PYTHONPATH"] = DELIVERY_ROOT + os.pathsep + REPO_ROOT
    p = subprocess.run(
        [sys.executable, STATE, *args, "--store", store, "--project", project],
        capture_output=True,
        text=True,
        env=env,
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def store_tree_snapshot(path: str) -> set[str]:
    snap = set()
    if not os.path.isdir(path):
        return snap
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            snap.add(os.path.relpath(os.path.join(root, name), path))
    return snap


def seed_session(fake: FakeBackend, project: str, sid: str, body: bytes) -> None:
    key = f"{project}/sessions/{sid}"
    h = sha256_hex(body)
    fake._store[key] = (body, h)


def test_valid_id_rejects_newline_suffix():
    assert not valid_id("p\n")
    assert not valid_id("p\r")
    assert not valid_id("a b")
    assert valid_id("p")


def test_read_binary_and_utf8_roundtrip():
    fake = FakeBackend()
    body = b"line1\n\xc3\xa9\n\xff\xfe"
    seed_session(fake, "p", "s1", body)
    hostile = HostileWriteBackend(fake)
    got = read_session(hostile, "p", "s1")
    assert got.found
    assert base64.standard_b64decode(got.body_base64) == body
    assert got.content_hash == sha256_hex(body)
    assert got.body_utf8 is None


def test_read_utf8_optional_text():
    fake = FakeBackend()
    body = b"---\nschema_version: 1\n---\n## Journal\n"
    seed_session(fake, "p", "utf8", body)
    got = read_session(fake, "p", "utf8")
    assert got.body_utf8 == body.decode("utf-8")


def test_read_not_found():
    fake = FakeBackend()
    got = read_session(fake, "p", "missing")
    assert not got.found
    assert got.content_hash is None


def test_read_hash_mismatch_errors():
    fake = FakeBackend()
    key = "p/sessions/bad"
    fake._store[key] = (b"x", "0" * 64)
    with pytest.raises(SessionQueryError) as ei:
        read_session(fake, "p", "bad")
    assert ei.value.reason == "hash_mismatch"


def test_invalid_ids_rejected():
    fake = FakeBackend()
    with pytest.raises(SessionQueryError):
        read_session(fake, "../evil", "s")
    with pytest.raises(SessionQueryError):
        read_session(fake, "p", "../evil")
    with pytest.raises(SessionQueryError):
        list_sessions(fake, "..")


def test_list_prefix_does_not_leak_adjacent_projects():
    fake = FakeBackend()
    seed_session(fake, "demo", "a", b"j")
    seed_session(fake, "demo-extra", "b", b"j")
    fake._store["demoX/sessions/x"] = (b"j", sha256_hex(b"j"))
    ids = list_sessions(fake, "demo").session_ids
    assert ids == ("a",)


def test_list_rejects_malformed_session_keys():
    fake = FakeBackend()
    fake._store["p/sessions/valid"] = (b"j", sha256_hex(b"j"))
    fake._store["p/sessions/nested/extra"] = (b"j", sha256_hex(b"j"))
    with pytest.raises(SessionQueryError) as ei:
        list_sessions(fake, "p")
    assert ei.value.reason == "invalid_session_key"


def test_list_bounded_truncation_and_continuation():
    fake = FakeBackend()
    for i in range(5):
        seed_session(fake, "p", f"s{i:02d}", b"x")
    first = list_sessions(fake, "p", limit=2)
    assert first.session_ids == ("s00", "s01")
    assert first.truncated
    assert first.next_after == "s01"
    second = list_sessions(fake, "p", limit=2, after=first.next_after)
    assert second.session_ids == ("s02", "s03")
    assert second.truncated
    third = list_sessions(fake, "p", limit=2, after=second.next_after)
    assert third.session_ids == ("s04",)
    assert not third.truncated


def test_list_empty_success():
    fake = FakeBackend()
    got = list_sessions(fake, "p")
    assert got.session_ids == ()
    assert not got.truncated


def test_list_invalid_limit():
    fake = FakeBackend()
    with pytest.raises(SessionQueryError):
        list_sessions(fake, "p", limit=0)
    with pytest.raises(SessionQueryError):
        list_sessions(fake, "p", limit=MAX_LIST_LIMIT + 1)
    with pytest.raises(SessionQueryError):
        list_sessions(fake, "p", limit=True)


def test_scan_limit_exceeded():
    fake = FakeBackend()
    for i in range(MAX_LIST_SCAN_KEYS + 1):
        sid = f"k{i:04d}"
        fake._store[f"p/sessions/{sid}"] = (b"x", sha256_hex(b"x"))
    with pytest.raises(SessionQueryError) as ei:
        list_sessions(fake, "p", limit=MAX_LIST_LIMIT)
    assert ei.value.reason == "scan_limit_exceeded"


def test_query_layer_never_mutates():
    fake = FakeBackend()
    seed_session(fake, "p", "s", b"body")
    hostile = HostileWriteBackend(fake)
    read_session(hostile, "p", "s")
    list_sessions(hostile, "p")


def test_read_corruption_not_not_found():
    class CorruptRead(FakeBackend):
        def read(self, key):
            raise BackendCorruptionError("corrupt")

    with pytest.raises(BackendCorruptionError):
        read_session(CorruptRead(), "p", "s")


def test_list_corruption_not_empty_success():
    class CorruptList(FakeBackend):
        def list(self, prefix):
            raise BackendCorruptionError("corrupt")

    with pytest.raises(BackendCorruptionError):
        list_sessions(CorruptList(), "p")


def test_list_iterator_error_propagates():
    class BrokenIter(FakeBackend):
        def list(self, prefix):
            def gen():
                yield "p/sessions/a"
                raise RuntimeError("iterator failed")

            return gen()

    with pytest.raises(RuntimeError, match="iterator failed"):
        list_sessions(BrokenIter(), "p")


def test_read_permission_error_propagates():
    class Denied(FakeBackend):
        def read(self, key):
            raise PermissionError("denied")

    with pytest.raises(PermissionError):
        read_session(Denied(), "p", "s")


def test_cli_missing_session_id_json_before_store():
    store = tempfile.mkdtemp(prefix="knokeep_q11_nosid_")
    try:
        before = store_tree_snapshot(store)
        rc, out, err = run_cli(store, "session-read")
        after = store_tree_snapshot(store)
        assert rc == 2
        body = json.loads(out)
        assert body["blocked"] and body["reason"] == "missing_session_id"
        assert not err
        assert before == after
    finally:
        import shutil

        shutil.rmtree(store, ignore_errors=True)


def test_cli_invalid_session_id_json_before_store():
    store = tempfile.mkdtemp(prefix="knokeep_q11_badsid_")
    try:
        before = store_tree_snapshot(store)
        rc, out, err = run_cli(store, "session-read", "--session-id", "p\n")
        after = store_tree_snapshot(store)
        assert rc == 2
        assert json.loads(out)["reason"] == "invalid_session_id"
        assert before == after
    finally:
        import shutil

        shutil.rmtree(store, ignore_errors=True)


def test_cli_invalid_list_limit_json_before_store():
    store = tempfile.mkdtemp(prefix="knokeep_q11_badlim_")
    try:
        before = store_tree_snapshot(store)
        rc, out, err = run_cli(store, "session-list", "--list-limit", "nope")
        after = store_tree_snapshot(store)
        assert rc == 2
        assert json.loads(out)["reason"] == "invalid_limit"
        assert before == after
    finally:
        import shutil

        shutil.rmtree(store, ignore_errors=True)


def test_cli_unknown_option_json():
    store = tempfile.mkdtemp(prefix="knokeep_q11_unknown_")
    try:
        before = store_tree_snapshot(store)
        rc, out, err = run_cli(store, "session-list", "--not-a-real-flag")
        after = store_tree_snapshot(store)
        assert rc == 2
        assert json.loads(out)["reason"] == "invalid_argument"
        assert before == after
    finally:
        import shutil

        shutil.rmtree(store, ignore_errors=True)


def test_cli_query_closes_backend(monkeypatch):
    import skill.knokeep_state as ks

    calls = {"close": 0}

    class TrackBackend:
        def close(self):
            calls["close"] += 1

        def read(self, key):
            return None

        def list(self, prefix):
            return iter(())

        def write(self, *a, **k):
            raise AssertionError

        def lock(self, *a, **k):
            raise AssertionError

        def unlock(self, *a, **k):
            raise AssertionError

        def renew(self, *a, **k):
            raise AssertionError

        def capabilities(self):
            return FakeBackend().capabilities()

        def health(self):
            return FakeBackend().health()

    monkeypatch.setattr(ks, "_backend", lambda store: TrackBackend())
    ks.session_read("/tmp/unused", "p", "s")
    assert calls["close"] == 1


def test_local_backend_roundtrip_via_cli():
    store = tempfile.mkdtemp(prefix="knokeep_q11_")
    try:
        rc, out, err = run_cli(store, "init")
        assert rc == 0, err
        sid = "20260928-2300-test"
        entry = "caf\xe9 binary \xff ok in journal"
        rc, out, err = run_cli(
            store,
            "session-append",
            "--session-id",
            sid,
            "--entry",
            entry,
        )
        assert rc == 0, err
        rc, out, err = run_cli(store, "session-read", "--session-id", sid)
        assert rc == 0, err
        data = json.loads(out)
        assert data["found"]
        raw = base64.standard_b64decode(data["body_base64"])
        assert entry in raw.decode("utf-8")
        assert data["content_hash"] == hashlib.sha256(raw).hexdigest()
        rc, out, err = run_cli(store, "session-list")
        assert rc == 0, err
        listed = json.loads(out)
        assert sid in listed["session_ids"]
    finally:
        import shutil

        shutil.rmtree(store, ignore_errors=True)


def test_cli_unknown_session_not_found():
    store = tempfile.mkdtemp(prefix="knokeep_q11_nf_")
    try:
        run_cli(store, "init")
        rc, out, err = run_cli(store, "session-read", "--session-id", "20260928-9999-none")
        assert rc == 0
        assert json.loads(out)["found"] is False
    finally:
        import shutil

        shutil.rmtree(store, ignore_errors=True)


def test_cli_validation_exit_2():
    store = tempfile.mkdtemp(prefix="knokeep_q11_val_")
    try:
        run_cli(store, "init")
        rc, out, err = run_cli(store, "session-read", "--session-id", "../evil")
        assert rc == 2
        assert json.loads(out)["blocked"]
    finally:
        import shutil

        shutil.rmtree(store, ignore_errors=True)


def test_interleaved_append_then_read():
    store = tempfile.mkdtemp(prefix="knokeep_q11_inter_")
    try:
        run_cli(store, "init")
        sid = "20260928-inter"
        run_cli(store, "session-append", "--session-id", sid, "--entry", "first")
        rc, out, _ = run_cli(store, "session-read", "--session-id", sid)
        h1 = json.loads(out)["content_hash"]
        run_cli(store, "session-append", "--session-id", sid, "--entry", "second")
        rc, out, _ = run_cli(store, "session-read", "--session-id", sid)
        h2 = json.loads(out)["content_hash"]
        assert h1 != h2
        assert b"second" in base64.standard_b64decode(json.loads(out)["body_base64"])
    finally:
        import shutil

        shutil.rmtree(store, ignore_errors=True)


def test_local_backend_copy_read_after_gate_persist(tmp_path):
    backend = LocalBackend(tmp_path)
    body = b"---\nschema_version: 1\n---\n## Journal\n[ts] x\n"
    key = f"{PROJ}/sessions/direct"
    assert isinstance(
        gate.persist(backend, key, body, ctx=create_ctx(), doc_type="journal"),
        OK,
    )
    got = read_session(backend, PROJ, "direct")
    assert got.found
    assert base64.standard_b64decode(got.body_base64) == body
    backend.close()


def test_backend_busy_surfaces():
    class BusyRead(FakeBackend):
        def read(self, key):
            raise BackendBusyError("busy")

    with pytest.raises(BackendBusyError):
        read_session(BusyRead(), "p", "s")
