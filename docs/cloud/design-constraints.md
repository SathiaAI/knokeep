# Design constraints from the pivot review

September 27, 2026. These constraints preserve the useful product direction while keeping unproven architecture and marketing claims explicit. They are proposals for engineering validation, not a record that the pivot has been implemented.

## Capture is the first dependency

An accurate store cannot recover events the client never saved. Prioritize client capture, durable acknowledgement and correct fresh-session resumption before expanding retrieval sophistication or replacing the storage engine. Test against ordinary handoff files and observable user effort.

Continuity research supports investigating the problem, not assuming market demand. [Handoff Debt, arXiv 2606.02875 v2](https://arxiv.org/abs/2606.02875v2), dated August 30, 2026, reports fewer agent events and cumulative prompt tokens when successor agents receive handoff context. It does not measure willingness to adopt or pay for KnoKeep. Run external usage trials alongside the bounded prototype.

## Preserve concurrency guarantees across any redesign

The built design uses backend-enforced stale-writer fencing, typed operation context, held leases, preservation of losing writes and a durable migration mutex. Replacing those mechanisms requires preserving their guarantees, not retaining only their names.

An expected-head comparison alone is insufficient. Two clients read head H. A commits, B's valid proposal is rejected because the head moved, and B crashes before saving it elsewhere. The store may have avoided an overwrite while losing useful work. Separately, if A's commit succeeds but its reply is lost, a retry can duplicate the write without durable operation identity and an outcome lookup.

The replacement contract must define atomic commit and deduplication, stable operation IDs, the response for an already-committed retry, retention of conflicting proposals and recovery after server/client failure. Distinguish accepted history from pending proposals. Do not advertise every submitted proposal as saved unless its stated durability condition has actually been met.

Independent offline branches cannot be made into one original predecessor-hash chain merely by sorting entries. Start with one authoritative sequencer and queued offline proposals, or explicitly design and validate multiple histories and import semantics.

## Storage and deployment

SQLite is a candidate for a bounded single-writer deployment. Stateless HTTP request handling does not make the database, idempotency state, credentials, backups or job ownership stateless. Deployment replicas need an explicit state and failure model. Keep Postgres viable and decide from tested concurrency, recovery and operating requirements. Do not remove a working backend on the basis of a model's confidence score.

An HTTP transport adapter is not a complete hosted product. Authentication, per-project authorization, credential expiry, retry behavior, rate limits, recovery and observable failures all require tests on the actual client paths.

## Privacy, backup and deletion

When a cloud agent reads context, its AI provider can see the supplied plaintext. Storage-host protection and AI-provider visibility are different boundaries. Describe each accurately; do not promise that no host or vendor can read material sent to the model.

An offline recovery phrase and encrypted snapshot impose user recovery duties. A retained snapshot plus a surviving recovery key can retain recoverable information. Deleting the current server copy does not erase user-held copies, independent backups or AI-vendor records. Scope erase promises to the copies and keys actually controlled, and validate backup generations, restoration and deletion reconciliation.

## Record provenance and poisoned memory

An exact record preserves observed bytes and provenance; it does not make every recorded statement true. Distinguish user instructions, agent assertions and tool observations. Stored text asserting “approval granted” or “tests passed” is not authoritative evidence by itself. Enforce project identity and write authority outside model-authored content.

Keep generated summaries/views separate from source observations. Expose unresolved conflicts rather than silently converting them into one confident narrative. A successor must be able to identify which file versions and source events support a proposed next action.

## Reuse before expansion

Evaluate [Entire CLI](https://github.com/entireio/cli) as a capture/checkpoint comparison and [LoopX](https://github.com/loopx-project/loopx) only if the orchestration pilot exposes gaps it can reduce. These are candidates, not evidence of complete Cowork/Hermes/Devin support. Pin versions, inspect licenses and run the same acceptance checks before adoption. Sources inspected September 27, 2026.

Vendor export, native memory and cross-vendor features can improve. Treat interoperability as an integration obligation rather than a guaranteed moat. The useful differentiator must be measured continuity across actual client surfaces, honest failure reporting and low user effort.

## First useful delivery

Deliver one inspectable synthetic capture/resume loop, with receipts, artifact access, conflict preservation, an ordinary handoff baseline and cost evidence. Expand through the staged matrix after repairing observed failures. Avoid making a general orchestration platform, speculative memory layers or an engine rewrite prerequisites for testing whether agents save and resume reliably.
