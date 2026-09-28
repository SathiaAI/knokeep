# Installed local-model comparison — September 28, 2026

Recommendation: use **Gemma 4 12B QAT provisionally for small, tool-free drafts and evidence classification**, with Qwen 3.8 27B as a backup. Codex checks the result. This test does not qualify autonomous coding, tool use, canonical memory writes or release approval.

| Installed model / quantization | Strict frozen rubric | If the ambiguous Windows label is accepted | Observed inference time | Load time | Changed-meaning case |
|---|---:|---:|---:|---:|---|
| Gemma 4 12B QAT, Q4_0 | 8/8 | 8/8 | 2.281s | 4.703s | Correct |
| Qwen 3.8 27B, Q4_K_M | 7/8 | 8/8 | 9.937s | 10.046s | Correct |
| Nemotron 3 Nano Omni 30B A3B, Q4_K_M | 5/8 | 6/8 | 7.532s | 19.547s | Incorrect |
| GLM 4.7 Flash, Q4_K_M | 5/8 | 6/8 | 4.360s | 11.047s | Incorrect |

All four runs returned strict JSON, process exit 0 and Hermes completed:true. No truncation or exhausted-iteration notice appeared in these runs. Tools and fallback chains were empty before and after; reported tool calls were zero. Times are observations on this machine, not a repeated or isolated hardware benchmark.

## What was compared

The actual installed Hermes AIAgent SDK ran in a new isolated profile for each model, using the installed LM Studio server on loopback. All four received byte-identical eight-case prompts. The answer key was frozen before the first GLM trial, and no answer key was given to a model. Context was explicitly 16,384 tokens; max output 4,096 per call; temperature0 and vendor-supported reasoning-off were requested; two-iteration maximum ; 150s internal and 180s outer wall-clock limit. LM Studio automatically chose model placement. RTX 5080, approximately 16GB VRAM. No downloads, paid fallback or persistent user configuration changes.

The SDK wrapper intended to capture requests did not intercept the actual runtime calls. Its empty array is an instrumentation limitation, not proof of zero inference or independently verified parameters. Reasoning-off/temperature/output budget are requested settings, and model identity is supported by the loaded-instance metadata plus requested model. Hermes reports one API call per run; this is not a network traffic audit. The zero-tools configuration also does not qualify a filesystem sandbox for future tool-enabled work.

## Rubric limits

T1 says Windows coverage is complete while only Linux ran. The frozen answer was UNVERIFIED; Qwen, GLM and Nemotron said FAIL. FAIL is defensible if "coverage complete" refers to test execution. We retain the original strict score and show a sensitivity column accepting both labels; do not infer that Qwen is substantively worse from this disagreement. All models correctly labeled the missing MCP prerequisite BLOCKED in this final run.

T2 is an unacknowledged save with no read-back: its outcome is unknown. GLM and Nemotron labeled it BLOCKED instead of UNVERIFIED. More seriously, T4 changes "reject the proposal to include ALL overdue orders regardless of approval" into "reject ALL overdue orders regardless of approval." Gemma and Qwen rejected that meaning change; GLM and Nemotron accepted it. That is directly relevant to KnoKeep's open-question and rejection preservation problem.

Eight handpicked, correlated cases in one prompt per model cannot estimate field reliability or general model capability. The same prompt across attempted configurations creates selection effects; a held-out set and actual bounded tool task are required before expanding a model's role. Gemma's speed and correct critical case justify a provisional small-task role, not replacing independent review. Deterministic scripts remain preferable for hashing, copying and counting known fields.

## Earlier attempts retained, not silently replaced

- Original Q6 GLM triage A1: 700-token cap, one iteration; 5/8 strict labels, but truncated and completed:false. That result alone was not a fair model verdict.
- Comparison A1: GLM timed out at 270s; the first cleanup failed and was corrected by stopping exact task processes. Gemma failed initialization from incorrect context detection. Qwen and Nemotron were interrupted during setup/inference when that configuration was superseded; not scored.
- A2: preflight refused to start because an earlier test still had a model loaded. No inference in this attempt.
- A3: explicit context fixed; Gemma 7/8 after two output truncations and a forced summary, 193.485s, completed:false. Qwen interrupted when the short-task profile was selected; remaining models not run. A4 is a separate configuration, not a retry counted as first-attempt success.
- Hermes's managed model inventory had Qwen and Nemotron vision projectors under model entries, while LM Studio correctly indexed the complete installed language models. The comparison used those full models without changing the user's managed inventory.

Original failed-attempt records remain in the coordinator's private work directories. Published final prompt/response/runtime files are byte-identical copies. The previously loaded managed GLM instance was restored after the temporary comparison, and test LM Studio instances were unloaded. No claim is made that local inference has zero electricity, hardware or supervision cost.
