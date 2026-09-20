#!/usr/bin/env python3
"""V1 (Option C) acceptance tests: enforced boundary, concurrency, path/metadata safety, store-scan.
Run: python tests/test_v1.py"""
import subprocess, sys, os, json, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "skill", "knokeep_state.py")
store = tempfile.mkdtemp(prefix="knokeep_test_")
proj = "demo"
results = []

def run(*args, project=proj):
    p = subprocess.run([sys.executable, STATE, *args, "--store", store, "--project", project],
                       capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()

def check(name, cond):
    results.append((name, cond)); print(("PASS " if cond else "FAIL ") + name)

def bf(text):
    f = os.path.join(store, "_body.md"); open(f, "w", encoding="utf-8").write(text); return f

rc, out, err = run("init"); check("init ok", rc == 0 and json.loads(out)["ok"])
rc, out, err = run("bootstrap"); h0 = json.loads(out)["version_hash"] if rc == 0 else None
check("bootstrap clean store", rc == 0 and h0)
# update requires expect-hash
rc, out, err = run("flush-state", "--body-file", bf("## Architecture\nPython 3.11 CLI"))
check("update without expect-hash blocked", rc != 0 and "expect_hash required" in err)
# correct hash accepted
rc, out, err = run("flush-state", "--body-file", bf("## Architecture\nPython 3.11 CLI v2"), "--expect-hash", h0)
h1 = json.loads(out)["version_hash"] if rc == 0 else None
check("hash-matched update ok", rc == 0 and json.loads(out)["ok"] and json.loads(out)["revision"] == 2)
# stale rejected
rc, out, err = run("flush-state", "--body-file", bf("## Architecture\nx"), "--expect-hash", "deadbeef0000")
check("stale update blocked", rc != 0 and "stale" in err)
# secret in body blocked (full-content scan)
rc, out, err = run("flush-state", "--body-file", bf("## Architecture\nkey sk-ant-EXAMPLE0000000000000000000000000000"), "--expect-hash", h1)
check("secret in state blocked", rc != 0 and "blocked" in err)
# flush-log requires expect-hash (lost-update guard like state) + secret-in-log blocked
_, out, _ = run("bootstrap"); lh0 = json.loads(out)["log_hash"]
rc, out, err = run("flush-log", "--body-file", bf("## Active State\nwiring\n\n## Next Step\nbuild SKILL.md"), "--expect-hash", lh0)
check("flush-log ok", rc == 0 and json.loads(out)["ok"]); lh1 = json.loads(out).get("version_hash") if rc == 0 else None
rc, out, err = run("flush-log", "--body-file", bf("## Next Step\ntoken ghs_EXAMPLE0000000000000000000000"), "--expect-hash", lh1)
check("secret in log blocked", rc != 0 and "blocked" in err)
# session-append clean + secret blocked
rc, out, err = run("session-append", "--session-id", "20260919-1200-aaaa", "--entry", "started helper")
check("session-append ok", rc == 0 and json.loads(out)["ok"])
rc, out, err = run("session-append", "--session-id", "20260919-1200-aaaa", "--entry", "AKIAEXAMPLE000000000")
check("session-append secret blocked", rc != 0 and "blocked" in err)
# path traversal + bad ids
rc, out, err = run("bootstrap", project="..")
check("path traversal blocked", rc != 0 and ("traversal" in err or "invalid project" in err))
rc, out, err = run("session-append", "--session-id", "../evil", "--entry", "x")
check("bad session id blocked", rc != 0 and "invalid session id" in err)
# missing session-id -> clean block, NOT a TypeError crash (regression: caught in dogfood soak)
rc, out, err = run("session-append", "--entry", "no id given")
check("missing session id clean-blocked (no crash)", rc != 0 and "invalid session id" in err and "internal error" not in err)
# missing body-file is an error (not silent empty overwrite)
rc, out, err = run("flush-log", "--body-file", os.path.join(store, "nope.md"))
check("missing body-file blocked", rc != 0 and "missing --body-file" in err)
# bootstrap resume line
rc, out, err = run("bootstrap"); d = json.loads(out) if rc == 0 else {}
check("bootstrap resume line", rc == 0 and "build SKILL.md" in d.get("next", "") and d.get("version_hash"))
# store-scan catches a helper-bypass write
open(os.path.join(store, proj, "leak.md"), "w", encoding="utf-8").write("ghp_EXAMPLE0000000000000000000000")
rc, out, err = run("bootstrap")
check("bootstrap detects bypass secret", rc != 0 and "store contains secrets" in err)
# reserved name + non-text file in store
rc, out, err = run("bootstrap", project="nul")
check("reserved name blocked", rc != 0 and "invalid project id" in err)
rc, out, err = run("bootstrap", project="CON.txt")
check("reserved stem+ext blocked", rc != 0 and "invalid project id" in err)
run("init", project="demo2")
open(os.path.join(store, "demo2", "notes.pdf"), "w", encoding="utf-8").write("hello world")
rc, out, err = run("bootstrap", project="demo2")
check("non-text file in store blocked", rc != 0 and "non-text" in err)
# benign OS junk (.DS_Store) must NOT block resume
run("init", project="demo3")
open(os.path.join(store, "demo3", ".DS_Store"), "wb").write(b"\x00\x01junk")
rc, out, err = run("bootstrap", project="demo3")
check("DS_Store ignored (resume ok)", rc == 0)

shutil.rmtree(store, ignore_errors=True)
passed = sum(1 for _, c in results if c)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
