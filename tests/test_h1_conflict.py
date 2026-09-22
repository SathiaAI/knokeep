"""Tests for the H1 Increment 1 skill-side conflict protocol
(bounded_cas_reread_reapply) in skill/knokeep_state.py.

Races are simulated deterministically by monkeypatching the module-level
_persist() to inject a competing write between a flush call's read and its
own persist attempt. The injected competitor only fires on writes to the
STATE doc (kind == "state"), never on the conflict-park or journal writes the
protocol itself performs — so the simulation models a real racing writer
without perturbing the code-under-test's own persistence.
"""
import json
import pathlib
import sys
import threading

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if p not in sys.path:
        sys.path.insert(0, p)

import knokeep_state as ks  # noqa: E402


def _direct_section_write(persist_fn, store, project, section, content, expect_hash):
    """Bypass flush_state entirely: perform one raw, successful CAS write to
    a section using the given (real, unpatched) persist function — simulates a
    competing writer landing first."""
    backend = ks._backend(store)
    key = ks._key(project, "state")
    blob = backend.read(key)
    fm, body = ks.parse(blob.body.decode("utf-8"))
    fm = dict(fm)
    fm["revision"] = int(fm.get("revision", 0)) + 1
    fm["updated"] = ks.now()
    new_body = ks._replace_section(body, section, content)
    res = persist_fn(backend, key, "state", ks._bytes(fm, new_body), expect_hash)
    assert isinstance(res, ks.OK), f"direct competing write failed: {res}"
    return res.new_hash


def _die_payload(exc_info):
    return json.loads(str(exc_info.value))


def test_disjoint_sections_both_succeed_via_reapply(tmp_path, monkeypatch):
    store = str(tmp_path)
    project = "proj-disjoint"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")

    real_persist = ks._persist
    done = {"v": False}

    def hijacked(backend, key, kind, raw_bytes, expected_hash):
        if not done["v"] and kind == "state":
            done["v"] = True
            # A concurrent writer commits to a DIFFERENT section first.
            _direct_section_write(real_persist, store, project, "Notes",
                                   "notes-from-writer2", r0["version_hash"])
        return real_persist(backend, key, kind, raw_bytes, expected_hash)

    monkeypatch.setattr(ks, "_persist", hijacked)

    result = ks.flush_state(store, project, "state=running",
                             expect_hash=r0["version_hash"], section="Active State")
    assert result["ok"] is True

    backend = ks._backend(store)
    blob = backend.read(ks._key(project, "state"))
    fm, body = ks.parse(blob.body.decode("utf-8"))
    assert ks._get_section(body, "Active State")[2].strip() == "state=running"
    assert ks._get_section(body, "Notes")[2].strip() == "notes-from-writer2"


def test_same_section_conflict_is_parked_not_lost(tmp_path, monkeypatch):
    store = str(tmp_path)
    project = "proj-same-section"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")

    real_persist = ks._persist
    done = {"v": False}

    def hijacked(backend, key, kind, raw_bytes, expected_hash):
        if not done["v"] and kind == "state":
            done["v"] = True
            # A concurrent writer commits to the SAME section first.
            _direct_section_write(real_persist, store, project, "Active State",
                                   "winner-content", r0["version_hash"])
        return real_persist(backend, key, kind, raw_bytes, expected_hash)

    monkeypatch.setattr(ks, "_persist", hijacked)

    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, "loser-content",
                        expect_hash=r0["version_hash"], section="Active State")
    payload = _die_payload(exc)
    assert payload["blocked"] is True
    assert payload["reason"] == "conflict_parked"
    conflict_key = payload["conflict_key"]

    backend = ks._backend(store)

    # Winner's write is intact.
    blob = backend.read(ks._key(project, "state"))
    fm, body = ks.parse(blob.body.decode("utf-8"))
    assert ks._get_section(body, "Active State")[2].strip() == "winner-content"

    # Loser's body was parked verbatim.
    cblob = backend.read(conflict_key)
    assert cblob is not None
    assert cblob.body.decode("utf-8") == "loser-content"

    # Journal has a conflict record with the key, no body bytes.
    # Conflict notes route to a FIXED, gate-safe "conflicts" journal.
    jblob = backend.read(ks._key(project, "journal", "conflicts"))
    assert jblob is not None
    jtext = jblob.body.decode("utf-8")
    assert "conflict_parked" in jtext
    assert conflict_key in jtext
    assert "loser-content" not in jtext


