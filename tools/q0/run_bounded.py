#!/usr/bin/env python3
"""Bounded supervisor for Q-series client runs (stdlib only, Windows + POSIX).

Runs ONE child command with a hard wall-clock limit and records, independently
of anything the child prints:
  - os_exit_code: the child's real OS return code, or null if it never started
    or could not be reaped after cleanup
  - outcome: "exited" | "timeout_killed" (termination confirmed) |
    "timeout_cleanup_failed" | "timeout_cleanup_unknown" | "launch_error"
  - cleanup: what the tree kill did and whether termination was confirmed
stdout/stderr go byte-for-byte to files; model text is never parsed for status.
One launch per invocation, no retries. The output directory must NOT exist:
an existing directory is refused before any child is launched, so earlier
evidence can never be overwritten.

usage: run_bounded.py --timeout SECONDS --out NEW_DIR [--cwd DIR] -- CMD [ARGS...]
Exit status of this script: 0 when a record was written (read run.json for the
child's result), 3 when the output directory already exists (nothing launched),
2 on usage error.

Liveness (_alive) fails closed: inspection errors raise, denied means alive.
Limits: descendants that leave the child's process group (POSIX setsid) or
break away from it on Windows are not tracked; liveness checks are PID-based.
"""
import argparse, datetime, json, os, signal, subprocess, sys, time

WIN = os.name == "nt"
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)          # hide Windows console windows
_HAS_PROC = os.path.isdir("/proc/self")
KILL_TIMEOUT_S = 15      # bound on the cleanup command itself
REAP_TIMEOUT_S = 10      # how long to wait for the child after cleanup


class OutputExists(Exception):
    pass


def _utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class InspectionError(Exception):
    """Process state could not be determined. Callers must treat it as NOT cleaned up."""


def _raw_probe(pid):
    """Platform probe. Returns "gone", "exists" or "denied" (exists but not inspectable);
    raises InspectionError on anything else. Test hook: smoke tests replace this."""
    if WIN:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            err = ctypes.get_last_error()
            if err == 87:                                   # ERROR_INVALID_PARAMETER: no such PID
                return "gone"
            if err == 5:                                    # ERROR_ACCESS_DENIED: it exists
                return "denied"
            raise InspectionError(f"OpenProcess error {err}")
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                raise InspectionError(f"GetExitCodeProcess error {ctypes.get_last_error()}")
            return "exists" if code.value == 259 else "gone"   # 259 = STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    if _HAS_PROC:
        try:
            with open(f"/proc/{int(pid)}/stat") as f:
                return "gone" if f.read().rsplit(")", 1)[1].split()[0] == "Z" else "exists"
        except FileNotFoundError:
            return "gone"
        except PermissionError:
            return "denied"
        except (OSError, IndexError, ValueError) as e:
            raise InspectionError(f"/proc read failed: {type(e).__name__}: {e}")
    try:                                                    # POSIX without /proc: zombies count as alive
        os.kill(int(pid), 0)
        return "exists"
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "denied"
    except OSError as e:
        raise InspectionError(f"kill(pid, 0) failed: {type(e).__name__}: {e}")


def _alive(pid):
    """Fail-closed liveness: True if running OR not inspectable ("denied");
    False only when the OS positively reports the PID gone (or a Linux zombie).
    Raises InspectionError when state is unknown. Never infers "dead" from an
    error or empty output. PID reuse can still mislead any PID-based check."""
    state = _raw_probe(pid)
    if state == "gone":
        return False
    if state in ("exists", "denied"):
        return True
    raise InspectionError(f"unexpected probe result {state!r}")


def _kill_tree(proc, kill_timeout=KILL_TIMEOUT_S, kill_cmd=None):
    """Kill the child and its descendants. Returns {method, ok, detail}. ok is
    True only if the kill command itself reported success in time."""
    if kill_cmd is None and not WIN:
        try:
            os.killpg(proc.pid, signal.SIGKILL)               # child leads its own session/group
            return {"method": "killpg SIGKILL", "ok": True, "detail": None}
        except ProcessLookupError:
            return {"method": "killpg SIGKILL", "ok": True, "detail": "group already gone"}
        except OSError as e:
            return {"method": "killpg SIGKILL", "ok": False, "detail": f"{type(e).__name__}: {e}"}
    cmd = kill_cmd or ["taskkill", "/PID", str(proc.pid), "/T", "/F"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=kill_timeout, creationflags=_NOWIN)
        return {"method": " ".join(cmd[:1] + cmd[-2:]) if kill_cmd is None else "override",
                "ok": r.returncode == 0, "detail": f"rc={r.returncode}"}
    except subprocess.TimeoutExpired:
        return {"method": "taskkill /T /F" if kill_cmd is None else "override", "ok": False,
                "detail": f"kill command exceeded {kill_timeout}s"}
    except OSError as e:
        return {"method": "taskkill /T /F" if kill_cmd is None else "override", "ok": False,
                "detail": f"{type(e).__name__}: {e}"}


def run(cmd, timeout, out, cwd=None, kill_timeout=KILL_TIMEOUT_S, reap_timeout=REAP_TIMEOUT_S, _kill_cmd=None):
    """_kill_cmd is a test hook that replaces the real cleanup command."""
    try:
        os.makedirs(out)                                  # fails if it exists: never overwrite evidence
    except FileExistsError:
        raise OutputExists(out)
    so, se = os.path.join(out, "stdout.bin"), os.path.join(out, "stderr.bin")
    rec = {"command": cmd, "timeout_s": timeout, "start_utc": _utc(), "os_exit_code": None,
           "outcome": None, "cleanup": None, "platform": sys.platform}
    t0 = time.monotonic()
    kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | _NOWIN} if WIN else {"start_new_session": True})
    with open(so, "xb") as fo, open(se, "xb") as fe:
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
                kill = _kill_tree(p, kill_timeout, _kill_cmd)
                try:
                    rec["os_exit_code"] = p.wait(timeout=reap_timeout)   # the killed child's real OS code
                    reaped = True
                except subprocess.TimeoutExpired:
                    reaped = False
                if kill["ok"] and reaped:
                    status, outcome = "confirmed", "timeout_killed"
                elif not reaped:
                    status, outcome = "failed", "timeout_cleanup_failed"          # child still running
                else:
                    status, outcome = "unknown", "timeout_cleanup_unknown"        # child gone, tree kill not confirmed
                rec["outcome"] = outcome
                rec["cleanup"] = dict(kill, status=status, child_reaped=reaped)
    rec["elapsed_s"] = round(time.monotonic() - t0, 3)
    rec["end_utc"] = _utc()
    rec["stdout_bytes"], rec["stderr_bytes"] = os.path.getsize(so), os.path.getsize(se)
    with open(os.path.join(out, "run.json"), "x", encoding="utf-8") as f:
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
    try:
        print(json.dumps(run(cmd, a.timeout, a.out, a.cwd)))
    except OutputExists:
        print(json.dumps({"outcome": "refused_existing_output", "out": a.out, "launched": False}))
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
