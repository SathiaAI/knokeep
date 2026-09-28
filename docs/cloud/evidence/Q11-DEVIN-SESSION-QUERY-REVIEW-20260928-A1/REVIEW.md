# Q11-DEVIN-SESSION-QUERY-REVIEW-20260928-A1

Independent review of SathiaAI/knokeep PR 72 (branch `fix/q11-session-queries`).
CLI/query increment only; not hosted/native-MCP or automatic-capture qualification.
Underlying model name: UNVERIFIED (not exposed by client).

## Input
- Reviewed HEAD: `e26db99c6f0abab9ad0da783487d8e531b689a5d` (verified via `git rev-parse HEAD`)
- Parent / stated base: `bc9fe11004c4568a72adf08d463048aca26daa92` (verified `HEAD~1`; single commit in range)
- main not used. No product code modified.
- sha256 of reviewed files at HEAD:
  - application/session_queries.py `423b2f965c7e187b74a9dd567258b5448de3258573b7bee87f42875c42285071`
  - application/identifiers.py `37f9370526d259c7bbb03ac00982beb2c31133f26bde3be803f1030f95f4b2b1`
  - skill/knokeep_state.py `583b82be1d38080b0ac0519c23ec50b69334234b0e42eff173b6f12d6c07a8a6`
  - docs/SESSION-QUERY-CONTRACT.md `8e1a033b2934de89ddfcf733136ac21ae43ae400fd7f39f6f2377c450568f025`

## Commands / exits / counts
| Command | Exit | Result |
|---|---|---|
| `python3 -m pytest tests/test_session_queries.py tests/test_session_query_coordinator.py tests/test_session_append_idempotency.py -q` | 1 | NOT RUN: `No module named pytest`; no downloads permitted |
| `python3 attack_reproducers.py <repo>` (Python 3.10.12, stdlib only, disposable `tempfile.mkdtemp` stores) | 0 | TOTAL=74 PASS=73 FAIL=1 (see attack_output.txt) |

The PR's own pytest suites were therefore NOT reproduced; all evidence below comes from the
independent stdlib reproducer exercising the actual code (application layer + CLI subprocess).

## Covered (reproduced OK)
- `valid_id`: rejects `p\n`, `p\r`, NBSP, non-ASCII digits, reserved names (`CON`, `con.txt`), leading/trailing dot, >64 chars, non-str; accepts well-formed ids.
- Validation before backend creation (CLI): 15 malformed invocations (bad project, limit 0/201/-1/`1e2`/Arabic-Indic digit, bad `--after`, `--session-id` on list, missing session id, LF in id, list option on read, write option on read, unknown option, missing/empty project) -> exit 2, JSON on stdout, store directory NOT created.
- Application layer validates before touching backend (backend raising on any call was never invoked).
- Namespace: `demo` list excludes `demo-extra` and `demo.x`; cross-namespace read -> `found:false`.
- Paging: limit/truncated/next_after/after correct; after-beyond-end and unknown project -> empty, exit 0.
- Exact bytes: CLI base64 == LocalBackend bytes, `content_hash` == sha256; non-UTF-8 body -> `body_utf8` omitted; CRLF and BOM preserved.
- Error vs not-found: `hash_mismatch` raises; published blob without journal record -> list and read exit 1 with blocked JSON (never `found:false`); healthy key beside orphan... see limitation L3.
- Iterators/limits: 5000 keys OK, 5001 -> `scan_limit_exceeded`; mid-iteration backend exception propagates (no partial page); malformed keys under prefix (`a/b`, empty, `.x`, `con`, `a\n`) -> `invalid_session_key`; duplicate keys de-duplicated.
- `limit` type strictness in app layer (bool, float, str, 0, 201, None rejected).
- Queries do not append `.knokeep-eval/events.jsonl`; legacy commands still do.
- Legacy CLI: `init`, `session-append`, `health` work; argparse errors for legacy commands remain prose on stderr, exit 2; `--project session-list` / `--store=session-read` values not misdetected.

## Findings
### F1 (low) Legacy CLI error-format change with abbreviated option values
`skill/knokeep_state.py` lines 1190-1199 (`CommandParser.error`): the command scan skips values only for
exact long option names in `takes_value`. argparse `allow_abbrev` (default True) accepts prefixes such as
`--proj`, so the value after an abbreviated option is scanned as a positional. If that value is
`session-list`/`session-read`, a malformed *legacy* command emits query-style JSON on stdout instead of
argparse usage prose on stderr.

Reproducer:
```sh
python3 skill/knokeep_state.py init --proj session-list --bogus
# HEAD e26db99: exit 2, stdout {"blocked": true, "reason": "invalid_argument", "message": "unrecognized arguments: --bogus"}
# base bc9fe11: exit 2, stdout empty, argparse usage on stderr
```
Exit code is unchanged (2); only the output channel/format differs. Requires an abbreviated option
whose value is literally a query command name. Suggested direction (not applied): use the parsed
`cmd` via `parse_known_args`, or set `allow_abbrev=False` / match prefixes of `takes_value`.

### Informational (not defects)
- argparse abbreviations (`--proj`, `--list`) are accepted by query commands (not mentioned in contract).
- CLI `--list-limit " 5"` is `.strip()`ed and `0005` accepted (lenient but bounded).
- The `conflicts` note journal (`{project}/sessions/conflicts`, written by `_park_conflict`) will appear as an ordinary session id in `session-list`; contract does not say whether it should.

No other defects reproduced.

## Limitations
- L1: PR pytest suites (`tests/test_session_queries.py`, `tests/test_session_query_coordinator.py`) not executed (pytest unavailable, downloads disallowed).
- L2: Linux/ext4 only; Windows (msvcrt/NTFS, case-insensitive) paths untested. Busy/permission (`BackendBusyError`, EACCES) CLI paths not exercised; only corruption path tested for exit 1.
- L3: The "healthy key beside orphan" check passed, i.e. read of a valid key still works while an orphan blob exists under the same prefix; only list fails closed.
- L4: No concurrency tests (concurrent append during paging) were run; contract already disclaims snapshot atomicity.
- Six-minute bound: review is partial by design.
