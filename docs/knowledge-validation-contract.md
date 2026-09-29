# Knowledge validation contract (citation checks)

## Purpose

`knowledge-validate` checks a version-1 JSON **proposal** against **raw session** sources already in the store. It confirms proposal shape, that cited raw records exist under the selected project namespace, and that each citation’s `quote` equals the cited byte range in that source.

This command does **not** generate, persist, approve, or certify knowledge. A successful run is **not** semantic review and does **not** mean content is production-ready.

## Non-goals (always false)

Every success response includes these flags set to `false`:

- `semantic_support_verified` — exact quotes do not prove full-claim support; claim text remains caller/model data.
- `source_authorship_verified` — hashes and record ids bind to stored bytes, not human authorship.
- `authorization_verified` — `--project` is store addressing only, not tenancy or permission.
- `project_completeness_verified` — only citations listed in the proposal are checked.
- `repository_freshness_verified` — reads are not an atomic snapshot of the whole project.

## Input

Strict JSON, UTF-8, max 64 KiB. Duplicate keys, non-finite numbers, and malformed JSON are rejected before any store access.

Version 1 fields only: `schema_version` (integer `1`, not bool/float), `snapshot_as_of` (canonical UTC timestamp matching raw-session capture format), `coverage` (`selected_sources_only`), and `claims` (1–32 items).

Each claim has exactly `id`, `text`, `status`, and `citations`. Identifiers follow shared project/session/operation rules. Claim text is non-empty, ≤1024 UTF-8 bytes. `status` is `current`, `historical`, or `unresolved` (caller assertion only).

Each citation has exactly `session_id`, `operation_id`, `source_sha256`, `record_id`, `byte_start`, `byte_end`, and `quote`. Per-claim citations: 1–16; total ≤128; distinct `(session_id, operation_id)` pairs ≤8. Hashes are 64 lowercase hex. Offsets are integers with `0 <= byte_start < byte_end`. Quote is non-empty, ≤400 UTF-8 bytes.

## Store behavior

The application layer uses **read-only** `StoreBackend` operations. It does not construct, close, write, or lock the backend supplied by tests or embedders.

The CLI opens `LocalBackend` for each run. **Constructor recovery side effects** (creating or repairing on-disk store layout) are unchanged from other read commands; application read-only helpers do not make CLI invocation side-effect-free. The CLI validates the full proposal schema and bounds **before** constructing the backend, then always closes the backend in a `finally` block.

Each distinct cited source is read at most once per validation call (in-memory cache only for that call). Missing sources, backend errors, corrupt raw documents, hash or record-id mismatch, capture time after `snapshot_as_of`, out-of-range offsets, or quote byte mismatch fail closed.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Proposal schema and all citations verified against stored sources. |
| 2 | Invalid proposal or citation/reference rejection (JSON on stdout). |
| 1 | Backend, corruption, or operational failure (JSON on stdout). |

Errors use stable `reason` codes with claim/citation indices where applicable. Responses do not echo quotes, source text, or secret-bearing exception strings.

## CLI

```
knowledge-validate --store <path> --project <id> --proposal-file <path>
```

`--proposal-file` is accepted only for `knowledge-validate`. Other commands reject it.
