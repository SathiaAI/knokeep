# Hermes Q0 source launch — BLOCKED

Coordinator-written report, September 27, 2026. The Hermes model did not produce this report and did not execute a KnoKeep save. No compatibility or reliability pass is claimed.

Input checkout: `49e4c1cc6ad75113c97de856a069a670bf0696df`. The installed Hermes CLI was launched in an isolated worktree with a fresh profile, only terminal/file tools, persistent memory disabled and no paid fallback. The configured local model was `mmproj-Qwen3.8-27B-BF16`; that configuration is not evidence that it answered.

| Attempt | Observed result | Attribution |
|---|---|---|
| 1, 22:53:12–22:54:56 UTC | Startup attempted dependency preparation, reported invalid `plugins.enabled` configuration, failed source launcher publication, then model initialization reported missing `pydantic_core._pydantic_core`. Zero input/output tokens; exit 1. | Coordinator isolation config incorrectly used a Boolean instead of a list. Runtime dependency preparation also failed; do not attribute all errors to KnoKeep. |
| 2, 22:57:44–22:57:55 UTC | Corrected the isolated plugin list; disabled automatic dependency installation for this child. Runtime emitted an initialization event, then HTTP 401 / Invalid API Key; zero input/output tokens; exit 1. | Local-provider launch configuration remains unresolved. A successful endpoint preflight did not establish that the agent resolved the same working connection. No paid fallback was used. |

Two failed launches reached the brief's stop point. No third attempt was made. Existing user profile settings were not edited. Private raw execution logs remain with the coordinator; only sanitized error excerpts are published here, without account/session identifiers, paths or credentials.

| Track | Status |
|---|---|
| Installed CLI invocation | Observed |
| Model response | BLOCKED before answer |
| Agent-to-KnoKeep CLI save/read | NOT RUN |
| Native MCP / automatic capture | NOT TESTED |
| Original store export | NONE — no source write occurred |
| Fresh-task retrieval | BLOCKED by missing source record |
| Evidence publication | Coordinator publication only |

Next action: inspect the isolated provider resolution and runtime dependencies without changing the user's ordinary configuration. A corrected launch must receive a new attempt identifier and preserve these failures. This is a setup failure, not proof that Hermes cannot use KnoKeep.
