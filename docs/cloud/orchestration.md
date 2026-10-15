# Six-client orchestration proposal

Status: proposed pilot, September 27, 2026. Client support is earned by executing the acceptance checks, not by listing an installed product or calling its underlying model through another runtime.

## Assignments

| Client family | Initial responsibility | Independent evidence |
|---|---|---|
| Codex/ChatGPT | Integration, job dispatch, storage contracts and release preparation | Actual Codex cloud probe; ChatGPT and desktop surfaces are separately enrolled if included in a compatibility claim |
| Claude Code | Capture adapters, retry, conflict and recovery behavior | Observed hook events, durable receipts, omitted events and fresh-session resume |
| Claude Cowork Cloud | Document/planning handoff and non-coder workflow | Clean-project read/save/resume and a separately measured bridge-independent path |
| Cursor | Connection and save-status experience; assigned adapters | Actual cloud-agent results; IDE checks remain separately labeled |
| Hermes | Monitoring and recovery probes in an isolated agent instance | Recorded model/provider, duplicate-run handling, restart and memory isolation |
| Devin | Bounded engineering and independent installation/recovery | Own continuity probe plus clean-environment install and actual test results |

SuperGrok may provide targeted adversarial review. It is not a seventh compatibility node unless explicitly enrolled and tested. The same model running in several clients provides several client tests, not independent model opinions.

## Start with a small durable job ledger

Use one issue per job, an isolated branch/workspace per attempt and a result carrying immutable artifact identifiers. Keep authoritative state in one append-only job log with one writer; issue labels can display that state. Begin with one serialized dispatcher. Introduce a persistent runner only after capability probes establish which interfaces and recovery behavior it needs.

Each job records an ID, project, input revision, owner, reviewer, dependencies, permitted tools/files, expected result, acceptance checks, deadline and verified billing route/cap. Each attempt records its generation number and provider run ID. Result acceptance is separate from process completion.

The lifecycle is queued, claimed, running, evidence submitted, reviewed, accepted and integrated. Failed, cancelled, blocked-on-access and blocked-on-budget outcomes remain visible. Save dispatch intent before launch, then save the provider run ID and outcome. If a crash leaves launch uncertain, reconcile with the provider before retrying.

Labels and assignees are not locks. Closing an old pull request does not stop a remote agent from writing or spending. Before reassignment, reconcile status, cancel or otherwise contain the old run, and issue a new generation. Accept results only for the current generation and reviewed commit. Workers do not receive production deployment authority by virtue of owning a job. Keep budget reservations until remote work is confirmed stopped or completed.

If GitHub Actions serializes the dispatcher, preserve the ledger independently of workflow starts: its default concurrency behavior can replace a pending run. Workflow cancellation does not prove that an independently launched provider job stopped. [GitHub concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency), checked September 27.

One integrator prepares changes after peer review. Follow existing branch protections and the actual merge authorization. A reviewed head commit must still be the intended head when merging. GitHub exposes an expected-head check in its [pull-request merge API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request); this does not fence unrelated external actions.

## Separate coordination, product and evaluation

The coordinator assigns work. KnoKeep stores project context. An independent evaluator holds expected outcomes. During a product test, the coordinator must not secretly reconstruct missing context in the successor's issue brief, PR description or private summary.

Keep answer keys outside the tested workers' accessible files. Use clean fixture repositories and isolated client projects where supported. Record native memory, instruction files and rules. A fresh chat in an existing memory-rich project is not necessarily a clean session. If isolation cannot be established, label the result as memory-assisted.

## Verify connections before depending on them

Candidate engineering interfaces include the [Claude Code programmatic CLI](https://code.claude.com/docs/en/headless), [Codex non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode), [Cursor Cloud Agents API](https://prod.cursor.com/docs/cloud-agent/api/endpoints), [Hermes agent Runs API](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server/) and [Devin's documented APIs](https://docs.devin.ai/api-reference/authentication). These are candidate interfaces, not proof of account eligibility or reliable KnoKeep capture. Sources checked September 27.

Cowork requires special qualification. Browser-mediated delivery can support an attended exchange, but does not demonstrate a native dispatch API or unattended execution. Verify project-file access, capture hooks if any, fresh-session behavior and operation without a desktop bridge on the actual account. Do not replace Cowork with Claude Code and retain the Cowork label.

Record authentication mode without recording credentials. Subscription access is not a blanket entitlement to separately billed APIs. Verify quota sharing, provider fallbacks, tool charges and cloud-route billing. No silent switch to a different paid provider is permitted. Calibrate a small bounded run sample before forecasting a campaign; scenario counts are not billing counts.

## Operating process

Start with low concurrency and at most two failed attempts before diagnostic review. Ordinary software checks status; a model is invoked for meaningful diagnosis rather than constant polling. A stop control halts new dispatches and reconciles already-running jobs.

Continuous monitoring needs a persistent runtime. It should check actual save/resume transactions, capture lag, authentication, backup/restore health and budget thresholds. An open chat or installed Hermes application does not establish unattended monitoring. A seven-day soak cannot support a claim of 30 days unattended operation.
