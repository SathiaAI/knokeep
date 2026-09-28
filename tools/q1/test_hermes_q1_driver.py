#!/usr/bin/env python3
"""Harmless driver tests with fake_hermes.py (no model, network or credentials)."""
import json, os, shutil, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
PY, DRV = sys.executable, os.path.join(HERE, "hermes_q1_driver.py")
SUP = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "run_bounded.py")
T = tempfile.mkdtemp(prefix="q1drv-")
home, proj, ev = os.path.join(T, "home"), os.path.join(T, "proj"), os.path.join(T, "evidence")
for d in (home, proj): os.makedirs(d)
open(os.path.join(home, "config.yaml"), "w").write("model:\n  default: x\nfallback_providers: []\nmemory:\n  memory_enabled: false\n  user_profile_enabled: false\n")
keyf = os.path.join(T, "key"); open(keyf, "w").write("TESTKEY-not-real-123")
p1 = os.path.join(T, "p1.txt"); open(p1, "w").write("enroll + task 1")
p2 = os.path.join(T, "p2.txt"); open(p2, "w").write("task 2")
fake = os.path.join(HERE, "fake_hermes.py")
ok = True

def drv(*extra, mode="ok", timeout="30", home_dir=home):
    env = dict(os.environ, FAKE_MODE=mode)
    cp = subprocess.run([PY, DRV, "--hermes-exe", fake, "--home", home_dir, "--project-dir", proj, "--evidence-dir", ev,
                         "--supervisor", SUP, "--key-file", keyf, "--timeout", timeout, *extra], capture_output=True, text=True, env=env)
    return cp

def check(name, cond, cp=None):
    global ok
    ok &= bool(cond)
    print(json.dumps({"case": name, "pass": bool(cond), "rc": cp.returncode if cp else None}))

def turn(n): return json.load(open(os.path.join(ev, f"turn-{n:02d}", "turn.json")))

cp = drv("--prompt-file", p1, "--turn", "1")
t1 = turn(1); sid = t1["session_id"]
check("turn1_new_session", cp.returncode == 0 and sid and t1["supervisor"]["os_exit_code"] == 0 and "--resume" not in t1["argv"]
      and t1["continuity"]["consistent_ids"] and t1["prompt"]["bytes"] == len("enroll + task 1"), cp)
cp = drv("--prompt-file", p2, "--turn", "2", "--session-id", sid)
t2 = turn(2)
check("turn2_resumes_same_session", cp.returncode == 0 and t2["continuity"]["resumed_same_session"] is True and t2["argv"][-2:] == ["--resume", sid], cp)
before = open(os.path.join(ev, "turn-01", "turn.json"), "rb").read()
cp = drv("--prompt-file", p1, "--turn", "1")
check("existing_turn_refused_not_overwritten", cp.returncode == 3 and open(os.path.join(ev, "turn-01", "turn.json"), "rb").read() == before, cp)
cp = drv("--prompt-file", p2, "--turn", "3")
check("resume_without_session_id_rejected", cp.returncode == 2 and not os.path.exists(os.path.join(ev, "turn-03")), cp)
cp = drv("--prompt-file", p1, "--turn", "1", "--session-id", "x")
check("turn1_with_session_id_rejected", cp.returncode == 2, cp)
cp = drv("--prompt-file", p2, "--turn", "3", "--session-id", sid, mode="wrong_session")
check("session_break_detected", cp.returncode == 0 and turn(3)["continuity"]["resumed_same_session"] is False, cp)
cp = drv("--prompt-file", p2, "--turn", "4", "--session-id", sid, mode="exit7")
v = turn(4)["verdict_inputs"]
check("real_exit_7_vs_reported_0", v["os_exit_code"] == 7 and v["reported_exit_code"] == 0 and v["exit_codes_agree"] is False, cp)
cp = drv("--prompt-file", p2, "--turn", "5", "--session-id", sid, mode="hang", timeout="3")
v = turn(5)["verdict_inputs"]
check("timeout_classified", v["outcome"] == "timeout_killed" and turn(5)["reported"]["init"] is not None, cp)
bad = os.path.join(T, "badhome"); os.makedirs(bad)
open(os.path.join(bad, "config.yaml"), "w").write("fallback_providers:\n  - provider: openrouter\n")
cp = drv("--prompt-file", p2, "--turn", "6", "--session-id", sid, home_dir=bad)
check("paid_fallback_profile_refused", cp.returncode == 4 and not os.path.exists(os.path.join(ev, "turn-06")), cp)
check("no_secret_in_outputs_or_records", all(not turn(n)["secret_in_output"] and "TESTKEY" not in json.dumps(turn(n)) for n in (1, 2, 3, 4, 5)))
lines = open(os.path.join(ev, "turns.jsonl")).read().splitlines()
check("ledger_append_only_5", len(lines) == 5)
cp = drv("--check-only")
check("check_only_no_launch", cp.returncode == 0 and '"preflight_ok": true' in cp.stdout, cp)
print(json.dumps({"all_pass": bool(ok), "platform": sys.platform}))
shutil.rmtree(T, ignore_errors=True)
sys.exit(0 if ok else 1)
