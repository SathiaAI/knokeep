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

The tests are included in Linux, Windows and macOS CI. Exact published-head CI and
independent Devin review must be checked separately; local results do not imply
either has completed.

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
