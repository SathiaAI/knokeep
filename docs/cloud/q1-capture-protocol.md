# KnoKeep: capture without repeated reminders

Status: proposed diagnostic protocol, awaiting independent Claude review. No capture worker launched by this document. Code stays pinned to `8fb9ef7ac4290b43352a40a9690c13c1f32a8917`. Previous guided evidence remains unchanged.

## Decision this test must answer

After an agent has been given KnoKeep's standing policy once, does it save important work without a human reminding it at each milestone, and does the saved record help a different fresh client continue?

This is policy-driven capture through the CLI, not a claim about native client hooks. Explicitly loading the pinned skill is a controlled enrollment method; it does not prove the installed client discovers or invokes it automatically. Test actual native enrollment separately. The existing skill and client shims differ; pin and record exactly which instructions each run receives rather than quietly treating them as equivalent.

## First batch and stop rule

Run one independent four-milestone project in actual Claude Code and one in actual Hermes. Expand to three fresh projects per client only if the first pair reveals no critical capture failure: at most six source sessions and 24 eligible opportunities. Report all planned, started, completed, blocked and stopped runs separately. Do not replace failures with new attempts or remove setup failures from the operational denominator.

These are correlated diagnostic observations, not 24 independent reliability trials. Publish raw counts per client and per session; no 99% reliability claim or unsupported statistical confidence. A critical failure pauses expansion for diagnosis. A later fix gets a new versioned run and preserves the original failure.

## Enrollment and task construction

- Give each source the same pinned `skill/SKILL.md` once through a recorded session-level setup, plus its store path, project, client and session identifiers. Let the agent read helper usage; do not provide saved body text or an executable save sequence as in A4. Do not silently supplement the current policy with stronger capture instructions.
- Use real synthetic work with observable milestones: a verified initial deliverable; a tested requirement change; a verified correction that supersedes an earlier decision; and a verified final milestone with a concrete unfinished next action. The task requests contain no `save`, `flush`, `/conclude`, tool-switch warning or impending-stop hint. Define expected facts for each milestone before launch and keep the rubric outside the source workspace.
- Count a milestone as eligible only when the predetermined verification criterion is observed. Separately count task failures and decision changes that current policy does not explicitly cover. Never describe a verification failure as a passed milestone to inflate the denominator.
- At every completed source turn, the coordinator copies the whole store read-only, records time and file hashes, and checks saved semantic facts against the private rubric. The coordinator must not call a saving tool, correct the source store, append an omitted decision or remind the worker. Use a stable copy for any bootstrap that writes telemetry.
- Freeze source prompts, expected outcomes and scoring rubric before dispatch. Publish a salted cryptographic commitment, keeping its salt and contents private until evaluation. This prevents the expected answer from being rewritten afterward and avoids leaking answers before the receiver runs.

## Scoring

Each opportunity has distinct outcomes: task milestone verified; save attempted; durable receipt returned; required facts correctly saved by the end of that turn; latest decision correctly replaces the old one; record usable by a fresh reader. One failed stage does not disappear inside an aggregate PASS. Late catch-up is reported separately and does not convert a missed checkpoint into on-time capture. A claimed save without bytes is a miss.

Score the whole session too: all critical facts captured, or incomplete. Preserve exact receipts and original bytes. Caller-declared labels remain unverified identity. Setups that do not expose an intended native capability are BLOCKED for that capability, not evidence of its absence on the platform.

## Test the added value, not just a correct answer

For a successfully captured source, prepare two isolated fresh receiving tasks on a different actual client. Both get identical normal project files and the same next-action request; only one gets the KnoKeep record. Neither gets the source transcript, expected answer, reviewer diagnosis or the other receiver's result. Randomize which arm runs first and record it before launch.

Do not strip ordinary project files or generated HANDOFF.md from the control simply to manufacture a KnoKeep advantage. Inventory any files containing decisions. If both receivers succeed from those artifacts, report no demonstrated incremental benefit for that case. An honest BLOCKED result in the control is useful evidence; it is not a model defect. Score actual outputs, accepted/rejected decisions, rework and coordinator intervention separately.

A single paired case is a diagnostic comparison, not a causal-effect estimate. Hold expected answers privately, check outputs mechanically where possible, then obtain independent Claude/Codex review tied to exact evidence commits.

## Ownership, limits and next client coverage

Codex owns frozen fixtures, the scoreboard, source snapshots and acceptance. Claude reviews the protocol, fixes the Hermes supervisor's missing OS exit code, and independently scores results. Before another Hermes inference, the supervisor must demonstrate exit 0, exit 7 and timeout using harmless local child processes with real OS exit statuses; no inference is needed for that repair. The original A4 artifacts stay untouched.

Use existing Claude Max authentication and the verified local GLM route with paid fallback disabled. No global configuration changes, model downloads, extra paid API route or automatic billing switch. Hard limits: six minutes per source turn, twenty minutes per source session, five minutes per receiver; one bounded attempt per planned job. Preserve interrupted processes and classify unstarted opportunities separately. No production/main merge or deployment.

After the diagnostic local pair, prioritize **Cursor Cloud to Codex Cloud** and **Claude Cowork Cloud to Devin Cloud**, with the same save/receipt/export/continue criteria and pinned Git carriers where necessary. These tests must run on the actual cloud surfaces. Hermes/Claude Code successes cannot stand in for them. Grok receives a narrowly scoped, unprimed evidence review only after results exist.

Windows Git contention issue #33 remains open. This capture batch uses one writer at a time; it cannot clear the concurrent-writer claim. Investigate that failure before the separate interruption/retry/two-writer phase. Native enrollment, capture, transport, useful continuation and concurrent safety remain separate acceptance gates.

## Decision after the batch

- If enrollment works but capture misses: prioritize a supported capture trigger or adapter and visible unsaved-work indication; do not disguise the miss with better coordinator prompts.
- If records save but transport fails: fix durable export/ingestion and task binding.
- If transfer works but the receiver resumes the wrong decision: fix the resume representation and conflict visibility.
- If ordinary project files perform equally well: test a harder real user workflow before claiming a commercial advantage.
- If all diagnostic gates pass: expand clients and scenarios; then design a properly powered reliability study with sessions as the sampling unit.
