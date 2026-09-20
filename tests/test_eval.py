#!/usr/bin/env python3
"""Eval-harness tests: telemetry logs decisions, scorecard aggregates, and the
event log NEVER contains a secret value (labels only)."""
import subprocess, sys, os, json, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "skill", "knokeep_state.py")
store = tempfile.mkdtemp(prefix="knokeep_eval_")
proj = "demo"
results = []
SECRET = "sk-ant-EVALSECRET000000000000000000000000"

def run(*args, project=proj):
    p = subprocess.run([sys.executable, STATE, *args, "--store", store, "--project", project],
                       capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()

def check(name, cond):
    results.append((name, cond)); print(("PASS " if cond else "FAIL ") + name)

def bf(text):
    f = os.path.join(store, "_body.md"); open(f, "w", encoding="utf-8").write(text); return f

run("init")
_, out, _ = run("bootstrap"); h0 = json.loads(out)["version_hash"]
run("flush-state", "--body-file", bf("## Architecture\nstale"), "--expect-hash", "deadbeef0000")  # block: stale
run("flush-state", "--body-file", bf("## Architecture\nkey " + SECRET), "--expect-hash", h0)  # block: secret

events = os.path.join(store, ".knokeep-eval", "events.jsonl")
check("events log created", os.path.exists(events))

raw = open(events, encoding="utf-8").read() if os.path.exists(events) else ""
check("telemetry contains NO secret value", SECRET not in raw)

rc, out, err = run("eval")
sc = json.loads(out) if rc == 0 else {}
check("eval returns scorecard", rc == 0 and "events" in sc)
check("writes_allowed counted (init)", sc.get("writes_allowed", 0) >= 1)
check("blocks counted", sc.get("blocks_total", 0) >= 2)
check("secret block classified", sc.get("secret_blocks", 0) >= 1)
check("concurrency block classified", sc.get("concurrency_blocks", 0) >= 1)
check("clean resume counted", sc.get("clean_resumes", 0) >= 1)

# eval must not require --project
p = subprocess.run([sys.executable, STATE, "eval", "--store", store], capture_output=True, text=True)
check("eval works without --project", p.returncode == 0 and "events" in json.loads(p.stdout))

# health: one-shot verdict + scorecard (gitleaks optional; verdict must be present)
p = subprocess.run([sys.executable, STATE, "health", "--store", store], capture_output=True, text=True)
hj = json.loads(p.stdout)
check("health returns verdict+scorecard", hj.get("verdict") in ("healthy", "attention") and "scorecard" in hj and "projects" in hj)

shutil.rmtree(store, ignore_errors=True)
passed = sum(1 for _, c in results if c)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
