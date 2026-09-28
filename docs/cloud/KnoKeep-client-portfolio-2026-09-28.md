# KnoKeep client portfolio and work allocation

Updated September 28, 2026, 03:22 EDT. Paul authorized work across the installed clients, including Devin, and asked to reduce Claude usage. The allocation below is operational; it is not a claim that all clients are qualified for autonomous product work.

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

Every job gets one owner, a job ID, a frozen input branch and full SHA, permitted files, acceptance criteria, a time limit, and a unique output branch. Evidence must reach GitHub with an exact final commit. Cursor now delivers allowed files privately with verified hashes; Codex reviews and publishes them with neutral metadata. A model response alone does not complete a job. A receiver must receive only its entitled durable inputs. Q8 used a curated input tree, but repository/network access was not isolated; the prompt prohibition does not prove containment.

Use at most two cloud workers plus one local model at a time for this pilot. Tasks may run together only when their outputs do not compete. Cursor implements a fix while Devin independently attacks a separate committed fix; neither changes the other's branch. Codex integrates only reviewed changes into a candidate branch. Production deployment and main merges remain outside the current execution.

Preserve failures. A retry has a new attempt ID and does not replace the first result. Distinguish BLOCKED, failed behavior, unverified claims and verified behavior. Unknown save outcomes require receipt lookup/read-back; never silently retry as a new logical operation. Keep open questions visible until an attributable decision resolves them.

## Cost controls

Use the existing subscription routes; no new paid APIs, top-ups or downloads. Cursor uses the already working Composer 2.5 route, with account usage checked separately from model-reported costs. Claude is not used as a routine dispatcher now that Codex can drive Cursor directly.

Devin's account UI was checked at approximately 01:29 EDT: Pro plan, 29% daily and 15% weekly quota used, on-demand balance $0, auto-reload off. The UI says sessions pause when included quota is exceeded without a balance. The new review has an eight-minute instruction and coordinator monitoring; this is not a verified provider-enforced wall-clock cap. No account limit or billing setting was changed. After the review, the account showed 33% daily and 17% weekly used, with $0 on-demand usage. These account-level deltas are not exclusive job billing measurements. That parser review finished in about two minutes. Later bounded recovery design, read/audit reviews, fresh checkpoint receiver and recovery-export corrections also completed; all model workers are now idle. The earlier account readings are not current invoice evidence.

Hermes uses loopback inference, zero tools for the current comparison, isolated test profiles and an empty fallback chain. Installed models are tried sequentially. Failed context detection and timeout cleanup are test failures, not model-quality scores. All claimed savings must include verification effort, time to fix wrong answers and the machine's availability; local inference is not costless.

## Completed qualification sequence

Codex maintains `outputs/KnoKeep-current-status.md` and the qualification report. Every active worker was checked before further dispatch. The bounded overnight sequence is finished, subject only to the final report publication checks; the recurring follow-up can then stop. Product readiness remains NOT QUALIFIED.

| Stage | Final evidence | Outcome and limits |
|---|---|---|
| Read freshness and bounded audit | PR66 bc9fe11004c4568a72adf08d463048aca26daa92 | All eight final-head checks passed. Actual Cursor implementation, Devin review/correction and independent Codex Windows reproductions. No automatic journal repair. |
| Complete-checkpoint workflow | PR68 ff6c6275c71f3d0439d6e370be73cfae7f298cd4 and PR69 d0b84084805e39c7a4f35b62798710df1743cd7b | Actual Cursor source and fresh actual Devin receiver passed one guided fixture; original store bytes, unresolved question and revised business result verified. PR58 remains experimental; no capture-rate/native MCP claim. |
| Recovery design | PR65 22e1e8592c949f5ff5724fb9b5609a415889b00f | 24 Linux cases and 24 Windows cases after declared comparator-order correction. Original failures retained. Design only. |
| Offline recovery export | PR71 054a51a7ef83d873aecb2659e71edb8b6472cbfe | All 14 final-head checks passed. Actual Cursor A1/A2 and Devin A1/A2 corrections; Codex Windows 46 passes/one POSIX skip plus eight independent process-crash assertions. Synthetic experiment only; no service restoration or completeness guarantee. |
| Installed Hermes model comparison | Full comparison report | Provisional Gemma for small checked tool-free tasks, Qwen backup. Eight handpicked cases cannot qualify autonomous tools or general reliability. GLM/Nemotron observations and failed setup attempts remain evidence. |

Cursor remains the preferred bounded implementation worker; Devin independently verifies or handles contained engineering; Codex owns dispatch, reproduction, integration and publication. Claude stays reserved for a specific difficult review. All are currently idle. Cowork's unsigned device-binding route stays cancelled. No new billing, downloads, main merges or deployment.

Full commit-message privacy checks remain mandatory. Neutral author/committer alone did not stop provider coauthor trailers in earlier Cursor commits. Private exact-file delivery now avoids that route; prior public metadata may persist and was not erased. Original failed client bytes are retained with explicit provenance.

Next release work concerns authenticated decisions, held-out capture/resume qualification, recovery decisions/service restoration, consented Cowork access and external demand. Additional useful tests do not by themselves supply owner consent or users.
