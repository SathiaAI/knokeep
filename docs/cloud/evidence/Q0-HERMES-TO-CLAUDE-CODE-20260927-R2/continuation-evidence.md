# Continuation Evidence — q0-hermes-a4

## Status
COMPLETED (not blocked). The saved records are complete and consistent with each other.

## Recorded next action
`store/data/q0-hermes-a4/session_log` (revision 2), "Next Step":
> Produce packing-report.csv from orders.csv using the decisions saved in system_state.

Deliverable written: `packing-report.csv`.

## Saved decisions used

| # | Decision | Supporting record files |
|---|----------|-------------------------|
| 1 | Include an order only when status is exactly `ready`; exclude `cancelled` and `hold` | `store/data/q0-hermes-a4/system_state` (Hard Constraints); `store/data/q0-hermes-a4/sessions/hermes-a4` (Journal 2026-09-28T01:10:04Z) |
| 2 | Rejected approach: including held orders | `system_state` (Hard Constraints); `sessions/hermes-a4` |
| 3 | Normalize SKU: lowercase, then replace each hyphen or space with `_`; keep existing underscores | `system_state`; `sessions/hermes-a4` |
| 4 | Sum units per normalized SKU | `system_state`; `sessions/hermes-a4` |
| 5 | Cartons hold 4 units; cartons = ceil(units / 4) | `system_state`; `sessions/hermes-a4` |
| 6 | Output `packing-report.csv`, columns `sku,units,cartons`, rows sorted by sku | `system_state` (Path & Variable Directory); `sessions/hermes-a4` |
| 7 | Input `orders.csv` with columns `order_id,sku,units,status` | `system_state` (Path & Variable Directory); `session_log` ("Input orders.csv is ready") |

Corroboration: `store/journal/journal.log` contains the same revision-2 `system_state` and `session_log` text and the same session entry. `store/.knokeep-eval/events.jsonl` shows flush-state, flush-log, and session-append operations, all `ok: true`. `VERIFIED-INPUT.json` records `original_bytes_match: true` for `orders.csv` and all store files. No contradictions were found.

## Application to orders.csv

| order_id | raw sku | units | status | included? | normalized sku |
|----------|---------|-------|--------|-----------|----------------|
| oa1 | Blue-Mug | 2 | ready | yes | blue_mug |
| oa2 | blue_mug | 3 | ready | yes | blue_mug |
| oa3 | BLUE-MUG | 9 | cancelled | no | — |
| oa4 | Green-Tea | 4 | ready | yes | green_tea |
| oa5 | green_tea | 1 | hold | no (held orders rejected) | — |
| oa6 | Blue Mug | 1 | ready | yes | blue_mug |
| oa7 | Green Tea | 2 | ready | yes | green_tea |

- blue_mug: 2 + 3 + 1 = 6 units, ceil(6/4) = 2 cartons
- green_tea: 4 + 2 = 6 units, ceil(6/4) = 2 cartons

## Integrity
The store files and `orders.csv` were only read, never modified. The only new files are `packing-report.csv` and this file.
