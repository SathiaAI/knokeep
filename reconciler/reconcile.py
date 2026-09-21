"""reconcile(sources) -> DriftReport — the reconciler's one entry point.

READ-ONLY drift observer (design/store_backend_contract.md §6; the
reconciler's own core safety rule per claude/knokeep-solution-design.md
"6. Reconciler" and claude/knokeep-threat-model.md §4):

    "Observes drift between the store, project files, GitHub, and a
    tracker. REPORTS, never silently overwrites an external system of
    record."

CRITICAL SAFETY INVARIANTS (all enforced in this module):
  1. Read-only: `reconcile()` calls nothing on a source except
     `.fetch_state()`. There is no code path here that writes to `sources`,
     the store, GitHub, or a tracker. (Statically checked in
     tests/test_reconciler_no_write_path.py.)
  2. Every string a source hands back is passed through
     `store.gate.secret_scan` BEFORE it can enter a `DriftReport`. A hit
     quarantines that field to a label-only placeholder
     ("[secret: <label> quarantined]") — the matched value itself never
     reaches the report, telemetry, or any exception message.
  3. Fetched text is treated as DATA, never as instructions: it is only
     ever scanned, compared for equality, and copied verbatim (or
     quarantined) into the report. It is never eval'd, exec'd, parsed as
     code, or used to alter this function's own control flow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Sequence, Tuple

from store.gate import secret_scan

from . import telemetry

# Placeholder used when a fetched value trips the secret scanner. Carries the
# scanner's LABEL only (see store/gate.py secret_scan docstring: "Never
# returns the matched value itself") — never the matched text.
_QUARANTINE_TEMPLATE = "[secret: {label} quarantined]"


@dataclass(frozen=True)
class FactSpec:
    """Declares one fact type the reconciler tracks across sources.

    `fields` maps source name -> the field name that source's
    `fetch_state()` dict uses for this fact (sources are free to use their
    own native field names; the reconciler does the mapping). A source
    absent from `fields`, or one that didn't return that field, is simply
    left out of the comparison for this fact.

    `source_of_truth` designates, for THIS fact type, which source name is
    authoritative when sources disagree — configurable per fact type, not
    global, per the task spec ("Designate one source-of-truth per fact
    type (configurable)").
    """

    fact_key: str
    fields: Mapping[str, str]
    source_of_truth: str


# JUDGMENT CALL: the task's example scenario ("store says next-step 'X',
# GitHub HEAD 'Y', tracker status 'Z'") compares three sources' views of one
# conceptual fact ("what should happen next") even though each source names
# it differently in its own native vocabulary (a store key, a HEAD ref, a
# tracker status field). FactSpec.fields is the configurable mapping that
# lets one fact_key ("next_step") pull from each source's own field name.
# Callers with a different fact vocabulary pass their own fact_specs to
# reconcile() instead of this default.
DEFAULT_FACT_SPECS: Tuple[FactSpec, ...] = (
    FactSpec(
        fact_key="next_step",
        fields={"store": "next_step", "github": "head", "tracker": "status"},
        source_of_truth="store",
    ),
)


@dataclass(frozen=True)
class FactDrift:
    """Per-fact-key comparison across whichever sources reported it."""

    fact_key: str
    # source name -> report-safe value (already secret-scanned/quarantined)
    values: Dict[str, str]
    # source name -> secret_scan() labels, ONLY for values that were quarantined
    quarantined: Dict[str, Tuple[str, ...]]
    source_of_truth: str
    source_of_truth_value: Optional[str]
    agree: bool
    diverging_sources: Tuple[str, ...]
    # sources this fact's spec named but which didn't report the field
    missing_sources: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DriftReport:
    """The reconciler's output. Never anything but a report: no field or
    method on this type performs, queues, or triggers a write anywhere."""

    facts: Tuple[FactDrift, ...]
    sources_consulted: Tuple[str, ...]

    @property
    def has_drift(self) -> bool:
        return any(not f.agree for f in self.facts)

    def fact(self, fact_key: str) -> Optional[FactDrift]:
        for f in self.facts:
            if f.fact_key == fact_key:
                return f
        return None


def _report_safe(value: object) -> Tuple[str, Tuple[str, ...]]:
    """Scan a fetched value and return (safe_text, labels).

    CRITICAL SAFETY INVARIANT #2: every string that can enter a DriftReport
    passes through `store.gate.secret_scan` first. Non-str values are
    stringified before scanning (a reconciler compares/report text, not
    typed payloads). `secret_scan` itself never returns matched text — only
    labels (store/gate.py) — so quarantining just means: don't put the
    original text in the report at all, put the label-only placeholder
    instead.
    """
    if value is None:
        return "", ()
    text = value if isinstance(value, str) else str(value)
    labels = tuple(secret_scan(text.encode("utf-8", errors="replace")))
    if labels:
        return _QUARANTINE_TEMPLATE.format(label=labels[0]), labels
    return text, ()


def reconcile(
    sources: Mapping[str, object],
    fact_specs: Sequence[FactSpec] = DEFAULT_FACT_SPECS,
    *,
    emit_telemetry: bool = True,
    telemetry_dir: Optional[str] = None,
) -> DriftReport:
    """Observe drift across `sources` and return a `DriftReport`. READ-ONLY.

    `sources`: mapping of source name -> object with `.fetch_state() -> dict`
    (a `StoreSource`, `FilesSource`, or any `GitHubSource`/`TrackerSource`-
    shaped mock). This function calls `.fetch_state()` exactly once per
    source and nothing else on any source object.

    `fact_specs`: which facts to compare and each one's designated
    source-of-truth (see `FactSpec`); defaults to `DEFAULT_FACT_SPECS`.

    `emit_telemetry`: if True (default), append a labels/counts-only JSONL
    event describing the outcome via `reconciler.telemetry`. Tests pass
    `telemetry_dir` (or `emit_telemetry=False`) to avoid writing outside
    their own temp directory.
    """
    raw_state: Dict[str, dict] = {}
    for name, source in sources.items():
        fetch_state = getattr(source, "fetch_state", None)
        if fetch_state is None or not callable(fetch_state):
            raise TypeError(
                f"source {name!r} does not implement fetch_state() -> dict"
            )
        result = fetch_state()  # the ONLY call this function ever makes on a source
        if not isinstance(result, dict):
            raise TypeError(
                f"source {name!r}.fetch_state() must return a dict, got {type(result)!r}"
            )
        raw_state[name] = result

    facts = []
    for spec in fact_specs:
        values: Dict[str, str] = {}
        quarantined: Dict[str, Tuple[str, ...]] = {}
        missing: list = []

        for source_name, field_name in spec.fields.items():
            source_dict = raw_state.get(source_name, {})
            if field_name not in source_dict:
                missing.append(source_name)
                continue
            safe_value, labels = _report_safe(source_dict[field_name])
            values[source_name] = safe_value
            if labels:
                quarantined[source_name] = labels

        distinct_values = set(values.values())
        agree = len(distinct_values) <= 1
        diverging = tuple(sorted(values.keys())) if not agree else ()
        source_of_truth_value = values.get(spec.source_of_truth)

        facts.append(
            FactDrift(
                fact_key=spec.fact_key,
                values=values,
                quarantined=quarantined,
                source_of_truth=spec.source_of_truth,
                source_of_truth_value=source_of_truth_value,
                agree=agree,
                diverging_sources=diverging,
                missing_sources=tuple(missing),
            )
        )

    report = DriftReport(facts=tuple(facts), sources_consulted=tuple(sorted(sources.keys())))

    if emit_telemetry:
        telemetry.emit_reconcile_event(report, telemetry_dir=telemetry_dir)

    return report


__all__ = ["reconcile", "DriftReport", "FactDrift", "FactSpec", "DEFAULT_FACT_SPECS"]
