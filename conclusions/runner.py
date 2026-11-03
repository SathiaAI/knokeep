# SPDX-License-Identifier: Elastic-2.0 OR LicenseRef-Viaknox-Commercial
# Copyright (c) 2026 Viaknox Holding LLC
"""End to end: read exact entries, propose conclusions, verify, promote.

    exact entries --generator--> candidates --store.propose--> proposed
                                                  --store.promote (verifier + judge)--> accepted | stays proposed

Safe by construction, because each step is already safe on its own:
  * the runner only READS exact entries and only WRITES inside the reserved
    `.conclusions/` segment, through the gate;
  * the generator has no tools and its output is untrusted;
  * nothing becomes `accepted` unless the verifier passes;
  * money stops the run: when the generator's dollar budget is spent, or the
    judge keeps failing, the run ends and says why instead of grinding on.

Re-running is cheap and safe: a candidate whose text matches an existing live or
proposed conclusion is skipped, never proposed twice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from . import store as cs
from . import verify as v
from .generator import ConclusionGenerator, DollarBudgetExceeded, GeneratorError, MAX_ENTRIES_CHARS

_SESSION_RE = re.compile(r"(?:^|/)sessions/([^/]+)/")
_MAX_CONSECUTIVE_JUDGE_FAILURES = 3


@dataclass(frozen=True)
class RunSummary:
    entries_read: int
    entries_skipped: int
    batches: int
    candidates: int
    duplicates: int
    proposed: int
    accepted: int
    left_proposed: int
    refused: Dict[str, int]    # store refusals by code (for example SECRET)
    failures: Dict[str, int]   # verifier failure codes across left-proposed conclusions
    stopped: Optional[str]     # None; "budget" or "judge_unavailable" ended the run; "generator_error" means at least one batch failed


def session_from_key(key: str) -> v.SourceInfo:
    """Default provenance: the session id in a `.../sessions/<id>/...` key."""
    m = _SESSION_RE.search(key)
    return v.SourceInfo(session_id=m.group(1) if m else None)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def run_space(
    backend,
    space: str,
    *,
    generator: ConclusionGenerator,
    judge: v.Judge,
    judge_family: str,
    source_info: Callable[[str], v.SourceInfo] = session_from_key,
    resolve: Optional[v.Resolve] = None,
    max_entries: int = 200,
    batch_chars: int = 20_000,
) -> RunSummary:
    if not isinstance(space, str) or not space.endswith("/") or space == "/":
        raise ValueError('space must be a key prefix ending in "/"')
    if not 1 <= batch_chars <= MAX_ENTRIES_CHARS:
        raise ValueError("batch_chars out of range")
    resolve = resolve or cs.backend_resolver(backend)

    # -- read the exact entries (read only) --------------------------------------
    texts: Dict[str, str] = {}
    hashes: Dict[str, str] = {}
    skipped = 0
    for key in sorted(backend.list(space)):
        if key.startswith(space + v.DERIVED_SEGMENT):
            continue
        if len(texts) >= max_entries:
            break
        blob = backend.read(key)
        try:
            text = bytes(blob.body).decode("utf-8") if blob is not None else ""
        except UnicodeDecodeError:
            text = ""
        if not text.strip() or len(text) > MAX_ENTRIES_CHARS:
            skipped += 1
            continue
        texts[key], hashes[key] = text, blob.version_hash

    batches: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    size = 0
    for key, text in texts.items():
        if current and size + len(text) > batch_chars:
            batches.append(current)
            current, size = {}, 0
        current[key] = text
        size += len(text)
    if current:
        batches.append(current)

    existing = {
        _norm(c["text"])
        for rec in cs.list_records(backend, space)
        if rec["status"] in (cs.PROPOSED, cs.ACCEPTED)
        for c in rec["claims"]
    }

    candidates = duplicates = proposed = accepted = left = 0
    refused: Dict[str, int] = {}
    failures: Dict[str, int] = {}
    stopped: Optional[str] = None
    judge_failures_in_a_row = 0
    ran = 0

    for batch in batches:
        ran += 1
        try:
            found = generator.propose(space, batch)
        except DollarBudgetExceeded:
            stopped = "budget"
            break
        except GeneratorError:
            stopped = "generator_error"
            continue
        for cand in found:
            candidates += 1
            norm = _norm(cand.text)
            if norm in existing:
                duplicates += 1
                continue
            claim = v.Claim(
                cand.text,
                tuple(v.Citation(key, hashes[key], quote) for key, quote in cand.cites),
            )
            try:
                cid = cs.propose(backend, space, [claim], generator.family)
            except cs.ConclusionRefused as exc:
                refused[exc.code] = refused.get(exc.code, 0) + 1
                continue
            existing.add(norm)
            proposed += 1
            verdict = cs.promote(
                backend, space, cid,
                resolve=resolve, source_info=source_info, judge=judge, judge_family=judge_family,
            )
            if verdict.accepted:
                accepted += 1
                judge_failures_in_a_row = 0
                continue
            left += 1
            codes = {f.code for f in verdict.failures}
            for code in codes:
                failures[code] = failures.get(code, 0) + 1
            judge_failures_in_a_row = judge_failures_in_a_row + 1 if v.JUDGE_ERROR in codes else 0
            if judge_failures_in_a_row >= _MAX_CONSECUTIVE_JUDGE_FAILURES:
                stopped = "judge_unavailable"
                break
        if stopped == "judge_unavailable":
            break

    return RunSummary(
        entries_read=len(texts), entries_skipped=skipped, batches=ran, candidates=candidates,
        duplicates=duplicates, proposed=proposed, accepted=accepted, left_proposed=left,
        refused=refused, failures=failures, stopped=stopped,
    )


__all__ = ["run_space", "RunSummary", "session_from_key"]
