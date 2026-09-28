"""Focused tests for the EXPERIMENTAL checkpoint_v1 prototype."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from experiments.checkpoint_v1 import checkpoint as cp  # noqa: E402
from store import gate  # noqa: E402
from store.context import AuthContext, CreateOnly, OperationContext  # noqa: E402
from store.local import LocalBackend  # noqa: E402

P = "proj"


def _be(tmp_path):
    return LocalBackend(str(tmp_path / "store"))


def _p(mid, pred=None, **kw):
    kw.setdefault("state", f"state-{mid}")
    kw.setdefault("log", f"log-{mid}")
    return cp.build_proposal(milestone_id=mid, writer="w1", predecessor=pred, **kw)


def _ref(r):
    return {"milestone_id": r["milestone_id"], "sha256": r["checkpoint_sha256"]}


def test_complete_read_and_retry_and_duplicate_after_successor(tmp_path):
    be = _be(tmp_path)
    r1 = cp.save(be, P, _p("m1"))
    assert r1["status"] == "accepted" and not r1["duplicate"]
    res = cp.resume(be, P)
    assert (res["state"], res["log"]) == ("state-m1", "log-m1")
    # exact retry after lost ack
    again = cp.save(be, P, _p("m1"))
    assert again["duplicate"] is True
    r2 = cp.save(be, P, _p("m2", _ref(r1)))
    assert r2["status"] == "accepted"
    late = cp.save(be, P, _p("m1"))
    assert late["status"] == "accepted" and late["duplicate"] is True
    assert late["checkpoint_sha256"] == r1["checkpoint_sha256"]
    assert late["current_head_sha256"] == r2["checkpoint_sha256"]
    assert cp.lookup(be, P, "m1")["status"] == "accepted"


def test_same_id_changed_bytes_rejected_even_if_staged(tmp_path):
    be = _be(tmp_path)
    r1 = cp.save(be, P, _p("m1"))
    stale = cp.save(be, P, _p("x", None))  # wrong predecessor -> staged only
    assert stale["status"] == "staged_stale" and stale["duplicate"] is False
    assert cp.lookup(be, P, "x")["status"] == "staged"
    with pytest.raises(cp.CheckpointError):
        cp.save(be, P, _p("x", _ref(r1), state="different"))
    with pytest.raises(cp.CheckpointError):
        cp.save(be, P, _p("m1", state="different"))
    assert cp.resume(be, P)["staged"] == ["x"]


@pytest.mark.parametrize("mut", [
    lambda p: p.update(state_sha256="0" * 64),
    lambda p: p.update(extra=1),
    lambda p: p.update(version=True),
    lambda p: p.update(state="AKIAIOSFODNN7EXAMPLE secret", state_sha256=cp._sha(b"AKIAIOSFODNN7EXAMPLE secret")),
    lambda p: p.update(log="x" * (cp.MAX_OBJECT_BYTES + 1), log_sha256=cp._sha(b"x" * (cp.MAX_OBJECT_BYTES + 1))),
    lambda p: p.update(completed_actions=["a1"], open_questions=[
        {"id": "q1", "question": "?", "affected_action_ids": ["a1"]}]),
])
def test_invalid_input_reserves_no_id(tmp_path, mut):
    be = _be(tmp_path)
    p = _p("bad")
    mut(p)
    with pytest.raises(cp.CheckpointError):
        cp.save(be, P, p)
    assert cp.lookup(be, P, "bad")["status"] == "not_found"


def test_strict_parser():
    for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'not json'):
        with pytest.raises(cp.CheckpointError):
            cp._strict_loads(raw)


def test_unsealed_legacy_and_legacy_untouched(tmp_path):
    be = _be(tmp_path)
    ctx = OperationContext(auth=AuthContext(), precondition=CreateOnly())
    gate.persist(be, f"{P}/system_state", b"legacy state\n", ctx=ctx, doc_type="system_state")
    before = be.read(f"{P}/system_state").body
    cp.save(be, P, _p("m1", legacy_state_sha256=cp._sha(b"other"), legacy_log_sha256=None))
    res = cp.resume(be, P)
    assert res["unsealed_legacy_changes"] == ["system_state"]
    assert res["state"] == "state-m1"
    assert be.read(f"{P}/system_state").body == before
    assert be.read(f"{P}/session_log") is None


def test_missing_and_corrupt_target_fail_closed(tmp_path):
    # Tamper while the backend is open (a fresh LocalBackend replays its journal
    # and would repair the data file -- that is existing engine behavior).
    be = _be(tmp_path)
    cp.save(be, P, _p("m1"))
    f = tmp_path / "store" / "data" / P / "checkpoints_v1" / "proposals" / "m1"
    good = f.read_bytes()
    f.write_bytes(good.replace(b"state-m1", b"state-XX"))
    with pytest.raises(cp.CheckpointError):
        cp.resume(be, P)
    f.unlink()
    with pytest.raises(cp.CheckpointError):
        cp.resume(be, P)


_CHILD = textwrap.dedent("""
    import os, sys, json
    sys.path.insert(0, {root!r})
    from experiments.checkpoint_v1 import checkpoint as cp
    from store.local import LocalBackend
    mode, store, pin = sys.argv[1], sys.argv[2], sys.argv[3]
    orig_commit, orig_publish = LocalBackend._commit, LocalBackend._publish
    def commit(self, k, *a, **kw):
        r = orig_commit(self, k, *a, **kw)
        if mode == "kill_after_proposal" and "/proposals/" in k:
            os._exit(77)
        return r
    def publish(self, rel, data_path, raw, **kw):
        if mode == "kill_before_head_publish" and rel.as_posix().endswith("HEAD"):
            os._exit(78)  # head journal record already fsynced by _commit
        return orig_publish(self, rel, data_path, raw, **kw)
    LocalBackend._commit, LocalBackend._publish = commit, publish
    be = LocalBackend(store)
    p = json.load(open(pin))
    # TEST-ONLY short lease TTL (0.5s) so a killed holder expires quickly.
    print(json.dumps(cp.save(be, "proj", p, lease_ttl_s=0.5)))
