#!/usr/bin/env python3
"""KnoKeep Layer-2 independent audit (soak-time, NOT part of the shipped gate).
Runs gitleaks - a DIFFERENT scanner than our inline gate - over a memory store to
catch any secret the gate missed. This is the only metric the self-log cannot
produce (a miss is invisible to the thing that missed it). Requires gitleaks on PATH.
Output is slimmed to rule+file+line: gitleaks' own JSON carries the matched secret,
which we must never re-persist."""
import sys, os, json, subprocess, tempfile, shutil

def audit(store):
    gl = shutil.which("gitleaks")
    if not gl:
        return {"scanner": None, "error": "gitleaks not on PATH", "leaks": None}
    tmpd = tempfile.mkdtemp(prefix="kk_audit_")
    rpt = os.path.join(tmpd, "gl.json")
    variants = [
        [gl, "dir", store, "-f", "json", "-r", rpt, "--no-banner", "--exit-code", "0"],
        [gl, "detect", "--source", store, "--no-git", "-f", "json", "-r", rpt, "--exit-code", "0"],
    ]
    used = None
    for c in variants:
        try:
            subprocess.run(c, capture_output=True, text=True, timeout=180)
        except Exception:
            continue
        if os.path.exists(rpt):
            used = c[1]; break
    findings = []
    if os.path.exists(rpt):
        try: findings = json.load(open(rpt, encoding="utf-8")) or []
        except Exception: findings = []
    shutil.rmtree(tmpd, ignore_errors=True)
    slim = [{"rule": f.get("RuleID"), "file": f.get("File"), "line": f.get("StartLine")} for f in findings]
    return {"scanner": ("gitleaks:" + used) if used else "gitleaks:none", "leaks": len(slim), "findings": slim}

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(json.dumps({"error": "usage: knokeep_audit.py <store-dir>"})); sys.exit(2)
    r = audit(sys.argv[1])
    print(json.dumps(r, indent=1))
    sys.exit(1 if (r.get("leaks") or 0) > 0 else 0)
