#!/usr/bin/env python3
"""Bounded supervisor for Q-series client runs (stdlib only, Windows + POSIX).

Runs ONE child command with a hard wall-clock limit and records, independently
of anything the child prints:
  - os_exit_code: the operating-system return code (None only if never started)
  - outcome: "exited" | "timeout_killed" | "launch_error"
  - elapsed_s, start/end UTC, and whether the whole process tree was cleaned up
stdout/stderr go byte-for-byte to files; model text is never parsed for status.
No retries: one launch per invocation.

usage: run_bounded.py --timeout SECONDS --out DIR [--cwd DIR] -- CMD [ARGS...]
Exit status of this script: 0 if the record was written (regardless of child
outcome), 2 on usage error. Read the JSON record for the child's result.
"""
import argparse, datetime, json, os, signal, subprocess, sys, time

WIN = os.name == "nt"


def _utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _alive(pid):
    if WIN:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        with open(f"/proc/{pid}/stat") as f:          # Linux: a killed-but-unreaped zombie is not alive
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_tree(proc):
    """Kill the child and every descendant. Returns a short description."""
    if WIN:
        r = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, text=True)
        return f"taskkill /T /F rc={r.returncode}"
    try:
        os.killpg(proc.pid, signal.SIGKILL)       # child was started in its own session/group
        return "killpg SIGKILL"
    except ProcessLookupError:
        return "group already gone"


def run(cmd, timeout, out, cwd=None):
    os.makedirs(out, exist_ok=True)
    so, se = os.path.join(out, "stdout.bin"), os.path.join(out, "stderr.bin")
    rec = {"command": cmd, "timeout_s": timeout, "start_utc": _utc(), "os_exit_code": None,
           "outcome": None, "tree_kill": None, "platform": sys.platform}
    t0 = time.monotonic()
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WIN else {"start_new_session": True}
    with open(so, "wb") as fo, open(se, "wb") as fe:
        try:
            p = subprocess.Popen(cmd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, cwd=cwd, **kw)
        except OSError as e:
            rec.update(outcome="launch_error", error=f"{type(e).__name__}: {e}")
            p = None
        if p is not None:
            rec["pid"] = p.pid
            try:
                rec["os_exit_code"] = p.wait(timeout=timeout)
                rec["outcome"] = "exited"
            except subprocess.TimeoutExpired:
                rec["outcome"] = "timeout_killed"
                rec["tree_kill"] = _kill_tree(p)
                try:
                    rec["os_exit_code"] = p.wait(timeout=30)   # the killed child's real OS code
                except subprocess.TimeoutExpired:
                    rec["os_exit_code"] = None
                    rec["error"] = "child did not exit 30 s after tree kill"
    rec["elapsed_s"] = round(time.monotonic() - t0, 3)
    rec["end_utc"] = _utc()
    rec["stdout_bytes"], rec["stderr_bytes"] = os.path.getsize(so), os.path.getsize(se)
    with open(os.path.join(out, "run.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--timeout", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cwd")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd or a.timeout <= 0:
        ap.error("need a positive --timeout and a command after --")
    print(json.dumps(run(cmd, a.timeout, a.out, a.cwd)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
