# Bounded client supervisor (`tools/q0/run_bounded.py`)

This fixes the A3/A4 gap where the PowerShell runner left the OS `EXIT` field blank, so a Hermes "exit_code: 0" was only **stream-reported**. It is stdlib-only Python and works on both Windows and POSIX. There is no network access and no retry.

`python tools/q0/run_bounded.py --timeout SECONDS --out DIR [--cwd DIR] -- CMD [ARGS...]`

The supervisor writes `DIR/stdout.bin` and `DIR/stderr.bin` byte-for-byte, plus `DIR/run.json`:

| Field | Meaning |
|---|---|
| `os_exit_code` | The child's real OS return code from `Popen.wait()`. The supervisor never parses it from model text. |
| `outcome` | `exited` · `timeout_killed` · `launch_error` |
| `tree_kill` | Only on timeout. Windows: `taskkill /PID <pid> /T /F`. POSIX: `killpg(SIGKILL)` on the child's own session. |
| `elapsed_s`, `start_utc`, `end_utc`, `pid`, `stdout_bytes`, `stderr_bytes`, `platform` | Timing, process and size metadata |

**Rule for scoring:** decide success from `outcome == "exited"` together with `os_exit_code`, never from anything the child prints. After a timeout, the killed child's code depends on the platform (Windows `taskkill /F` gives 1, POSIX gives -9). **Classify by `outcome`, not by the number.**

## Smoke evidence (`smoke_run_bounded.py`; harmless local children, no model, no credentials)

| Case | Linux (py 3.11.15) | Windows (py 3.11.15) |
|---|---|---|
| Child exits 0: stdout and stderr preserved | exited / 0 | exited / 0 |
| Child **prints** `{"exit_code": 0, "text": "PASS"}` but exits 7 | exited / **7** | exited / **7** |
| Child plus grandchild sleeping 120 s, 3 s limit | timeout_killed / -9, 3.0 s | timeout_killed / 1, 3.25 s |
| After the timeout, are the child and grandchild PIDs gone? | both gone | both gone |

Raw lines are in `smoke-linux.jsonl` and `smoke-windows.jsonl`; both runs have `all_pass: true` and smoke rc 0.

**Limitations:**
- A grandchild that escapes into a new process group (POSIX `setsid`) or breaks away from its Windows job would survive the tree kill. That is out of scope here.
- The liveness check can be fooled by PID reuse.
- These are supervisor checks only. No Hermes or model run was made.
