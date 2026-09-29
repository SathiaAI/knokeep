# Knowledge revisions contract

## Purpose

`knowledge-save` and `knowledge-read` persist and retrieve **caller-authored proposal revisions** in one journal document per knowledge id. Each revision stores the exact proposal bytes submitted after schema and citation checks against raw session sources (Q17). The commands do not generate knowledge, approve semantic claims, grant access, or promote content to an authoritative status.

## Storage model

- One store key: `{project}/knowledge/{knowledge_id}` (`doc_type=journal`).
- Document layout: magic line `KNOKEEP-KNOWLEDGE/1`, then up to 32 framed revisions (canonical JSON header, proposal bytes, line feeds).
- Limits: 64 KiB per proposal, 2048-byte header, 3 MiB total document.
- Content hash: SHA-256 of the blob body must match `version_hash` before parsing.

## Save (`knowledge-save`)

Required options: `--store`, `--project`, `--knowledge-id`, `--client`, `--operation-id`, `--proposal-file`, `--expect-hash`.

- `--expect-hash none` — create-only when no document exists (uses create-only persist; no lease on create).
- `--expect-hash <64-hex>` — append only if the current document hash matches exactly (fenced overwrite with held lease).

Create-only safety relies on the backend atomic missing-key / EXISTS precondition; the backend does not fence an in-flight create. A create whose call outlives the lease may still commit; the application reports a verified readback or an uncertain outcome.

Before any backend I/O, ids, caller strings, precondition shape, proposal bounds, Q17 schema, and gate scans (decoded JSON strings and raw proposal bytes) are validated.

Duplicate retry: the same `(client, operation_id, proposal bytes, proposal hash/length, base_document_sha256)` where `base_document_sha256` is the caller’s normalized precondition (`none` → null) returns the original record as `duplicate: true` without re-running Q17 (`source_checks_executed_this_call: false`). A changed binding under the same `operation_id` fails.

New revisions run Q17 against current raw sources while holding the document lease, then persist. Definitive stale/exists results return `precondition_conflict` (exit 2). Uncertain write outcomes may perform one validating readback.

## Read (`knowledge-read`)

Required options: `--store`, `--project`, `--knowledge-id`, `--operation-id` only (no `--client`). Read-only at the application layer. The CLI opens and closes `LocalBackend` per invocation; **constructor recovery side effects** apply as for other commands that open the local store.

Missing knowledge or operation: `found: false`, exit 0. Corrupt knowledge document: `knowledge_corrupt`, exit 1. Q17 failures on read: `source_reference_failed` (exit 2) or `source_backend_error` (exit 1). Success returns exact proposal bytes as base64 plus record metadata.

Every success response includes `semantic_support_verified`, `source_authorship_verified`, `authorization_verified`, `project_completeness_verified`, and `repository_freshness_verified` as **false**, plus `source_reads_non_atomic: true`.

## Exit codes (summary)

| Code | Typical reasons |
|------|-----------------|
| 0 | Success or not found (read) |
| 1 | Corruption, backend/uncertain/operational failures |
| 2 | Invalid arguments, precondition conflict, document full, source reference failure |

## Limitations

- Evidence in this increment is bounded to **FakeBackend** and **LocalBackend** only.
- Source reads during save/read are not an atomic snapshot of the project.
- Duplicate acknowledgment is not proof that the current call performed no write.
- Backend calls may block beyond the cooperative application retry budget; there is no cancellation on the store port.
