# Hermes Q1 multi-turn driver (`tools/q1/hermes_q1_driver.py`)

Job #36. The driver runs one bounded Hermes turn per invocation. It is stdlib-only and **makes no model call by itself**. It is intended for the protocol in #38; nothing here runs a Q1 session.

## Installed Hermes semantics (read from source and `chat --help`, v0.21.5 at upstream 26472756)

- **Query:** `chat --query-file PATH --oneshot` answers one query and exits. The file is read literally: nothing is shell-interpreted.
- **Resume:** `--resume SESSION_ID` resumes that session. The oneshot resume contract (`hermes_cli/oneshot.py::_load_resume_target`) loads the stored transcript and continues it; **an unknown id raises instead of silently starting a fresh session**.
- **Session id:** `--format stream-json` emits `{"type":"system","subtype":"init","model","session_id"}` first and `{"type":"result","session_id","exit_code","tokens",...}` last, and prints `session_id: <id>` on stderr (`hermes_cli/stream_json.py`). The driver takes the session id from these three sources and flags any disagreement.
- **Model field:** the stream `model` field is the *configured* label. The model that actually answered comes from the isolated profile's `logs/agent.log` (`API call #N: model=… provider=…`), which the driver counts per turn. (A3 lesson: the label is not proof.)
- **Isolation options that exist but are not defaulted:** `--ignore-rules` (skips AGENTS.md/SOUL.md/memory injection) and `--safe-mode`. The driver passes `--ignore-rules` only when asked, and records the choice. The packet decides.

## Contract

- **Turn 1:** `--turn 1 --prompt-file <enrollment + first task>`. `--session-id` is rejected.
- **Turn N>1:** `--turn N --session-id <id from turn 1> --prompt-file <task N>`. It is rejected without an id.
- **No added text:** the driver adds nothing to prompts. No reminder, no save body, no rubric.
- **Environment:** fresh `HERMES_HOME`, `HERMES_DISABLE_LAZY_INSTALLS=1`, the local endpoint key placed in the child environment only (from `--key-file`), and paid-provider variables removed.
- **Preflight refuses (rc 4)** unless the profile has `fallback_providers: []`, memory and user profile disabled, and no `.env` or `auth.json`.
- **Supervisor:** each turn runs under the approved supervisor, `tools/q1/vendor/run_bounded.py`. It is a byte-identical copy of `tools/q0/run_bounded.py` at `28668664317a6d9a2ed1dcbd886f5a8832e3e2c8` (git blob `4b887fff9ae3392a7de29b0a9fc72c5941b0dd5b`, sha256 `1b54a664…445a`).
- **Evidence** goes to `<evidence>/turn-NN/`: `stdout.bin`, `stderr.bin`, `run.json` and `turn.json`, all exclusive-create. An existing turn directory is refused (rc 3). `turns.jsonl` is append-only.
  - `turn.json` records: real OS exit and outcome, the Hermes-reported exit and whether the two agree, session continuity, route counts, prompt sha256, argv (never the key) and provenance hashes.
  - A parse failure is recorded, never fatal.
- **Check only:** `--check-only` runs the read-only checks (profile preflight, `--version`, `GET /v1/models`; listing a model does not prove it can load).

## Tests (`test_hermes_q1_driver.py` with `fake_hermes.py`; no model, network or credentials)

| Case | Result |
|---|---|
| Turn 1 new session; turn 2 `--resume` same session | pass |
| Existing turn evidence refused, byte-identical (rc 3) | pass |
| Resume without an id, or turn 1 with an id, rejected (rc 2) | pass |
| Session break detected (returned id differs) | pass |
| Real OS exit 7 while the stream reports exit_code 0: `exit_codes_agree: false` | pass |
| Hang: `timeout_killed` with the partial stream kept | pass |
| Profile with a paid fallback refused (rc 4), no launch | pass |
| No key in outputs or records; ledger append-only; `--check-only` launches nothing | pass |

Both runs had `all_pass: true` and rc 0, on Linux (Python 3.11.15) and on Windows with the Hermes venv Python 3.11.15.

**Real read-only check on the target machine** (fresh isolated profile, A4 route config): preflight OK, Hermes v0.21.5, `/v1/models` returned HTTP 200 with `GLM-4.7-Flash-Q4_K_M` listed. No inference.

## Limits

- `--resume` continuity through the `chat --oneshot --format stream-json` path has **not** been exercised with the real model yet. Only the fake double has been run through it.
- Hermes's internal API retries (3) are not configurable from here. The supervisor bounds wall-clock time only.
- The key environment name is an existing local convention.
- The log route counts depend on Hermes's log format.
