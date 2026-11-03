# SPDX-License-Identifier: Elastic-2.0 OR LicenseRef-Viaknox-Commercial
# Copyright (c) 2026 Viaknox Holding LLC
"""Tests for the end-to-end runner (conclusions/runner.py), on the in-memory
backend with a scripted generator and judge: no network."""
from __future__ import annotations

import pytest

from conclusions import runner as rn
from conclusions import store as cs
from conclusions import verify as v
from conclusions.generator import Candidate, DollarBudgetExceeded, GeneratorError
from store import gate
from store.context import create_ctx
from store.fake import FakeBackend
from store.types import OK

SPACE = "proj/"
FAKE_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # AWS's published documentation example, not a real key
QA = "chose Postgres for the login service"
QB = "Postgres is the database for login"


class Gen:
    """A scripted generator: returns what it is told, records what it was sent."""

    family = "vendorx"

    def __init__(self, *results):
        self.results, self.sent = list(results), []

    def propose(self, space, entries):
        self.sent.append(dict(entries))
        r = self.results.pop(0) if self.results else []
        if isinstance(r, Exception):
            raise r
        return r


def _put(be, key, text):
    r = gate.persist(be, key, text.encode(), ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r, OK), (key, r)


def _seed(be):
    _put(be, "proj/sessions/s1/log.md", "We chose Postgres for the login service because of row level security.")
    _put(be, "proj/sessions/s2/log.md", "Decision recorded: Postgres is the database for login, per the design review.")
    _put(be, "proj/system_state.md", "Deploys happen on Tuesdays and Thursdays after the build is green.")


def _two_session_candidate(text="The login service uses Postgres."):
    return Candidate(text, (("proj/sessions/s1/log.md", QA), ("proj/sessions/s2/log.md", QB)))


def _yes(text, quotes):
    return True


def _run(be, gen, **kw):
    kw.setdefault("judge", _yes)
    kw.setdefault("judge_family", "typesafe")
    return rn.run_space(be, SPACE, generator=gen, **kw)


def test_the_whole_pipeline_accepts_a_corroborated_conclusion():
    be = FakeBackend(); _seed(be)
    gen = Gen([_two_session_candidate()])
    s = _run(be, gen)
    assert (s.entries_read, s.candidates, s.proposed, s.accepted, s.left_proposed, s.stopped) == (3, 1, 1, 1, 0, None)
    assert [r["claims"][0]["text"] for r in cs.recall(be, SPACE)] == ["The login service uses Postgres."]


def test_a_single_session_conclusion_is_proposed_but_not_accepted():
    be = FakeBackend(); _seed(be)
    one = Candidate("Deploys run on Tuesdays and Thursdays.", (("proj/system_state.md", "Deploys happen on Tuesdays and Thursdays"),))
    s = _run(be, Gen([one]))
    assert (s.proposed, s.accepted, s.left_proposed) == (1, 0, 1)
    assert s.failures == {v.NOT_CORROBORATED: 1}   # system_state.md has no session id
    assert cs.recall(be, SPACE) == []


def test_the_generator_only_ever_sees_exact_entries_never_conclusions():
    be = FakeBackend(); _seed(be)
    gen = Gen([_two_session_candidate()], [])
    _run(be, gen)
    _run(be, gen)  # second run: conclusions now exist in the store
    for sent in gen.sent:
        assert set(sent) == {"proj/sessions/s1/log.md", "proj/sessions/s2/log.md", "proj/system_state.md"}
        assert not any(".conclusions/" in k for k in sent)


def test_a_rerun_does_not_propose_the_same_conclusion_twice():
    be = FakeBackend(); _seed(be)
    _run(be, Gen([_two_session_candidate()]))
    s = _run(be, Gen([_two_session_candidate("  The LOGIN service   uses Postgres. ")]))
    assert (s.candidates, s.duplicates, s.proposed) == (1, 1, 0)
    assert len(cs.list_records(be, SPACE)) == 1


def test_a_stale_or_superseded_conclusion_may_be_proposed_again():
    be = FakeBackend(); _seed(be)
    _run(be, Gen([_two_session_candidate()]))
    rec = cs.list_records(be, SPACE)[0]
    # evidence disappears -> stale
    _put_over(be, "proj/sessions/s1/log.md", "Rewritten completely.")
    assert cs.recheck(be, SPACE, rec["id"]) == cs.STALE_STATUS
    s = _run(be, Gen([Candidate("The login service uses Postgres.", (("proj/sessions/s2/log.md", QB),))]))
    assert s.duplicates == 0 and s.proposed == 1


