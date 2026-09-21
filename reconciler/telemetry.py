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
import re
import time
from pathlib import Path
from typing import Optional

from store.gate import secret_scan

DEFAULT_TELEMETRY_DIR = Path(".knokeep-eval")
TELEMETRY_FILENAME = "reconciler.jsonl"

# Source names (reconcile()'s `sources` mapping keys) and fact keys
# (FactSpec.fact_key) are CALLER-DEFINED IDENTIFIERS, not fetched VALUES —
# reconcile()'s secret-scan/quarantine path (CRITICAL SAFETY INVARIANT #2)
# only ever covers fetched values, never these structural identifiers. A
# caller (e.g. the MCP server's knokeep_reconcile tool, which accepts
# arbitrary fact_specs[].fact_key strings from a tool argument) could
# otherwise smuggle an arbitrary/sensitive-looking string into this local
# telemetry file simply by using it AS an identifier (review finding).
#
# Two independent checks gate what gets persisted verbatim: (1) a small,
# bounded charset/length shape -- catches malformed/oversized/control-
# character identifiers -- and (2) the SAME `store.gate.secret_scan` used
# everywhere else in this project -- catches an identifier that is ITSELF
# secret-shaped (most real secrets, e.g. "ghp_...", "sk-...", are already
# charset-legal short alnum/hyphen strings, so charset alone would not have
# caught them). Anything failing either check becomes a fixed, content-free
# placeholder — never a transformed/partial version of the original value.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_REDACTED_IDENTIFIER = "[non-identifier]"


def _safe_identifier(value: str) -> str:
    """Return `value` unchanged if it's a small, closed-vocabulary-shaped
    identifier that ALSO doesn't itself look like a secret; otherwise a
    fixed, content-free placeholder. Never returns a transformed/partial
    version of `value` — only the original or the fixed placeholder — so
    this can never leak a fragment of a sensitive string."""
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        return _REDACTED_IDENTIFIER
    try:
        if secret_scan(value.encode("utf-8", errors="replace")):
            return _REDACTED_IDENTIFIER
    except Exception:
        return _REDACTED_IDENTIFIER
    return value


def emit_reconcile_event(report, *, telemetry_dir: Optional[Path] = None) -> Path:
    """Append one JSONL record summarizing a `reconcile()` outcome.

    `report` is a `reconcile.DriftReport`. Only labels/counts derived from it
    are recorded — see the module docstring. Returns the path written to.
    """
    directory = Path(telemetry_dir) if telemetry_dir is not None else DEFAULT_TELEMETRY_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / TELEMETRY_FILENAME

    # Sanitize BEFORE sorting/persisting — see _safe_identifier's docstring:
    # these are caller-defined identifiers, never scanned as fetched values.
    diverging_fact_keys = tuple(
        _safe_identifier(f.fact_key) for f in report.facts if not f.agree
    )
    sources_consulted = tuple(_safe_identifier(s) for s in report.sources_consulted)
    quarantined_field_count = sum(len(f.quarantined) for f in report.facts)

    record = {
        "event": "reconcile",
        "ts": time.time(),
        "sources_consulted": sorted(sources_consulted),
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
