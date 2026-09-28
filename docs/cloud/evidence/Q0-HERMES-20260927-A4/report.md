# Q0-HERMES-20260927-A4: source result (issue #32)

Written by the coordinator (Claude Cowork) from raw logs and store bytes, September 28, 2026 UTC. The model's own summary is not relied on. **Scope:** guided capability, local Hermes, CLI path only. This makes no claim of spontaneous capture, native MCP, or cloud execution. The receiving client has not been run yet.

## Setup
- **Code:** `8fb9ef7ac4290b43352a40a9690c13c1f32a8917` (`test/q0-reviewed-fixes-2026-09-27`). All four push checks were green at this SHA before launch; test-windows went green on attempt 2 after one coordinator-authorized rerun.
- **Client:** installed Hermes CLI in a new isolated profile (`logs/isolated-profile-config.yaml`).
  - Model: `GLM-4.7-Flash-Q4_K_M` on the local llama.cpp server, provider `custom`.
  - `fallback_providers: []`, memory and user profile off, no Honcho, `HERMES_DISABLE_LAZY_INSTALLS=1` from the first launch.
  - The profile has no `.env` or auth file. The local endpoint key was passed only through the child environment, and is not in any prompt or evidence.
- **Checkout:** a new checkout on `q0/hermes/20260927-a4` at the SHA above; new store directory. Project `q0-hermes-a4`, session `hermes-a4`, client `hermes`.
- **Prompt:** a short executable step list, kept private until the receiver has been evaluated. Its SHA-256 is `8793db04687542a5dd047c8b4923286466ff3bf1705ab00e5a71bb71946ab1b7`.

## Runs (runtime-enforced limits; no retries)

| Run | Limits | Start / end (UTC) | Exit | Model route actually used |
|---|---|---|---|---|
| Tiny preflight | 1 turn, `--run-budget 120`, kill at 150 s | 01:08:29 / 01:08:47 (19 s) | 0, `OK-HERMES-A4` | 1 × `GLM-4.7-Flash-Q4_K_M` / `custom` |
| Source | 30 turns, `--run-budget 600`, kill at 600 s | 01:09:10 / 01:11:07 (**117 s**) | **0** | 14 × `GLM-4.7-Flash-Q4_K_M` / `custom`. No fallback lines in the agent log, and the server log shows requests proxied to GLM. |

The run ended with `Turn ended: reason=text_response(finish_reason=stop)` after 14 of 30 API calls. Tokens: 3,894 in, 2,935 out, 120,201 cache-read. Local route, so $0.

## Source actions, all performed by the Hermes agent (`logs/10-source.stdout.jsonl`)

| Step | Exit | Receipt |
|---|---|---|
| `init --client hermes` | 0 | |
| `bootstrap` | 0 | state `ddff89ac…`, log `901bf9b9…` (separate hashes) |
| write `state.md`, then `flush-state --expect-hash <state hash>` | 0 | revision 2, `bcbff151…` |
| write `log.md`, then `flush-log --expect-hash <log hash>` | 0 | revision 2, `f7cd58ef…` |
| write `journal.txt`, then `session-append --session-id hermes-a4 --entry-file` | 0 | `q0-hermes-a4/sessions/hermes-a4` |
| `bootstrap`, then `cat` + `sha256sum` of all three docs, then `cat orders.csv` | 0 | read-back |

No packing report was computed or created; no `packing-report*` file exists.

## Coordinator verification

Verified against the actual bytes. Nothing was repaired; the independent bootstrap ran on a **copy** of the store.

| Check | Result |
|---|---|
| Returned `version_hash` = SHA-256 of the stored file (state and log) | PASS |
| `client: hermes` in state, log and journal front matter; bootstrap `state_client` / `log_client` = `hermes`, `client_labels_verified: false` | PASS |
| `active` = `Rules settled; orders.csv ready; packing report not yet produced.`; `next` = `Produce packing-report.csv from orders.csv using the decisions saved in system_state.`; no `section_warnings`; 0 conflicts | PASS |
| State, log and journal bodies present verbatim; the journal entry holds the decisions and the handoff | PASS |
| Telemetry: `init`, `bootstrap`, `flush-state`, `flush-log`, `session-append` and `bootstrap` all `allow`; no blocks | PASS |
| Export: all 10 store files plus `orders.csv` byte-identical to the originals (`tests/fixtures/q0/Q0-HERMES-20260927-A4/MANIFEST.*`; `.gitattributes` `* -text`) | PASS |

## Limits
- This is a **guided** save: the prompt listed the exact commands and body text. It does not show that Hermes would save on its own.
- Labels are caller-declared (#7 / #31). Local machine only; the CLI path only.
- The model server is shared with the user's own Hermes app.
- Fresh-client retrieval is **pending**; the coordinator will run it.
- In the published logs, local paths, the username and local session IDs are redacted mechanically. The originals are kept privately.
