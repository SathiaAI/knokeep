# Q9-DEVIN-RECOVERY-EXPORT-REVIEWFIX-20260928-A1

- Input: `experiment/recovery-export-v1-a2` at `b41d2b23c4fbbad16900d93d63adb0115c48bfa2` (draft PR71).
- Output: `fix/q9-export-review-a1`.
- Changed files:
  - `experiments/recovery_export_v1/export.py`
  - `experiments/recovery_export_v1/README.md`
  - new `experiments/recovery_export_v1/test_export_review.py`
  - this file
- `store/`, `skill/`, the original A2 `test_export.py` and the workflow are unchanged. The existing workflow already triggers on `fix/q9-**`.
- **Status: UNQUALIFIED experimental module.**

## Gaps from `q9-recovery-export-review.md`, confirmed against the actual code, and what changed
| Gap in b41d2b2 | Change |
|---|---|
| Inventory ran `list(scandir)` before counting entries | Entries are counted as scandir yields them; the budget is enforced mid-iteration |
| Hashing counted `st_size`, not bytes actually read | `_Budget` counts bytes actually read; bytes read ≠ `st_size` gives `REFUSE_STORE_CHANGED_DURING_EXPORT`. The held-lock read is capped at `MAX_TOTAL_BYTES + 1` |
| Copy did not check the bytes it copied | The copy hashes the bytes actually copied and compares them to the pass-1 hash, giving `REFUSE_STORE_CHANGED_DURING_EXPORT` |
| Parser kept latest bodies with no separate cap | `MAX_RETAINED_BODY_BYTES` (32 MiB; replaced bodies are subtracted) plus a key-count cap, giving `REFUSE_BUDGET_EXCEEDED` |
| Verifier manifest read had no bound | `lstat` size check plus a read capped at 16 MiB + 1 → `manifest_too_large` |
| Verifier enumeration and hashing followed links and had no bounds | `lstat`-only walk that rejects `link_or_reparse`, `hardlink` and `special_file`, with depth and entry budgets. It runs **before** any data is read. Bytes read are capped at 2×`MAX_TOTAL_BYTES` |
| An `.incomplete-*` root could verify | Rejected as `incomplete_attempt_root` |
| Malformed field types and IO errors raised exceptions | Full type and shape validation of the manifest. IO errors → `io_error`, journal errors → `journal_unreadable`, budget → `verify_budget_exceeded` |
| Unexpected entries could go unnoticed | Root entries are allowlisted (`root_extra_entry` / `root_entry_unsafe`). Archive and candidate files are reported separately as missing or extra |
| POSIX directory fsync missed intermediate parents, the attempt root after the manifest write, and the output parent after rename | Every created directory is fsynced bottom-up, then the attempt root. Any failure gives `REFUSE_OUTPUT_DURABILITY_FAILED` and the attempt is kept. After the manifest write the attempt root is fsynced again; after rename, the output parent. A failure there gives `EXPORT_DURABILITY_UNCONFIRMED` (CLI exit 1) |
| Windows had no distinct durability status | Windows reports `unsupported_windows`, never success |
| Manifest contained absolute fsync paths | Fsync labels are relative. Verify rejects absolute or unsafe labels (`manifest_absolute_or_unsafe_path`) |

- `VERIFY_OK` now includes `source_authenticated: false`. Verify still recomputes every derived field, so a self-consistent re-signed manifest cannot hide a forged candidate.
- No signing keys were added. Nothing is repaired or truncated, and nothing switches service. Attempts and source bytes are never removed.

## Commands (Linux, Python 3.10.12, nothing installed)
| Command | Exit / result |
|---|---|
| `python3 -m unittest discover -s experiments/recovery_export_v1 -p 'test_*.py' -v` | 0; 38 tests OK (16 original A2 + 22 new), 0 skipped on Linux |
| Same 22 new tests against the b41d2b2 `export.py` (copied to a scratch package) | 10 failures, 19 errors |
| `python3 -m pytest -q tests/test_local_backend.py` | 0; 59 passed, 4 skipped (Windows-only) |
| `pyflakes experiments/recovery_export_v1/` and `flake8 --max-line-length 120 experiments/recovery_export_v1/` | clean |

- Some of the failures and errors against b41d2b2 come from missing new helpers (for example `_write_new_file`, `_EXPORT_SUCCESS_CODES`) rather than from the behaviour itself.
- The behavioural discriminators include:
  - the incomplete-root verify;
  - the malformed-type exceptions;
  - the link read-before-reject;
  - the actual-bytes counter;
  - the source change during copy;
  - the entry counting while scanning;
  - the absolute-path manifest.
- The new tests have no silent skips. One POSIX-only fsync test skips on Windows by design; the Windows durability path is covered by a separate "unsupported" test.

## Remaining gaps (still UNQUALIFIED)
- Windows and macOS were not run here. Hard-link and reparse handling and the held-lock read on Windows are **UNVERIFIED** in this pass.
- The model assumes a cooperative filesystem:
  - there are lstat→open TOCTOU windows in both export and verify;
  - a path swapped for a link between the walk and the open is not caught;
  - `O_NOFOLLOW` / `openat` are not used.
