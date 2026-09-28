#!/usr/bin/env python3
"""Hermes Q1 multi-turn driver: ONE bounded turn per invocation (stdlib only).

Turn 1 starts a new Hermes session from an enrollment+first-task prompt file.
Turn N>1 resumes it with `--resume <session_id>` (installed Hermes contract:
the stored transcript is loaded and continued; an unknown id is an error, never
a silent fresh session). The driver adds NOTHING to prompts: no reminders, no
generated save bodies, no rubric. It only launches, records and checks.

Each turn is run by the approved supervisor (tools/q0/run_bounded.py, imported
by explicit path) and recorded in <evidence>/turn-NN/ (created fresh; an
existing turn directory is refused, nothing is overwritten):
  stdout.bin / stderr.bin / run.json   supervisor (real OS exit, timeout class)
  turn.json                            driver record (exclusive create)
and one line is appended to <evidence>/turns.jsonl.

turn.json: prompt sha256/bytes, exact argv (secrets never included), supervisor
record, Hermes-reported fields (init model/session, result exit_code/session/
tokens/error), session continuity, route from the isolated profile's agent.log
(model/provider per API call, fallback lines), preflight + provenance hashes.
Parse failures are recorded, never fatal: partial data is kept.

Usage (no inference):  --check-only   (version, profile, endpoint /v1/models)
"""
import argparse, hashlib, importlib.util, json, os, re, subprocess, sys, urllib.request

DRIVER_VERSION = "q1-hermes-driver/1"
SCRUB_ENV = ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "NOUS_API_KEY",
             "HERMES_PROFILE", "HERMES_INFERENCE_MODEL", "HERMES_INFERENCE_PROVIDER")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()