def test_whole_doc_stale_is_parked_not_resubmitted(tmp_path, monkeypatch):
    store = str(tmp_path)
    project = "proj-whole-doc"
    r0 = ks.flush_state(store, project, "v0")

    real_persist = ks._persist
    done = {"v": False}

    def hijacked(backend, key, kind, raw_bytes, expected_hash):
        if not done["v"] and kind == "state":
            done["v"] = True
            # Competing whole-doc writer commits first, using the same base hash.
            backend2 = ks._backend(store)
            key2 = ks._key(project, "state")
            blob = backend2.read(key2)
            fm, _ = ks.parse(blob.body.decode("utf-8"))
            fm = dict(fm)
            fm["revision"] = int(fm.get("revision", 0)) + 1
            fm["updated"] = ks.now()
            res = real_persist(backend2, key2, "state", ks._bytes(fm, "winner-doc"), r0["version_hash"])
            assert isinstance(res, ks.OK)
        return real_persist(backend, key, kind, raw_bytes, expected_hash)

    monkeypatch.setattr(ks, "_persist", hijacked)

    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, "loser-doc", expect_hash=r0["version_hash"])
    payload = _die_payload(exc)
    assert payload["reason"] == "conflict_parked"

    backend = ks._backend(store)
    blob = backend.read(ks._key(project, "state"))
    fm, body = ks.parse(blob.body.decode("utf-8"))
    assert body.strip() == "winner-doc"

    cblob = backend.read(payload["conflict_key"])
    assert cblob.body.decode("utf-8") == "loser-doc"


def test_retry_exhaustion_is_parked(tmp_path, monkeypatch):
    store = str(tmp_path)
    project = "proj-exhaustion"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")

    real_persist = ks._persist
    state_calls = {"n": 0}

    def hijacked(backend, key, kind, raw_bytes, expected_hash):
        if kind == "state":
            state_calls["n"] += 1
            # Every state attempt: a competitor edits a DIFFERENT section right
            # before we persist, so our target section never conflicts in
            # content, but our CAS token is stale every time -> exhaustion.
            cur_hash = ks._backend(store).read(ks._key(project, "state")).version_hash
            _direct_section_write(real_persist, store, project, "Notes",
                                   f"notes-{state_calls['n']}", cur_hash)
        return real_persist(backend, key, kind, raw_bytes, expected_hash)

    monkeypatch.setattr(ks, "_persist", hijacked)

    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, "loser-active",
                        expect_hash=r0["version_hash"], section="Active State")
    payload = _die_payload(exc)
    assert payload["reason"] == "conflict_retry_exhausted"
    assert state_calls["n"] == ks._MAX_CAS_ATTEMPTS

    backend = ks._backend(store)
    blob = backend.read(ks._key(project, "state"))
    fm, body = ks.parse(blob.body.decode("utf-8"))
    assert ks._get_section(body, "Active State")[2].strip() == ""  # loser never applied

    cblob = backend.read(payload["conflict_key"])
    assert cblob.body.decode("utf-8") == "loser-active"


def test_f1_winner_committed_before_first_read_parks(tmp_path):
    """F1 regression (adversarial finding): a winner that commits BEFORE the
    loser's flush even reads must NOT be silently overwritten. The loser holds a
    now-stale expect_hash; it must park, never clobber, never return success."""
    store = str(tmp_path)
    project = "proj-f1"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")
    # Winner commits to Active State using r0's hash -> store advances past r0.
    _direct_section_write(ks._persist, store, project, "Active State", "winner", r0["version_hash"])
    # Loser flushes the SAME section with the now-stale r0 hash: must park.
    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, "loser", expect_hash=r0["version_hash"], section="Active State")
    payload = _die_payload(exc)
    assert payload["reason"] == "conflict_parked"
    backend = ks._backend(store)
    blob = backend.read(ks._key(project, "state"))
    _, body = ks.parse(blob.body.decode("utf-8"))
    assert ks._get_section(body, "Active State")[2].strip() == "winner"   # winner intact
    assert backend.read(payload["conflict_key"]).body.decode("utf-8") == "loser"  # loser parked


def test_crlf_section_replace_no_duplicate():
    """F3 regression: CRLF '## Heading' lines must be found (not appended as a
    duplicate), and a sibling section preserved."""
    body = "## Active State\r\nold\r\n\r\n## Notes\r\nkeep\r\n"
    got = ks._get_section(body, "Active State")
    assert got is not None                       # heading found despite CRLF
    new = ks._replace_section(body, "Active State", "fresh")
    assert new.count("## Active State") == 1     # not duplicated
    assert "fresh" in new and "keep" in new      # replaced + sibling preserved
    assert "old" not in new


