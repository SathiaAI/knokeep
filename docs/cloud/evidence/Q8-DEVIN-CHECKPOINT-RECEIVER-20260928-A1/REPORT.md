# Q8-DEVIN-CHECKPOINT-RECEIVER-20260928-A1 — receiver report

Starting HEAD verified: 50b9d13a9a4676a3aee60ae713adebf61ad9416b (branch q8/checkpoint-receiver-input-a1).
Output branch: q8/devin-checkpoint-receiver-a1. Observed client/model: Devin (Cognition AI); underlying model UNVERIFIED (not exposed).

## Steps
1. Copied whole `pilot/input-store` to `store/` before opening it. Verified all 6 hashes in `pilot/input-store-manifest.json` (all match).
2. `resume` on copy (exit 0) -> head display-source-1 (sha 1477798d...e6e3, writer cursor). Accepted rule: owned release needs r3 AND signed_check yes. Rejected: signed r2 exception. Prior work: inventory-owned-eligibility. Open: Q_LOAN (blocks release-loan).
3. Recomputed from `pilot/display-units-next.csv` using that rule:
   - Uncontested eligible owned: cedar, oak (`eligible-owned-units.csv`)
   - Definite holds: birch (signed_check no), maple (r2), ash (signed_check no; was eligible in prior record)
   - Disputed loan, not released: elm, pine (Q_LOAN open)
4. `save` of `proposal-display-receiver-1.json` (writer devin, predecessor display-source-1 / 1477798d...e6e3, Q_LOAN carried unchanged, no resolutions) -> accepted, exit 0.
5. Fresh `resume` -> head display-receiver-1, both new state and log bodies returned, exit 0.
6. `lookup display-source-1` -> accepted (exit 0); `lookup display-receiver-1` -> accepted (exit 0).

Commands: `python3 -m experiments.checkpoint_v1.checkpoint --store docs/cloud/evidence/Q8-DEVIN-CHECKPOINT-RECEIVER-20260928-A1/store --project q8-display {resume | save --input <proposal> | lookup --milestone-id M}`.
Receipts: `01-resume`, `02-save`, `03-resume`, `04-lookup-prior`, `05-lookup-new` (`.stdout.json`, `.stderr`, `.exit`).

## Checkpoint references
- Prior (immutable, unchanged bytes): display-source-1 sha256 1477798dc17ff9b539f627e866184a2141fe1fb5fc95f653da406d61bd4db6e3
- New head: display-receiver-1 sha256 e8144b06a2a134273e8138350203e80ed731f701650372987376f20661f61601, depth 2

## Integrity
- `store-manifest.sha256`: full relative-path SHA-256 of the evidence-local store, including locks/fence metadata. `.gitattributes` `* -text` scoped to this directory.
- `pilot/input-store` rehashed after work (`original-input-store-after.sha256`): identical to manifest. `pilot/display-units-next.csv` sha256 96a5cc03f060ee1f2377193bd8446a6a765c7ce99a8fc69aac78f3f4475fcb2b, unchanged (git status clean on pilot/).

## Limits
- Q_LOAN unresolved; no loan release authorized; no owner approvals supplied or invented.
- `writer` is caller-declared, not authentication. Only LocalBackend via ordinary CLI calls; proves neither native MCP integration nor reliability.
- Eligibility result depends on the carried rule and CSV contents; semantic truth not independently verified.
