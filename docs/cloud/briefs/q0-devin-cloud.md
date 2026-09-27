# Q0 dispatch brief: Devin Cloud

Prepared September 27, 2026. Status: READY FOR COORDINATOR DISPATCH; this file is not evidence that the job ran.

## Assignment

- Job ID: `Q0-DEVIN-CLOUD-20260927-A1`; attempt generation: 1; node: F.
- Actual test surface: Devin Cloud.
- Repository: `SathiaAI/knokeep`.
- Input branch: `docs/cloud-handoff-2026-09-27`.
- Required input commit: `49e4c1cc6ad75113c97de856a069a670bf0696df`.
- Output branch: `q0/devin-cloud/20260927-a1`; draft PR base: `docs/cloud-handoff-2026-09-27`.
- Evidence directory: `docs/cloud/evidence/Q0-DEVIN-CLOUD-20260927-A1/`.
- Synthetic store export directory: `tests/fixtures/q0/Q0-DEVIN-CLOUD-20260927-A1/`.
- Owner: this actual client; coordinator: Codex; independent review: another worker assigned by the coordinator.
- Stop point: deliver one bounded Q0 source result within 20 minutes of launch, or report the specific blocker. After two failed attempts at a step, preserve both and request diagnostic review. Do not dispatch another client or Q1–Q4.

## Before running

Record the actual starting commit with `git rev-parse HEAD`, branch, runtime/version, model/provider, available tools, memory settings, authentication mode (no secrets) and job/run identifiers. The platform may name its working branch `work`; the immutable input commit controls. If the input commit differs, stop dependent testing and report CHECKOUT BLOCKED. Do not silently use main, reset another worker's checkout, or delete user changes.

Read `docs/cloud/README.md`, `orchestration.md`, `continuity-acceptance.md` and `design-constraints.md` at the pinned input commit. The new output branch is for this attempt only. If the platform assigns a branch name, report the actual name and reconcile it with the coordinator; never force-push over another job.

Use the actual Devin cloud session and record provider session ID, account route and verified per-job cap before a billable launch. Installation on a desktop is not cloud evidence. Use only existing authorized access; do not request or paste credentials into the report. Verify checkout, file access, available native tools, CLI access where exposed, network behavior and artifact export independently. Preserve the session status and spend evidence; report unavailable cost data rather than inventing a price.

The review session reports that this account accepts the v3 organization API and refused v1; verify the available route without silently changing credentials or access. The [v3 create-session schema](https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions), checked September 27, 2026, includes `max_acu_limit`: populate it with the job's already authorized ceiling, record the accepted setting and consumed ACUs, and verify stopping/status. Account-level enforcement and billing remain UNVERIFIED until observed. Do not copy documentation examples that bypass approval. The repository is reported to be indexed by Devin; label the run memory-assisted unless fresh-session access to prior project knowledge is demonstrably isolated. A new session ID alone is not isolation.

## Run and record

1. Use an isolated disposable synthetic project/store. Record the exact source inputs, executable probe script, commands, stdout/stderr and exit codes; avoid placeholders such as `<harness>` in the reproducible evidence. Do not dump environment variables or credentials.
2. Attempt a write through the existing KnoKeep path that this client actually exposes. Score agent-to-KnoKeep CLI, registered native MCP, and a shell-created MCP protocol harness separately. A CLI path is a valid observed integration; it does not establish native MCP enrollment or automatic capture. A protocol harness alone establishes neither.
3. Record the write result and full content hash. Independently compare it with saved bytes and read it from another process. Compare the persisted client identity with the actual runtime; report mismatches without rewriting the source evidence. Explicitly select a persistent backend; the MCP server's default fake backend is not durable storage evidence. Preserve rejected attempts and label legitimate content rejected by the secret gate as a possible false positive.
4. Test only relevant, authorized network destinations. Record settings when observable, hostname, method and result. A failed call proves failure for that route/configuration, not a vendor-wide internet ban. Do not change network permissions or enable new services as part of this probe.
5. Before ending, preserve the original saved synthetic store in the export directory, with a SHA-256/byte-size manifest and exact restore/read commands. Quiesce your own writers; document required metadata and omitted transient locks. Verify copied bytes against the original receipts. If the original store is lost, report LOST/BLOCKED; do not reconstruct it from the report and call it the original.
6. Prepare a neutral continuation containing only immutable checkout/artifact coordinates, project ID and retrieval commands. A genuinely separate task is required for fresh-session verification; leave it pending until observed. The coordinator must verify the exported store is available before dispatching that task. No shared `/tmp` assumption. Same-session process restart is not fresh-session resume.
7. Mark every step PASS, FAIL or BLOCKED, with its evidence path and exact reason. Track native-MCP support, CLI support, publication, store transfer, fresh-session retrieval and automatic capture separately. Q0 does not establish capture reliability or a blind behavioral comparison.

## Publish and finish

Write `report.md`, a reproducible probe script, sanitized command/output logs, receipts/manifest and `continuation.md` in the assigned evidence/export locations. Keep original failures; append corrections. Record elapsed time and observed usage/cost, or UNAVAILABLE. Use only the job's verified account authorization; this brief grants no new paid API, hosting, credentials or subscription purchases.

Stage only these synthetic evidence/fixture files. Review the staged diff for secrets, private conversation/account data and unintended product changes; run available secret scanning and record its scope/result. Check every manifest member is tracked, including hidden files that ignore rules may omit. Verify the bundle from committed Git objects or a clean archive, not only the source working tree. Preserve required byte fidelity across line-ending conversion. Commit, push to the assigned output branch and open a **draft** PR to the named base. Do not merge. If the platform provides Create PR/Update PR, use or request that publication step explicitly. A local commit or a PR-metadata tool response is not publication.

The pinned source commit predates these brief revisions; the coordinator delivers this complete brief separately. On the output branch, inspect ignored manifest paths with `git check-ignore`. After reviewing an exact synthetic manifest member for secrets, an exact-path `git add -f -- <that-file>` is allowed; never force-add an entire directory or weaken global store ignores. Add a `.gitattributes` `-text` rule limited to this job's export directory if needed to preserve bytes. This evidence-only configuration change is allowed and must appear in the diff. After pushing, verify the remote commit contains EVERY manifest member and run the size/hash check from a clean checkout or archive of that remote commit. Local staging and a remote branch name alone are insufficient. Record separate statuses for commit, branch push, draft PR creation and remote verification.

Return job ID, input commit, actual output branch, remote head commit, real PR URL, evidence paths, store-export path, publication status and pending steps. Verify the PR and file can be fetched from GitHub independently of this chat. If you cannot publish, preserve the export for the coordinator and mark DELIVERY BLOCKED; coordinator publication must be attributed to the coordinator.

Only synthetic fixtures belong in this public repository. Publishing source reports and markers makes them accessible to future workers: label any resulting Q0 restore as a mechanical retrieval check. Q2 and later behavioral comparisons need separate access-controlled fixtures/answer keys; a prompt to ignore an accessible report is not isolation.
