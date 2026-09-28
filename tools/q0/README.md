# Bounded client supervisor (`tools/q0/run_bounded.py`)

This fixes the A3/A4 gap where the PowerShell runner left the OS `EXIT` field blank, so a Hermes "exit_code: 0" was only **stream-reported**. It is stdlib-only Python and works on both Windows and POSIX. There is no network access and one launch per invocation, with no retry.

`python tools/q0/run_bounded.py --timeout SECONDS --out NEW_DIR [--cwd DIR] -- CMD [ARGS...]`

- **Evidence is never overwritten.** `NEW_DIR` must not exist. If it does, the supervisor prints `{"outcome": "refused_existing_output", "launched": false}` and exits **3** without starting the child. The output files are also opened in exclusive-create mode.
- The supervisor writes `stdout.bin`, `stderr.bin` (byte-for-byte) and `run.json`. It exits 0 once the record is written.
- On Windows, children and cleanup commands run with `CREATE_NO_WINDOW`, so no console windows appear.

| `run.json` field | Meaning |
|---|---|
| `os_exit_code` | The child's real OS return code from `Popen.wait()`, never parsed from model text. It is `null` if the child never started or could not be reaped after cleanup. |
| `outcome` | `exited`, `timeout_killed` (termination confirmed), `timeout_cleanup_failed`, `timeout_cleanup_unknown` or `launch_error` |
| `cleanup` | Present only on timeout: `method` (`taskkill /T /F` bounded to 15 s, or POSIX `killpg SIGKILL`), `ok`, `detail`, `status` (`confirmed` / `failed` / `unknown`), `child_reaped` |
| `elapsed_s`, `start_utc`, `end_utc`, `pid`, `stdout_bytes`, `stderr_bytes`, `platform` | Timing, process and size metadata |

What the cleanup statuses mean:
- `confirmed`: the kill command succeeded **and** the child was reaped within 10 s.
- `failed`: the child is still running, so its exit code is `null`.
- `unknown`: the child is gone, but the tree-kill command did not report success.

**Scoring rule:** decide success from `outcome == "exited"` together with `os_exit_code`. A killed child's number depends on the platform (Windows `taskkill /F` gives 1, POSIX gives -9), so **classify by `outcome`**.

**Liveness helper `_alive`: fails closed.** It returns False (dead) **only** when the OS positively reports the PID gone. On Linux, a zombie also counts as gone.

| Probe result | `_alive` returns | How each platform produces it |
|---|---|---|
| Access denied | **True** (treated as alive) | Windows `OpenProcess` error 5; POSIX `EPERM` |
| No such PID | False | Windows error 87 or exit code other than `STILL_ACTIVE`; POSIX `ESRCH` or no `/proc` entry |
| Any other error | raises `InspectionError` | — |

- Windows uses the `OpenProcess` / `GetExitCodeProcess` API directly. It no longer runs `tasklist`, and it never reads empty output as "dead".
- Callers must treat `InspectionError` as **not cleaned up**.
- POSIX systems without `/proc` fall back to `kill(pid, 0)`, which cannot tell a zombie from a live process.
- The smoke script aborts with rc 2 and makes no claims if its own PID does not inspect as alive.

## Smoke evidence (`smoke_run_bounded.py`; harmless local children, no model or credentials)

| Case | Expected |
|---|---|
| Child exits 0 | `exited` / 0; stdout and stderr kept |
| Child prints `{"exit_code": 0, "text": "PASS"}` but exits 7 | `exited` / **7** |
| Child plus grandchild, 3 s limit | `timeout_killed`, cleanup `confirmed`, both PIDs gone |
| Output directory already exists | CLI exit 3, prior evidence byte-identical, no `run.json`, child **not launched** |
| Simulated failing kill command (hook) | `timeout_cleanup_failed`, `os_exit_code` null; the test child is then force-killed and verified gone |
| Simulated denied inspection or inspection error (the #37 regression) | Denied counts as alive; an error raises and is never reported as cleaned up; the real child is then killed and verified gone |
| Simulated hanging kill command, 1 s bound | `timeout_cleanup_failed`, "kill command exceeded 1s", bounded elapsed; test child cleaned up |

Raw results are in `docs/cloud/evidence/Q1-SUPERVISOR-SMOKE/smoke-{linux,windows}.jsonl`.

**Limitations:**
- Descendants that leave the process group (`setsid`) or break away from it on Windows are not tracked.
- Liveness is PID-based, so PID reuse could fool it.
- The `_kill_cmd` hook exists only for tests.
- This is not a containment system. No Hermes or model run has been made with it yet.
