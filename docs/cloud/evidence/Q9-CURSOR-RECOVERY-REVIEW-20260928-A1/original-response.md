## Scope

Reviewed **product** recovery/locking/parser at `3821cdc` (`store/local.py`) and **design evidence** at `22e1e859` on `q7/devin-recovery-design-a1` (fetched, not checked out). Re-ran `recovery_probe_a2.py` from a temp tree against this workspace’s `store` — **24/24 PASS, Linux only** (design doc’s Windows matrix remains **UNVERIFIED** here).

---

## Product baseline (what recovery export must not contradict)

`LocalBackend` treats the journal as durability truth and **fails closed** when replay cannot reach EOF:

```457:464:store/local.py
    def _journal_scan_complete(scan_state: Dict[str, int]) -> bool:
        """True only when the parser consumed journal.log to EOF (end == size).
        ...
        return scan_state.get("end", 0) == scan_state.get("size", 0)
```

```572:582:store/local.py
    def _resume(self) -> None:
        ...
        if not self._journal_scan_complete(scan_state):
            self._journal_ambiguous = True
            return
        ...
        self._materialize_from_journal(latest)
```

```600:611:store/local.py
        ...
        if not self._journal_scan_complete(scan_state):
            raise _RecoveryIncomplete()
```

Ambiguous journal → no startup materialization, read/list/write corruption path; staging kept when ambiguous (`__init__` ~417–419). Parser matches length-prefixed legacy + `KKJ2` v2 records, stops on digest mismatch or overrun (`_iter_journal_records` ~514–570). Publish path is journal fsync → staged file fsync → `os.replace` → **POSIX parent-dir fsync** (`_fsync_dir` is **no-op on Windows** ~287–295).

Cooperating writers are excluded via `cas.lock` (`_FileLock` ~218–276); export design correctly assumes **no global snapshot** across files.

---

## A2 design (what it proves vs what it is not)

The probe is **offline, read-only on the store**, copies a **byte archive**, builds a **non-authoritative prefix-derived `candidate-data`**, labels best case `SYNTACTICALLY_CONSISTENT_COMPLETENESS_UNPROVEN`, and never truncates the source. A2 fixes A1’s Windows lock read pattern (`read_via_held` on the held handle, ~100–116 in probe). Overlap refusal uses `realpath` + `normcase` (~125–130). Incomplete attempts are unique `os.mkdir` + `"xb"` writes, no `rmtree` (~169–175).

**Not shipping** (stated in `REPORT_A2.md` and embodied in code): full-RAM double inventory, no output fsync, `os.rename` finalization without directory durability, TOCTOU, no junction/reparse/UNC/8.3 coverage, quadratic tail resync capped at 1 MiB.

---

## Concrete design flaws (minimum next experiment must address)

| Area | Flaw |
|------|------|
| **Publication durability** | Files and dirs written without `fsync`; final `os.rename(attempt, final)` (~193) can leave a **visible final name** with torn contents after power loss, or an **orphan incomplete** tree — opposite failure modes, neither crash-safe. Product already fsyncs blob + journal; export does not. |
| **Atomic vs durable** | Rename is atomic for name visibility, not **durability** without fsync(file) + fsync(parent dir). On Windows, product explicitly skips dir fsync (`_fsync_dir`); export inherits that gap **UNVERIFIED** for NTFS metadata persistence. |
| **Windows `cas.lock`** | Held-handle read is plausible (`msvcrt` byte-0 lock) but **UNVERIFIED**; probe still **raises** on read failure instead of `REFUSE_LOCK_UNREADABLE` (~100–116). |
| **Snapshot semantics** | Two full-tree SHA passes (~139–146) bound cooperative drift only; hostile TOCTOU (swap path after `realpath`, reparse/junction aliases) remains; overlap check does not cover junction/subst/UNC (**UNVERIFIED** on Windows). |
| **Path rules** | Refuses symlinks + `st_nlink>1` (~110–111); does not classify **reparse points** or directory junctions; case-fold collision listing is correct on Linux probe; **NTFS/APFS case behavior UNVERIFIED**. |
| **Tail analysis** | `resync` scans every offset with re-hash (~73–80) — **O(tail²)** bounded at 1 MiB tail / 64 MiB store; not true streaming; can refuse large tails without producing partial metadata. |
| **Parser drift risk** | Probe embeds `parse_at`/`strict_prefix` (~44–70) parallel to `LocalBackend._iter_journal_records`; one self-test compares `product_prefix_end` (~219–222). Shipping tool should **call product parser** or share one module — drift is a correctness bug. |
| **Partial export UX** | Crash mid-copy leaves `.incomplete-*` with `ATTEMPT.json` `status: incomplete` (~171) — good preservation, but nothing prevents a human/tool from treating `archive/` as complete without reading manifest flags. |
| **Completeness / rollback** | `boundary-truncation-coherent-rollback` case (~243) shows **undetectable prefix loss**; design correctly refuses `completeness_proven`; any “approve candidate” flow needs **external anchor** (journal length+hash at backup time, generation receipts, etc.). |
| **Verification without “believing the model”** | In-export checks: dual inventory hash, per-file manifest re-read (~178–179), `published_mismatch`, explicit `candidate_is_authoritative: false`. **Missing:** signed manifest, hash of manifest file, export timestamp bound to archive, and a **deterministic verifier** (stdlib script) that replays checks offline — verifier must not re-parse tail optimistically beyond product rules. |