def load_supervisor(path):
    spec = importlib.util.spec_from_file_location("run_bounded", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def exe_argv(exe):
    return [sys.executable, exe] if exe.lower().endswith(".py") else [exe]


def preflight(a):
    """Read-only isolation checks on the fresh profile. Returns (ok, facts)."""
    f = {"home_exists": os.path.isdir(a.home)}
    cfg = os.path.join(a.home, "config.yaml")
    f["config_sha256"] = sha(cfg) if os.path.isfile(cfg) else None
    text = open(cfg, encoding="utf-8").read() if os.path.isfile(cfg) else ""
    f["fallback_providers_empty"] = bool(re.search(r"(?m)^fallback_providers:\s*\[\]\s*$", text))
    f["memory_disabled"] = bool(re.search(r"(?m)^\s+memory_enabled:\s*false\s*$", text))
    f["user_profile_disabled"] = bool(re.search(r"(?m)^\s+user_profile_enabled:\s*false\s*$", text))
    f["no_profile_env_file"] = not os.path.exists(os.path.join(a.home, ".env"))
    f["no_profile_auth_file"] = not os.path.exists(os.path.join(a.home, "auth.json"))
    f["key_file_present"] = bool(a.key_file) and os.path.isfile(a.key_file)
    ok = all(v for k, v in f.items() if k != "config_sha256") and f["config_sha256"] is not None
    return ok, f


def parse_stream(stdout_bytes, stderr_bytes):
    rep = {"init": None, "result": None, "stderr_session_id": None, "parse_errors": 0, "events": 0}
    for line in stdout_bytes.decode("utf-8", "replace").splitlines():
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            ev = json.loads(s)
        except ValueError:
            rep["parse_errors"] += 1
            continue
        rep["events"] += 1
        if ev.get("type") == "system" and ev.get("subtype") == "init" and rep["init"] is None:
            rep["init"] = {"model": ev.get("model"), "session_id": ev.get("session_id")}
        elif ev.get("type") == "result":
            rep["result"] = {k: ev.get(k) for k in ("session_id", "exit_code", "tokens", "duration_ms", "error")}
    m = re.findall(r"(?m)^session_id:\s*(\S+)\s*$", stderr_bytes.decode("utf-8", "replace"))
    rep["stderr_session_id"] = m[-1] if m else None
    return rep


def route_from_log(home, session_id):
    """Model/provider per API call and fallback mentions for this session from the isolated profile log."""
    out = {"api_calls": 0, "routes": {}, "fallback_lines": 0, "log_found": False}
    log = os.path.join(home, "logs", "agent.log")
    if not session_id or not os.path.isfile(log):
        return out
    out["log_found"] = True
    tag = f"[{session_id}]"
    with open(log, encoding="utf-8", errors="replace") as f:
        for line in f:
            if tag not in line:
                continue
            if "API call #" in line:
                out["api_calls"] += 1
                m = re.search(r"model=(\S+) provider=(\S+)", line)
                k = f"{m.group(1)}|{m.group(2)}" if m else "unparsed"
                out["routes"][k] = out["routes"].get(k, 0) + 1
            if re.search(r"(?i)fallback", line):
                out["fallback_lines"] += 1
    return out


def check_only(a):
    ok, facts = preflight(a)
    v = subprocess.run(exe_argv(a.hermes_exe) + ["--version"], capture_output=True, text=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    facts["hermes_version"] = (v.stdout.strip().splitlines() or [""])[0]
    facts["endpoint"] = None
    if a.endpoint and facts["key_file_present"]:
        key = open(a.key_file, encoding="utf-8").read().strip()
        req = urllib.request.Request(a.endpoint.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                ids = [m.get("id") for m in json.load(r).get("data", [])]
            facts["endpoint"] = {"http": 200, "model_listed": a.model in ids}   # listing != loadable
        except Exception as e:
            facts["endpoint"] = {"error": type(e).__name__}
    print(json.dumps({"check_only": True, "preflight_ok": ok, "facts": facts}, indent=2))
    return 0 if ok else 4


def main(argv=None):
    ap = argparse.ArgumentParser(description="Hermes Q1 driver: one bounded turn")
    ap.add_argument("--hermes-exe", required=True)
    ap.add_argument("--home", required=True, help="fresh isolated HERMES_HOME")
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--evidence-dir", required=True)
    ap.add_argument("--supervisor", required=True, help="path to approved tools/q0/run_bounded.py")
    ap.add_argument("--prompt-file")
    ap.add_argument("--turn", type=int)
    ap.add_argument("--session-id", help="required for --turn > 1, forbidden for turn 1")
    ap.add_argument("--model", default="GLM-4.7-Flash-Q4_K_M")
    ap.add_argument("--provider", default="custom")
    ap.add_argument("--toolsets", default="terminal,file")
    ap.add_argument("--max-turns", type=int, default=30)
    ap.add_argument("--run-budget", type=int, default=600)
    ap.add_argument("--timeout", type=float, default=630, help="supervisor hard wall-clock limit")
    ap.add_argument("--key-file", help="local endpoint key file; value goes only into the child env")
    ap.add_argument("--key-env", default="HERMES_A3_LOCAL_KEY")
    ap.add_argument("--endpoint", help="for --check-only: base URL ending in /v1")
    ap.add_argument("--ignore-rules", action="store_true", help="pass --ignore-rules (record in packet)")
    ap.add_argument("--check-only", action="store_true")
    a = ap.parse_args(argv)
    if a.check_only:
        return check_only(a)
    if not a.prompt_file or not a.turn or a.turn < 1:
        ap.error("--prompt-file and --turn >= 1 are required")
    if a.turn == 1 and a.session_id:
        ap.error("turn 1 starts a new session; --session-id is not allowed")
    if a.turn > 1 and not a.session_id:
        ap.error("turn > 1 must resume an explicit --session-id")

    turn_dir = os.path.join(a.evidence_dir, f"turn-{a.turn:02d}")
    if os.path.exists(turn_dir):
        print(json.dumps({"refused": "turn evidence exists", "turn": a.turn, "launched": False}))
        return 3
    ok, facts = preflight(a)
    if not ok:
        print(json.dumps({"refused": "preflight failed", "facts": facts, "launched": False}))
        return 4
    rb = load_supervisor(a.supervisor)

    cmd = exe_argv(a.hermes_exe) + ["chat", "--query-file", os.path.abspath(a.prompt_file), "--oneshot",
                                    "--format", "stream-json", "--provider", a.provider, "-m", a.model,
                                    "-t", a.toolsets, "--max-turns", str(a.max_turns),
                                    "--run-budget", str(a.run_budget), "--in", os.path.abspath(a.project_dir)]
    if a.turn > 1:
        cmd += ["--resume", a.session_id]
    if a.ignore_rules:
        cmd += ["--ignore-rules"]

    for k in SCRUB_ENV:                                   # child inherits this process env
        os.environ.pop(k, None)
    os.environ["HERMES_HOME"] = os.path.abspath(a.home)
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    key = None
    if a.key_file:
        key = open(a.key_file, encoding="utf-8").read().strip()
        os.environ[a.key_env] = key

    os.makedirs(a.evidence_dir, exist_ok=True)
    rec = rb.run(cmd, a.timeout, turn_dir)                # refuses an existing dir; real OS exit
    so = open(os.path.join(turn_dir, "stdout.bin"), "rb").read()
    se = open(os.path.join(turn_dir, "stderr.bin"), "rb").read()
    rep = parse_stream(so, se)
    sid = (rep["result"] or {}).get("session_id") or (rep["init"] or {}).get("session_id") or rep["stderr_session_id"]
    ids = {x for x in ((rep["init"] or {}).get("session_id"), (rep["result"] or {}).get("session_id"),
                       rep["stderr_session_id"]) if x}
    continuity = {"requested": a.session_id, "reported": sid, "consistent_ids": len(ids) <= 1,
                  "resumed_same_session": (sid == a.session_id) if a.turn > 1 else None}
    turn = {"driver": DRIVER_VERSION, "turn": a.turn,
            "prompt": {"sha256": sha(a.prompt_file), "bytes": os.path.getsize(a.prompt_file)},
            "argv": cmd, "supervisor": rec, "reported": rep, "session_id": sid, "continuity": continuity,
            "route": route_from_log(a.home, sid), "preflight": facts,
            "provenance": {"driver_sha256": sha(os.path.abspath(__file__)), "supervisor_sha256": sha(a.supervisor),
                           "ignore_rules": a.ignore_rules},
            "secret_in_output": bool(key) and (key.encode() in so or key.encode() in se)}
    turn["verdict_inputs"] = {"os_exit_code": rec.get("os_exit_code"), "outcome": rec.get("outcome"),
                              "reported_exit_code": (rep["result"] or {}).get("exit_code"),
                              "exit_codes_agree": rec.get("os_exit_code") == (rep["result"] or {}).get("exit_code")}
    with open(os.path.join(turn_dir, "turn.json"), "x", encoding="utf-8") as f:
        json.dump(turn, f, indent=2)
    with open(os.path.join(a.evidence_dir, "turns.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"turn": a.turn, "session_id": sid, "outcome": rec.get("outcome"),
                            "os_exit_code": rec.get("os_exit_code"), "resumed_same_session":
                            continuity["resumed_same_session"]}) + "\n")
    print(json.dumps(turn["verdict_inputs"] | {"turn": a.turn, "session_id": sid, "continuity": continuity}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
