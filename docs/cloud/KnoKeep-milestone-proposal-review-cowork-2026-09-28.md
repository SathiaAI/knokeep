# Review: "A completed handoff must be one durable object" (Claude Cowork, 2026-09-28)

Reviewed: outputs/KnoKeep-milestone-completion-proposal-2026-09-28.md. Direction agreed: one immutable checkpoint, and completeness kept separate from truth. The proposal still leaves eight concrete contract gaps. Each could re-create the Q2/issue-49 class of failure.

## A. One durable authority
1. **How is the head found?** "Publish through one journal write" doesn't say how a reader finds the *latest* checkpoint. A journal scan is O(n) and backend-specific. A mutable pointer is a second write, and a crash between checkpoint and pointer leaves a durable-but-unpointed checkpoint. **Contract needed:** head = the unique checkpoint with no successor, discovered from content-addressed keys, never from timestamps. A pointer, if used, is a cache that a reader must validate (pointer target durable; no successor exists).
2. **Forks.** An append-only journal lets two writers with the same predecessor *both* succeed, so there are two "latest" checkpoints. **Contract needed:** publish is a create-only write on a key derived from the predecessor (e.g. `checkpoints/<pred-hash>/successor`). The first writer wins, and the second gets EXISTS and parks its bytes. That uses the existing C2 create-only semantics on all four backends, not a LocalBackend-journal special case.
3. **Legacy docs as a second authority.** "Compatibility views" must be generated *from* the checkpoint and never read by resume as input. Any state/log write newer than the head checkpoint (old clients, manual edits) is surfaced as `unsealed_changes`, with a `[!]` in `resume_line`, and never merged. Old CLI versions that don't know checkpoints will keep writing state/log, so decide whether new stores refuse legacy writes or only flag them.

## B. Stale predecessor
4. **A retry must not rebase.** If a retry re-reads the head and builds a new predecessor, it produces a different payload for the same milestone. Rule: the **predecessor is part of the payload**, and a retry reuses the original predecessor and bytes. If the head has moved, the result is "conflict/parked", never an automatic rebase. (The proposal says "never retry by silently replacing the expected predecessor", so make it testable with this exact case.)
5. **The parked proposal needs an owner and an exit.** Specify where parked checkpoints live, how resume surfaces them (count plus IDs), and who may resolve them (a new checkpoint that cites the parked hash). Otherwise they are silently ignored forever.

## C. Crash / lost-ack retry
6. **Look up by milestone ID independently of the head.** After a lost reply, the head may already be *past* our checkpoint (another writer committed after it). The retry must find "ID X already durable with identical payload" and return `duplicate:true`, **not** a stale-predecessor conflict. That needs an ID index (create-only key `milestones/<id>` → checkpoint hash), written in the same atomic object or derivable from it. Reusing an ID with a different payload raises an explicit conflict, as proposed.
7. **Receipt semantics.** "Saved" = a durable receipt *and* a read-back of the checkpoint by ID with a verified body hash. A caller that crashed can only learn the outcome by that lookup, so the capture driver must always do lookup-before-retry.

## D. Other pitfalls
8. **Growth and retention.** A full state and log in every checkpoint grows the journal roughly quadratically for chatty sessions. Define retention (keep the chain hashes and the last N bodies) without breaking predecessor verification or export/restore.
9. **Put open questions in the checkpoint as a structured field** (`open_questions[]` with the affected action), not only prose. That is the one place where Q2's ambiguity-preservation failure becomes checkable: a checkpoint cannot mark an item complete while an open question references it. This is not truth, but it is enforceable completeness.
10. **Size and secret gating are atomic.** One oversized or secret-bearing body must refuse the whole checkpoint. Add the test "state clean + log secret → nothing published, ID not reserved" (mirrors PR #44's secret-rejection test).
11. **Clock-free ordering.** Never order checkpoints or detect staleness by `updated` timestamps; chain hashes only.
12. **Test 4 (with/without checkpoint)** also needs a *discriminating fixture* (e.g. an active long-lead row at or above threshold), otherwise both arms produce the same output and the comparison says nothing.

## Suggested acceptance additions to the prototype checks
- A crash **after** the checkpoint durable but **before** the ID index or pointer (if they are separate) must behave like "not yet saved" or "saved", consistently across fresh readers. Keep the original bytes for each crash point.
- Two writers, same predecessor, simultaneous: exactly one successor; the other is parked with its bytes intact (on all 4 backends).
- Lost-ack retry after another writer advanced the head: `duplicate:true`, no conflict.
- Legacy client write after checkpoint: surfaced as unsealed, never read as authority.
