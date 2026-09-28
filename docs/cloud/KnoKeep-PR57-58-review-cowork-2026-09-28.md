# Review: PR57 recovery fix `8e8c52f2330d27b1f5940d604619662809962e04` and PR58 experiment `2cf12292cbb5a8f13280ae38f9b895632ee2d1a2` (Claude Cowork, 2026-09-28)

Isolated clone at q1-hermes-prep\pr44\repo, detached at each exact SHA; the Codex worktrees were not touched. Fixed to these SHAs; the separately reported OverflowError probe is out of scope.

## Verified independently
| Check | Result |
|---|---|
| PR57 scope | vs d75d27e, `store/local.py` adds `_recover_key_before_decision` + `_RecoveryIncomplete`, called under cas.lock at the top of `_write_under_cas_lock`, before any create/CAS/generation/fence decision |
| **My long-lived stale-head repro** (`pr44\stale_head_repro_58.py`, no fresh reader) @2cf1229 | **FIXED.** B's save of m2b → `staged_stale`; a fresh reader's head = **m2a**; `lookup(m2a)` = accepted. (Before: B accepted m2b and m2a became staged.) |
| PR57 targeted tests @8e8c52f | `-k recover/journal/torn/long_lived/unpublished/corrupt`: 16 passed. New tests: long-lived CAS, long-lived create-only, publish failure fails closed, scan stopped before EOF fails closed |
| PR58 checkpoint tests @2cf1229 | **33 passed** |
| Carry-or-resolve | `_question_transition` enforced on save *and* on every chain read: a predecessor question must be carried unchanged or closed via `resolved_questions[{id, decision_ref}]`; a question cannot be both open and closed; closing an unknown question is rejected |
| outcome_unknown | Readback failure after an acknowledged head write → `CheckpointOutcomeUnknown` (CLI status `outcome_unknown`), kept distinct from errors. The head STALE/EXISTS path re-reads after write-time recovery and returns `duplicate:true` for an identical lost-ack retry |
| Lock order | Checkpoint: HEAD advisory lease → `gate.persist` → cas.lock → recovery `_publish` (recovery only republishes the key being written). No reverse acquisition seen |

## Findings
1. **HIGH: one torn journal tail permanently blocks ALL writes (availability brick). Reproduced** (`pr44\torn_tail_repro.py` @8e8c52f):
   - seed `p/a`, then append 11 bytes of a partial record (a crash mid-append);
   - a fresh `LocalBackend` opens fine (`_resume` ignores the torn tail) and reads work;
   - **every later write to any key**, new or CAS, returns `ERROR(CORRUPTION)`.

   Nothing repairs it: there is no repair tool, and reopening doesn't help. Failing closed is right when unparsable bytes are *followed by* further data. A torn **final** partial record at EOF is the normal crash signature and was never acknowledged (fsync hadn't returned). Suggest: under cas.lock (at init and at write-time recovery), if the unparsable region is a partial record that runs to EOF, truncate to the last valid end, then fsync, then log a repair event. Keep fail-closed only for garbage followed by more bytes. Add a regression for "crash mid-append, then next write succeeds".
2. **MEDIUM: long-lived reads/resume stay stale.** In the same repro, long-lived B's `resume()` reported head **m1** while m2a was durable. Write-time recovery protects decisions but not reads, so an agent resuming from a long-lived process can act on an older checkpoint. Either run the same recovery (for HEAD and the chain keys) inside `resume()`/`lookup()`, or document "construct a fresh backend per resume" and enforce it in the CLI/MCP.
3. **MEDIUM: `decision_ref` is an unauthenticated free string.** Any writer can close an inherited question with `decision_ref:"x"` and, in the same checkpoint, mark the affected action complete. This is structurally complete but not authorized. At minimum, bind it to a reference form (checkpoint/proposal hash or external decision ID) and surface closed questions and their refs prominently in `resume`.
4. **LOW: `outcome_unknown` over-reports.** If a head write returned OK but readback fails chain validation (e.g. an old chain violating a newer rule), the head *is* committed. Consider `committed_unreadable`, separate from true unknown.
5. **Accepted limits (unchanged, confirmed in code):** full journal scan and SHA per write (O(journal) per write, quadratic overall); 256-checkpoint cap with no rollover; recovery only for the key being written; no repair tool.

## Verdict
PR57 correctly closes the stale-decision overwrite for writes. It should not merge as-is while finding 1 (a torn tail bricks writes store-wide) stands. PR58's carry-or-resolve and outcome_unknown work as designed at this SHA; findings 2–3 limit what the experiment can claim.