def _put_over(be, key, text):
    from store.context import overwrite_ctx
    cur = be.read(key).version_hash
    lease = be.lock(key, ttl_s=30); be.unlock(lease)
    r = gate.persist(be, key, text.encode(), ctx=overwrite_ctx(cur, lease), doc_type="system_state")
    assert isinstance(r, OK)


def test_exact_entries_are_never_touched():
    be = FakeBackend(); _seed(be)
    before = {k: be.read(k).version_hash for k in be.list("") if ".conclusions/" not in k}
    _run(be, Gen([_two_session_candidate(), Candidate("Deploys on Tuesdays.", (("proj/system_state.md", "Deploys happen on Tuesdays and Thursdays"),))]))
    after = {k: be.read(k).version_hash for k in be.list("") if ".conclusions/" not in k}
    assert before == after


def test_a_secret_in_a_candidate_is_refused_and_counted_not_stored():
    be = FakeBackend(); _seed(be)
    bad = Candidate(f"Deploys use {FAKE_AWS_KEY}.", (("proj/sessions/s1/log.md", QA), ("proj/sessions/s2/log.md", QB)))
    s = _run(be, Gen([bad, _two_session_candidate()]))
    assert s.refused == {cs.SECRET: 1} and s.proposed == 1 and s.accepted == 1
    assert all(FAKE_AWS_KEY not in str(r) for r in cs.list_records(be, SPACE))


def test_the_judge_decides_and_unsure_leaves_it_proposed():
    be = FakeBackend(); _seed(be)
    s = _run(be, Gen([_two_session_candidate()]), judge=lambda t, q: None)
    assert (s.accepted, s.left_proposed) == (0, 1) and s.failures == {v.NOT_ENTAILED: 1}


def test_the_run_stops_when_the_generator_budget_is_spent():
    be = FakeBackend(); _seed(be)
    for i in range(4):
        _put(be, f"proj/sessions/s{i + 3}/log.md", "x" * 30 + f" note {i}")
    gen = Gen([_two_session_candidate()], DollarBudgetExceeded("cap"), [_two_session_candidate("never reached")])
    s = _run(be, gen, batch_chars=100)
    assert s.stopped == "budget" and s.accepted == 1 and len(gen.sent) == 2   # the third batch was never sent


def test_a_failed_batch_does_not_stop_the_other_batches():
    be = FakeBackend(); _seed(be)
    gen = Gen(GeneratorError("status 503"), [_two_session_candidate()])
    s = _run(be, gen, batch_chars=80)
    assert s.stopped == "generator_error" and s.accepted == 1 and s.batches >= 2


def test_it_stops_when_the_judge_keeps_failing():
    be = FakeBackend(); _seed(be)

    def broken(text, quotes):
        raise RuntimeError("judge down")

    many = [_two_session_candidate(f"Claim number {i} about Postgres.") for i in range(6)]
    s = _run(be, Gen(many), judge=broken)
    assert s.stopped == "judge_unavailable" and s.proposed == 3 and s.left_proposed == 3 and s.accepted == 0


def test_empty_binary_and_oversized_entries_are_skipped_not_fatal():
    be = FakeBackend(); _seed(be)
    _put(be, "proj/empty.md", "   ")
    s = _run(be, Gen([]))
    assert s.entries_read == 3 and s.entries_skipped == 1


def test_the_default_provenance_reads_the_session_from_the_key():
    assert rn.session_from_key("proj/sessions/abc-123/log.md").session_id == "abc-123"
    assert rn.session_from_key("proj/system_state.md").session_id is None
    assert rn.session_from_key("proj/notes/sessions-list.md").session_id is None


def test_arguments_are_validated():
    be = FakeBackend()
    for bad in ("", "/", "proj"):
        with pytest.raises(ValueError):
            rn.run_space(be, bad, generator=Gen(), judge=_yes, judge_family="typesafe")
    with pytest.raises(ValueError):
        rn.run_space(be, SPACE, generator=Gen(), judge=_yes, judge_family="typesafe", batch_chars=0)
