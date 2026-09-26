"""Tests for the H1 Increment 1 skill-side conflict protocol
(bounded_cas_reread_reapply) in skill/knokeep_state.py.

H1 Increment 2 (D-006/D-007): the skill now HOLDS a per-key advisory lease
across read->write, so same-doc writers SERIALIZE and a stale-base writer
PARKS its edit for review (never auto-merged, never lost). Races are therefore
simulated deterministically by committing a competing write BEFORE the loser's
flush (a pre-lease competitor via _direct_section_write) so the loser reads an
already-advanced doc under its lease and parks; retry-exhaustion is simulated by
monkeypatching _persist to force STALE on the state overwrite. The Increment-1
mid-write injection no longer applies: while a writer holds the lease, no
competitor can interpose on the same key.
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


def test_disjoint_section_stale_base_parks(tmp_path):
    """D-007 serialize+park: under the held lease, a writer editing a DIFFERENT
    section with a STALE base no longer auto-merges — it parks (never lost).
    Increment-1 auto-merged this; held-lease serialization + the F1 guard make
    the safe choice to park a never-observed base."""
    store = str(tmp_path)
    project = "proj-disjoint"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")

    # Competitor commits "Notes" first (pre-lease). The loser edits the DISJOINT
    # "Active State" but with the now-stale r0 base -> parks, does not merge.
    _direct_section_write(ks._persist, store, project, "Notes",
                          "notes-from-writer2", r0["version_hash"])

    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, "state=running",
                        expect_hash=r0["version_hash"], section="Active State")
    payload = _die_payload(exc)
    assert payload["reason"] == "conflict_parked"

    backend = ks._backend(store)
    blob = backend.read(ks._key(project, "state"))
    fm, body = ks.parse(blob.body.decode("utf-8"))
    # Competitor's Notes intact; loser's Active State NOT applied, parked verbatim.
    assert ks._get_section(body, "Notes")[2].strip() == "notes-from-writer2"
    assert ks._get_section(body, "Active State")[2].strip() == ""
    assert backend.read(payload["conflict_key"]).body.decode("utf-8") == "state=running"


def test_disjoint_section_current_base_succeeds(tmp_path):
    """The complement: a writer editing a section with the CURRENT base still
    succeeds under the held lease — serialization does not block legitimate
    sequential edits, it only parks stale-base ones."""
    store = str(tmp_path)
    project = "proj-disjoint-ok"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")

    # Writer 1 commits Notes; writer 2 re-reads the CURRENT hash and edits the
    # disjoint Active State -> succeeds (no conflict, no park).
    h1 = _direct_section_write(ks._persist, store, project, "Notes",
                               "notes-1", r0["version_hash"])
    result = ks.flush_state(store, project, "state=running",
                             expect_hash=h1, section="Active State")
    assert result["ok"] is True

    backend = ks._backend(store)
    blob = backend.read(ks._key(project, "state"))
    _, body = ks.parse(blob.body.decode("utf-8"))
    assert ks._get_section(body, "Active State")[2].strip() == "state=running"
    assert ks._get_section(body, "Notes")[2].strip() == "notes-1"
    assert list(backend.list(project + "/conflicts/")) == []


def test_same_section_conflict_is_parked_not_lost(tmp_path):
    store = str(tmp_path)
    project = "proj-same-section"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")

    # A concurrent writer commits to the SAME section first (pre-lease), so the
    # loser reads an already-advanced doc under its held lease and must park.
    _direct_section_write(ks._persist, store, project, "Active State",
                          "winner-content", r0["version_hash"])

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


def test_whole_doc_stale_is_parked_not_resubmitted(tmp_path):
    store = str(tmp_path)
    project = "proj-whole-doc"
    r0 = ks.flush_state(store, project, "v0")

    # Competing whole-doc writer commits first (pre-lease) using r0's base.
    backend2 = ks._backend(store)
    key2 = ks._key(project, "state")
    blob = backend2.read(key2)
    fm, _ = ks.parse(blob.body.decode("utf-8"))
    fm = dict(fm); fm["revision"] = int(fm.get("revision", 0)) + 1; fm["updated"] = ks.now()
    res = ks._persist(backend2, key2, "state", ks._bytes(fm, "winner-doc"), r0["version_hash"])
    assert isinstance(res, ks.OK)

    # Loser flushes whole-doc with the now-stale r0 hash -> parks (single CAS
    # attempt, never blindly resubmitted).
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
    state_overwrites = {"n": 0}

    def hijacked(backend, key, kind, raw_bytes, expected_hash, lease=None):
        # Under the held lease a same-key competitor cannot interpose mid-hold,
        # so the retry loop is now driven by repeated fence/CAS loss. Simulate it
        # deterministically: force every STATE overwrite to STALE so the section
        # reapply loop exhausts _MAX_CAS_ATTEMPTS and parks the loser verbatim.
        # Conflict-park creates (expected_hash None) pass through to the real
        # backend so parking still succeeds.
        if kind == "state" and expected_hash is not None:
            state_overwrites["n"] += 1
            cur = backend.read(key)
            return ks.STALE(cur.version_hash if cur else None)
        return real_persist(backend, key, kind, raw_bytes, expected_hash, lease=lease)

    monkeypatch.setattr(ks, "_persist", hijacked)

    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, "loser-active",
                        expect_hash=r0["version_hash"], section="Active State")
    payload = _die_payload(exc)
    assert payload["reason"] == "conflict_retry_exhausted"
    assert state_overwrites["n"] == ks._MAX_CAS_ATTEMPTS

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
    # CRLF: a CRLF-stored baseline vs an LF-rewritten current must compare equal
    # (Codex 4077649614) — strip trailing CR as well as LF.
    assert ks._section_eq("body\r\n", "body\n") is True
    assert ks._section_eq("body\r", "body") is True
    assert ks._section_eq("body\r\n\r\n", "body") is True


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


def test_target_last_section_current_base_succeeds(tmp_path):
    """Editing the LAST section with the CURRENT base succeeds under the held
    lease even after an earlier section changed. (Increment-1 exercised the
    trailing-newline reapply path here; under serialize+park a stale base would
    park instead, so this now covers the current-base success case. The
    _section_eq trailing-newline tolerance is unit-tested in
    test_section_eq_semantics.)"""
    store = str(tmp_path)
    project = "proj-target-last"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")

    # An earlier section changes first; the target writer re-reads the CURRENT
    # hash and edits the LAST section "Notes" -> succeeds.
    h1 = _direct_section_write(ks._persist, store, project, "Active State",
                               "active-from-writer2", r0["version_hash"])
    result = ks.flush_state(store, project, "notes=target",
                             expect_hash=h1, section="Notes")
    assert result["ok"] is True

    backend = ks._backend(store)
    blob = backend.read(ks._key(project, "state"))
    fm, body = ks.parse(blob.body.decode("utf-8"))
    assert ks._get_section(body, "Notes")[2].strip() == "notes=target"
    assert ks._get_section(body, "Active State")[2].strip() == "active-from-writer2"


def test_malformed_expect_hash_fails_fast_not_parked(tmp_path):
    """CodeRabbit/Codex 4077649632: a malformed --expect-hash (truncated,
    uppercase, non-hex) is a caller input error. It must fail loud as
    invalid_expect_hash BEFORE any read/reapply/park — no durable park write,
    no conflict-list pollution."""
    store = str(tmp_path)
    project = "proj-badhash"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")

    for bad in ("deadbeef", r0["version_hash"].upper(), r0["version_hash"][:-1], "zz"):
        with pytest.raises(SystemExit) as exc:
            ks.flush_state(store, project, "x", expect_hash=bad, section="Active State")
        assert _die_payload(exc)["reason"] == "invalid_expect_hash"

    # Nothing was parked for any of the malformed attempts.
    backend = ks._backend(store)
    assert list(backend.list(project + "/conflicts/")) == []
    # A valid hash still works.
    ok = ks.flush_state(store, project, "ok", expect_hash=r0["version_hash"],
                        section="Active State")
    assert ok["ok"] is True


def test_parked_conflict_records_doc_kind(tmp_path):
    """Codex 4077649622: the parked conflict must record whether the losing
    body targeted the state or the log doc, so a later reviewer can reapply it
    without the original command context."""
    store = str(tmp_path)
    project = "proj-dockind"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")
    _direct_section_write(ks._persist, store, project, "Active State", "winner",
                          r0["version_hash"])
    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, "loser", expect_hash=r0["version_hash"],
                        section="Active State")
    payload = _die_payload(exc)
    assert payload["reason"] == "conflict_parked"
    assert payload["doc_kind"] == "state"

    backend = ks._backend(store)
    jblob = backend.read(ks._key(project, "journal", "conflicts"))
    assert "doc_kind=state" in jblob.body.decode("utf-8")


def test_resolve_conflict_marks_settled_and_survives_replay(tmp_path):
    """Deferral C: resolving a parked conflict makes bootstrap count it settled,
    not outstanding; the marker is a durable gate.persist write, so every fresh
    backend instance (each bootstrap constructs one and replays the journal)
    still sees it — a raw file delete would have been undone by replay."""
    store = str(tmp_path)
    project = "proj-resolve"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")
    _direct_section_write(ks._persist, store, project, "Active State",
                          "winner", r0["version_hash"])
    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, "loser",
                        expect_hash=r0["version_hash"], section="Active State")
    conflict_key = _die_payload(exc)["conflict_key"]

    b1 = ks.bootstrap(store, project)
    assert b1["conflict_count"] == 1 and b1["settled_count"] == 0

    assert ks.resolve_conflict(store, project, conflict_key)["ok"] is True

    b2 = ks.bootstrap(store, project)            # fresh backend + journal replay
    assert b2["conflict_count"] == 0             # no longer outstanding
    assert b2["settled_count"] == 1              # counted as settled
    assert "parked conflict" not in b2["resume_line"]
    # The parked body itself is retained (resolve records, never deletes).
    assert ks._backend(store).read(conflict_key) is not None

    # Idempotent: resolving again is a no-op OK.
    assert ks.resolve_conflict(store, project, conflict_key)["ok"] is True
    b3 = ks.bootstrap(store, project)
    assert b3["conflict_count"] == 0 and b3["settled_count"] == 1


def test_resolve_conflict_rejects_bad_key(tmp_path):
    """resolve refuses a key outside this project's conflicts and an unknown
    (never-parked) key — and writes no marker for either."""
    store = str(tmp_path)
    project = "proj-resolve-bad"
    ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")
    with pytest.raises(SystemExit) as e1:
        ks.resolve_conflict(store, project, "other/system_state")
    assert _die_payload(e1)["reason"] == "invalid_conflict_key"
    with pytest.raises(SystemExit) as e2:
        ks.resolve_conflict(store, project, f"{project}/conflicts/never/there-0")
    assert _die_payload(e2)["reason"] == "unknown_conflict_key"
    assert list(ks._backend(store).list(project + "/conflict-resolved/")) == []


def test_section_body_heading_injection_rejected(tmp_path):
    """Deferral A: a SECTION body containing a level-2-heading-looking line is
    rejected up front (it would shift section boundaries on reparse); nothing is
    parked. A clean body still succeeds; whole-doc writes are exempt."""
    store = str(tmp_path)
    project = "proj-inject"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")

    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, "line1\n## Injected\nmore",
                        expect_hash=r0["version_hash"], section="Active State")
    assert _die_payload(exc)["reason"] == "section_body_heading_injection"
    assert list(ks._backend(store).list(project + "/conflicts/")) == []  # nothing parked

    ok = ks.flush_state(store, project, "clean body",
                        expect_hash=r0["version_hash"], section="Active State")
    assert ok["ok"] is True

    # Whole-doc writes are the caller's full structure and may contain headings.
    b = ks.bootstrap(store, project)
    w = ks.flush_state(store, project, "## Active State\nx\n\n## Notes\ny",
                       expect_hash=b["version_hash"])
    assert w["ok"] is True


# --- _park_conflict hardening (mutation-testing gaps) ------------------------

def _park_events(store):
    p = pathlib.Path(store) / ks.EVENTS_DIR / "events.jsonl"
    if not p.exists():
        return []
    lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [(l, json.loads(l)) for l in lines if json.loads(l).get("op") == "park"]


def _force_section_park(store, project, losing, session=None):
    """Winner lands on Active State first; the loser flushes the same section
    with the stale base and parks. Returns the die() payload."""
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")
    _direct_section_write(ks._persist, store, project, "Active State", "winner",
                          r0["version_hash"])
    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, losing, expect_hash=r0["version_hash"],
                        section="Active State", session=session)
    return _die_payload(exc)


def _force_whole_doc_park(store, project, losing, session=None):
    """Winner commits a whole-doc update first; the loser's whole-doc flush with
    the stale base parks. Returns the die() payload."""
    r0 = ks.flush_state(store, project, "v0")
    backend = ks._backend(store)
    k = ks._key(project, "state")
    fm, _ = ks.parse(backend.read(k).body.decode("utf-8"))
    fm = dict(fm); fm["revision"] = int(fm.get("revision", 0)) + 1; fm["updated"] = ks.now()
    res = ks._persist(backend, k, "state", ks._bytes(fm, "winner-doc"), r0["version_hash"])
    assert isinstance(res, ks.OK)
    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, losing, expect_hash=r0["version_hash"], session=session)
    return _die_payload(exc)


# CRLF, a tab, trailing whitespace, non-ASCII, blank line, and NO trailing newline:
# every one of these is something a normalizing store would silently alter.
_ODD_BODY = "líne one  \r\nline\ttwo \r\n\r\n  last line without newline"


def test_parked_body_is_byte_verbatim_section(tmp_path):
    """The parked copy is the loser's bytes exactly — not stripped, not
    newline-normalized, not re-encoded (compare bytes, never .strip())."""
    store = str(tmp_path)
    payload = _force_section_park(store, "proj-verbatim-sec", _ODD_BODY)
    assert payload["reason"] == "conflict_parked"
    cblob = ks._backend(store).read(payload["conflict_key"])
    assert cblob.body == _ODD_BODY.encode("utf-8")
    assert cblob.body.endswith(b"newline")          # no trailing newline was added
    assert b"\r\n" in cblob.body and b"\t" in cblob.body


def test_parked_body_is_byte_verbatim_whole_doc(tmp_path):
    store = str(tmp_path)
    payload = _force_whole_doc_park(store, "proj-verbatim-doc", _ODD_BODY)
    assert payload["reason"] == "conflict_parked"
    cblob = ks._backend(store).read(payload["conflict_key"])
    assert cblob.body == _ODD_BODY.encode("utf-8")


def test_journal_note_carries_no_raw_session_id_or_body_value(tmp_path):
    """The conflicts journal note (and the conflict key) name the session and
    the body only by hex digests: the raw session id and any value from the
    losing body must never appear, and nothing is written under the raw
    session's own journal key."""
    import hashlib
    import re as _re
    store = str(tmp_path)
    project = "proj-journal-hex"
    sid = "sess-Distinct-Q7x9"                       # valid id, distinctive
    marker = "marker-value-zq81-distinct"           # gate-safe but unmistakable
    losing = f"first line\n{marker}\nlast line"
    payload = _force_section_park(store, project, losing, session=sid)
    assert payload["reason"] == "conflict_parked"
    assert payload["journal_recorded"] is True

    backend = ks._backend(store)
    jtext = backend.read(ks._key(project, "journal", "conflicts")).body.decode("utf-8")
    assert sid not in jtext
    assert marker not in jtext and "first line" not in jtext
    assert backend.read(ks._key(project, "journal", sid)) is None   # no journal under the raw id

    sess_label = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:8]
    body_label = hashlib.sha256(losing.encode("utf-8")).hexdigest()[:12]
    assert f"sess={sess_label}" in jtext
    assert _re.search(r"sess=([0-9a-f]{8})\b", jtext).group(1) == sess_label
    assert payload["conflict_key"].endswith(f"/{sess_label}-{body_label}")
    assert payload["conflict_key"] in jtext
    assert "section=Active State" in jtext
    # The die() payload itself is labels/hashes only as well.
    assert sid not in json.dumps(payload) and marker not in json.dumps(payload)


