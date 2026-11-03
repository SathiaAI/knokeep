# SPDX-License-Identifier: Elastic-2.0 OR LicenseRef-Viaknox-Commercial
# Copyright (c) 2026 Viaknox Holding LLC
"""Calibrate the Jev judge against labelled cases. Manual tool, not part of CI:
it makes live calls to Jev, inside a hard call/token budget.

    python tools/eval_jev_judge.py [--cases F] [--out F] [--max-calls N] [--max-tokens N]

Reads the key from TYPESAFE_API_KEY if set; where a proxy injects the
Authorization header, no key is needed. Prints only case ids, labels and
scores. It never prints the key, the claims, or the evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from conclusions.jev import Budget, BudgetExceeded, JevError, JevJudge, Thresholds  # noqa: E402


def _verdict(t: Thresholds, s: float, o: float):
    if s <= t.reject_support_at or o >= t.reject_overreach_at:
        return False
    if s >= t.accept_support_at and o <= t.accept_overreach_at:
        return True
    return None


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=os.path.join(here, "..", "conclusions", "eval", "jev_judge_cases.json"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-calls", type=int, default=80)
    ap.add_argument("--max-tokens", type=int, default=60_000)
    args = ap.parse_args()

    cases = json.load(open(args.cases, encoding="utf-8"))["cases"]
    budget = Budget(args.max_calls, args.max_tokens)
    judge = JevJudge.from_env(budget=budget)

    rows = []
    for c in cases:
        try:
            s, o = judge.score(c["claim"], c["evidence"])
            rows.append({"id": c["id"], "kind": c["kind"], "label": c["label"], "support": s, "overreach": o})
        except BudgetExceeded:
            print("BUDGET EXHAUSTED - stopping", file=sys.stderr)
            break
        except JevError as exc:
            rows.append({"id": c["id"], "kind": c["kind"], "label": c["label"], "error": str(exc)})

    print(f"{'id':5} {'kind':14} {'label':12} {'support':>8} {'overreach':>10}")
    for r in rows:
        if "error" in r:
            print(f"{r['id']:5} {r['kind']:14} {r['label']:12} ERROR {r['error']}")
        else:
            print(f"{r['id']:5} {r['kind']:14} {r['label']:12} {r['support']:8.2f} {r['overreach']:10.2f}")

    scored = [r for r in rows if "error" not in r]
    t = Thresholds()
    print(f"\ncalls={budget.calls} tokens={budget.tokens} errors={len(rows) - len(scored)}")
    for label in ("supported", "unsupported"):
        group = [r for r in scored if r["label"] == label]
        out = {True: 0, None: 0, False: 0}
        for r in group:
            out[_verdict(t, r["support"], r["overreach"])] += 1
        print(f"default thresholds, {label:11} n={len(group):2}: accepted={out[True]} unsure={out[None]} rejected={out[False]}")
    false_acc = [r["id"] for r in scored if r["label"] == "unsupported" and _verdict(t, r["support"], r["overreach"]) is True]
    print("FALSE ACCEPTS:", false_acc or "none")

    uns = [r for r in scored if r["label"] == "unsupported"]
    sup = [r for r in scored if r["label"] == "supported"]
    if uns and sup:
        print(f"highest support among unsupported: {max(r['support'] for r in uns):.2f}")
        print(f"lowest support among supported:    {min(r['support'] for r in sup):.2f}")
        print(f"highest overreach among supported: {max(r['overreach'] for r in sup):.2f}")
        print(f"lowest overreach among unsupported:{min(r['overreach'] for r in uns):.2f}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"thresholds": t.__dict__, "calls": budget.calls, "tokens": budget.tokens, "rows": rows}, fh, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
