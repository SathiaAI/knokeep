# checkpoint_v1 — EXPERIMENTAL prototype (not production)

This prototype is not wired into the CLI bootstrap, skill or shims, and it changes no product behavior.
Its dedicated experimental CI runs separately from the product checks on Linux, Windows and macOS.
It shows how one complete state+log checkpoint could be kept as the authoritative
experimental resume input. It does **not** prove semantic truth or production readiness.

## Protocol
- Each proposal is one immutable canonical JSON object. It is written through
  `store.gate.persist` with `CreateOnly` to `<project>/checkpoints_v1/proposals/<id>`.
  A durable proposal that no head references is **STAGED**, not accepted.
- `<project>/checkpoints_v1/HEAD` is the one authoritative pointer. It is published by a single
  gated, fenced CAS write while the writer holds a `LocalBackend.lock` lease. The HEAD
  journal event is the logical commit. The head is never inferred from timestamps, listing order or leaves.
- The predecessor is part of the payload, so a proposal is never silently rebased. A mismatch returns
  `staged_stale` and keeps the loser's bytes for review. Nothing is deleted or compacted.
- `resume` reads the head, verifies the target and chain hashes (at most 256 steps) and returns both
  complete bodies. It lists staged IDs and reports changed legacy `system_state`/`session_log` hashes as
  `unsealed_legacy_changes`, which it never imports or overwrites. If the head target is missing or corrupt,
  `resume` fails closed.
- Receipts keep `checkpoint_sha256` (this proposal) separate from `current_head_sha256`.
- Default lease TTL is 30s. The process-kill tests use a 0.5s TTL, test-only.
- Busy lease acquisition returns `staged_busy`: proposal bytes remain durable but
  unaccepted. Retry the same payload after contention; never rebase it automatically.
- `resolved_questions` entries contain an open question ID and nonempty `decision_ref`.
  Every predecessor question must be carried unchanged or explicitly resolved;
  dropping it while completing its action is refused. References are caller-declared
  audit evidence, not authentication or proof that a real decision exists.
- `resolves_staged` binds each retained proposal ID and its exact SHA-256. Resume
  distinguishes outstanding staged proposals from explicitly resolved ones; bytes remain.
- An acknowledged head whose readback fails is `outcome_unknown` (CLI exit4), not a
  definite failed save. Lookup by ID before retrying. Invalid input uses exit2.

## Usage
    python -m experiments.checkpoint_v1.checkpoint --store S --project P save --input proposal.json
    python -m experiments.checkpoint_v1.checkpoint --store S --project P resume
    python -m experiments.checkpoint_v1.checkpoint --store S --project P lookup --milestone-id M

## Limits
- Only LocalBackend was tried, with two real process-kill probes: one after proposal durability and
  before the head, and one after the head journal fsync and before publish/reply. These probes do not
  qualify other backends or other crash boundaries.
- Secret detection reuses the existing scanner rules. It is not a universal guarantee.
- `writer` is caller-declared. It is not authentication.
- The open/completed conflict check validates only the declared structure.
- Maximum canonical proposal size:256KiB; lists and history are bounded at256.
  The next save at the history ceiling refuses acceptance and retains its staged
  proposal. There is no rollover or compaction protocol. This limit alone prevents
  adopting this prototype as a continuously running production store.
- LocalBackend now recovers the target key from the durable journal before deciding
  writes (#55). This avoids accepting a stale successor after another process crashes
  after fsync. The cost is a full journal scan per write; torn/unparsable tails block
  writes. Existing long-lived read/list calls can still return older published data;
  use a fresh LocalBackend for authoritative recovery reads (the CLI does this).
- The initial worker's14 passing tests were insufficient: independent boundary tests
  first failed10/14, then a long-lived-writer crash test exposed the engine defect.
  Both failures drove changes. Current focused suite has33 passing tests on Windows;
  cross-platform experimental CI is separate from the original product CI.
