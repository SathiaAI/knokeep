# A completed handoff must be one durable object

Status: proposal for review, not implemented or approved. Evidence: [interruption probe PR46](https://github.com/SathiaAI/knokeep/pull/46), [open issue49](https://github.com/SathiaAI/knokeep/issues/49). The accepted journal boundary contains a new architecture section and an old conflicting constraint. No file is corrupt. The product saved individually valid writes from different stages of a milestone.

A warning cannot reliably detect contradictory natural-language decisions. An agent can also seal a wrong summary. The design must separate completeness of a saved milestone from truth of its contents.

Proposed next implementation: one immutable checkpoint containing the complete state and log bodies, their hashes, a caller-supplied milestone ID, the preceding checkpoint hash and the declared writer. Publish the entire checkpoint through one existing gated journal write; only after that durable receipt may the capture driver label the milestone saved. A crash before the receipt is an uncertain acknowledgement, resolved by retrying the same ID and payload. An ID reused with different bytes is an explicit conflict. A competing writer based on an old checkpoint must retain its proposed bytes for review. Never retry by silently replacing the expected predecessor.

Resume must read that complete checkpoint as its input, verify its body hashes, and expose later unsealed legacy document changes separately. A mutable pointer alone is insufficient unless it points to an immutable object already durably present. Existing state/log documents can be compatibility views; they cannot quietly remain competing authorities. Legacy stores have an explicit unsealed status and migration/export path. This changes the authoritative resume contract and deserves its own reviewed version, not an overnight patch presented as a warning fix.

Before adopting it, implement a bounded prototype on a separate branch and require these checks:

1. Kill the writer before, during and after journal fsync, before publish and before reply. Every fresh reader sees either the preceding complete checkpoint or the new complete checkpoint; never a mixture. Keep original journals for each boundary.
2. Deliver the same milestone repeatedly after lost replies: one durable event. Changed payload with the same ID is rejected. Two independent processes and two agent clients must both exercise this.
3. Retain old export/read compatibility, authenticate no writer merely from its label, and scan every embedded body. Test size limits, malformed metadata, poisoned instructions and restore from an old snapshot.
4. Compare fresh continuation with identical ordinary project files, with and without the checkpoint. Give the receiver no source conversation or answer key, and enforce access boundaries beyond a prompt. Score decision fidelity separately from byte hashes and file output.

Owner: Codex authors the versioned API and crash harness; actual Claude Code independently reviews or co-implements; Claude Cowork challenges compatibility and runs a separate receiver; Cursor and Devin each run an independent pair once their routes qualify. Paul is needed only for account/device access that the existing sessions cannot grant. The prototype can start after the current Windows fence defect and evidence reviews finish; production readiness remains blocked until these checks pass.

Do not widen the storage rewrite to build search, embeddings, graphs, billing or a hosted service before this one reliable save-and-resume path works. The current tests support a guided transport experiment, not a production memory product.