def test_park_event_scope_whole_doc(tmp_path):
    """Deferral B: a whole-document park emits exactly one durable park event
    with scope=whole_doc, keyed to the parked conflict, carrying no body bytes."""
    store = str(tmp_path)
    project = "proj-event-doc"
    payload = _force_whole_doc_park(store, project, "loser-doc-body", session="sess-ev-doc")
    events = _park_events(store)
    assert len(events) == 1
    line, ev = events[0]
    assert ev["decision"] == "park" and ev["project"] == project
    assert ev["scope"] == "whole_doc"
    assert ev["reason"] == "conflict_parked" and ev["doc_kind"] == "state"
    assert ev["conflict_key"] == payload["conflict_key"]
    assert "loser-doc-body" not in line and "sess-ev-doc" not in line


def test_park_event_scope_section(tmp_path):
    """Deferral B: a section-scoped park emits the event with scope=section."""
    store = str(tmp_path)
    project = "proj-event-sec"
    payload = _force_section_park(store, project, "loser-sec-body", session="sess-ev-sec")
    events = _park_events(store)
    assert len(events) == 1
    line, ev = events[0]
    assert ev["decision"] == "park"
    assert ev["scope"] == "section"
    assert ev["reason"] == "conflict_parked" and ev["doc_kind"] == "state"
    assert ev["conflict_key"] == payload["conflict_key"]
    assert "loser-sec-body" not in line and "sess-ev-sec" not in line