- Directory fsync is best-effort. There is no power-loss guarantee, and a rename is not a durability barrier on every filesystem.
- Empty directories are neither archived nor verified.
- `ATTEMPT.json` stays in completed exports (status "incomplete") and is not covered by the manifest.
- The manifest hash is a self-check, not authentication. A clean parse still has `completeness_proven: false` and `candidate_is_authoritative: false`, and cannot detect a coherent rollback without an external anchor.
- The limits are fixed constants with no override. Stores over 64 MiB, 10,000 entries or 32 MiB of retained history refuse.

---

# A2 follow-up (same branch)

The A1 result above is preserved as recorded.

## Windows run of A1 (from the coordinator; not reproduced here)

- 38 tests: 35 pass, 2 fail, 1 POSIX-only skip.
- Failure 1 was in production code. The candidate hard-link test returned `VERIFY_OK`, because `os.DirEntry.stat()` on Windows does not populate `st_nlink`.
- Failure 2 was in the test only. The output-parent fsync test patched `_WINDOWS=False` and then ran the real `_fsync_dir`, which cannot open a directory on Windows.

## Changes

- **Link counts:** both the source inventory and the verifier walk now read regular-file link counts with a direct `os.lstat(entry.path)` and require `st_nlink == 1`. They also re-check the reparse and regular-file type on that `lstat` result.
  - Independent reproduction on Linux: a `DirEntry` proxy whose `stat()` reports `st_nlink = 0`, as on Windows, makes both new hard-link tests fail against A1 (891ee85) and pass on A2.
- **Fault-injection test (`test_output_parent_fsync_failure_after_rename_unconfirmed`):** this is an A1 test, not an A2 one. It now models every earlier fsync as `fsync_ok` and fails only the output parent, and it never calls the real `_fsync_dir`.
  - The real POSIX durability test (skipped on Windows) and the Windows "unsupported" test are kept separately.
  - No actual POSIX fsync is claimed on Windows.
- **Budget consistency:** the verifier now gives the archive tree and the candidate tree separate `MAX_TREE_ENTRIES` budgets. Previously both shared one budget, so a valid export could fail its own verification.
  - Archive entries are a subset of the source entries.
  - Export now refuses with `REFUSE_BUDGET_EXCEEDED` if the candidate tree (files plus parent directories) would exceed `MAX_TREE_ENTRIES` or `MAX_DEPTH`, or if the manifest would exceed `MAX_MANIFEST_BYTES`.
  - Verify checks bytes separately: archive ≤ `MAX_TOTAL_BYTES`, candidate ≤ `MAX_RETAINED_BODY_BYTES`.
  - The manifest caps on `published_mismatch_keys` and the fsync-result count are 2×`MAX_TREE_ENTRIES` (+1).
- **Malformed manifests:** manifest fields must match exactly, with no unknown or missing top-level keys. `limits` and `durability_notes` have fixed keys and types, and `platform` must be a string. Arbitrary nesting therefore never reaches canonical re-serialization.
  - Only JSON, UTF-8 and budget errors are converted to refusals.
  - Other exceptions still propagate, so genuine bugs are not suppressed.
- **Docs:** the "never follows links" wording in the docstring and README now says this holds only on a cooperative, stable filesystem, which is the known TOCTOU limit. The `ATTEMPT.json` and empty-directory limits stay documented, and there is still no service restore.
- **Test fixture correction:** `test_verify_entry_and_byte_budgets` is also an A1 test. It now patches the entry limit to 6 instead of 3, because at 3 the manifest's own list caps refuse first. The reason is still fail-closed, but it is a different reason.
- The original A2 `test_export.py` is unchanged.

## Commands (Linux, Python 3.10.12, nothing installed)

| Command | Exit / result |
|---|---|
| `python3 -m unittest discover -s experiments/recovery_export_v1 -p 'test_*.py' -v` | 0; 47 tests OK (16 A2 + 22 A1 + 9 new), 0 skipped on Linux |
| New test file against the A1 `export.py` (891ee85, copied to a scratch package) | 10 failures: 2 Windows link-count, 4 budget-consistency, 4 nested-field subtests |
| `python3 -m pytest -q tests/test_local_backend.py` | 0; 59 passed, 4 skipped (Windows-only) |
| `pyflakes` / `flake8 --max-line-length 120` on `experiments/recovery_export_v1/` | clean |

- `test_malformed_encodings_fail_closed` and the lone-surrogate test already passed on A1. They are kept as regression guards and are not claimed as discriminators.

## Remaining (still UNQUALIFIED)

- **Windows is not rerun here.** The link-count fix is verified only through a simulated `DirEntry`. Real Windows behaviour of `os.lstat().st_nlink` and of the reparse checks is UNVERIFIED.
- All of the A1 remaining gaps still apply:
  - TOCTOU under a cooperative-filesystem assumption;
  - no power-loss guarantee;
  - `ATTEMPT.json` not covered by the manifest;
  - empty directories ignored;
  - fixed limits;
  - the manifest hash is not authentication;
  - completeness is not proven.
