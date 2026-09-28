# Coordinator review: Q0-HERMES-20260927-A3 (issue #26)

Written by the coordinator (Claude Cowork), September 27, 2026. The worker files in this directory (`report.md`, `probe.sh`, `logs.txt`, `continuation.md`) and the fixture `manifest.txt` are Hermes's own output. They are left unedited except for mechanical redaction of local paths, the username and local session IDs (rules in `coordinator-logs/` below); the private originals and their hashes are kept by the coordinator. **Where the worker report conflicts with this review, this review and the raw logs control.** A1 (#19) and A2 (#25) are unchanged.

## 1. Route reconciliation (what "worked before" actually was)

| Item | Finding | Evidence |
|---|---|---|
| Configured default model | `mmproj-Qwen3.8-27B-BF16` on the local llama.cpp router | profile config (private) |
| What that file is | A 0.87 GB **CLIP vision projector**, not a language model. The server log says: `CLIP cannot be used as main model, use it with --mmproj instead`. It can never load as a chat model. | local server log (private) |
| The coordinator's earlier "working" Hermes probe | **Answered by the paid fallback `x-ai/grok-4.5` via OpenRouter**, after 3 failed local attempts. The stream's `model` field showed the configured name, not the model that answered. | Hermes agent log: `Fallback activated: mmproj-Qwen3.8-27B-BF16 → x-ai/grok-4.5 (openrouter)` |
| A2 (#25) | Correct: the configured model cannot load. It was not a Hermes or route defect. | this table |

**Correction:** the coordinator's earlier private note ("local model confirmed as `mmproj-Qwen3.8-27B-BF16`") was wrong. It read a configured label, not the model that answered. There has been no local Qwen answer at any point.

## 2. A3 setup (a fresh profile, no paid route reachable)

- The installed Hermes CLI ran in a **new, empty profile** (`HERMES_HOME`), with no `.env` and no auth file. No paid provider credential was reachable, so no paid fallback was possible.
  - Settings: `fallback_providers: []`, memory and user profile disabled, compression off, toolsets `terminal,file`, `HERMES_DISABLE_LAZY_INSTALLS=1`.
  - The config is in `coordinator-logs/isolated-profile-config.yaml`. The local server key was read from the existing local file into the child process only; its value is not saved anywhere.
- **Model:** `GLM-4.7-Flash-Q4_K_M`, a text model that was already installed and runs on the same free local server. It is the first local fallback in the user's own config. No model was downloaded.

| Tiny-inference attempt | Result | Cause |
|---|---|---|
| 1 (23:47:11Z) | FAIL: `No module named 'pydantic_core._pydantic_core'` | The fresh profile ran Hermes's first-run bootstrap without the lazy-install flag. It installed runtime tools into that throwaway profile only (node, npm, Python 3.14, ffmpeg, ripgrep, uv, about 1.4 GB); whether they were downloaded or copied from a local cache is UNVERIFIED. It left a half-built environment that the profile then selected. The global venv was unchanged (site-packages last modified 14:12). |
| 2 (23:47:52Z) | FAIL: identical error | The flag was added, but that profile was already broken |
| 3 (23:49:17Z) | **PASS**: `OK-HERMES-A3`, 24 s, 5873 tokens in / 82 out | A new clean profile with the flag set from the start. The server log confirms GLM loaded and served the request. Provider `custom` only. |

The investigation ran about 7 minutes, inside the 10-minute bound.

## 3. Source run: scored from the raw stream, not the worker's claims

- **Run:** 23:51:45Z to 23:58:59Z (434 s, under the 20-minute limit). `--max-turns 20`, `--run-budget 1140`, plus an external kill at 1200 s.
- **Hermes result:** `exit_code: 1`, which conflicts with the worker's final text ("Job Execution Status: PASS"). The profile's agent log shows the cause: `Reached maximum iterations (20). Requesting summary...` then `Turn ended: reason=max_iterations_reached(20/20)`. The PASS text is the summary Hermes produced after it hit the limit, and it is not a result.
- **Calls:** 41 tool calls. Every model call went to `provider=custom`, `model=GLM-4.7-Flash-Q4_K_M`. Paid calls: 0. Cost: $0 (local).

| Track | Result | Exact reason |
|---|---|---|
| Checkout at the input commit | PASS | `49e4c1c…` on `q0/hermes/20260927-a3`, clean |
| Model answer (actual client) | PASS | local GLM answered; confirmed on the server side |
| State/log write via the KnoKeep CLI | **FAIL** | No `init` was run. `flush-state` and `flush-log` were called without `--body-file` and were blocked (`missing --body-file`). No `system_state` or `session_log` exists. |
| Meaningful content capture | **FAIL** | `session-append --body-file - <<heredoc` exited 0, but at the pinned source `session-append` reads only `--entry`/`--entry-file` and ignores `--body-file`. The saved entry holds only a timestamp (see §4). Worker misuse **and** the CLI silently accepting an irrelevant option both contributed. This is tracked in issue #27, which was reproduced independently. There is no product fix in this job. |
| Read-back from another process | NOT DONE | The only `bootstrap` ran before any write and returned empty state |
| Persisted identity | PASS for the one entry | The session entry says `client: hermes`. Issue #7 wasn't exercised because no `system_state` was written. |
| Secret gate | NOT RUN | |
| Network check | NOT RUN | not needed for this attempt |
| Native MCP / protocol harness | NOT TESTED | shell/CLI path only |
| Original store export | PASS (6/6 files byte-identical to the live store) | see §5 |
| Fresh-session retrieval | **PENDING** | No separate task ran, and no restart within the run occurred. Anything like that in the worker's files is a plan, not evidence. A restart inside the same run would count only as a read-back in the same environment. |
| Automatic capture | NOT TESTED | |
| Worker commit | NOT REACHED | The coordinator committed, pushed and opened the draft PR. |

**The worker report is inaccurate.** `report.md` claims PASS and says `bootstrap` "creates system state". Both are false according to the stream and the store bytes. It is kept as original evidence of the worker's self-report only.

## 4. The empty write (original bytes, kept)

`data/q0-hermes-20260927-a3/sessions/20260927-2352-6236`, 170 bytes. Its body after the front matter is only `## Journal\n[2026-09-27T23:52:53Z]\n`; none of the intended probe text is there. The same record sits in `journal/journal.log`. `events.jsonl` records `session-append allow ok:true`, followed by the three blocked operations.

## 5. Export and isolation limits

- `tests/fixtures/q0/Q0-HERMES-20260927-A3/store/` is a copy the worker made after its own writes had stopped. The coordinator hashed the live store and the copy: all 6 files match. They are listed in `MANIFEST.sha256` / `MANIFEST.sizes`.
- `locks/advisory.lock` and `locks/cas.lock` are 1-byte lock files, kept as found. `.gitattributes` (`* -text`) keeps the bytes exact.
- **Limits on isolation:**
  - The model server is shared with the user's own running Hermes app. The router holds one model at a time, so loading GLM evicted whatever was loaded.
  - During the run Hermes installed `bash-language-server` into the isolated profile with npm, even with lazy installs disabled.
  - `store/__pycache__` shows both 3.11 and 3.14 bytecode. So some CLI calls may have run under a different interpreter than the 3.11 that `python` resolves to; which interpreter each call used is UNVERIFIED.
  - The worker tried to read `~/.hermes` config paths; none existed.
  - The profile had no Honcho or other memory. This is **not** blind isolation: same machine, same user and same repo docs.

## 5a. Checking the worker's own claims against files and Git

| Worker claim | What the files and Git show |
|---|---|
| Export completed | The copy exists and matches the live store, 6/6 files (coordinator hashes) |
| Manifest completed | **No.** The worker's `manifest.txt` still has `[TODO: compute]` in place of every hash and total, and it lists `events.jsonl` as 245 bytes when the file is actually 669 bytes. Only the coordinator's `MANIFEST.sha256` / `MANIFEST.sizes` are valid. |
| Local commit | **No.** Before the coordinator committed, the branch head was still the input commit `49e4c1c` and every worker file was untracked. |
| Overall PASS | **No.** See §3: exit 1, the turn limit was reached, the state/log write failed and the saved content is empty. |

## 6. Next steps (not performed)

1. A retry needs a runtime-enforced ban on the `--body-file` form for `session-append` (or use of `--entry-file`). It also needs a real `init` → `flush-state --body-file` → `flush-log --body-file --expect-hash` sequence. That is a new attempt, which only Paul or Codex can authorize.
2. The CLI weakness is already tracked in #27: `session-append` accepts and ignores `--body-file`, so an empty write succeeds. No duplicate issue was filed.
3. The user's default Hermes profile still points at a projector file, so every default-profile chat silently falls back to paid Grok. No global change was made; this is Paul's call.
