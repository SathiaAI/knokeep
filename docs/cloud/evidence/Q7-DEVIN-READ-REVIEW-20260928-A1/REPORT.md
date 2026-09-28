# Q7-DEVIN-READ-REVIEW-20260928-A1 — independent review of PR66 final commit

- Input: `fix/q7-read-freshness-reviewed` at `9cd040c943219d7bd65133b6f9c7de5c55561bb6`. Its parent, `db225c80637cbbaed154310ecb4d89365fc1491c`, is confirmed with `git rev-parse HEAD HEAD^`.
- Evidence branch: `q7/devin-read-review-a1`, created from that head. The only file added is in this directory; product code and existing tests are unchanged.
- Reviewed diff, `git diff HEAD^ HEAD`:
  - `store/local.py`:
    - `_scavenge_staging()` is skipped when `_journal_ambiguous` is set.
    - `_recover_key_before_decision` keeps only the target key's record and inlines the orphan check (`latest is None and data_path.is_file()`).
  - `tests/test_local_backend.py`: adds `test_ambiguous_startup_preserves_aged_staging_evidence`.
  - Evidence files under `docs/cloud/evidence/Q7-READ-FRESHNESS-20260928/`.

## Commands and results (Linux, Python 3.10.12, nothing installed)
| Command | Result |
|---|---|
| `python3 -m pytest -q tests/test_local_backend.py` | 59 passed, 4 skipped (Windows-only), exit 0 |
| `python3 -m pytest -q docs/cloud/evidence/Q7-READ-FRESHNESS-20260928/test_q7_read_adversarial.py` | collection error `KeyError: 'KNOKEEP_REVIEW_ROOT'`, exit 2. It needs an environment variable I did not set, so this is **UNVERIFIED** and was not run. |
| `python3 docs/cloud/evidence/Q7-DEVIN-READ-REVIEW-20260928-A1/probe_read_review.py` | 4/4 PASS, exit 0 (`probe_results.txt`) |

## Independent probes (`probe_read_review.py`)
1. **Ambiguous journal keeps staging.** I appended a legacy-format tail to the journal (the parent commit's test uses a `KKJ2` tail). On restart the backend reports the journal as ambiguous, the aged staged file is kept, `read()` raises `BackendCorruptionError`, and the journal bytes are unchanged.
2. **Clean store still clears staging.** An aged staged file is removed on restart of a clean store, so the scavenger still works.
3. **Orphan published file, empty journal.** Writing to that key returns `ERROR(CORRUPTION)` and the orphan's bytes are kept. A write to an unrelated key returns OK, so fail-closed is per key; before this commit the check was also per key.
4. **Child save made durable before publish, parent writes first.** The child journals `p/k` but never publishes it. The parent's **first** operation after that is a write, not a read. It returns `EXISTS` against the child's hash, so the child's record was replayed before the decision and the defect is not masked by an earlier read. A later `read()` returns the child's body.

## Assessment
- **Keeping only the target key's record** gives the same result as before: a key counts as an orphan only if it has no journal record and a published file exists. The other keys' records were never used for this decision. Memory use is now O(1) records instead of O(keys). I found no defect.
- **Skipping the scavenger** only happens at startup under `cas.lock`, when `_resume` has marked the journal ambiguous. Since `read`/`list` already fail closed in that state, keeping the staged files loses nothing.
- **Limits:**
  - Staged files are never cleared while the journal stays ambiguous. There is no bound on their growth and no documented review step to release them.
  - `_journal_ambiguous` is only computed at startup (`_resume`). I did not check whether damage appearing during runtime switches read/list into the ambiguous state (**UNVERIFIED**).
- Not reviewed in depth: the surrounding Cursor read/list fix beyond the lines above, Windows behaviour, and the coordinator's JSON evidence files (not relied on).
