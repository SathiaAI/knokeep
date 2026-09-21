"""A planted (synthetic, non-functional) secret inside fetched GitHub text
must be quarantined to a label-only placeholder and must NEVER appear
verbatim anywhere in the DriftReport, per CRITICAL SAFETY INVARIANT #2.

The planted value below is a synthetic look-alike ("ghp_" + "A"*36) with the
exact shape `store/gate.py`'s scanner matches (`ghp_[A-Za-z0-9]{20,}`) — not
a real credential.
"""
from __future__ import annotations

import json

from store import gate
from store.fake import FakeBackend
from store.types import OK

from reconciler.reconcile import reconcile

PLANTED_SECRET = "ghp_" + "A" * 36


class _FakeGitHubWithSecret:
    def fetch_state(self) -> dict:
        # Simulate a HEAD commit message that accidentally embeds a token —
        # untrusted, fetched text, treated as DATA never as instructions.
        return {"head": f"deploy: rotate creds token={PLANTED_SECRET}"}


class _FakeTracker:
    def fetch_state(self) -> dict:
        return {"status": "ship"}


def _dump_report(report) -> str:
    """Render everything the report carries into one string, the same way a
    human-facing renderer or a log line would, so the assertion below is a
    real end-to-end check and not just a check of one specific field."""
    parts = []
    for f in report.facts:
        parts.append(f.fact_key)
        parts.extend(f.values.values())
        for labels in f.quarantined.values():
            parts.extend(labels)
        if f.source_of_truth_value is not None:
            parts.append(f.source_of_truth_value)
    return "\n".join(parts)


def test_planted_github_secret_is_quarantined_and_never_appears_verbatim(tmp_path):
    backend = FakeBackend()
    r = gate.persist(backend, "next_step", b"ship", expected_hash=None, doc_type="system_state")
    assert isinstance(r, OK)

    from reconciler.sources import StoreSource

    sources = {
        "store": StoreSource(backend, keys=["next_step"]),
        "github": _FakeGitHubWithSecret(),
        "tracker": _FakeTracker(),
    }

    report = reconcile(sources, telemetry_dir=tmp_path)

    fact = report.fact("next_step")
    assert fact is not None

    # It WAS detected and quarantined.
    assert "github" in fact.quarantined
    assert fact.quarantined["github"], "expected at least one secret_scan label"
    assert any("github" in label.lower() for label in fact.quarantined["github"])

    # The report's field for github is a label-only placeholder...
    assert fact.values["github"] == f"[secret: {fact.quarantined['github'][0]} quarantined]"

    # ...and the raw secret is NEVER present anywhere in the report, however
    # it's rendered/serialized.
    rendered = _dump_report(report)
    assert PLANTED_SECRET not in rendered
    assert PLANTED_SECRET not in repr(report)

    # Divergence is still reported correctly for the OTHER (clean) sources —
    # quarantining one field must not silently suppress the rest of the fact.
    assert fact.values["store"] == "ship"
    assert fact.values["tracker"] == "ship"
    assert fact.agree is False  # github's quarantine label differs from "ship"


def test_planted_secret_never_reaches_the_telemetry_file(tmp_path):
    backend = FakeBackend()
    r = gate.persist(backend, "next_step", b"ship", expected_hash=None, doc_type="system_state")
    assert isinstance(r, OK)

    from reconciler.sources import StoreSource

    sources = {
        "store": StoreSource(backend, keys=["next_step"]),
        "github": _FakeGitHubWithSecret(),
        "tracker": _FakeTracker(),
    }

    reconcile(sources, telemetry_dir=tmp_path)

    telemetry_file = tmp_path / "reconciler.jsonl"
    raw = telemetry_file.read_text(encoding="utf-8")
    assert PLANTED_SECRET not in raw

    record = json.loads(raw.strip().splitlines()[0])
    assert record["quarantined_field_count"] == 1
    assert record["has_drift"] is True


def test_two_different_quarantined_secrets_do_not_falsely_agree(tmp_path):
    """Two DIFFERENT secrets that both quarantine to the identical
    "[secret: <label> quarantined]" placeholder must NOT be reported as
    agreeing -- that would hide real drift behind a shared label (review
    finding). Both are the SAME kind of secret (same label) here
    specifically to prove the fix compares something other than the
    label/placeholder text."""
    backend = FakeBackend()
    r = gate.persist(backend, "next_step", b"ship", expected_hash=None, doc_type="system_state")
    assert isinstance(r, OK)

    from reconciler.sources import StoreSource

    secret_a = "ghp_" + "A" * 36
    secret_b = "ghp_" + "B" * 36
    assert secret_a != secret_b

    class _GitHubWithSecret:
        def fetch_state(self) -> dict:
            return {"head": secret_a}

    class _TrackerWithDifferentSecret:
        def fetch_state(self) -> dict:
            return {"status": secret_b}

    sources = {
        "store": StoreSource(backend, keys=["next_step"]),
        "github": _GitHubWithSecret(),
        "tracker": _TrackerWithDifferentSecret(),
    }

    report = reconcile(sources, telemetry_dir=tmp_path)
    fact = report.fact("next_step")
    assert fact is not None

    # Both quarantined to the SAME label-only placeholder text...
    assert fact.values["github"] == fact.values["tracker"]
    assert "github" in fact.quarantined and "tracker" in fact.quarantined

    # ...but they must NOT be reported as agreeing: they are two different
    # secrets, and a shared placeholder must never manufacture false
    # agreement.
    assert fact.agree is False
    assert "github" in fact.diverging_sources
    assert "tracker" in fact.diverging_sources

    # Neither raw secret ever appears anywhere in the report.
    rendered = _dump_report(report)
    assert secret_a not in rendered
    assert secret_b not in rendered


def test_secret_scan_itself_never_returns_the_matched_value():
    """Sanity check on the underlying primitive the reconciler relies on
    (store.gate.secret_scan): it must return labels only."""
    labels = gate.secret_scan(PLANTED_SECRET.encode())
    assert labels
    joined = " ".join(labels)
    assert PLANTED_SECRET not in joined
