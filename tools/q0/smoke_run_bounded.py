#!/usr/bin/env python3
"""Harmless local smoke checks for run_bounded.py. No network, model or credentials.

1. child exits 0            -> outcome exited, os_exit_code 0, stdout preserved
2. child exits 7            -> outcome exited, os_exit_code 7 even though it PRINTS "exit_code: 0"
3. child + grandchild sleep -> outcome timeout_killed, both PIDs gone afterwards
Prints one JSON line per case and exits 0 only if every check passes.
"""
import json, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_bounded as rb

PY = sys.executable
out = tempfile.mkdtemp(prefix="rb-smoke-")
results, ok = [], True


def case(name, cmd, timeout, checks):
    global ok
    d = os.path.join(out, name)
    rec = rb.run(cmd, timeout, d)
    so = open(os.path.join(d, "stdout.bin"), "rb").read()
    se = open(os.path.join(d, "stderr.bin"), "rb").read()
    res = {k: v(rec, so, se) for k, v in checks.items()}
    passed = all(res.values())
    ok &= passed
    print(json.dumps({"case": name, "pass": passed, "outcome": rec["outcome"], "os_exit_code": rec["os_exit_code"],
                      "elapsed_s": rec["elapsed_s"], "tree_kill": rec["tree_kill"], "checks": res}))
    return rec, d


case("exit0", [PY, "-c", "import sys; print('hello'); sys.stderr.write('err-line\\n'); sys.exit(0)"], 30, {
    "outcome_exited": lambda r, o, e: r["outcome"] == "exited",
    "os_exit_0": lambda r, o, e: r["os_exit_code"] == 0,
    "stdout_kept": lambda r, o, e: o.strip() == b"hello",
    "stderr_kept": lambda r, o, e: e.strip() == b"err-line"})

case("exit7", [PY, "-c", "import sys; print('{\"exit_code\": 0, \"text\": \"PASS\"}'); sys.exit(7)"], 30, {
    "outcome_exited": lambda r, o, e: r["outcome"] == "exited",
    "os_exit_7_not_text": lambda r, o, e: r["os_exit_code"] == 7 and b'"exit_code": 0' in o})

pidfile = os.path.join(out, "grandchild.pid")
child = ("import subprocess, sys, time; "
         f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
         f"open({pidfile!r}, 'w').write(str(g.pid)); print('started', flush=True); time.sleep(120)")
t0 = time.monotonic()
rec, d = case("timeout_tree", [PY, "-c", child], 3, {
    "outcome_timeout": lambda r, o, e: r["outcome"] == "timeout_killed",
    "bounded_elapsed": lambda r, o, e: r["elapsed_s"] < 3 + 30,
    "exit_code_recorded": lambda r, o, e: r["os_exit_code"] is not None,
    "stdout_before_kill_kept": lambda r, o, e: b"started" in o})
time.sleep(1)
gpid = int(open(pidfile).read())
gone = {"child_gone": not rb._alive(rec["pid"]), "grandchild_gone": not rb._alive(gpid)}
ok &= all(gone.values())
print(json.dumps({"case": "timeout_tree_cleanup", "pass": all(gone.values()), "checks": gone}))
print(json.dumps({"all_pass": bool(ok), "platform": sys.platform, "python": sys.version.split()[0]}))
sys.exit(0 if ok else 1)
