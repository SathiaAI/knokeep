"""Seeded-divergence test for the reconciler (T2 acceptance: "produces a
correct drift report for a seeded divergence").

Store says next-step "X", GitHub HEAD says "Y", tracker status says "Z" —
three different values for the same tracked fact. reconcile() must report
the divergence (agree=False, all three sources listed as diverging) and
correctly name the configured source-of-truth ("store") and its value.
"""
from __future__ import annotations

from store import gate
from store.fake import FakeBackend

from reconciler.reconcile import reconcile
from reconciler.sources import StoreSource


class _FakeGitHub:
    """A GitHubSource-shaped mock (see reconciler.sources.GitHubSource).
    Test-only stand-in — never touches the network."""

    def __init__(self, head: str) -> None:
        self._head = head

    def fetch_state(self) -> dict:
        return {"head": self._head}


class _FakeTracker:
    """A TrackerSource-shaped mock. Test-only stand-in."""

    def __init__(self, status: str) -> None:
        self._status = status

    def fetch_state(self) -> dict:
        return {"status": self._status}


def test_seeded_divergence_is_reported_with_source_of_truth(tmp_path):
    backend = FakeBackend()
    write_result = gate.persist(
        backend, "next_step", b"X", expected_hash=None, doc_type="system_state"
    )
    from store.types import OK

    assert isinstance(write_result, OK)

    sources = {
        "store": StoreSource(backend, keys=["next_step"]),
        "github": _FakeGitHub(head="Y"),
        "tracker": _FakeTracker(status="Z"),
    }

    report = reconcile(sources, telemetry_dir=tmp_path)

    assert report.has_drift is True
    assert report.sources_consulted == ("github", "store", "tracker")

    fact = report.fact("next_step")
    assert fact is not None
    assert fact.values == {"store": "X", "github": "Y", "tracker": "Z"}
    assert fact.agree is False
    assert fact.diverging_sources == ("github", "store", "tracker")
    assert fact.source_of_truth == "store"
    assert fact.source_of_truth_value == "X"
    assert fact.quarantined == {}
    assert fact.missing_sources == ()


def test_agreement_across_sources_is_reported_as_no_drift(tmp_path):
    backend = FakeBackend()
    from store.types import OK

    r = gate.persist(backend, "next_step", b"ship", expected_hash=None, doc_type="system_state")
    assert isinstance(r, OK)

    sources = {
        "store": StoreSource(backend, keys=["next_step"]),
        "github": _FakeGitHub(head="ship"),
        "tracker": _FakeTracker(status="ship"),
    }

    report = reconcile(sources, telemetry_dir=tmp_path)

    assert report.has_drift is False
    fact = report.fact("next_step")
    assert fact.agree is True
    assert fact.diverging_sources == ()
    assert fact.source_of_truth_value == "ship"


def test_telemetry_sanitizes_non_identifier_source_and_fact_key_names(tmp_path):
    """Source names and fact_keys are CALLER-DEFINED IDENTIFIERS, never
    secret-scanned fetched VALUES -- telemetry must not persist an oddly-
    shaped or sensitive-looking identifier verbatim (review finding)."""
    import json

    from reconciler.reconcile import FactSpec

    class _Weird:
        def __init__(self, value: str) -> None:
            self._value = value

        def fetch_state(self) -> dict:
            return {"v": self._value}

    weird_source_name = "sk-" + "A" * 40  # secret-SHAPED SOURCE NAME, not a value
    weird_fact_key = "also not an identifier " + "B" * 40  # spaces -> not identifier-shaped

    sources = {
        weird_source_name: _Weird("x"),
        "tracker": _Weird("y"),
    }
    fact_specs = (
        FactSpec(
            fact_key=weird_fact_key,
            fields={weird_source_name: "v", "tracker": "v"},
            source_of_truth="tracker",
        ),
    )

    reconcile(sources, fact_specs, telemetry_dir=tmp_path)

    raw = (tmp_path / "reconciler.jsonl").read_text(encoding="utf-8")
    assert weird_source_name not in raw
    assert ("B" * 40) not in raw
    assert "[non-identifier]" in raw

    record = json.loads(raw.strip().splitlines()[0])
    assert "[non-identifier]" in record["sources_consulted"]
    assert record["diverging_fact_keys"] == ["[non-identifier]"]
    # A normal, well-formed identifier alongside the weird ones is untouched.
    assert "tracker" in record["sources_consulted"]


def test_reconcile_emits_a_labels_and_counts_only_telemetry_event(tmp_path):
    """The JSONL telemetry event records counts/labels about the outcome —
    never the fact values themselves."""
    import json

    backend = FakeBackend()
    from store.types import OK

    r = gate.persist(backend, "next_step", b"X", expected_hash=None, doc_type="system_state")
    assert isinstance(r, OK)

    sources = {
        "store": StoreSource(backend, keys=["next_step"]),
        "github": _FakeGitHub(head="Y"),
        "tracker": _FakeTracker(status="Z"),
    }

    reconcile(sources, telemetry_dir=tmp_path)

    telemetry_file = tmp_path / "reconciler.jsonl"
    assert telemetry_file.exists()
    lines = telemetry_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert record["event"] == "reconcile"
    assert record["has_drift"] is True
    assert record["diverging_fact_count"] == 1
    assert record["diverging_fact_keys"] == ["next_step"]
    assert record["sources_consulted"] == ["github", "store", "tracker"]
    # Labels/counts only: none of the actual fact values ("X"/"Y"/"Z") appear
    # anywhere as JSON string values in the telemetry record.
    assert record["diverging_fact_keys"] == ["next_step"]
    dumped_values = [v for v in record.values() if isinstance(v, str)]
    dumped_values += [v for lst in record.values() if isinstance(lst, list) for v in lst if isinstance(v, str)]
    assert "X" not in dumped_values
    assert "Y" not in dumped_values
    assert "Z" not in dumped_values
