# Q7 recovery design — correction A2

- Input for A1 and A2: `integration/q5-reviewed-a1` at `380fe31e592b1aade88f7fcc7351455b1d4191af`. The A1 branch commit `521400dbd7a4e0a40f1245adf4344fcb787afd7b` is preserved. A2 is a new commit on top of it; no history was rewritten.
- Changes:
  - Added `recovery_probe_a2.py` and `probe_results_A2_linux.txt`.
  - Added a correction notice to the top of `REPORT.md`.
  - A1's `recovery_probe.py` and `probe_results.txt` are unchanged.
  - Product code and tests are unchanged.
- **Status: design evidence only, NOT shipping.** The product still has no recovery or service-restoration command. A damaged journal still fails writes closed with CORRUPTION, and nobody should restore service from these probes.

## Commands and results
| Command | Result |
|---|---|
| `python3 docs/cloud/evidence/Q7-DEVIN-RECOVERY-DESIGN-20260928-A1/recovery_probe_a2.py` (Linux, Python 3.10.12) | 24/24 PASS, 0 SKIP, `RESULT OK`, exit 0 (`probe_results_A2_linux.txt`) |
| `python3 -m pytest -q tests/test_local_backend.py` | 46 passed, 4 skipped (Windows-only), exit 0 |
| Windows run of `recovery_probe_a2.py` | **UNVERIFIED.** No Windows host was available here. The Windows lock fix below has only been reasoned about, not run. Do not treat the Linux PASS as a Windows PASS. |

## A2 fixes
1. **Windows lock read (the reported A1 failure).**
   - The bytes of `locks/cas.lock` are now read through the **same handle that holds the lock**: `seek(0); read(); seek(0)`, in `read_via_held`. The file is not reopened.
   - `msvcrt.locking` locks byte 0 for that handle, so reads through the holder should be allowed. That is my inference; it is UNVERIFIED on Windows.
   - If it works, the snapshot of every regular file, including all bytes of `cas.lock`, is exact for the moment of the inventory.
   - If Windows still denies the read, the design must refuse (`REFUSE_*`), not skip the bytes. The probe currently raises instead of mapping that error to a verdict, which is a known gap.
   - Not attainable either way: an atomic point-in-time snapshot across files. The lock excludes only cooperating writers. The two hashed inventory passes narrow the race but do not close it.
2. **Output/store overlap.**
   - Before any output is created, the probe applies `realpath` to both paths, then `normcase`, then checks that neither contains the other.
   - This refuses:
     - output equal to the store,
     - output inside the store,
     - the store inside the output,
     - a symlink alias that resolves into the store's `data/`.
   - The fixture tree hash was unchanged in every case.
   - Not covered: Windows junctions, substituted drives and UNC or 8.3 aliases (UNVERIFIED).
3. **No automatic deletion.**
   - Each attempt gets a unique `.incomplete-recovery-candidate-<sha12>-<random64>` directory, created exclusively with `os.mkdir`, containing an `ATTEMPT.json`. Files inside it are created with mode `"xb"`.
   - The probe never runs `rmtree`. The test puts a foreign file inside the interrupted attempt, and the rerun leaves both the attempt and that file byte-identical.
   - An existing final candidate gives `REFUSE_OUTPUT_EXISTS`.
4. **No completeness claim.**
   - The best verdict is now `SYNTACTICALLY_CONSISTENT_COMPLETENESS_UNPROVEN`, and the manifest always contains `completeness_proven: false` and `candidate_is_authoritative: false`.
   - The `boundary-truncation-coherent-rollback` fixture shows why: the journal is cut at a record boundary and the published data is rolled back to match. It is indistinguishable from an honest store.
   - Loss is detected only when the published data happens to disagree (`boundary-truncation-published-hint`). Proving completeness needs an external anchor, for example a recorded journal length plus hash, or a client-side generation receipt.
5. **Keys.**
   - Keys are validated with `store.gate._valid_key_shape`. Keys like `.`, `a/./b`, `a/b.` and `../escape` are listed and not materialized.
   - Keys that differ only by case (for example `a/Readme` and `a/README`) are listed as `CASE_FOLD_COLLISION_NOT_MATERIALIZED`, and neither is written. The test ran on case-sensitive Linux; the NTFS or APFS behaviour is UNVERIFIED.
6. **Budgets and links.**
   - The probe refuses with `REFUSE_BUDGET_EXCEEDED` if the total inventory exceeds 64 MiB or the unparsed tail exceeds 1 MiB. This caps the O(tail^2) hashing in the resync scan.
   - Symlinks, special files and regular files with `st_nlink > 1` (hard links) are refused.

## Still NOT fit to ship (the probe must not be productized as written)
- It loads the whole inventory into RAM twice, within the budget, instead of streaming.
- The resync scan is quadratic in the worst case: every offset re-hashes the claimed body. It is bounded only by the budget.
- Output files and directories are never fsynced, so a crash during export can leave a partial candidate. The final rename is not durable.
- There is a race between the `lstat`/`realpath` checks and the reads, so a hostile local process can swap paths. Hard-link detection covers only `st_nlink`. Windows reparse points and junctions are unhandled.
- This is not a security sandbox and makes no universal safety claim.

## Minimum recovery plan (unchanged in intent; wording corrected)
- Any future tool must stay offline and read-only toward the store, and only ever create a separately named candidate that is marked non-authoritative.
- Service restoration and history completeness are separate human decisions. With these bytes alone, completeness can never be proven.
- Before that tool ships, it needs: streaming I/O with fsync; the budgets above; a Windows CI run of this probe covering the lock read, overlap aliases, case collision and junctions; and an external durability anchor if anyone wants a completeness claim.
- Residual business decisions (unchanged from A1): accept prefix loss, prefer the published value, or rebuild from external sources, each signed off per key.
