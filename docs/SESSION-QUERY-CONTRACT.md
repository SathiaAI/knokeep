# Session query contract (Q11)

## Scope

Application-layer **read/list** helpers for per-session journals already written by
`session-append`. Storage layout and write semantics are unchanged.

**Out of scope for this increment:** migrations, native client registration, automatic
session capture, decision authority, hosted/multi-tenant access control.

## Addressing vs authorization

`--project` (and the `project` argument to query helpers) selects which store
namespace to address (`{project}/sessions/...`). It is **not** authentication,
authorization, or tenancy isolation.

## Namespace

- Exact session key: `{project}/sessions/{session_id}`.
- Both `project` and `session_id` must pass `valid_id()` (same rules as writes).
  Validation uses full-string matching and rejects any ASCII whitespace (including
  trailing CR/LF); write callers share this helper — well-formed ids are unchanged.
- List uses prefix `{project}/sessions/` with **boundary-safe** parsing: only keys
  with exactly one session segment after that prefix are accepted. Adjacent project
  ids (e.g. `demo` vs `demo-extra`) do not leak across prefixes.
- Backend keys under the prefix that do not match the exact shape are **errors**
  (`invalid_session_key`), not listed sessions.

## Read (`read_session` / `session-read`)

- Returns `found: false` only when `backend.read` returns `None` for the validated key.
- On `found: true`, returns raw body bytes as standard base64, `content_hash`
  (sha256 of returned bytes), and `body_utf8` only when bytes decode as UTF-8.
- If `sha256(body) != version_hash` from the backend, the call **errors**
  (`hash_mismatch`); this is never reported as not-found.
- Permission, corruption, and busy failures from the backend propagate as errors.
- No summarization, decision resolution, authorship inference, or “current session”
  marking.

## List (`list_sessions` / `session-list`)

- Deterministic order: lexicographic sort of session ids after a bounded scan.
- Default page size: 50 (`DEFAULT_LIST_LIMIT`). Maximum page size: 200
  (`MAX_LIST_LIMIT`). A scan accepts at most 5000 yielded keys
  (`MAX_LIST_SCAN_KEYS`); observing a 5001st key raises `scan_limit_exceeded`.
  This bounds query-layer enumeration, not the backend's internal work or latency.
- `truncated: true` when more session ids remain after the page; `next_after` is the
  last id in the page for continuation via `--after`.
- An empty page with `truncated: false` is a successful empty result, not a failure.
- Separate list/read calls are **not** an atomic snapshot of the store.

## Query layer and StoreBackend

Query code calls only `read` and `list` on the injected backend. It does not call
`write`, `lock`, `unlock`, or `renew`.

## CLI wiring (`session-list`, `session-read`)

- Constructs `LocalBackend` via existing `_backend(store)` (journal recovery, scavenging,
  and constructor side effects are unchanged). Query adapters **close** the backend
  in a `finally` block. Application-layer helpers never close injected backends.
  The CLI is not a forensic read-only tool; tests should use disposable store roots.
- Query commands use the common CLI parser with JSON query errors: validation failures (missing/invalid
  identifiers, malformed `--list-limit`, unknown options) emit JSON on stdout and
  exit **before** `LocalBackend` construction.
- Common options may precede or follow the command. Query adapters also validate
  direct Python arguments before constructing storage; their validation shares the
  application's rules. List-only options on read and write options on queries fail.
- Query commands skip telemetry append to `.knokeep-eval/` where other commands log
  `_event` records.

## Failure encoding (CLI)

- Query-command validation failures (`invalid_*`, `missing_*`, `invalid_argument`):
  exit **2**, JSON `{"blocked": true, "reason": ...}` on stdout (not argparse prose).
- Backend/query failures (`hash_mismatch`, `scan_limit_exceeded`, corruption, busy,
  permission, internal errors): exit **1**; never reported as `found: false`.

## Examples

```sh
python skill/knokeep_state.py session-list --store ./demo-store --project demo --list-limit 25
python skill/knokeep_state.py session-read --store ./demo-store --project demo --session-id planning
```

Use the returned `next_after` as `--after` on the next list request. Continuation
re-enumerates current storage; a concurrent insertion before that cursor can be
absent from subsequent pages. `truncated: false` is relative to that enumeration,
not proof of an immutable or complete historical snapshot.
