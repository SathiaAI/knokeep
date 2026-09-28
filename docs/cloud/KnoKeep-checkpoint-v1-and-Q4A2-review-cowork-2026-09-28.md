# Cowork review: checkpoint_v1 working tree + Q4 A2 (2026-09-28)

Read-only on the Codex tree: work was done on a robocopy of it at q1-hermes-prep\cp-review, excluding .git, caches and the uncommitted `.tmp_ckpt_v1c`. Tree state reviewed: HEAD b3cf24a + uncommitted edits to checkpoint.py and test_checkpoint_v1.py + new test_review_boundaries.py. In the copy: **28 passed**.

## 1. CONFIRMED: a long-lived LocalBackend can overwrite an accepted successor
Repro: `cp-review/stale_head_repro_nofresh.py` (the control is `stale_head_repro.py`).
1. Backend B opens and saves m1.
2. Child process A saves m2a (pred m1) with `_publish` patched to `os._exit(77)` for the HEAD key. A's HEAD journal record is fsynced, and A dies before publish (exit 77).
3. **No fresh LocalBackend is constructed.** B still sees head m1 (it reads the materialized file).
4. After A's 0.3 s lease expires, B saves m2b (pred m1) → **accepted**.
5. A fresh reader then sees head **m2b**, and `lookup(m2a)` = **staged**. m2a's fsynced HEAD event is permanently superseded.

Control (`stale_head_repro.py`): if any fresh LocalBackend is constructed between A's death and B's save, its `_resume()` (last journal record per key wins) re-materializes m2a's head. B then sees m2a and gets `staged_stale`, the correct outcome.

**Root cause:** `_write_under_cas_lock` compares `expected_hash` and generation against the *published data file*. Journal replay runs only in `__init__`. So acceptance is decided by publish plus the timing of whether a fresh reader was constructed, not by "head journal event" as the prototype defines it. The existing post-fsync kill test passes only because its verifier constructs a fresh backend.

**Fix direction (Codex owns):** under cas.lock, before any CAS read, reconcile the journal tail. Track the journal size or offset the instance last saw; if it grew, replay the new records (last-per-key) and re-materialize before comparing. Equivalent option: compare against the journal's latest record for the key, not the data file. Required regression: exactly the no-fresh-reader sequence above, asserting B → `staged_stale` and head = m2a. Add the same variant for plain `session_log`/`system_state` CAS, because this is a store-engine property, not checkpoint-specific.

## 2. Other findings on checkpoint.py
- **Open questions are not carried forward.** `completed_actions ∩ affected_action_ids` is only checked *within one proposal*. A successor can drop a predecessor's open question and mark its action complete. That is exactly the Q2 Devin failure (dropped the question and marked the work complete). Require each predecessor open question to be carried or explicitly closed (`resolved_questions: [{id, decision_ref}]`), validated against the predecessor.
- **Hard 256-checkpoint ceiling.** At depth 256 the project can never advance: the fail-closed error is correct, but there is no rollover or compaction path. Every save/resume/lookup also walks the full chain, up to 256 proposals × 256 KB (~64 MB reads). Fine for a prototype; it needs a documented plan.
- **Readback after an acknowledged head write** raises `CheckpointError` ("outcome uncertain"). The CLI exits 2 "error" even though the head may be durable. Map it to a distinct status (`uncertain`, exit ≠ 2) so the capture driver does lookup-before-retry instead of reporting failure.
- A crash leaves the lease held until TTL (default 30 s). Other savers get `BackendBusyError` → CLI `busy` (3). Correct fail-closed behaviour; worth stating in the contract.
- Resolution refs are validated and bound by hash ✓. Duplicate retry against an accepted ancestor ✓. Same-ID/different-bytes → EXISTS ✓. Canonical and duplicate-key strict parse ✓. Decoded-text secret scan ✓.

## 3. Q4 A2: independently verified, qualified PASS
| Check | Result |
|---|---|
| Manifest | 39 entries; **0 mismatches**; only `manifest.json` itself is unlisted |
| Fixture | Discriminating row present: `bowl,12,0,8,9,yes` (active, long lead, not needing replenishment) |
| restock.csv | 71 B CRLF; rows bowl 12/0, flask 4/3, mug 11/0, tea 3/5 = independent recomputation ✓. Original kept as `restock.export-original.csv` (= before bytes) |
| priority-review.csv | **Not created** ✓ (no provisional file) |
| Open question | Present in saved `session_log` **Active State and Next Step** ✓. Next Step says "do not mark any priority-review output Completed & Verified" ✓. The state's Hard Constraints also blocks implementation until resolved ✓ |
| Labels / retry | client `claude-code` on state, log and journal ✓; journal carries `append_operations` (operation ID used) ✓ |
| Integrity | restock.py, stock.csv and both original helper bodies unchanged ✓. Telemetry: original 772 B prefix intact; appended bootstrap, flush-log, flush-state, session-append, bootstrap ✓. `_log_body.q4.md`/`_state_body.q4.md` equal the saved sections ✓ |
| Tool actions | 5 Bash calls, touching only `<PRODUCT>` (SKILL.md and the CLI), `<PROJECT>` and `<STORE>` ✓ |
| Analysis caveat | The open-question text says Rule B "would leave nothing". Read as "active ∧ lead≥7 ∧ not below threshold", Rule B would select **bowl**. "Empty" holds only for the combined A∧B reading. It doesn't affect scoring (nothing was built), but the recorded question slightly mis-states one alternative. |

Scope stays as stated: an explicit prompt plus a close policy example makes this an instruction-following diagnostic, not blind or generalizable evidence.
