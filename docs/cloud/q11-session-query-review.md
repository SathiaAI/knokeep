# Q11: session queries for fresh readers

The CLI could append session records but could not discover or read them directly.
This candidate adds bounded `session-list` and byte-preserving `session-read` over
the existing StoreBackend protocol, with CLI wiring kept outside the application
query module. Base is reviewed core `bc9fe11004c4568a72adf08d463048aca26daa92`.

Actual Cursor Composer 2.5 implemented the first candidate and one correction.
Codex verified the six delivered file hashes, reproduced failure cases, corrected
remaining validation and argument-routing gaps, and integrated CI coverage.
Provider artifacts remain private; no private planning memory is included here.

## Independent Windows verification

- First candidate: 59 related tests passed, while independent adversarial probes
  exposed 13 failing cases across ID validation, list-limit types, error encoding,
  premature store construction and resource cleanup.
- Cursor's correction reduced those failures to three. Coordinator integration
  validates before constructing storage and supports common options before the
  command, using one shared parser.
- Final local candidate: **93 pytest tests passed**, including 25 independent
  regressions, application/CLI tests, append idempotency, writer attribution and
  resume-section coverage. **33/33 legacy skill checks** and **7/7 shared skill/MCP
  store checks** passed against the modified implementation.
- Initial pytest execution encountered 29 fixture setup errors using the default
  temporary location. Repeating with a new dedicated workspace temporary directory
  passed. This environmental failure is retained; it is not counted as a product
  pass or silently removed from the qualification history.

The original published candidate `e26db99c6f0abab9ad0da783487d8e531b689a5d`
passed all eight push/PR checks across Linux, Windows and macOS.

## Independent Devin review and correction

Actual Devin ran 74 standard-library checks on the original published candidate:
73 passed and one found a low-severity compatibility regression. With
`init --proj session-list --bogus`, the query error router mistook an abbreviated
option's value for a query command and emitted JSON instead of legacy usage text.
The correction handles unambiguous long-option abbreviations and stops routing
when a legacy command is encountered. Three independent regression cases cover
option placement and attached values.

[Original review and reproducers](https://github.com/SathiaAI/knokeep/tree/e79d94ba0210332f096c79eeed133b2e46b507b7/docs/cloud/evidence/Q11-DEVIN-SESSION-QUERY-REVIEW-20260928-A1)
are preserved on a separate evidence branch. Devin did not have pytest available,
did not install dependencies, and did not test busy/permission or concurrency
failures. Its model identity was not verified.

Codex reproduced all **74/74** assertions on Windows after the correction, with
**96 focused pytest cases** and **33/33 legacy checks** passing. The original
Linux-specific base-comparison tail was replaced by a separate base subprocess;
the 74 assertions were unchanged. An initial reproduction hit Windows console
encoding on a Unicode test label; its failure was retained and process-local
UTF-8 I/O allowed the rerun. No global encoding settings changed.

The corrected published head requires its own CI result. Check the PR's exact
head status; the earlier candidate's eight passes do not cover a later commit.

## Boundaries

No new storage format, transport registration, hosted service, automatic capture,
fuzzy retrieval, authenticated authorship or authorization is implemented here.
Project selection is addressing only. Query helpers do not invoke mutation/lease
methods, but constructing LocalBackend retains its existing recovery, scavenging
and directory side effects. Read this as application-level queries, not forensic
filesystem inspection. Multi-record reads/list pages are not atomic snapshots.
The 5000-key enumeration ceiling bounds application work, not adapter internals.

The shared identifier helper additionally rejects a trailing newline previously
accepted by regex prefix matching; well-formed identifiers retain their behavior.
Original client record bytes are returned as stored, with a computed hash and
base64 representation; this does not establish original capture completeness.
