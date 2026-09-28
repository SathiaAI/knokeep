#!/usr/bin/env python3
"""Harmless local smoke checks for run_bounded.py. No network, model or credentials.

1. exit0            child exits 0 -> exited / 0, stdout+stderr preserved
2. exit7            child PRINTS "exit_code": 0 but exits 7 -> exited / 7
3. timeout_tree     child + grandchild, 3 s limit -> timeout_killed, cleanup confirmed, both gone
4. existing_output  output dir already exists -> refused, CLI exit 3, prior evidence byte-identical, child never ran
5. cleanup_fail     simulated failing kill command -> timeout_cleanup_failed, exit code null
6. cleanup_hang     simulated hanging kill command -> failed within the kill bound
7. inspection_fail_closed  simulated denied / erroring process inspection must never read as "dead"
Precondition: the script's own PID must inspect as alive, else it aborts (rc 2)
without claiming anything, because liveness checks would be meaningless.
Children in 5/6 are left running by design and are then force-killed by this
script (finally block) and verified gone. Exits 0 only if every check passes.
"""
import json, os, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_bounded as rb

PY = sys.executable
out = tempfile.mkdtemp(prefix="rb-smoke-")
ok = True

try:
    _self_ok = rb._alive(os.getpid()) is True
except rb.InspectionError as e:
    _self_ok = False
if not _self_ok:
    print(json.dumps({"case": "self_pid_sanity", "pass": False, "detail": "process inspection unavailable; no liveness claims made"}))
    sys.exit(2)
print(json.dumps({"case": "self_pid_sanity", "pass": True}), flush=True)


def gone(pid):
    """Verified gone only when the OS positively says so; inspection errors count as NOT verified."""
    try:
        return not rb._alive(pid)
    except rb.InspectionError:
        return False


def force_cleanup(pid):
    class _P: pass
    _P.pid = pid
    try:
        rb._kill_tree(_P)
    finally:
        for _ in range(20):
            if gone(pid):
                return True
            time.sleep(0.25)
    return gone(pid)


def report(name, rec, checks):
    global ok
    passed = all(checks.values())
    ok &= passed
    line = {"case": name, "pass": passed, "checks": checks}
    if rec:
        line.update(outcome=rec.get("outcome"), os_exit_code=rec.get("os_exit_code"),
                    elapsed_s=rec.get("elapsed_s"), cleanup=rec.get("cleanup"))
    print(json.dumps(line), flush=True)


def files(d):
    return open(os.path.join(d, "stdout.bin"), "rb").read(), open(os.path.join(d, "stderr.bin"), "rb").read()


# 1
d = os.path.join(out, "exit0")
r = rb.run([PY, "-c", "import sys; print('hello'); sys.stderr.write('err-line\\n'); sys.exit(0)"], 30, d)
o, e = files(d)
report("exit0", r, {"exited": r["outcome"] == "exited", "os_exit_0": r["os_exit_code"] == 0,
                    "stdout_kept": o.strip() == b"hello", "stderr_kept": e.strip() == b"err-line"})
# 2
d = os.path.join(out, "exit7")
r = rb.run([PY, "-c", "import sys; print('{\"exit_code\": 0, \"text\": \"PASS\"}'); sys.exit(7)"], 30, d)
o, _ = files(d)
report("exit7", r, {"exited": r["outcome"] == "exited", "os_exit_7_not_text": r["os_exit_code"] == 7 and b'"exit_code": 0' in o})
# 3
pidfile = os.path.join(out, "grandchild.pid")
child = ("import subprocess, sys, time; "
         "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
         f"open({pidfile!r}, 'w').write(str(g.pid)); print('started', flush=True); time.sleep(60)")
d = os.path.join(out, "timeout_tree")
r = rb.run([PY, "-c", child], 3, d)
time.sleep(1)
gpid = int(open(pidfile).read())
o, _ = files(d)
report("timeout_tree", r, {"timeout_killed": r["outcome"] == "timeout_killed",
                           "cleanup_confirmed": (r["cleanup"] or {}).get("status") == "confirmed",
                           "exit_code_recorded": r["os_exit_code"] is not None,
                           "bounded": r["elapsed_s"] < 3 + rb.KILL_TIMEOUT_S + rb.REAP_TIMEOUT_S,
                           "stdout_before_kill_kept": b"started" in o,
                           "child_gone": gone(r["pid"]), "grandchild_gone": gone(gpid)})
# 4
d = os.path.join(out, "existing_output")
os.makedirs(d)
prior = os.path.join(d, "stdout.bin")
open(prior, "wb").write(b"EARLIER EVIDENCE\n")
marker = os.path.join(out, "should_not_exist.txt")
cp = subprocess.run([PY, rb.__file__, "--timeout", "10", "--out", d, "--", PY, "-c",
                     f"open({marker!r}, 'w').write('ran')"], capture_output=True, text=True)
report("existing_output", None, {"cli_exit_3": cp.returncode == 3,
                                 "refused": '"refused_existing_output"' in cp.stdout,
                                 "evidence_identical": open(prior, "rb").read() == b"EARLIER EVIDENCE\n",
                                 "no_run_json": not os.path.exists(os.path.join(d, "run.json")),
                                 "child_not_launched": not os.path.exists(marker)})
# 5 and 6
for name, kill_cmd, kt in (("cleanup_fail", [PY, "-c", "import sys; sys.exit(1)"], 5),
                           ("cleanup_hang", [PY, "-c", "import time; time.sleep(30)"], 1)):
    d = os.path.join(out, name)
    r = None
    try:
        r = rb.run([PY, "-c", "import time; time.sleep(60)"], 2, d, kill_timeout=kt, reap_timeout=2, _kill_cmd=kill_cmd)
        c = r["cleanup"] or {}
        checks = {"not_claimed_killed": r["outcome"] == "timeout_cleanup_failed",
                  "cleanup_failed": c.get("status") == "failed" and c.get("ok") is False,
                  "exit_code_null": r["os_exit_code"] is None,
                  "bounded": r["elapsed_s"] < 2 + kt + 2 + 3}
        if name == "cleanup_hang":
            checks["kill_timeout_reported"] = "exceeded" in (c.get("detail") or "")
    finally:
        if r and r.get("pid"):                           # guaranteed cleanup of the deliberately surviving child
            checks["test_child_cleaned_up"] = force_cleanup(r["pid"])
    report(name, r, checks)

# 7: fail-closed inspection (the #37 review regression)
live = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                        **({"creationflags": rb._NOWIN} if rb.WIN else {}))
real_probe = rb._raw_probe
checks = {}
try:
    rb._raw_probe = lambda pid: "denied"
    checks["denied_reads_alive"] = rb._alive(live.pid) is True
    def _boom(pid):
        raise rb.InspectionError("simulated: Access denied")
    rb._raw_probe = _boom
    try:
        rb._alive(live.pid)
        checks["error_raises"] = False
    except rb.InspectionError:
        checks["error_raises"] = True
    checks["error_not_reported_cleaned"] = gone(live.pid) is False
finally:
    rb._raw_probe = real_probe
    live.kill()
    live.wait(timeout=10)
    checks["real_child_really_gone"] = gone(live.pid)
report("inspection_fail_closed", None, checks)

print(json.dumps({"all_pass": bool(ok), "platform": sys.platform, "python": sys.version.split()[0]}))
sys.exit(0 if ok else 1)
