"""Shared identifier validation (skill + application query layer).

Uses full-string matching so values like ``p\\n`` are rejected (``re.match`` alone
would accept the prefix). Any ASCII whitespace anywhere in the string is rejected.
Valid identifier shapes are unchanged for well-formed ids; write callers share
this helper.
"""
from __future__ import annotations

import re

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

RESERVED = (
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def valid_id(v: object) -> bool:
    if not isinstance(v, str):
        return False
    if any(ch.isspace() for ch in v):
        return False
    if not ID_RE.fullmatch(v):
        return False
    if v in (".", "..") or v.startswith(".") or v.endswith("."):
        return False
    return v.split(".")[0].lower() not in RESERVED and v.lower() not in RESERVED
