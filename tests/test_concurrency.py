#!/usr/bin/env python3
"""Concurrency over the V2 store: two concurrent flush-state with the same expect-hash
-> exactly one wins, one stale (no lost update); and the store is never wedged after
contention (LocalBackend's OS-level cas.lock releases on process exit + journal replay,
so there is no V1-style TTL lockfile to break)."""
import subprocess, sys, os, json, tempfile, shutil, concurrent.futures

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "skill", "knokeep_state.py")
store = tempfile.mkdtemp(prefix="knokeep_conc_"); proj = "demo"

def run(args):
    return subprocess.run([sys.executable, STATE, *args, "--store", store, "--project", proj], capture_output=True, text=True)

run(["init"])
h0 = json.loads(run(["bootstrap"]).stdout)["version_hash"]

def w(tag):
    b = os.path.join(store, f"_b{tag}.md"); open(b, "w", encoding="utf-8").write(f"## Architecture\nwriter {tag}")
    return run(["flush-state", "--body-file", b, "--expect-hash", h0])

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
    r = list(ex.map(w, ["A", "B"]))

oks = sum(1 for x in r if x.returncode == 0)
# H1 (bounded_cas_reread_reapply): the losing whole-doc writer no longer gets a
# bare "stale" — it fails closed with its body PARKED under conflicts/ (no lost
# update, nothing silently dropped). Exactly one wins, exactly one parks.
parked = sum(1 for x in r if "conflict_parked" in x.stderr)
ok = (oks == 1 and parked == 1)
print(f"oks={oks} parked={parked}")
print(("PASS " if ok else "FAIL ") + "2-process no-lost-update (one wins, one parked)")

# The store is not wedged after contention: a fresh flush with the current hash
# proceeds (no deadlock; the winner's lock was released, journal is consistent).
h2 = json.loads(run(["bootstrap"]).stdout)["version_hash"]
bb = os.path.join(store, "_sb.md"); open(bb, "w", encoding="utf-8").write("## Architecture\nafter contention")
r2 = run(["flush-state", "--body-file", bb, "--expect-hash", h2])
ok2 = r2.returncode == 0
print(("PASS " if ok2 else "FAIL ") + "post-contention flush proceeds (store not wedged)")
shutil.rmtree(store, ignore_errors=True)
sys.exit(0 if (ok and ok2) else 1)