def test_park_event_scope_section_on_retry_exhaustion(tmp_path, monkeypatch):
    """The exhaustion path parks through the same door: reason differs, scope
    is still section."""
    store = str(tmp_path)
    project = "proj-event-exhaust"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")
    real_persist = ks._persist

    def hijacked(backend, key, kind, raw_bytes, expected_hash, lease=None):
        if kind == "state" and expected_hash is not None:
            cur = backend.read(key)
            return ks.STALE(cur.version_hash if cur else None)
        return real_persist(backend, key, kind, raw_bytes, expected_hash, lease=lease)

    monkeypatch.setattr(ks, "_persist", hijacked)
    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, "loser-active",
                        expect_hash=r0["version_hash"], section="Active State")
    payload = _die_payload(exc)
    events = _park_events(store)
    assert len(events) == 1
    _, ev = events[0]
    assert ev["reason"] == "conflict_retry_exhausted" == payload["reason"]
    assert ev["scope"] == "section"
    assert ev["conflict_key"] == payload["conflict_key"]


def test_secret_in_losing_body_is_refused_not_parked(tmp_path):
    """A losing body that contains a secret must NOT be stored anywhere: the
    park fails loud and distinctly (conflict_body_secret_blocked, labels only),
    no conflict key exists, no journal note, no park event."""
    store = str(tmp_path)
    project = "proj-park-secret"
    secret = "AKIAIOSFODNN7EXAMPLE"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")
    _direct_section_write(ks._persist, store, project, "Active State", "winner",
                          r0["version_hash"])
    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, f"aws key {secret} here",
                        expect_hash=r0["version_hash"], section="Active State")
    payload = _die_payload(exc)
    assert payload["reason"] == "conflict_body_secret_blocked"
    assert payload["reasons"]                                  # labels present...
    assert secret not in str(exc.value)                        # ...the value is not
    assert payload["base_hash"] == r0["version_hash"]

    backend = ks._backend(store)
    assert list(backend.list(project + "/conflicts/")) == []
    assert backend.read(ks._key(project, "journal", "conflicts")) is None
    assert _park_events(store) == []


