# checkpoint_v1 — EXPERIMENTAL prototype (not production)

This prototype is not wired into the CLI bootstrap, skill, shims or CI, and it changes no product behavior.
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
