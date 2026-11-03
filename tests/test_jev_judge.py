# SPDX-License-Identifier: Elastic-2.0 OR LicenseRef-Viaknox-Commercial
# Copyright (c) 2026 Viaknox Holding LLC
"""Tests for the Jev judge adapter (conclusions/jev.py). No network: every
call goes through an injected fake transport."""
from __future__ import annotations

import hashlib
import json

import pytest

from conclusions import jev
from conclusions import verify as v
from conclusions.jev import Budget, BudgetExceeded, JevError, JevJudge, Thresholds

CLAIM = "The login service uses Postgres."
QUOTES = ["We chose Postgres for the login service because of row level security."]
FAKE_KEY = "tsk_test_NOT_A_REAL_KEY_0000000000000000"


def _ok(q0, q1, tokens=(300, 20)):
    return 200, json.dumps({
        "model": "jev-1.14.0",
        "answers": {"q0": {"type": "noul", "noul": q0}, "q1": {"type": "noul", "noul": q1}},
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
    }).encode()


class Fake:
    """A scripted transport that records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, json.loads(body), dict(headers), timeout))
        r = self.responses.pop(0) if self.responses else self.calls and _ok(0.0, 1.0)
        if isinstance(r, Exception):
            raise r
        return r


def _judge(fake, *, max_calls=10, max_tokens=100_000, key=None, **kw):
    sleeps = []
    j = JevJudge(budget=Budget(max_calls, max_tokens), api_key=key, transport=fake, sleep=sleeps.append, **kw)
    j.sleeps = sleeps
    return j


@pytest.mark.parametrize(
    "support,overreach,expected",
    [
        (0.96, 0.05, True),
        (0.90, 0.30, True),   # exactly on the accept thresholds
        (0.89, 0.05, None),   # support just under: unsure
        (0.96, 0.31, None),   # overreach just over: unsure
        (0.70, 0.40, None),
        (0.50, 0.05, False),  # support at the reject line
        (0.02, 0.01, False),
        (0.96, 0.70, False),  # high overreach rejects even with high support
        (0.96, 0.99, False),
    ],
)
def test_two_probabilities_become_true_false_or_unsure(support, overreach, expected):
    assert _judge(Fake(_ok(support, overreach)))(CLAIM, QUOTES) is expected


def test_the_request_has_the_expected_shape_and_no_auth_when_there_is_no_key():
    fake = Fake(_ok(0.96, 0.05))
    _judge(fake)(CLAIM, QUOTES)
    url, body, headers, timeout = fake.calls[0]
    assert url == "https://api.typesafe.ai/v1/systemone"
    assert body["model"] == "jev-latest"
    assert body["state"] == {"claim": CLAIM, "evidence": QUOTES}
    assert set(body["questions"]) == {"q0", "q1"}
    assert all(q["type"] == "noul" and q["instructions"] for q in body["questions"].values())
    assert "authorization" not in {k.lower() for k in headers}  # a proxy may inject it
    assert 0 < timeout <= 10


def test_a_key_is_sent_as_bearer_and_never_shown():
    fake = Fake(_ok(0.96, 0.05), (401, b""))
    j = _judge(fake, key=f"  {FAKE_KEY}  ")
    j(CLAIM, QUOTES)
    assert fake.calls[0][2]["authorization"] == f"Bearer {FAKE_KEY}"
    assert FAKE_KEY not in repr(j) and "has_key=True" in repr(j)
    with pytest.raises(JevError) as exc_info:
        j(CLAIM, QUOTES)
    assert FAKE_KEY not in str(exc_info.value)


def test_from_env_reads_the_key_name_and_https_is_required(monkeypatch):
    monkeypatch.setenv(jev.ENV_KEY_NAME, FAKE_KEY)
    j = JevJudge.from_env(budget=Budget(5, 5000), transport=Fake(_ok(0.9, 0.1)))
    j(CLAIM, QUOTES)
    assert "has_key=True" in repr(j)
    with pytest.raises(ValueError):
        JevJudge(budget=Budget(1, 1), base_url="http://api.typesafe.ai")
    with pytest.raises(ValueError):
        JevJudge(budget=None)


def test_transient_failures_are_retried_and_each_retry_costs_a_call():
    fake = Fake((503, b""), (429, b""), _ok(0.96, 0.05))
    j = _judge(fake)
    assert j(CLAIM, QUOTES) is True
    assert len(fake.calls) == 3 and j.sleeps == [0.25, 1.0]
    assert j._budget.calls == 3


def test_a_transport_error_is_retried_once_then_succeeds():
    fake = Fake(JevError("network error"), _ok(0.96, 0.05))
    assert _judge(fake)(CLAIM, QUOTES) is True
    assert len(fake.calls) == 2


def test_it_gives_up_after_the_retries_and_fails_closed():
    fake = Fake((503, b""), (503, b""), (503, b""))
    with pytest.raises(JevError):
        _judge(fake)(CLAIM, QUOTES)
    assert len(fake.calls) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retried_and_never_echo_the_request(status):
    echo = json.dumps({"detail": [{"input": CLAIM}]}).encode()
    fake = Fake((status, echo))
    with pytest.raises(JevError) as exc_info:
        _judge(fake)(CLAIM, QUOTES)
    assert len(fake.calls) == 1
    assert CLAIM not in str(exc_info.value) and str(status) in str(exc_info.value)


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"{}",
        json.dumps({"answers": {"q0": {"type": "noul", "noul": 0.9}}}).encode(),  # q1 missing
        json.dumps({"answers": {"q0": {"type": "noul", "noul": 1.5}, "q1": {"type": "noul", "noul": 0.1}}}).encode(),
        json.dumps({"answers": {"q0": {"type": "noul", "noul": -0.1}, "q1": {"type": "noul", "noul": 0.1}}}).encode(),
        json.dumps({"answers": {"q0": {"type": "noul", "noul": True}, "q1": {"type": "noul", "noul": 0.1}}}).encode(),
        json.dumps({"answers": {"q0": {"type": "noul", "noul": "0.9"}, "q1": {"type": "noul", "noul": 0.1}}}).encode(),
        json.dumps({"answers": {"q0": {"type": "other", "noul": 0.9}, "q1": {"type": "noul", "noul": 0.1}}}).encode(),
        b'{"answers": {"q0": {"type": "noul", "noul": NaN}, "q1": {"type": "noul", "noul": 0.1}}}',
    ],
)
def test_a_malformed_or_out_of_range_answer_is_an_error_not_a_guess(payload):
    with pytest.raises(JevError):
        _judge(Fake((200, payload)))(CLAIM, QUOTES)


def test_empty_input_never_reaches_the_network():
    fake = Fake(_ok(0.9, 0.1))
    j = _judge(fake)
    for claim, quotes in (("", QUOTES), ("  ", QUOTES), (CLAIM, []), (None, QUOTES)):
        with pytest.raises(JevError):
            j(claim, quotes)
    assert fake.calls == [] and j._budget.calls == 0


def test_an_oversized_request_is_refused_before_any_call():
    fake = Fake(_ok(0.9, 0.1))
    with pytest.raises(JevError):
        _judge(fake)(CLAIM, ["x" * 1000] * 80)
    assert fake.calls == []


def test_the_call_budget_is_a_hard_stop_checked_before_each_call():
    fake = Fake(_ok(0.96, 0.05), _ok(0.96, 0.05))
    j = _judge(fake, max_calls=2)
    assert j(CLAIM, QUOTES) is True and j(CLAIM, QUOTES) is True
    with pytest.raises(BudgetExceeded):
        j(CLAIM, QUOTES)
    assert len(fake.calls) == 2  # the third never left the machine


def test_the_token_budget_stops_the_next_call_once_spent():
    fake = Fake(_ok(0.96, 0.05, tokens=(900, 100)), _ok(0.96, 0.05))
    j = _judge(fake, max_tokens=1000)
    assert j(CLAIM, QUOTES) is True  # spends 1000
    with pytest.raises(BudgetExceeded):
        j(CLAIM, QUOTES)
    assert len(fake.calls) == 1


def test_a_retry_cannot_slip_past_the_call_budget():
    fake = Fake((503, b""), _ok(0.96, 0.05))
    j = _judge(fake, max_calls=1)
    with pytest.raises(BudgetExceeded):
        j(CLAIM, QUOTES)
    assert len(fake.calls) == 1


def test_custom_thresholds_are_respected():
    strict = Thresholds(accept_support_at=0.99, accept_overreach_at=0.01, reject_support_at=0.2, reject_overreach_at=0.9)
    assert _judge(Fake(_ok(0.96, 0.05)), thresholds=strict)(CLAIM, QUOTES) is None
    assert _judge(Fake(_ok(0.995, 0.005)), thresholds=strict)(CLAIM, QUOTES) is True


# -- wired into the verifier ------------------------------------------------------

DOCS = {
    "proj/a.md": b"We chose Postgres for the login service because of row level security.",
    "proj/b.md": b"Decision recorded: Postgres is the database for login, per the design review.",
}
QA, QB = "chose Postgres for the login service", "Postgres is the database for login"


def _conclusion(family="anthropic"):
    h = lambda k: hashlib.sha256(DOCS[k]).hexdigest()
    return v.Conclusion(
        "proj/",
        (v.Claim(CLAIM, (v.Citation("proj/a.md", h("proj/a.md"), QA), v.Citation("proj/b.md", h("proj/b.md"), QB))),),
        family,
    )


def _verify(judge, family="anthropic"):
    return v.verify(
        _conclusion(family),
        resolve=lambda k, h: DOCS.get(k) if DOCS.get(k) and hashlib.sha256(DOCS[k]).hexdigest() == h else None,
        source_info=lambda k: v.SourceInfo(session_id={"proj/a.md": "s1", "proj/b.md": "s2"}[k]),
        judge=judge,
        judge_family=JevJudge.FAMILY,
    )


def test_a_supporting_jev_answer_lets_the_verifier_accept():
    assert _verify(_judge(Fake(_ok(0.97, 0.04)))).accepted


def test_unsure_and_errors_and_budget_exhaustion_all_reject_in_the_verifier():
    assert v.NOT_ENTAILED in {f.code for f in _verify(_judge(Fake(_ok(0.75, 0.2)))).failures}
    assert v.NOT_ENTAILED in {f.code for f in _verify(_judge(Fake(_ok(0.05, 0.9)))).failures}
    assert v.JUDGE_ERROR in {f.code for f in _verify(_judge(Fake((503, b""), (503, b""), (503, b"")))).failures}
    assert v.JUDGE_ERROR in {f.code for f in _verify(_spent_judge()).failures}


def _spent_judge():
    j = _judge(Fake(_ok(0.9, 0.1)), max_calls=1)
    j(CLAIM, QUOTES)  # uses the only call
    return j


def test_jev_must_not_judge_a_conclusion_that_jev_generated():
    verdict = _verify(_judge(Fake(_ok(0.97, 0.04))), family="typesafe")
    assert not verdict.accepted and {f.code for f in verdict.failures} == {v.JUDGE_NOT_INDEPENDENT}


def test_the_calibration_cases_are_well_formed_and_balanced():
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "conclusions", "eval", "jev_judge_cases.json")
    cases = json.load(open(path, encoding="utf-8"))["cases"]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)) and len(cases) >= 30
    assert {c["label"] for c in cases} == {"supported", "unsupported"}
    assert {"injection", "overreach", "wrong_fact", "contradiction"} <= {c["kind"] for c in cases}
    for c in cases:
        assert 0 < len(c["claim"]) <= v.MAX_CLAIM_CHARS
        assert c["evidence"] and all(len(e) >= v.MIN_QUOTE_CHARS for e in c["evidence"])
