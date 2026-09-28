#!/usr/bin/env python3
"""Fake Hermes for driver tests: mimics the stream-json envelope and the profile agent.log.
No model, no network. FAKE_MODE: ok | exit7 | hang | wrong_session | rotate_log.
FAKE_CALLS: API-call log lines to write this turn. Writes the names of sensitive env vars it
inherited to <HERMES_HOME>/fake-env-seen.json (names only)."""
import json, os, sys, time, uuid
a = sys.argv[1:]
home = os.environ.get("HERMES_HOME", ".")
seen = sorted(k for k in os.environ if k.upper().startswith(("HERMES_", "OPENAI_", "OPENROUTER_", "ANTHROPIC_"))
              or any(p in k.upper() for p in ("API_KEY", "BASE_URL", "TOKEN", "SECRET")))
open(os.path.join(home, "fake-env-seen.json"), "w").write(json.dumps(seen))
if a[:1] == ["--version"]:
    print("Fake Hermes 0.0 (test double)"); sys.exit(0)
mode = os.environ.get("FAKE_MODE", "ok")
sid = a[a.index("--resume") + 1] if "--resume" in a else "fake_" + uuid.uuid4().hex[:8]
if mode == "wrong_session":
    sid = "fake_other"
logs = os.path.join(home, "logs"); os.makedirs(logs, exist_ok=True)
lp = os.path.join(logs, "agent.log")
if mode == "rotate_log" and os.path.exists(lp):
    open(lp, "w").write("rotated\n")
with open(lp, "a") as f:
    for i in range(int(os.environ.get("FAKE_CALLS", "1"))):
        f.write(f"2026 INFO [{sid}] agent.conversation_loop: API call #{i+1}: model=GLM-fake provider=custom in=1\n")
if mode == "hang":
    print('{"type": "system", "subtype": "init", "model": "fake", "session_id": "%s"}' % sid, flush=True)
    time.sleep(60)
print('{"type": "system", "subtype": "init", "model": "fake", "session_id": "%s"}' % sid)
code = 7 if mode == "exit7" else 0
print('{"type": "result", "session_id": "%s", "exit_code": 0, "text": "PASS", "tokens": {"input": 1}}' % sid)
sys.stderr.write("\nsession_id: %s\n" % sid)
sys.exit(code)
