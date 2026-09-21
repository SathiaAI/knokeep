"""Local-only reconciler telemetry — labels/counts only, no phone-home.

Matches the project's existing telemetry model (claude/knokeep-threat-model.md
§4: "No phone-home / telemetry to owner or server. Telemetry is local JSONL,
labels-only, read by health/eval. Privacy pillar — non-negotiable."):
  - written to a local file only, never sent over a network;
  - one JSON object per line (JSONL), append-only;
  - carries LABELS AND COUNTS ONLY — fact keys and source names (closed-
    vocabulary identifiers the caller chose, not fetched content) plus
    integer counts — never a fact's value, a file's contents, or a matched
    secret string.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

DEFAULT_TELEMETRY_DIR = Path(".knokeep-eval")
TELEMETRY_FILENAME = "reconciler.jsonl"


def emit_reconcile_event(report, *, telemetry_dir: Optional[Path] = None) -> Path:
    """Append one JSONL record summarizing a `reconcile()` outcome.

    `report` is a `reconcile.DriftReport`. Only labels/counts derived from it
    are recorded — see the module docstring. Returns the path written to.
    """
    directory = Path(telemetry_dir) if telemetry_dir is not None else DEFAULT_TELEMETRY_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / TELEMETRY_FILENAME

    diverging_fact_keys = tuple(f.fact_key for f in report.facts if not f.agree)
    quarantined_field_count = sum(len(f.quarantined) for f in report.facts)

    record = {
        "event": "reconcile",
        "ts": time.time(),
        "sources_consulted": sorted(report.sources_consulted),
        "fact_count": len(report.facts),
        "diverging_fact_count": len(diverging_fact_keys),
        "diverging_fact_keys": sorted(diverging_fact_keys),
        "quarantined_field_count": quarantined_field_count,
        "has_drift": report.has_drift,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return path


__all__ = ["emit_reconcile_event", "DEFAULT_TELEMETRY_DIR", "TELEMETRY_FILENAME"]
