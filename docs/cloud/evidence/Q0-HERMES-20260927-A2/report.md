# Hermes A2 — local model load failure

Coordinator-written evidence, September 27, 2026. Tracks issue #21; prior A1 setup failures remain in PR #19. Input code commit: `49e4c1cc6ad75113c97de856a069a670bf0696df`.

Before this separate attempt, a read-only runtime-resolution check confirmed the coordinator's earlier configuration error: without `model.key_env`, the custom local-provider route selected a placeholder; declaring the variable resolved the expected existing key and endpoint. No key value was printed or saved to the test profile. This fixed the isolated mapping, not a product defect.

The actual installed Hermes CLI was then launched in a fresh profile/worktree, local model only, paid fallbacks empty, persistent memory disabled, and terminal/file tools only. The model identifier was `mmproj-Qwen3.8-27B-BF16`, taken from the configured route and endpoint model listing. Listing a model does not prove it can load.

Observed runtime result:

```text
HTTP 500: model name=mmproj-Qwen3.8-27B-BF16 failed to load
custom returned a server error on all 3 attempts
exit_code: 1
input tokens: 0; output tokens: 0
```

The process ran from 23:19:48 to 23:24:55 UTC, 307.19 seconds, then exited itself. A later coordinator stop request found the tracked process already gone and terminated nothing. The user's separate model server was not stopped. No paid fallback was used, and no model answer or KnoKeep write occurred.

The runtime performed three internal retries despite the job brief's two-failure instruction. Retry limits must be enforced in runtime configuration or the supervisor, not assumed from a prompt. The twenty-minute external deadline was not reached.

| Track | Result |
|---|---|
| Local-provider mapping correction | Verified by read-only resolver comparison |
| Model answer | BLOCKED by local server model-load error |
| KnoKeep CLI write/read | NOT RUN |
| Original store export / fresh retrieval | NONE / BLOCKED |
| Native MCP / automatic capture | NOT TESTED |
| Publication | Coordinator diagnostic report only |

Next diagnostic: verify which installed text model can actually load on the local server and correct the isolated route accordingly; inspect server resource/load errors. Do not treat endpoint discovery, a model name or a repaired authentication mapping as a successful inference. No model downloads, global model changes or paid reroutes were performed in this job. No product code changed.
