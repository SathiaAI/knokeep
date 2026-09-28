"""Regression coverage for lost-ack replay of the skill session append API."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "skill")]
import knokeep_state as ks
from store import gate
from store.context import create_ctx
from store.local import LocalBackend

def body(store):
    backend = LocalBackend(store)
    try:
        return backend.read("p/sessions/s").body
    finally:
        backend.close()

def append(store, text="alpha", op="job-1", client="test"):
    return ks.session_append(str(store), "p", "s", client, text, operation_id=op)

def test_retry_after_later_write_returns_current_hash_without_duplicate(tmp_path):
    first = append(tmp_path)
    append(tmp_path, "beta", "job-2")
    before = body(tmp_path)
    replay = append(tmp_path)
    assert first["duplicate"] is False and replay["duplicate"] is True
    assert body(tmp_path) == before
    assert before.count(b"] alpha\n") == 1
    assert replay["current_version_hash"] == hashlib.sha256(before).hexdigest()
    assert replay["current_version_hash"] != first["current_version_hash"]

@pytest.mark.parametrize("text,client", [("different", "test"), ("alpha", "other")])
def test_reused_id_with_different_payload_fails_without_document_change(tmp_path, text, client):
    append(tmp_path)
    before = body(tmp_path)
    journal = (tmp_path / "journal/journal.log").read_bytes()
    with pytest.raises(SystemExit, match="reused with different content"):
        append(tmp_path, text, client=client)
    assert body(tmp_path) == before
    assert (tmp_path / "journal/journal.log").read_bytes() == journal

def test_no_id_keeps_legacy_repeat_behavior(tmp_path):
    append(tmp_path, op=None)
    append(tmp_path, op=None)
    assert body(tmp_path).count(b"] alpha\n") == 2

def test_secret_rejection_does_not_reserve_operation_id(tmp_path):
    with pytest.raises(SystemExit):
        append(tmp_path, "AKIAEXAMPLE000000000")
    assert append(tmp_path)["duplicate"] is False

@pytest.mark.parametrize("ledger", ['not-json', '[]', '{"job-1":"bad-hash"}'])
def test_invalid_ledger_fails_closed(tmp_path, ledger):
    backend = LocalBackend(tmp_path)
    raw = f"---\nappend_operations: {ledger}\n---\n## Journal\n".encode()
    gate.persist(backend, "p/sessions/s", raw, ctx=create_ctx(), doc_type="journal")
    backend.close()
    before = body(tmp_path)
    with pytest.raises(SystemExit, match="invalid append operation ledger"):
        append(tmp_path)
    assert body(tmp_path) == before

@pytest.mark.parametrize("extra", [
    ["session-append", "--operation-id", "a", "--entry", "alpha"],
    ["bootstrap", "--operation-id", "a", "--session-id", "s"],
])
def test_cli_rejects_unbound_or_misplaced_operation_id_before_store(tmp_path, extra):
    store = tmp_path / "unused"
    p = subprocess.run([sys.executable, str(ROOT/"skill/knokeep_state.py"),
                        *extra, "--store", str(store), "--project", "p"],
                       capture_output=True, timeout=10)
    assert p.returncode == 2
    assert not store.exists()

def test_real_process_death_after_fsync_then_same_request_retries_once(tmp_path):
    script = """
import os,sys
sys.path[:0]=[sys.argv[1],sys.argv[1]+'/skill']
import knokeep_state as ks
from store.local import LocalBackend
original=LocalBackend._journal_append
def die_after_commit(self,*a,**kw):
    original(self,*a,**kw)
    os._exit(73)
LocalBackend._journal_append=die_after_commit
ks._LEASE_TTL_S=.1
ks.session_append(sys.argv[2],'p','s','test','alpha',operation_id='job-1')
"""
    p = subprocess.run([sys.executable, "-c", script, str(ROOT), str(tmp_path)],
                       capture_output=True, timeout=10)
    assert p.returncode == 73, p.stderr
    time.sleep(.15)
    assert body(tmp_path).count(b"] alpha\n") == 1
    before = (tmp_path/"journal/journal.log").read_bytes()
    assert append(tmp_path)["duplicate"] is True
    assert body(tmp_path).count(b"] alpha\n") == 1
    assert (tmp_path/"journal/journal.log").read_bytes() == before

def test_two_fresh_processes_same_operation_append_once(tmp_path):
    script = """
import pathlib,sys,time,json
sys.path[:0]=[sys.argv[1],sys.argv[1]+'/skill']
import knokeep_state as ks
deadline=time.monotonic()+5
while not pathlib.Path(sys.argv[3]).exists():
    if time.monotonic()>deadline:raise RuntimeError('barrier timeout')
    time.sleep(.01)
print(json.dumps(ks.session_append(sys.argv[2],'p','s','test','alpha',operation_id='job-1')))
"""
    trigger = tmp_path/"go"
    store = tmp_path/"store"
    procs = [subprocess.Popen([sys.executable, "-c", script, str(ROOT), str(store), str(trigger)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    outputs = []
    try:
        trigger.write_text("go")
        for p in procs:
            out, err = p.communicate(timeout=15)
            assert p.returncode == 0, err
            outputs.append(json.loads(out))
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.communicate(timeout=3)
    assert sorted(x["duplicate"] for x in outputs) == [False, True]
    assert body(store).count(b"] alpha\n") == 1

