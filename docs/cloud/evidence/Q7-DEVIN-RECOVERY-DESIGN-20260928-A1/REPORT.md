# Q7-DEVIN-RECOVERY-DESIGN-20260928-A1 — conservative offline crash-tail recovery workflow

- Input: `integration/q5-reviewed-a1` at exact HEAD `380fe31e592b1aade88f7fcc7351455b1d4191af` (verified with `git rev-parse HEAD` after fetch).
- Output branch: `q7/devin-recovery-design-a1`. The only files added are in this directory. `store/`, `tests/` and `skill/` are unchanged (`git diff --quiet HEAD -- store tests skill` → unchanged).
- Scope: this is a design plus a synthetic probe. It is not a product repair tool. It does not duplicate Cursor's fixes for the parent read/list prepublish-crash bug or the constructor prefix-replay bug.

## Commands and results (Linux, Python 3.10.12, pytest was already present)
| Command | Result |
|---|---|
| `git fetch origin integration/q5-reviewed-a1 && git checkout -B q7/devin-recovery-design-a1 FETCH_HEAD && git rev-parse HEAD` | `380fe31e…` |
| `python3 docs/cloud/evidence/Q7-DEVIN-RECOVERY-DESIGN-20260928-A1/recovery_probe.py` | 14/14 PASS, `RESULT OK`, exit 0 (`probe_results.txt`) |
| `python3 -m pytest -q tests/test_local_backend.py` | 46 passed, 4 skipped (Windows-only paths), exit 0 |
| Windows run of the probe | **UNVERIFIED**, because no Windows host was available in this task. The `msvcrt` branch and the symlink SKIP path have not been executed. |

## The workflow in plain terms (for a non-coder founder)
Recovery happens in two separate stages. Do not merge them.
- **Service restoration** gets the store accepting writes again.
- **Complete history recovery** proves that nothing committed was lost.

The tool can show you that nothing was lost only when the journal is clean. When the journal is damaged, it hands you evidence and a proposed copy, and a person decides.

1. **Stop and freeze.** Stop every KnoKeep process that uses the store. The tool then takes the same non-blocking exclusive lock on the existing `locks/cas.lock` that startup and `write()` use (`flock`, or `msvcrt.locking` on Windows). If the lock is held, it refuses with `REFUSE_BUSY_WRITER_ACTIVE`. It never creates or writes the lock file. A lock file that is missing or is a symlink gives `REFUSE_NOT_A_STORE`.
2. **Take a full inventory.** It walks the store with `lstat` and never follows links. Any symlink or special file gives `REFUSE_SYMLINK_OR_SPECIAL`, and nothing is copied. A missing journal gives `REFUSE_JOURNAL_MISSING`: the tool never makes an empty one and never treats published data alone as the complete history.
3. **Check that nothing moved.** It inventories a second time and compares SHA-256 hashes. Any difference, such as a journal that grew because some writer ignored the lock, gives `REFUSE_STORE_CHANGED_DURING_EXPORT`, and the output directory stays empty.
4. **Archive the originals.** Every byte of `journal/`, `data/`, `locks/` and `staging/` is copied into `<out>/.incomplete-recovery-candidate-<journal-sha12>/archive/`. Each copy is re-hashed against the manifest. An export that is interrupted leaves only the `.incomplete-*` directory. That directory is never trusted and is rebuilt on the next run. The finished directory is named `recovery-candidate-<sha12>` and appears only through one final `os.replace`. If it already exists, the tool refuses with `REFUSE_OUTPUT_EXISTS` instead of overwriting.
5. **Classify the journal.**
   - *Readable prefix:* the records the current parser accepts, using the same bounds as `store/local.py`. The probe checks this against `LocalBackend._iter_journal_records` on every fixture, without constructing a backend.
   - *Unparsed range:* `[prefix_end, size)`.
   - *Ambiguous later frames:* every offset after the stop point that parses as a digest-valid frame, with overlaps listed. Each one is reported as **ambiguous**, never as recovered, because it could be a real later record or just bytes inside a damaged record's body.
6. **Compare with the published data.** Every file in `data/` whose hash differs from the latest prefix record for its key, or that has no prefix record at all, is reported in `published_mismatch`. It is kept in the archive and never overwritten. Published data newer than the prefix means the prefix is not the whole history.
7. **Build a separate, labelled candidate.** `candidate-data/` holds the latest prefix value for each key. Keys that fail the gate charset or contain `..` segments are listed and not materialized. `MANIFEST.json` records `candidate_is_authoritative: false` and one of two verdicts:
   - `CLEAN_NO_RECOVERY_NEEDED`: the whole file parsed and every published file matches.
   - `INCOMPLETE_HUMAN_DECISION_REQUIRED`: everything else.
