## Architecture
Bounded inventory toy: `restock.py` reads `stock.csv` and writes `restock.csv` (active SKUs only; free = stock-reserved floored at 0; reorder = threshold-free floored at 0; sorted by sku). `stock.csv` also has `lead_days` (not used by restock).

## Path & Variable Directory
- Project dir: this exported project directory (earlier export path was `docs/cloud/q2-work/project`)
- `PROJECT=q2-cursor-a1`; earlier writer label `cursor-cloud`, latest writer label `claude-code`
- Invoke: `python restock.py stock.csv restock.csv`
- Historical restock output: `restock.export-original.csv`; record bodies for this update: `_log_body.q4.md`, `_state_body.q4.md` (the original `_log_body.md`/`_state_body.md` are unchanged)

## Hard Constraints
Do not change KnoKeep implementation, `restock.py` or `stock.csv`. Do not implement `priority-review.csv` until the open criteria contradiction (see session log Active State / Next Step) is resolved by an authorized decision. No secrets in flushed bodies.
