# Hermes Q1 multi-turn driver (`tools/q1/hermes_q1_driver.py`, `q1-hermes-driver/2`)

Job #36. The driver runs one bounded Hermes turn per invocation. It is stdlib-only and **makes no model call by itself**. It is intended for the protocol in #38; nothing here runs a Q1 session.

## Installed Hermes semantics (read from source and `chat --help`, v0.21.5 at upstream 26472756)

- **Query:** `chat --query-file PATH --oneshot` answers one query and exits. The file is read literally, so the prompt bytes are not exposed to a shell. The executable is the native `hermes.exe`, not a shim.
- **Resume:** `--resume SESSION_ID` continues that session. The oneshot resume contract (`hermes_cli/oneshot.py::_load_resume_target`) loads the stored transcript; **an unknown id raises, it does not start a fresh session**.
- **Session id:** `--format stream-json` emits an `init` event (`model`, `session_id`) and a final `result` event (`session_id`, `exit_code`, `tokens`), and prints `session_id: <id>` on stderr. The driver compares all three sources.
- **Model field:** the stream `model` is the *configured* label. The model that actually answered comes from the isolated profile's `logs/agent.log`. That log is read **per turn, as a byte delta** from a mark taken before launch. If the log is rotated or truncated during the turn, the route is recorded as `unknown` and never guessed.
- **Isolation options that exist but are not defaulted:** `--ignore-rules` and `--safe-mode`. `--ignore-rules` is passed only on request and recorded; the packet decides.

## Contract

- **Turn 1:** `--turn 1 --prompt-file <enrollment + first task>`; `--session-id` is rejected. **Turn N>1:** `--session-id` is required.
- **No added text:** nothing is added to prompts. No reminder, no save body, no rubric.
- **Environment scrub:** it applies before the check-only `--version` and endpoint call **and** before every run.
  - Removed: every inherited `HERMES_*` variable (`HERMES_CONFIG`, `HERMES_CONFIG_PATH`, `HERMES_ENV`, `HERMES_ENV_PATH`, `HERMES_PROFILE`, inference overrides), provider prefixes (`ANTHROPIC_`, `OPENAI_`, `OPENROUTER_`, `XAI_`, `NOUS_`, `OLLAMA_`, `HONCHO_` and others), and any name containing `API_KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `BEARER`, `BASE_URL`, `API_BASE` or `ENDPOINT`.
  - Then **only** `HERMES_HOME`, `HERMES_DISABLE_LAZY_INSTALLS=1` and the local key variable (read from `--key-file`) are set.
  - The removed names are recorded; values never are.
  - The endpoint check bypasses proxies for localhost.
- **Profile preflight refuses (rc 4)** unless all of these hold:
  - `model.provider == custom`, `model.default == --model`, `model.key_env == --key-env`, and `model.base_url` is a localhost `http://…:port/v1` equal to `--endpoint` (now required);
  - `fallback_providers: []`;
  - memory and user profile disabled;
  - no Honcho anywhere in the config or a `honcho.json`;
  - no `mcp_servers` entries and no installed plugins;
  - no `.env` or `auth.json`.

  The facts are reported in `preflight`, including `honcho_configured`, `mcp_servers_configured`, `plugins_installed` and `plugins_config_key`.
- **Supervisor:** the approved supervisor, `tools/q1/vendor/run_bounded.py`, a byte-identical copy of `tools/q0/run_bounded.py` at `28668664317a6d9a2ed1dcbd886f5a8832e3e2c8` (git blob `4b887fff9ae3392a7de29b0a9fc72c5941b0dd5b`).
- **Evidence** goes to `<evidence>/turn-NN/` and is exclusive-create. An existing directory is refused (rc 3). `turns.jsonl` is append-only.
  - `turn.json` records: real OS exit and outcome, reported exit and whether they agree, session continuity, the per-turn route delta, prompt sha256, argv (never the key), the scrubbed variable names, preflight facts and provenance hashes.

## Tests (`test_hermes_q1_driver.py` with `fake_hermes.py`; no model, network or real credentials)

20 cases cover:
- the child environment contains **only** the three intended variables, even when polluted `HERMES_CONFIG`, `HERMES_ENV_PATH`, `HERMES_PROFILE`, `OPENROUTER_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and `CUSTOM_ENDPOINT` are injected (this also found and fixed inherited `GH_TOKEN`/`GITHUB_TOKEN` pass-through);
- **per-turn API counts** (2 then 3, not a cumulative 5) and a rotated log giving `unknown`;
- refusal of a remote `base_url`, wrong provider, wrong model, Honcho, MCP servers or a paid fallback;
- new session and resume, session break, OS exit 7 against a reported 0, timeout, evidence never overwritten, no key in outputs, an append-only ledger, and a scrubbed check-only run.

## Limits

- `--resume` has not been exercised with the real model; only the fake has been run through it.
- Hermes's internal retries (3) cannot be configured from here.
- The per-turn route relies on Hermes's log format and on no concurrent writer to the same isolated profile.
- The YAML checks are line-based for the flat keys used by the frozen profile.
- Built-in tools and skills bundled with the Hermes install are not enumerated here.
