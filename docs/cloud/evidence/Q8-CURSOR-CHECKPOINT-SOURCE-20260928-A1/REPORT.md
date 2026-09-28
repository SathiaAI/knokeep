# Q8-CURSOR-CHECKPOINT-SOURCE-20260928-A1 — evidence report

## Identifiers

| Field | Value |
| --- | --- |
| Job | Q8-CURSOR-CHECKPOINT-SOURCE-20260928-A1 |
| Input / starting `HEAD` | `f3893911a01272d47b94108d028204c0dc439133` |
| Output branch | `q8/cursor-checkpoint-source-a1` |
| Final implementation SHA (no product code changed) | `f3893911a01272d47b94108d028204c0dc439133` |
| Source CSV (`q8-display-units.csv`) SHA-256 | `983848c56b4ea9d9bab9765a8443672fb0128d10022914f4d6bda7748fe7785c` |
| Client | Cursor Cloud Agent |
| Model | Composer 2.5 |

## Business result (uncontested)

- **Eligible owned (r3 + signed yes):** `cedar`, `ash` → `eligible-owned-units.csv`
- **Held owned:** `birch` (signed_check no), `maple` (r2; rejected exception not policy)
- **Disputed loan (not released):** `elm` — meets r3+yes but `Q_LOAN` open; no `release-loan`

## Checkpoint workflow

Project `q8-display`, milestone `display-source-1`, writer `cursor`. Proposal built with `experiments.checkpoint_v1.checkpoint.build_proposal`; full input retained as `proposal-input.json`.

| Step | Command | Exit |
| --- | --- | --- |
| Save | `python3 -m experiments.checkpoint_v1.checkpoint --store docs/cloud/evidence/Q8-CURSOR-CHECKPOINT-SOURCE-20260928-A1/store-local --project q8-display save --input docs/cloud/evidence/Q8-CURSOR-CHECKPOINT-SOURCE-20260928-A1/proposal-input.json` | 0 |
| Resume (fresh CLI) | `python3 -m experiments.checkpoint_v1.checkpoint --store …/store-local --project q8-display resume` | 0 |
| Exact duplicate retry | same `save` command and `proposal-input.json` | 0 |

Receipts: `save-receipt-1.json`, `resume-receipt.json`, `save-receipt-2-duplicate-retry.json`.

### Resume verification

`resume` returned `status: ok` with full `state` and `log` strings matching the proposal (multi-line bodies present in `resume-receipt.json`). `open_questions` includes `Q_LOAN` unchanged.

### Duplicate retry / journal bytes

Ordinary exact-payload retry after successful save (not a crash or lost-ack simulation).

| Metric | Before duplicate retry | After duplicate retry |
| --- | --- | --- |
| `journal.log` bytes | 1405 | 1405 |
| `journal.log` SHA-256 | `774169d7e9aefb2afc1e893062873ec85d13aeeea20da31ff60ce40fb6e5f2c4` | `774169d7e9aefb2afc1e893062873ec85d13aeeea20da31ff60ce40fb6e5f2c4` |

Second save receipt: `duplicate: true`, same `checkpoint_sha256` as first save.

## Store export

Whole evidence-local `store-local/` tree (data, journal, locks) is committed under this directory. Relative-path SHA-256 manifest: `store-local-manifest.sha256`. Scoped `.gitattributes` sets `* -text` to preserve original bytes in Git.

## Limits

Experimental `checkpoint_v1` prototype only; receipts attest accepted bytes, not semantic audit of prose. No loan release authorized; `Q_LOAN` left open per brief.