def test_conflict_key_gate_safe_with_high_entropy_session(tmp_path):
    """Codex P2: a valid but random-looking session id must NOT make the conflict
    key trip the gate's high-entropy KEY scanner (which would wrongly fail the
    park / mislabel it conflict_body_secret_blocked). The key uses a hex digest
    of the session, so parking a CLEAN body still succeeds."""
    store = str(tmp_path)
    project = "proj-randsess"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")
    sid = "aB3xK9mQ2pL5vN8wR1tY4uZ7cD0eF6gH"  # valid id, high-entropy / random-looking
    # Winner commits Active State before the loser reads -> loser must park.
    _direct_section_write(ks._persist, store, project, "Active State", "winner", r0["version_hash"])
    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, "loser-clean-body",
                        expect_hash=r0["version_hash"], section="Active State", session=sid)
    payload = _die_payload(exc)
    assert payload["reason"] == "conflict_parked"      # not conflict_body_secret_blocked / park_failed
    # The gate-safe journal write must succeed for a high-entropy session too:
    # the note uses a hex session label, so the journal key is not entropy-flagged.
    assert payload["journal_recorded"] is True
    backend = ks._backend(store)
    assert backend.read(payload["conflict_key"]).body.decode("utf-8") == "loser-clean-body"


def test_whole_doc_park_records_caller_base_not_winner(tmp_path):
    """Codex P2: the parked whole-doc conflict must record the caller's DECLARED
    base (expect_hash) as base_hash, distinct from the winner's current_hash —
    preserving lineage."""
    store = str(tmp_path)
    project = "proj-lineage"
    r0 = ks.flush_state(store, project, "v0")
    # Winner commits a whole-doc update using r0's hash -> store advances.
    backend = ks._backend(store)
    k = ks._key(project, "state")
    blob = backend.read(k)
    fm, _ = ks.parse(blob.body.decode("utf-8"))
    fm = dict(fm); fm["revision"] = int(fm.get("revision", 0)) + 1; fm["updated"] = ks.now()
    res = ks._persist(backend, k, "state", ks._bytes(fm, "winner-doc"), r0["version_hash"])
    assert isinstance(res, ks.OK)
    winner_hash = res.new_hash
    # Loser flushes whole-doc with the now-stale r0 hash -> parks.
    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, "loser-doc", expect_hash=r0["version_hash"])
    payload = _die_payload(exc)
    assert payload["reason"] == "conflict_parked"
    assert payload["base_hash"] == r0["version_hash"]        # caller's declared base
    assert payload["current_hash"] == winner_hash            # the winner
    assert payload["base_hash"] != payload["current_hash"]   # distinct lineage


def test_threaded_disjoint_sections_no_loss(tmp_path):
    store = str(tmp_path)
    project = "proj-threaded"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")

    barrier = threading.Barrier(2)
    results = {}

    def worker(name, section, content):
        barrier.wait()
        try:
            results[name] = ks.flush_state(store, project, content,
                                            expect_hash=r0["version_hash"], section=section)
        except SystemExit as e:
            results[name] = json.loads(str(e))

    t1 = threading.Thread(target=worker, args=("w1", "Active State", "thread-active"))
    t2 = threading.Thread(target=worker, args=("w2", "Notes", "thread-notes"))
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)

    assert "w1" in results and "w2" in results
    backend = ks._backend(store)
    blob = backend.read(ks._key(project, "state"))
    fm, body = ks.parse(blob.body.decode("utf-8"))

    for name, section, content in (("w1", "Active State", "thread-active"),
                                    ("w2", "Notes", "thread-notes")):
        r = results[name]
        if r.get("ok"):
            assert ks._get_section(body, section)[2].strip() == content
        else:
            assert r["reason"] in ("conflict_parked", "conflict_retry_exhausted")
            cblob = backend.read(r["conflict_key"])
            assert cblob is not None
            assert cblob.body.decode("utf-8") == content


def test_section_eq_semantics():
    """_section_eq: trailing-newline-insensitive, but None (absent) stays
    distinct from "" (present-but-empty) so a real add/remove is not masked."""
    assert ks._section_eq("body", "body\n") is True
    assert ks._section_eq("body\n\n", "body") is True
    assert ks._section_eq(None, None) is True
    assert ks._section_eq(None, "") is False       # absent vs empty: a real change
    assert ks._section_eq("", None) is False
    assert ks._section_eq("a", "b") is False


def test_bootstrap_surfaces_parked_conflicts(tmp_path):
    """A resuming session must SEE parked conflicts, never silently miss a
    losing writer's work: bootstrap reports conflict_count and a marked
    resume_line."""
    store = str(tmp_path)
    project = "proj-bootstrap-surface"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")

    # No conflicts yet.
    b0 = ks.bootstrap(store, project)
    assert b0["conflict_count"] == 0
    assert "parked conflict" not in b0["resume_line"]

    # Force a park: winner commits Active State before the loser reads.
    _direct_section_write(ks._persist, store, project, "Active State",
                          "winner", r0["version_hash"])
    with pytest.raises(SystemExit):
        ks.flush_state(store, project, "loser-body",
                        expect_hash=r0["version_hash"], section="Active State")

    b1 = ks.bootstrap(store, project)
    assert b1["conflict_count"] == 1
    assert "[!] 1 parked conflict(s)" in b1["resume_line"]
    assert len(b1["conflicts"]) == 1
