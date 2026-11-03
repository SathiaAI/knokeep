# SPDX-License-Identifier: Elastic-2.0 OR LicenseRef-Viaknox-Commercial
# Copyright (c) 2026 Viaknox Holding LLC
"""The automatic verifier for proposed conclusions.

A conclusion is a short derived statement ("we chose Postgres for the login
service") that cites exact memory entries. It may be accepted with no person
in the loop ONLY if every check here passes. The checks are deterministic and
fail closed; the one model-based check (does the evidence support the claim?)
is injected, runs last, and runs only if everything deterministic already
passed.

This module is pure: it does no I/O, calls no model and touches no store. The
caller supplies two small functions:

  resolve(key, version_hash) -> bytes | None
      the exact bytes of that version of that entry, or None if unknown;
  source_info(key) -> SourceInfo
      who/what produced the entry (session, owner statement, untrusted mark).

Safety properties, each covered by tests:
  * A conclusion is informational. Text shaped like an instruction to an agent
    (or a request for access) is refused (heuristic, deliberately cheap).
  * It stays inside ONE space: every cited key sits under the conclusion's
    space prefix, and none sits under the derived `.conclusions/` segment
    (evidence is exact memory only).
  * It is grounded: each citation names an exact version and quotes it
    verbatim; a tiny or missing quote does not count.
  * It is corroborated: supported by two independent sessions, or by an owner
    statement.
  * It is untainted: any untrusted-marked source blocks automatic acceptance.
  * It is clean: the gate's own secret scanner finds nothing in it.
  * The judge model is from a different family than the generator, and a
    judge that is missing, errs, or is unsure means REJECT.
  * A verdict carries codes and indexes only: never claim text, a quote, or a
    matched secret value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from store import gate

# --- limits -----------------------------------------------------------------
MAX_CLAIMS = 20
MAX_CLAIM_CHARS = 500
MAX_TOTAL_CHARS = 4000
MAX_CITATIONS_PER_CLAIM = 10
MIN_QUOTE_CHARS = 12
MAX_QUOTE_CHARS = 1000

# Derived conclusions live under this segment of a space. Evidence must be
# EXACT memory, so a citation into it is refused: otherwise derived text could
# launder itself into "evidence" for further derived text.
DERIVED_SEGMENT = ".conclusions/"

# --- failure codes (closed vocabulary) --------------------------------------
SHAPE = "SHAPE"
CROSS_SPACE = "CROSS_SPACE"
CITES_DERIVED = "CITES_DERIVED"
UNGROUNDED = "UNGROUNDED"
SECRET = "SECRET"
INSTRUCTION_SHAPED = "INSTRUCTION_SHAPED"
NOT_CORROBORATED = "NOT_CORROBORATED"
TAINTED = "TAINTED"
NO_JUDGE = "NO_JUDGE"
JUDGE_NOT_INDEPENDENT = "JUDGE_NOT_INDEPENDENT"
JUDGE_ERROR = "JUDGE_ERROR"
NOT_ENTAILED = "NOT_ENTAILED"


@dataclass(frozen=True)
class Citation:
    key: str
    version_hash: str
    quote: str


@dataclass(frozen=True)
class Claim:
    text: str
    citations: Tuple[Citation, ...]


@dataclass(frozen=True)
class Conclusion:
    space: str  # key prefix ending in "/"
    claims: Tuple[Claim, ...]
    generator_family: str  # model family that produced it, e.g. "anthropic"


@dataclass(frozen=True)
class SourceInfo:
    """What the verifier needs to know about a cited entry."""

    session_id: Optional[str] = None  # the session that wrote it, if known
    owner_statement: bool = False  # written or confirmed by the owner
    tainted: bool = False  # carries the untrusted mark (relayed/outside text)


@dataclass(frozen=True)
class Failure:
    code: str
    claim_index: Optional[int] = None  # which claim, never its text


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    failures: Tuple[Failure, ...] = ()
    # True when the ONLY reason it cannot be accepted is an untrusted source;
    # the caller may then keep it as `proposed`, never as accepted.
    tainted: bool = False
    secret_labels: Tuple[str, ...] = field(default=())  # labels only


Resolve = Callable[[str, str], Optional[bytes]]
SourceInfoFn = Callable[[str], SourceInfo]
# judge(claim_text, quotes) -> True (supported) / False (not supported) / None (unsure)
Judge = Callable[[str, Sequence[str]], Optional[bool]]


# --- instruction-shaped text (best-effort, fail-closed) ---------------------
# Conclusions describe what is true; they must not tell an agent what to do or
# ask for access. This is a cheap heuristic, not a guarantee: anything it
# misses still has to pass grounding, corroboration and the judge, and a
# conclusion can never grant authority regardless (see module docstring).
_IMPERATIVE_START = (
    r"always|never|do not|don't|ignore|disregard|override|bypass|skip|"
    r"run|execute|approve|grant|send|delete|deploy|disable|enable|"
    r"reveal|print|paste|forward|share|install|merge|push|commit"
)
_INSTRUCTION_RES = [
    re.compile(rf"(?:^|[.!?;:]\s+|\n)\s*(?:{_IMPERATIVE_START})\b", re.IGNORECASE),
    re.compile(r"\b(?:ignore|disregard|forget)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all)\b", re.IGNORECASE),
    re.compile(r"\b(?:system prompt|developer message)\b", re.IGNORECASE),
    re.compile(r"\byou\s+(?:must|should|shall|will|are to|need to)\b", re.IGNORECASE),
    re.compile(r"\b(?:the )?(?:agent|assistant|model|ai)\s+(?:must|should|shall|needs? to)\b", re.IGNORECASE),
    re.compile(r"\b(?:grant|give|widen|expand|elevate)\b[^.\n]{0,40}\b(?:access|permission|permissions|scope|privileges?|token|key)\b", re.IGNORECASE),
    re.compile(r"```|<\s*/?\s*(?:system|assistant|tool)\b", re.IGNORECASE),
]


def _instruction_shaped(text: str) -> bool:
    return any(rx.search(text) for rx in _INSTRUCTION_RES)


def _shape_failures(c: Conclusion) -> List[Failure]:
    out: List[Failure] = []
    if not isinstance(c.space, str) or not c.space.endswith("/") or c.space == "/" or "\x00" in c.space:
        out.append(Failure(SHAPE))
    if not isinstance(c.generator_family, str) or not c.generator_family.strip():
        out.append(Failure(SHAPE))
    if not c.claims or len(c.claims) > MAX_CLAIMS:
        out.append(Failure(SHAPE))
        return out
    total = 0
    for i, claim in enumerate(c.claims):
        text = claim.text
        total += len(text) if isinstance(text, str) else 0
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_CLAIM_CHARS or "\x00" in text:
            out.append(Failure(SHAPE, i))
        n = len(claim.citations)
        if n < 1 or n > MAX_CITATIONS_PER_CLAIM:
            out.append(Failure(SHAPE, i))
            continue
        for cit in claim.citations:
            q = cit.quote
            if (
                not isinstance(cit.key, str) or not cit.key
                or not isinstance(cit.version_hash, str) or not cit.version_hash
                or not isinstance(q, str)
                or len(q.strip()) < MIN_QUOTE_CHARS or len(q) > MAX_QUOTE_CHARS
                or "\x00" in q
            ):
                out.append(Failure(SHAPE, i))
                break
    if total > MAX_TOTAL_CHARS:
        out.append(Failure(SHAPE))
    return out


def verify(
    conclusion: Conclusion,
    *,
    resolve: Resolve,
    source_info: SourceInfoFn,
    judge: Optional[Judge] = None,
    judge_family: Optional[str] = None,
) -> Verdict:
    """Decide whether `conclusion` may be accepted automatically. Never raises
    for a bad conclusion; every problem becomes a Failure and the answer is
    "not accepted". Checks run cheapest first; the judge runs only if every
    deterministic check passed."""
    failures: List[Failure] = _shape_failures(conclusion)
    if failures:
        # Malformed input is not examined further.
        return Verdict(False, tuple(failures))

    secret_labels: List[str] = []
    any_tainted = False
    corroborated: List[bool] = []

    for i, claim in enumerate(conclusion.claims):
        # -- clean: no secrets, nothing instruction-shaped ---------------------
        scan_text = "\n".join([claim.text] + [c.quote for c in claim.citations])
        try:
            labels = gate.secret_scan(scan_text.encode("utf-8"))
        except (ValueError, UnicodeError):
            labels = ["unscannable"]
        if labels:
            failures.append(Failure(SECRET, i))
            secret_labels.extend(labels)
        if _instruction_shaped(claim.text):
            failures.append(Failure(INSTRUCTION_SHAPED, i))

        sessions: set = set()
        owner_backed = False
        for cit in claim.citations:
            # -- one space only ------------------------------------------------
            if not cit.key.startswith(conclusion.space):
                failures.append(Failure(CROSS_SPACE, i))
                continue
            # -- evidence must be exact memory, never another conclusion ------
            if cit.key.startswith(conclusion.space + DERIVED_SEGMENT):
                failures.append(Failure(CITES_DERIVED, i))
                continue
            # -- grounded: exact version exists and contains the quote verbatim
            raw = resolve(cit.key, cit.version_hash)
            if raw is None:
                failures.append(Failure(UNGROUNDED, i))
                continue
            try:
                source_text = bytes(raw).decode("utf-8")
            except UnicodeDecodeError:
                failures.append(Failure(UNGROUNDED, i))
                continue
            if cit.quote not in source_text:
                failures.append(Failure(UNGROUNDED, i))
                continue
            # -- who produced it -------------------------------------------------
            info = source_info(cit.key)
            if info.tainted:
                any_tainted = True
            if info.owner_statement:
                owner_backed = True
            if info.session_id:
                sessions.add(info.session_id)
        corroborated.append(owner_backed or len(sessions) >= 2)

    # -- corroborated: two independent sessions, or an owner statement ---------
    for i, ok in enumerate(corroborated):
        if not ok:
            failures.append(Failure(NOT_CORROBORATED, i))

    # -- untainted: an untrusted source blocks automatic acceptance ------------
    if any_tainted:
        failures.append(Failure(TAINTED))

    if failures:
        only_taint = all(f.code == TAINTED for f in failures)
        return Verdict(
            False, tuple(failures), tainted=any_tainted and only_taint,
            secret_labels=tuple(sorted(set(secret_labels))),
        )

    # -- the judge: independent family, fail closed ---------------------------
    if judge is None:
        return Verdict(False, (Failure(NO_JUDGE),))
    if (
        not isinstance(judge_family, str) or not judge_family.strip()
        or judge_family.strip().casefold() == conclusion.generator_family.strip().casefold()
    ):
        return Verdict(False, (Failure(JUDGE_NOT_INDEPENDENT),))
    for i, claim in enumerate(conclusion.claims):
        quotes = [c.quote for c in claim.citations]
        try:
            supported = judge(claim.text, quotes)
        except Exception:  # a judge that errs is a judge that did not approve
            failures.append(Failure(JUDGE_ERROR, i))
            continue
        if supported is not True:  # False and "unsure" both reject
            failures.append(Failure(NOT_ENTAILED, i))
    if failures:
        return Verdict(False, tuple(failures))
    return Verdict(True)


__all__ = [
    "Citation", "Claim", "Conclusion", "SourceInfo", "Failure", "Verdict",
    "verify", "DERIVED_SEGMENT",
    "SHAPE", "CROSS_SPACE", "CITES_DERIVED", "UNGROUNDED", "SECRET", "INSTRUCTION_SHAPED",
    "NOT_CORROBORATED", "TAINTED", "NO_JUDGE", "JUDGE_NOT_INDEPENDENT",
    "JUDGE_ERROR", "NOT_ENTAILED",
]