def test_session_append_waits_out_a_briefly_held_journal_lease(tmp_path):
    """A concurrent appender (or a parking write) holds the journal lease
    across its read->append section. lock() refuses immediately, so without
    a backoff the 50-attempt budget burns through in milliseconds and the
    append is falsely reported exhausted; with backoff it lands once the
    holder releases."""
    import time as _time

    store = str(tmp_path)
    project = "proj-journal-backoff"
    sid = "sess-backoff-1"
    jkey = ks._key(project, "journal", sid)
    holder = ks._backend(store)
    lease = holder.lock(jkey, ttl_s=30)

    def _release_later():
        _time.sleep(0.4)
        holder.unlock(lease)

    t = threading.Thread(target=_release_later)
    t.start()
    try:
        t0 = _time.monotonic()
        res = ks.session_append(store, project, sid, "cowork", "entry landed after the holder released")
        elapsed = _time.monotonic() - t0
    finally:
        t.join()
    assert res == {"ok": True, "log": jkey}
    assert elapsed >= 0.3, "must have waited for the holder rather than exhausting instantly"
    body = ks._backend(store).read(jkey).body.decode("utf-8")
    assert "entry landed after the holder released" in body


@pytest.mark.parametrize("bad_section", ["Missing\n## Injected", "Active State\r\n## X", "", "   ", "a\rb"])
def test_section_name_with_line_breaks_or_empty_is_rejected(tmp_path, bad_section):
    """The section NAME is interpolated into '## {name}'; a multi-line name
    would persist a second level-2 heading (the boundary ambiguity the body
    guard prevents), so it is refused up front, before any read/lock/park."""
    store = str(tmp_path)
    project = "proj-secname"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")
    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, "clean body", expect_hash=r0["version_hash"], section=bad_section)
    assert _die_payload(exc)["reason"] == "invalid_section_name"
    backend = ks._backend(store)
    assert list(backend.list(project + "/conflicts/")) == []
    body = backend.read(ks._key(project, "state")).body.decode("utf-8")
    assert body.count("\n## ") + body.startswith("## ") == 2, body  # still exactly the two headings


@pytest.mark.parametrize("body", ["safe\r## Injected\nmore", "safe\r\n## Injected", "\r## Injected"])
def test_section_body_heading_after_bare_carriage_return_is_rejected(tmp_path, body):
    store = str(tmp_path)
    project = "proj-inject-cr"
    r0 = ks.flush_state(store, project, "## Active State\n\n## Notes\n\n")
    with pytest.raises(SystemExit) as exc:
        ks.flush_state(store, project, body, expect_hash=r0["version_hash"], section="Active State")
    assert _die_payload(exc)["reason"] == "section_body_heading_injection"
    assert list(ks._backend(store).list(project + "/conflicts/")) == []
