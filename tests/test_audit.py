#!/usr/bin/env python3
"""Layer-2 audit tests: gitleaks catches a planted secret, passes a clean store,
and the audit output never re-persists the matched secret value."""
import subprocess, sys, os, json, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT = os.path.join(ROOT, "tools", "knokeep_audit.py")
results = []

def check(name, cond):
    results.append((name, cond)); print(("PASS " if cond else "FAIL ") + name)

def run_audit(d):
    p = subprocess.run([sys.executable, AUDIT, d], capture_output=True, text=True)
    return p.returncode, p.stdout

if not shutil.which("gitleaks"):
    print("SKIP: gitleaks not installed"); sys.exit(0)

# clean store -> 0 leaks, exit 0
clean = tempfile.mkdtemp(prefix="kk_clean_")
open(os.path.join(clean, "system_state.md"), "w").write("## Architecture\nreferences only, e.g. os.getenv('API_KEY')\n")
rc, out = run_audit(clean)
sc = json.loads(out)
check("clean store: 0 leaks", sc.get("leaks") == 0)
check("clean store: exit 0", rc == 0)
shutil.rmtree(clean, ignore_errors=True)

# dirty store -> leaks detected, exit 1, secret value NOT echoed
dirty = tempfile.mkdtemp(prefix="kk_dirty_")
PLANT = "AKIA" + "Q7ZX4K2MB9DL3PYW"
open(os.path.join(dirty, "leak.md"), "w").write(
    "-----BEGIN RSA PRIVATE KEY-----\nMIIabc123\n-----END RSA PRIVATE KEY-----\naws_key = " + PLANT + "\n")
rc, out = run_audit(dirty)
sc = json.loads(out)
check("dirty store: leak detected", (sc.get("leaks") or 0) >= 1)
check("dirty store: exit 1", rc == 1)
check("audit output does NOT echo secret", PLANT not in out)
shutil.rmtree(dirty, ignore_errors=True)

passed = sum(1 for _, c in results if c)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
