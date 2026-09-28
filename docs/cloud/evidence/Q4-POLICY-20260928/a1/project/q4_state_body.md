## Architecture
Inventory toy: `restock.py` reads `stock.csv` and writes a restock CSV (active SKUs only; free = max(0, stock-reserved); reorder = max(0, threshold-free); sorted by sku). Unchanged from the exported record.

## Path & Variable Directory
- Project dir: the exported project directory (relative files: `stock.csv`, `restock.py`, `restock.csv`, `restock.current.csv`)
- `restock.csv` = historical output from the earlier export (flask/mug/tea); kept as-is.
- `restock.current.csv` = output for current `stock.csv` (adds `bowl`).
- Invoke: `python restock.py stock.csv restock.current.csv`
- Store project slugs: exported record lives under `q2-cursor-a1` (client label `cursor-cloud`); this receiver session was instructed to write under `q2-cursor-source-a1`, which was empty at bootstrap (`q2-cursor-source-a1` is the session-journal id inside `q2-cursor-a1`).

## Hard Constraints
Do not change KnoKeep implementation, `stock.csv`, `restock.py`, or the original `_state_body.md` / `_log_body.md`. No secrets in flushed bodies. Do not finalize `priority-review.csv` until its selection rule is resolved by an authorized decision.
