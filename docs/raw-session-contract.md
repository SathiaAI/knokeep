# Raw session capture contract (bounded slice)

Raw session bytes live at `{project}/raw-sessions/{session}` as a single gated blob
(`doc_type="journal"`). Legacy session journals under `{project}/sessions/{session}` are
unchanged.

## CLI

- `raw-session-append --store --project --session-id --client --operation-id --source-file`
- `raw-session-read --store --project --session-id --operation-id`

`--source-file` is a filesystem path or `-` for binary stdin. At most 1 MiB + 1 bytes are
read without newline transformation; intentional empty input is allowed.

## On-disk envelope

```
KNOKEEP-RAW/1\n
{canonical JSON header}\n
{len bytes of source}\n
(repeated, up to 256 records, total blob <= 4 MiB)
```

Header fields (v=1): `project`, `session`, `op`, `client`, `captured` (UTC
`YYYY-MM-DDTHH:MM:SS.ffffffZ` with a real calendar/time), `len`, `sha256` (lowercase hex of
source). All header string fields are JSON strings (never numbers). Header JSON is canonical
ASCII (`sort_keys`, compact separators). `record_id` is SHA-256 of the stored header bytes.

## Validation

Source must pass `store.gate.validate_write` for the raw key with
`doc_type="journal"` before acquiring the lease. The combined envelope is built and
validated under the lease after reading the existing records, before any payload write.
Responses never echo rejected source bytes on secret or validation failures.

## Idempotency

`operation_id` binds `client` and exact source bytes. Same tuple returns `duplicate: true`
without a payload write; conflicting reuse errors.

For an uncertain backend result, reconciliation may find that this call's record was
written and then included in a later version. Its receipt can then have `duplicate: true`:
the flag means the record was already present when reconciled, not that this call could
not have written it. A lost or unresolved write can return `outcome_uncertain`; callers
must retry with the same operation ID, client, session and exact source bytes. Do not
substitute a new operation ID after an uncertain result.

## Errors and side effects

Missing/invalid arguments, missing source files and oversized sources exit with code 2.
Content rejections such as BOM, invalid UTF-8 or a detected secret exit with code 1.
Clients must inspect the JSON `reason`; the exit code alone does not distinguish every
input rejection from a backend failure.

A conflicting retry or corrupt stored document is detected after taking the lease.
The durable fence allocation can therefore advance even though the data document stays
unchanged. “No payload write” does not imply that all lock metadata is unchanged.

These semantics describe the bounded LocalBackend slice. They do not qualify other
backends, complete capture coverage, retrieval, synthesis or production readiness.
