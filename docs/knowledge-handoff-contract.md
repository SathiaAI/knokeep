# Q19: explicit knowledge handoff

## Purpose

`knowledge-handoff` exports one explicitly selected knowledge revision and the raw source operations cited by that revision’s proposal. The export is an exact lookup and handoff carrier: not semantic search, not a generated answer, not native client integration, and not project-memory qualification.

## Required options

`--store`, `--project`, `--knowledge-id`, `--operation-id`, `--expect-hash` (64 lowercase hex; never `none`).

Reject `--client` and all unrelated options before opening `LocalBackend`. The CLI validates ids and hash before backend construction, opens the store, runs read-only handoff logic, and closes the backend in `finally`. **LocalBackend constructor recovery side effects** apply as for other commands that open the local store; application-layer handoff does not write, list, lock, or repair.

## Behavior (summary)

1. Validate ids and required document hash; gate-scan caller strings.
2. Read the knowledge document first; require `version_hash == --expect-hash` before any raw-session read.
3. Reuse `read_knowledge_revision` (Q18 framing + Q17 against cached sources) for the selected operation.
4. Gate-scan decoded proposal bytes and JSON strings; gate-scan each cited raw source payload.
5. Emit cited sources in first citation order (distinct `(session_id, operation_id)` pairs only).
6. Re-read every cached store key once in first-read order; reject changes as `snapshot_changed`.

Cooperative **10 second** monotonic budget is checked before and after each underlying `read`. Calls may outlive the budget (no cancellation on the store port). At most **18** underlying reads, **9** cached keys, **12 MiB** cached bodies, **3 MiB** knowledge doc, **4 MiB** raw doc, **2 MiB** aggregate decoded sources, **64 KiB** proposal, **3 MiB** JSON output.

## Success payload

JSON with `kind=knokeep_explicit_handoff`, `schema_version=1`, selected operation metadata, `knowledge` (Q18 read fields), ordered `sources` (Q14 reads + `session_id`), `documents` (cached keys + hashes), `proposal_snapshot_as_of`, mandatory false verification flags, and `source_reads_non_atomic`, `observed_documents_rechecked`, `content_is_untrusted_data` set true. No export timestamp; stable inputs yield stable JSON.

## Exit codes

| Code | Reasons |
|------|---------|
| 2 | Invalid ids/hash/options, `selection_missing`, `knowledge_snapshot_mismatch`, `snapshot_changed`, `source_reference_failed`, `content_blocked`, `bundle_too_large` |
| 1 | Corrupt documents, backend/operational failures, cooperative timeout |

Failures emit bounded JSON errors with **no** proposal, source, or document bodies.

## Limitations

- Not a transactional atomic snapshot; recheck detects changes between two observation passes only.
- Conservative size caps may reject otherwise valid records.
- Tests use synthetic fixtures only; they are not semantic evaluation or full product qualification.