""")


def _run_child(tmp_path, mode, proposal):
    pin = tmp_path / f"in-{mode}.json"
    pin.write_text(json.dumps(proposal))
    script = tmp_path / "child.py"
    script.write_text(_CHILD.format(root=str(ROOT)))
    return subprocess.run([sys.executable, str(script), mode, str(tmp_path / "store"), str(pin)],
                          capture_output=True, text=True, timeout=60)


def test_real_kill_after_proposal_durable_before_head(tmp_path):
    be = _be(tmp_path)
    r1 = cp.save(be, P, _p("m1"))
    be.close()
    out = _run_child(tmp_path, "kill_after_proposal", _p("m2", _ref(r1)))
    assert out.returncode == 77, out.stderr  # a REAL killed process, not a constructed prefix
    be = _be(tmp_path)
    res = cp.resume(be, P)
    assert (res["milestone_id"], res["state"], res["log"]) == ("m1", "state-m1", "log-m1")
    assert res["staged"] == ["m2"]
    assert cp.lookup(be, P, "m2")["status"] == "staged"


def test_real_kill_after_head_journal_before_publish(tmp_path):
    be = _be(tmp_path)
    r1 = cp.save(be, P, _p("m1"))
    be.close()
    out = _run_child(tmp_path, "kill_before_head_publish", _p("m2", _ref(r1)))
    assert out.returncode == 78, out.stderr
    be = _be(tmp_path)  # fresh reader: journal replay publishes the head
    res = cp.resume(be, P)
    assert (res["milestone_id"], res["state"], res["log"]) in (
        ("m1", "state-m1", "log-m1"), ("m2", "state-m2", "log-m2"))
    # retry after lost reply is duplicate if accepted
    if res["milestone_id"] == "m2":
        import time; time.sleep(0.6)  # wait out the killed child's test-only lease
        assert cp.save(be, P, _p("m2", _ref(r1)))["duplicate"] is True


def test_two_processes_same_predecessor(tmp_path):
    be = _be(tmp_path)
    r1 = cp.save(be, P, _p("m1"))
    be.close()
    script = tmp_path / "child.py"
    script.write_text(_CHILD.format(root=str(ROOT)))
    procs = []
    for mid in ("a", "b"):
        pin = tmp_path / f"{mid}.json"
        pin.write_text(json.dumps(_p(mid, _ref(r1))))
        procs.append(subprocess.Popen([sys.executable, str(script), "none",
                                       str(tmp_path / "store"), str(pin)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    outs = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
    statuses = sorted(o["status"] for o in outs)
    assert statuses == ["accepted", "staged_stale"]
    be = _be(tmp_path)
    res = cp.resume(be, P)
    loser = [o for o in outs if o["status"] == "staged_stale"][0]["milestone_id"]
    assert res["staged"] == [loser]
    assert cp.lookup(be, P, loser)["status"] == "staged"
