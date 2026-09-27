# Q0 dispatch brief: Claude Cowork Cloud

Prepared September 27, 2026. Status: READY FOR COORDINATOR DISPATCH; this file is not evidence that the job ran.

## Assignment

- Job ID: `Q0-COWORK-CLOUD-20260927-A1`; attempt generation: 1; node: B.
- Actual test surface: Claude Cowork Cloud.
- Repository: `SathiaAI/knokeep`.
- Input branch: `docs/cloud-handoff-2026-09-27`.
- Required input commit: `49e4c1cc6ad75113c97de856a069a670bf0696df`.
- Output branch: `q0/cowork-cloud/20260927-a1`; draft PR base: `docs/cloud-handoff-2026-09-27`.
- Evidence directory: `docs/cloud/evidence/Q0-COWORK-CLOUD-20260927-A1/`.
- Synthetic store export directory: `tests/fixtures/q0/Q0-COWORK-CLOUD-20260927-A1/`.
- Owner: this actual client; coordinator: Codex; independent review: another worker assigned by the coordinator.
- Stop point: deliver one bounded Q0 source result within 20 minutes of launch, or report the specific blocker. After two failed attempts at a step, preserve both and request diagnostic review. Do not dispatch another client or Q1–Q4.

## Before running

Record the actual starting commit with `git rev-parse HEAD`, branch, runtime/version, model/provider, available tools, memory settings, authentication mode (no secrets) and job/run identifiers. The platform may name its working branch `work`; the immutable input commit controls. If the input commit differs, stop dependent testing and report CHECKOUT BLOCKED. Do not silently use main, reset another worker's checkout, or delete user changes.

Read `docs/cloud/README.md`, `orchestration.md`, `continuity-acceptance.md` and `design-constraints.md` at the pinned input commit. The new output branch is for this attempt only. If the platform assigns a branch name, report the actual name and reconcile it with the coordinator; never force-push over another job.

Use an isolated Cowork cloud task/project where available, not Claude Code or the existing review conversation as a substitute. Record whether project memory and the desktop bridge are present. Attempt a synthetic planning/document workflow through the actual tools exposed. If access requires the desktop bridge, report BRIDGE-DEPENDENT; do not disable the user's live bridge. If cloud file access, command execution, KnoKeep access or publication is unavailable, report that precise boundary. A coordinator-published report does not prove Cowork can publish or operate unattended.

The review conversation reports that Git clone/push works with its existing credential, while GitHub API PR creation is proxy-blocked and credential setup depends on the desktop bridge. These are source-session observations, UNVERIFIED for a fresh task. Test branch push and PR creation separately: a successful push is `BRANCH PUSHED / PR PENDING`, not a failed save; the coordinator may create the draft PR from that verified branch. Never paste credentials into a prompt or evidence. Record any bridge-assisted setup. Start in a clean project without KnoKeep project memories or source answers; the existing review conversation is ineligible as the test session. The coordinator should use an available launch control; if none can create the task, record the required human trigger and count it as manual setup. Do not claim unattended operation.

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