---

## Ranked requirements (next experimental offline recovery-export)

**P0 — safety & store immutability**

1. Open store **read-only** where the OS allows; never mutate `journal/`, `data/`, `locks/`, `staging/`.
2. Refuse if `cas.lock` not acquirable (`REFUSE_BUSY`) or unreadable through held handle (`REFUSE_LOCK_UNREADABLE`).
3. Refuse output/store overlap after `realpath` + `normcase`; extend with **Windows junction/reparse probes** (acceptance tests required — **UNVERIFIED** here).
4. Refuse symlinks, non-regular files, hard links (`st_nlink>1`), and invalid keys; do not materialize case-fold collisions (mirror `gate._valid_key_shape` + product `_check_case_collision` intent).
5. Unique incomplete attempts; never delete or overwrite prior attempts; `REFUSE_OUTPUT_EXISTS` for final name.

**P1 — semantic alignment with product**

6. Prefix replay **identical stop position** to `LocalBackend._iter_journal_records` + `_journal_scan_complete` (shared code path).
7. Archive **exact bytes** of every regular file including full `locks/cas.lock` via held handle.
8. Candidate blobs = latest **strict-prefix** record per key only; never auto-truncate/repair journal; surface `TAIL_*`, `ambiguous_later_frames`, `PUBLISHED_DATA_DIFFERS_FROM_PREFIX`, invalid/case keys as **uncertain regions** in machine-readable report.
9. Verdict never claims completeness or authority (`completeness_proven: false`, `candidate_is_authoritative: false` always).

**P2 — operability & durability**

10. **Streaming** inventory with byte budgets (store total, tail resync work); refuse `REFUSE_BUDGET_EXCEEDED` without loading 64 MiB twice.
11. **Durable publication**: write under incomplete dir → fsync each file → fsync incomplete dir → atomic rename to final → fsync output parent (define Windows behavior explicitly; document if parent fsync unavailable).
12. Bounded partial failure: interrupt leaves only incomplete dir; reruns create new incomplete; verifier rejects incomplete `ATTEMPT.json`.
13. Ship **offline verifier**: inputs = `MANIFEST.json` + tree; outputs = pass/fail on manifest hashes, flags, and prefix replay using product parser — no LLM, no “trust the exporter.”

---

## Minimal acceptance criteria

**Linux (CI on ext4 or similar)**

- All 24 A2 probe scenarios PASS (replicated here on current `local.py`).
- Parser stop offset matches `LocalBackend._iter_journal_records` on randomized tails (property or fuzz subset).
- Simulated power loss after export: verifier rejects torn final or accepts only fully fsynced publication (test via subprocess + mount options if available; else **UNVERIFIED** for real power loss).
- Growth during export → `REFUSE_STORE_CHANGED_DURING_EXPORT`, no output dir created.
- Interrupted export → prior `.incomplete-*` byte-preserved; second run adds new attempt + new final or `REFUSE_OUTPUT_EXISTS`.

**Windows (mandatory before any “experimental” label beyond design)**

- Held-lock read of `cas.lock` matches full file hash; second-handle read must not be used.
- Concurrent holder → `REFUSE_BUSY_WRITER_ACTIVE`.
- Overlap: store inside output, output inside store, symlink alias into `data/` (A2); plus **junction** from output into store (**UNVERIFIED** until run).
- Case-fold collision keys on case-insensitive volume: neither materialized, finding recorded (**UNVERIFIED** here).
- `os.replace`/rename + directory durability behavior documented with at least one crash/inject test (**UNVERIFIED** here).

---

## Gaps vs coordinator review

`COORDINATOR_REVIEW.md` at the fetched commit notes a **fixture ordering fix** for Windows concurrent-writer comparison (not in vanilla A2 runner). Treat **24 Linux + 24 Windows (coordinator runner)** as the evidence bar; do not equate “Linux PASS on current branch” with Windows sign-off.

---

## Git state (unchanged)

```
On branch q9/cursor-recovery-review-a1
nothing to commit, working tree clean
HEAD: 3821cdc048f593725e25ba1b86045d84c525179c
```

(Input commit verified; no tracked-file changes made.)