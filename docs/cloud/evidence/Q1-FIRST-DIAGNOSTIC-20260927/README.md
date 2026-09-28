# First Q1 diagnostic: capture succeeded in one source; both fresh receivers failed

Observed September 27, 2026 local / September 28 UTC. Job [#36](https://github.com/SathiaAI/knokeep/issues/36). Product `8fb9ef7ac4290b43352a40a9690c13c1f32a8917`. These are actual installed Claude Code and Hermes sessions, not substitute Codex agents. No native cloud MCP or automatic client enrollment was tested.

| Attempt | Task milestones verified | Save opportunities | On-time durable captures | Result |
|---|---:|---:|---:|---|
| Claude Code A1 | 1 | 1 | 0 | Source attempted both saves; our command permission matcher refused its compound shell command. Three later turns unstarted. |
| Hermes A1, local GLM | 0 | 0 | 0 | Task self-check remained wrong; outer supervisor killed after 360.422 seconds. Three later turns unstarted. This is a task/time-limit failure, not an eligible forgetting event. |
| Claude Code A2, revised command permission | 4 | 4 | 4 | Nine matching durable receipts; same session, verified work before capture at every milestone. Original A1 failure retained. |

Claude independently checked all four A2 checkpoint fact sets, receipt hashes and ordering: [review](https://github.com/SathiaAI/knokeep/issues/36#issuecomment-5862321889). All task facts came from source work; the coordinator supplied no saved body or per-turn capture reminders. Standing skill policy was explicitly loaded once. A2 expanded the harness's allowed Bash command composition; this shell is not a security sandbox.

## Fresh receiver comparison

Both actual Hermes sessions used `GLM-4.7-Flash-Q4_K_M`, the same local custom endpoint/config, separate fresh profiles, identical prompt, and identical three ordinary files. Frozen randomized order was files-only, then files-plus-original-store. Neither received the source chat, private rubric, runtime profile or `.git`. No input bytes changed in either arm.

| Arm | Elapsed | Calls on measured route | Output | Verdict |
|---|---:|---:|---|---|
| Ordinary files only | 87.704 seconds | 9 | Reran existing packing report; falsely inferred that was the recorded next action | INCORRECT_CONTINUATION |
| Same files plus store | 94.344 seconds | 13 | Listed `store/` but never read state/log/journal; reran report and incorrectly excluded unapproved ready rows | INCORRECT_CONTINUATION |

Both processes exited 0. Neither produced `review-queue.csv`. The frozen expected output was the header `order_id,reason,owner` and one row `p3,awaiting approval,Warehouse Control`. Exit success is not task success. No incremental benefit was demonstrated in this case. The treatment failed to retrieve available records; it also made a separate reasoning error about the existing packing rule. This is not evidence that the record was missing or corrupted.

Claude's [independent receiver audit](https://github.com/SathiaAI/knokeep/issues/36#issuecomment-5862395814) agrees. Both arms rewrote the existing packing report to identical bytes; byte immutability does not mean no writes occurred. This tests unaided discovery of a raw store with a generic continuation request; it does not test the normal bootstrap hook or the usefulness of record contents once retrieved. The treatment's eight first searches confused filename wildcards with content regexes.

The saved next-action note already contained the source's predicted answer, accurately labelled unimplemented. A successful receiver would therefore prove transport and execution of that plan, not independent reasoning from previously unseen requirements. Neither receiver succeeded, so no copied-answer benefit occurred.

## Defects exposed and next work

1. Store discovery needs an explicit, supported entry point. Dumping a directory beside a project was insufficient on this route. Test a generated, readable resume artifact or actual bootstrap adapter as a new version; do not retroactively change these receiver results.
2. An accepted intermediate source version contradicted itself. At milestone 3, hash `68c736be709c57740c6343b976e8f7961ac879ac86f6167528d216c633b495e8` held the new eligibility in Architecture and the old rule in Hard Constraints. Later updates corrected it. Test interruption at that exact boundary; per-document durable writes do not make a whole milestone atomic.
3. The local Hermes source failed before a verified milestone. Do not expand its capture reliability batch until its route/task failure is diagnosed. Independent actual-cloud transport tests can proceed under [#41](https://github.com/SathiaAI/knokeep/issues/41).

## Evidence and limits

`original-byte-manifest.json` covers 67 byte-identical synthetic checkpoint/receiver files. Store and project bytes were copied, not rebuilt or redacted. `visible-tool-trace.json` files are derived views: private machine paths/runtime IDs and reasoning/signatures were removed; tool commands, visible output and event ordering remain. Private raw trace fingerprints preserve a reference to the originals. Process summaries are likewise sanitized. Raw private evidence remains outside Git.

Pre-dispatch commitments: A1 `abd6cb0f6f15bb0491acc13ffb119d8c2be889d635a82604ff319444f9e1a94d`; A2 `f7c82146e9666c7b732417fa8f3f6a189489b1e8cd38df614f56ea0544ed7b2a`; receiver inventory `8b88daa1384aa367c1d8bd4f91c488a973fec5851b02d8a451a395d9b0f82036`. Original packets include machine paths and runtime identifiers, so `rubric-derived.json` is explicitly a sanitized derivative, not a public reconstruction of those hashes. Independent Claude checked the original A2 commitment privately. The public commitment is not fully reproducible without authorized access to the private original.

Pins: protocol PR38 `3256e6a3d1ef2f3afda1a80462c12be079086282`; supervisor PR37 `28668664317a6d9a2ed1dcbd886f5a8832e3e2c8`; Claude adapter A1 `2fb2262930ffee76aaeb95777cdc9345385bd99e`, A2 `d9584063f4703aecec4f463dd294fd21e3af1dae`; Hermes adapter PR39 `f6c0c3b9885e1d1ff3a6976bc2102051e54b0795`. Identical enrollment skill hash `20e4927d1b068bd3640a5e0027a80a81bc9a38d2857a3fa4c39d5fcd77f61606`.

Claude route was existing Max authentication, `claude-opus-5-5`, no API-key override; built-in agents-md/telemetry plugins remained enumerated. Fresh Hermes profiles automatically gained bundled skills, with rule injection off and no observed skill use. Hermes treatment inspected its installed Python runtime outside the input directory; neither trace shows private rubric/other-session access. These are fresh histories, not formal filesystem containment or a claim of full blinding. Caller-declared writer labels are unauthenticated. No dollar cost is inferred from token estimates. Single correlated synthetic sessions cannot establish reliability, demand or superiority over other products.