8. **Human decision, outside the tool.** Nothing switches over to the candidate automatically. The tool never truncates, rewrites or renames the real store.

## Adversarial cases (all in `recovery_probe.py`; input bytes are hash-checked unchanged after each case)
| Case | Verdict / findings |
|---|---|
| clean mixed legacy + v2 | CLEAN_NO_RECOVERY_NEEDED |
| final partial legacy frame | INCOMPLETE; TAIL_UNPARSABLE_NO_LATER_FRAME_FOUND |
| final v2 frame torn inside the body | INCOMPLETE; TAIL_UNPARSABLE_NO_LATER_FRAME_FOUND |
| **final v2 frame torn inside the 8-byte fence trailer** | INCOMPLETE; **TAIL_DAMAGED_AMBIGUOUS**: at offset+4 the torn v2 frame is a *digest-valid legacy frame* (key, body and digest are all intact). A naive resync would "recover" it with fence 0. The design reports it as ambiguous. |
| damaged length covering a later valid frame | INCOMPLETE; ambiguous hits at the v2 offset and again at +4 (legacy view), overlapping; published data differs |
| valid-looking frame (with its magic and digest) inside a later record's body, after damage | INCOMPLETE; four overlapping ambiguous hits (the outer record and the forged inner one, each seen as v2 and as legacy). Nothing is promoted. |
| published data newer than the prefix | INCOMPLETE; `published_mismatch` = the key |
| `../escape` key in the journal | INCOMPLETE; INVALID_KEYS_NOT_MATERIALIZED; nothing written outside the output directory |
| missing journal | REFUSE_JOURNAL_MISSING |
| concurrent writer holding cas.lock | REFUSE_BUSY_WRITER_ACTIVE |
| journal growth during export (a writer that ignores the lock) | REFUSE_STORE_CHANGED_DURING_EXPORT; output directory empty |
| export interrupted mid-copy, then rerun | only `.incomplete-*` is left; the rerun produces the full candidate; a third run gives REFUSE_OUTPUT_EXISTS |
| symlink under `data/` pointing outside | REFUSE_SYMLINK_OR_SPECIAL; the target is untouched; nothing copied (SKIP if the OS cannot create symlinks) |

## Recommended smallest useful implementation
- Ship **one offline read-only command**, `knokeep recover-export <store> <out-parent>`, that follows steps 1–7 exactly. Leave out any in-place repair, auto-truncate, and "use candidate" switch.
- Leave the `write()` fail-closed behaviour (`_RecoveryIncomplete` → CORRUPTION) as it is. Service is restored only when a person deliberately stands up a **new** store from a candidate they approved, and the original stays archived.
- Go criteria:
  - The probe cases above run as pytest on Linux and Windows CI, including the `msvcrt` lock path and a symlink or junction fixture where the OS allows it.
  - The input tree hash is identical before and after every case.
  - `candidate_is_authoritative` is always false.
  - `REFUSE_*` whenever the lock or inventory stability cannot be established.
  - The prefix end matches `_iter_journal_records` on every fixture.
- No-go if any of these happen:
  - the tool writes inside `<store>`
  - it follows a link
  - it labels a damaged journal as complete
  - it materializes an ambiguous frame or an invalid key as data
  - it overwrites an existing candidate

## Residual decisions and limits (human or business, not code)
- **Accepting an incomplete history.** If the tail is damaged or published data differs, nobody can prove completeness from these bytes. The owner must choose one of:
  - (a) restore service from the prefix and accept possible loss of later writes,
  - (b) prefer the published `data/` value for the mismatched keys,
  - (c) rebuild from external sources such as clients or git history.

  Each choice needs a written sign-off naming the keys involved.
- **Ambiguous frames need manual review.** They could be re-journaled into a new store only after a person approves each key. Their fence value cannot be trusted (see the torn-fence finding above).
- **Out of scope:**
  - This is not a security sandbox. It is hardened against symlinks and traversal in the fixtures only.
  - Between the `lstat` check and `read_bytes`, a hostile local process could swap a file. The lock and the double inventory reduce this risk but do not remove it.
  - Hard links and Windows reparse points other than symlinks are **UNVERIFIED**.
- **A writer that ignores cas.lock** (for example an older build or a manual edit) is caught only if it changes bytes between the two inventory passes. It is not excluded entirely.
- **Resync cost** is O(n) frame attempts per byte offset of the unparsed tail. This is fine for synthetic fixtures. For multi-GB journals it needs a cap or a streaming design, which is **UNVERIFIED**.
- **Advisory and fence-alloc sidecars** are archived but not interpreted. Fence continuity in any new store is an open design decision.
