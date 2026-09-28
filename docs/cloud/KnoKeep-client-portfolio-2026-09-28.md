# KnoKeep client portfolio and work allocation

Updated September 28, 2026, 03:03 EDT. Paul authorized work across the installed clients, including Devin, and asked to reduce Claude usage. The allocation below is operational; it is not a claim that all clients are qualified for autonomous product work.

| Client | Assigned role | Current work / qualification (latest stages below) | Review required |
|---|---|---|---|
| Codex | Coordinator, task definitions, independent reproduction, integration and evidence publication | Dispatching Cursor directly; monitoring Devin and local-model tests; maintaining the report and branch ledger | Code authored by Codex gets a different client's bounded review before integration |
| Cursor | Primary implementation worker on isolated branches | Implemented read-only health inspection, journal-aware reads through three correction passes and a separate bootstrap-audit correction; completed Q8 source pilot. [PR62](https://github.com/SathiaAI/knokeep/pull/62): independent 11/11 probes, 49/49 health and 10/10 evaluation checks on Windows; all eight exact-head CI checks passed | Codex reviews actual code, reruns discriminating cases and checks the final SHA |
| Devin | Independent verification, clean-environment installation, crash/retry reproduction and contained engineering tasks | Completed job Q5-DEVIN-FRAMING-REVIEW-20260928-A1 reviewed [PR60](https://github.com/SathiaAI/knokeep/pull/60) at 263ebe7a622d7421ba34b26568b464e9c2363279. [PR63](https://github.com/SathiaAI/knokeep/pull/63): Codex independently confirmed 17/17 cases pass on the fix, six fail on its base, product unchanged | Codex verifies Git blobs, commands, exit status and claims. Earlier Q2 continuation lost uncertainty, so Devin is not an authority on whether the memory is complete |
| Hermes with installed local models | Candidate for small evidence packets, extraction and draft summaries with independent validation | Completed comparison: provisional Gemma for small tool-free tasks, Qwen backup; strict scores 8/8 and 7/8 (Qwen only differs on an ambiguous rubric label), GLM/Nemotron 5/8 and miss a meaning change. Full setup caveats and sensitivity scores are in KnoKeep-local-model-comparison-2026-09-28.md. No model qualified for authoritative decisions or autonomous repository writes | Machine-check structure and provenance; Codex checks meaning. Scripts handle purely mechanical work |
| Claude Code | Reserve for difficult design changes and independent review of high-impact implementation | Earlier recovery and checkpoint reviews complete; currently idle to conserve Claude allowance | Reviewer must cite concrete code and reproductions, not simply vote approval |
| Claude Cowork Cloud | Product/workflow review and qualification of its own cloud-client surface | Existing review complete. Fresh scheduled route remains blocked by device binding; no access expansion or relaunch | Verify actual surface, durable export and fresh-session continuation separately from local CLI success |
| SuperGrok | Occasional bounded adversarial review of a frozen evidence packet | No new job now. Previous review found a real lost-clause issue but searched outside its assigned packet, so it was not fully unprimed | Independent source checks; no authority to accept code, write canonical memory or declare release readiness |

## Dispatch contract

Every job gets one owner, a job ID, a frozen input branch and full SHA, permitted files, acceptance criteria, a time limit, and a unique output branch. The worker must put evidence in GitHub and report the exact final commit. A chat response alone does not complete a job. A receiver must receive only its entitled durable inputs. Q8 used a curated input tree, but repository/network access was not isolated; the prompt prohibition does not prove containment.

Use at most two cloud workers plus one local model at a time for this pilot. Tasks may run together only when their outputs do not compete. Cursor implements a fix while Devin independently attacks a separate committed fix; neither changes the other's branch. Codex integrates only reviewed changes into a candidate branch. Production deployment and main merges remain outside the current execution.

Preserve failures. A retry has a new attempt ID and does not replace the first result. Distinguish BLOCKED, failed behavior, unverified claims and verified behavior. Unknown save outcomes require receipt lookup/read-back; never silently retry as a new logical operation. Keep open questions visible until an attributable decision resolves them.

## Cost controls

Use the existing subscription routes; no new paid APIs, top-ups or downloads. Cursor uses the already working Composer 2.5 route, with account usage checked separately from model-reported costs. Claude is not used as a routine dispatcher now that Codex can drive Cursor directly.

Devin's account UI was checked at approximately 01:29 EDT: Pro plan, 29% daily and 15% weekly quota used, on-demand balance $0, auto-reload off. The UI says sessions pause when included quota is exceeded without a balance. The new review has an eight-minute instruction and coordinator monitoring; this is not a verified provider-enforced wall-clock cap. No account limit or billing setting was changed. After the review, the account showed 33% daily and 17% weekly used, with $0 on-demand usage. These account-level deltas are not exclusive job billing measurements. That parser review finished in about two minutes. Later bounded recovery design, read review and fresh checkpoint receiver also completed; current independent bootstrap-audit review is active. The earlier account readings are not current invoice evidence.

Hermes uses loopback inference, zero tools for the current comparison, isolated test profiles and an empty fallback chain. Installed models are tried sequentially. Failed context detection and timeout cleanup are test failures, not model-quality scores. All claimed savings must include verification effort, time to fix wrong answers and the machine's availability; local inference is not costless.

## Acceptance and continuation

Codex owns the current ledger at `outputs/KnoKeep-current-status.md`; each worker's committed evidence is the reviewable source. A heartbeat continues meaningful remaining stages and stays quiet when nothing actionable changes. Running work must be checked before redispatch so a monitor restart does not create duplicate jobs.

The model comparison, Devin parser review, combined PR64 checks and Q8 guided checkpoint pilot are complete. Current work is final PR66 audit review/CI and a safe offline recovery-export experiment. Further Hermes role expansion needs a held-out test and bounded real tool task. Torn-tail recovery, stale long-lived reads, authenticating decisions, semantic generalization and external demand evidence still prevent a production-readiness claim. Green unit tests do not close those gaps.

## Latest stages at 03:03 EDT

- Combined candidate PR64 `380fe31` passed all eight exact-head checks. PR66 `bc9fe11` includes the independently reproduced read/list and audit corrections; all eight final-head CI checks passed, including the new audit regression script on all platforms.
- Actual Devin recovery design PR65 `22e1e85`: corrected Windows runner 24/24 and original Linux A2 24/24. Design evidence only; no service-restoration implementation. Read review PR67 `3a5ca4a`: four contained probes independently repeated by Codex; its fourth is same-process, not a process-kill test.
- Actual Cursor source PR68 `ff6c627` and fresh Devin receiver PR69 `d0b8408` passed the one guided complete-checkpoint pilot. Codex verified the original Git bytes and meaning; separate Windows resume/lookup/duplicate also passed. PR58 stays experimental.
- Cursor remains implementation owner, Devin independent verifier/contained engineer, Codex integrator. Claude and local models are idle. No new billing, downloads, main merges or deployment.
- Full commit-message privacy check is mandatory: neutral author/committer alone did not prevent Cursor platform coauthor trailers. Preserve original evidence locally, publish neutral exact-tree/file derivatives where needed, and never claim old public metadata was erased.

PR70 af4074521f87a1fd7827e6698c08923130fabf6c preserves the independent audit findings. Codex reproduced its first nine probes on Windows; the 1100-directory fixture was not repeated there. That correction completed and is integrated into PR66. Actual Devin now owns fix/q9-export-review-a1, reviewing and hardening the explicitly unqualified recovery-export experiment. Cursor delivered its two draft attempts privately as hashed exact file text outside Git; Codex publishes after inspection, avoiding provider-added account coauthors. This is an orchestration limitation, not a model-quality judgment.

Recovery stage: actual Cursor A1/A2 files are preserved as evidence; A1 failed Windows and independent cases despite 12 Linux passes. A2 passed 16 Windows cases and four discriminating reproductions, but PR71 remains UNQUALIFIED pending actual Devin correction and Codex reproduction. Neither an agent's completion label nor passing its own test list is a release decision.
