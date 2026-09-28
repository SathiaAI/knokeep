# Q5-DEVIN-FRAMING-REVIEW-20260928-A1 — independent review of journal framing bounds

- Branch under review: `fix/journal-framing-bounds`
- Starting HEAD (verified): `263ebe7a622d7421ba34b26568b464e9c2363279` ("Reject impossible journal lengths before reading framed payloads")
- Parent/base (verified `HEAD^`): `8e8c52f2330d27b1f5940d604619662809962e04`
- Evidence branch: `q5/devin-framing-review-a1`, created from the exact head above. Final SHA: the commit adding this directory (only files under this directory are added; `store/` and `tests/` are byte-identical to 263ebe7, checked with `git diff --quiet 263ebe7 -- store tests`).
- Reviewed diff: `git diff 8e8c52f 263ebe7` → `store/local.py` (+13/-1), `tests/test_local_backend.py` (+68). main was not reviewed.

## Verdict
No defect found in the patch within the bounded review. The new checks reject every impossible length I built before it reaches `read()`, valid legacy/KKJ2 records still replay, and the journal bytes are never changed. The unfixed base fails 6 of the same cases (unbounded `read(n)` with n up to 2^64-1).

## Commands and exit results (Python 3.10.12, Linux; pytest installed with pip, no other deps)
| Command | Result |
|---|---|
| `git fetch origin fix/journal-framing-bounds && git checkout -B q5/devin-framing-review-a1 FETCH_HEAD; git rev-parse HEAD HEAD^` | 263ebe7…, 8e8c52f… (exit 0) |
| `python3 -m pytest -q tests/test_local_backend.py` at 263ebe7 | 46 passed, 4 skipped (Windows-only), exit 0 |
| `python3 docs/cloud/evidence/Q5-DEVIN-FRAMING-REVIEW-20260928-A1/synthetic_framing_probe.py` at 263ebe7 `store/local.py` | 17/17 PASS, exit 0 (`probe_head.txt`) |
| Same probe with `store/local.py` temporarily replaced by `git show 8e8c52f:store/local.py` (restored afterwards) | 6 FAILED, exit 1 (`probe_base_8e8c52f.txt`) |
| `python3 -m pytest -q conformance` | collection error: `ModuleNotFoundError: boto3`, exit 2 → UNVERIFIED (not installed, to stay in scope) |

## Probe design (`synthetic_framing_probe.py`)
- Builds a journal: a valid legacy record + a valid KKJ2 record (fence 7), then a small synthetic tail (a few bytes to ~1.1 KB). It never creates a huge file or buffer.
- Wraps the journal file handle's `read(n)` with a spy that raises if `n` is negative or larger than the file size. This makes UINT32/UINT64 claims safe to test even against the unfixed parser.
- For each case it checks: the number of records yielded, the prefix records (and the KKJ2 fence) intact, `_scan_state` end==size only for clean files, and **the journal bytes are identical after the scan**.

Cases and outcomes (head / base):
- UINT32 key_len (legacy, KKJ2): rejected, yielded 2 / base `read(4294967295)`
- key_len 1025 (legacy): rejected / base reads the key, then `read(7740398493674204011)`
- key_len 0: rejected / base also stops (digest mismatch), so no behaviour change for gated data
- UINT64 body_len (legacy, KKJ2), body_len 2^63 (KKJ2): rejected / base `read(2^64-1)`, `read(2^63)`
- body_len off by one, truncated legacy digest, truncated KKJ2 fence, magic only, magic + partial key_len, bad digest: stop before the record on both versions (not a crash)
- Valid trailing legacy record, valid KKJ2 record with a 1024-byte key and fence 2^63-1, valid KKJ2 with an empty body: accepted on both, end==size

## Verified guarantees (at 263ebe7)
1. `key_len` must satisfy `1 <= key_len <= gate._MAX_KEY_LEN (1024)` and `key_len + 8 + trailer <= remaining`, and `body_len + trailer <= remaining`, before any `read(key_len)` or `read(body_len)`. trailer = 32 legacy / 40 KKJ2. The largest read seen in all cases was 1024 bytes.
2. Python ints do not overflow, so `body_len + trailer_len` with body_len = 2^64-1 compares correctly and cannot wrap.
3. Compatibility: the gate allows only ASCII keys (`KEY_RE = [A-Za-z0-9._/-]`, len ≤ 1024 chars). So UTF-8 byte length equals char length, and every key `_journal_append` can write through `write()` passes the new key_len bound. The size bound is always satisfied by a complete record. Legacy and KKJ2 records mixed in one file still replay.
4. Parser rejection ≠ repair: `_iter_journal_records` only opens the journal `rb`. On rejection it returns with `end < size`, and `_recover_key_before_decision` raises `_RecoveryIncomplete` (fail closed). The probe checks byte-identity after each scan. The PR's own test checks `persist` → `ERROR(CORRUPTION)` with the journal unchanged, before and after restart. There is no `truncate`/`ftruncate`/rewrite of the journal in `store/local.py` (`rg truncate` finds only comments).

## Residual limits / not covered
- **Torn-tail recovery is still out of scope:** any rejected tail (including a real crash-torn append) permanently fails writes closed with CORRUPTION until someone steps in. This is existing behaviour from 8e8c52f and the patch does not change it.
- `size` comes from one `fstat` at open. Records appended after that by a writer that does not hold `cas.lock` would be left unread in this pass (they end cleanly at the size boundary). The fix's comment assumes callers hold `cas.lock`. I did not test cross-process lock coverage of `_resume()` at construction (UNVERIFIED).
- If a journal was written by some older version whose gate allowed keys >1024 bytes or empty keys, those records would now be rejected. I found no such writer in the current tree. History before 8e8c52f was not audited (UNVERIFIED).
- Garbage whose lengths look plausible and fit inside the file is still read (at most file size) and rejected by the digest. Memory is bounded by the journal size, not by a per-record cap.
- Conformance suite not run (boto3 missing). Windows-only tests skipped on Linux. Neither is verified.
