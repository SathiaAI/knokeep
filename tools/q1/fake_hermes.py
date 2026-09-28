#!/usr/bin/env python3
"""Fake Hermes for driver tests: mimics the stream-json envelope. No model, no network.
Behaviour from env FAKE_MODE: ok (default) | exit7 | hang | wrong_session."""
import os, sys, time, uuid
a = sys.argv[1:]
if a[:1] == ["--version"]:
    print("Fake Hermes 0.0 (test double)"); sys.exit(0)
mode = os.environ.get("FAKE_MODE", "ok")
sid = a[a.index("--resume") + 1] if "--resume" in a else "fake_" + uuid.uuid4().hex[:8]
if mode == "wrong_session":
    sid = "fake_other"
if mode == "hang":
    print('{"type": "system", "subtype": "init", "model": "fake", "session_id": "%s"}' % sid, flush=True)
    time.sleep(60)
print('{"type": "system", "subtype": "init", "model": "fake", "session_id": "%s"}' % sid)
print('{"type": "text", "text": "hello"}')
code = 7 if mode == "exit7" else 0
print('{"type": "result", "session_id": "%s", "exit_code": %d, "text": "PASS", "tokens": {"input": 1}}' % (sid, 0))
sys.stderr.write("\nsession_id: %s\n" % sid)
sys.exit(code)
