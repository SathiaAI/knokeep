# Q2-CURSOR-CLOUD-SOURCE-20260927-A1 result

## Input and environment

| Field | Value |
| --- | --- |
| Input HEAD (recorded before edits) | `4d9c2d00be4e448cdefe123ffb0e13460de150ca` |
| Evidence branch | `q2/cursor-cloud-evidence-a1` |
| Client label | `cursor-cloud` (not authenticated) |
| Session label | `q2-cursor-source-a1` |
| Store | `docs/cloud/q2-work/store` |
| Project | `q2-cursor-a1` |
| Actual model (cursor-cloud run-info) | `composer-2.5` |

**Resume (bootstrap after flushes):** Export capture pending push; next step is unimplemented `priority-review.csv` per brief.

**Native KnoKeep MCP:** not used. All KnoKeep persistence via `python3 skill/knokeep_state.py` CLI (subprocess, not MCP enrollment).

## Commands and exit statuses

| Command | Exit |
| --- | ---: |
| `python3 skill/knokeep_state.py bootstrap --store docs/cloud/q2-work/store --project q2-cursor-a1` | 0 |
| `python3 skill/knokeep_state.py init --store docs/cloud/q2-work/store --project q2-cursor-a1 --client cursor-cloud` | 0 |
| `python3 skill/knokeep_state.py flush-state … --expect-hash 4ca65ab…` | 0 |
| `python3 skill/knokeep_state.py flush-log … --expect-hash 9f58b44…` | 0 |
| `python3 skill/knokeep_state.py session-append --session-id q2-cursor-source-a1 …` | 0 |
| `python3 skill/knokeep_state.py health --store docs/cloud/q2-work/store --project q2-cursor-a1` | 0 (verdict `healthy`) |
| `cd docs/cloud/q2-work/project && python3 restock.py stock.csv restock.csv` | 0 |
| Inline CSV verification script | 0 (`VERIFY OK`) |
| Git byte check (pre-push) | 0 (see below) |

Note: `python` is not on PATH; verification used `python3`. Brief invocation form remains `python restock.py …`.

## Toy deliverable verification

Active rows only (`active` = `yes`). Expected `restock.csv`:

| sku | free_units | reorder_units |
| --- | ---: | ---: |
| flask | 4 | 3 |
| mug | 11 | 0 |
| candle | (excluded, inactive) | |
| tea | 3 | 5 |

Sorted by `sku`; mug retained with zero reorder units.

## Unfinished (by design)

- `priority-review.csv` not implemented; criteria captured in store log **Next Step**.

## KnoKeep receipts

Post-flush bootstrap hashes match persisted docs:

- `system_state` `version_hash`: `c8726ae561c5e89df562a5c136813ba2b23bfb42e484da2ee5b951aaac21bb1f`
- `session_log` `log_hash`: `54372dc9b2e45945a73534ab3300364fa2196a0fd07778ce09bd4e8fc6ec9879`

`health` reported no parks/blocks/errors for this project.

## Export

- Manifest: `docs/cloud/q2-work/manifest.json` (tracked `project/` + `store/` paths only; `store/.knokeep-eval/` exists locally but is repo-gitignored and omitted from manifest).
- `.gitattributes` in `docs/cloud/q2-work` uses `-text` for byte preservation.

Pre-push git/object check: for each manifest entry, `git hash-object` on the working-tree path matches the recorded `git_blob` (exit 0, no mismatches).

## Commit

- Commit: `1314454c13ede309415c7a4cb326571e47cb8778`
- Branch pushed: `q2/cursor-cloud-evidence-a1` (no PR opened)

## Coordinator correction and independent verification
The initial export omitted original telemetry because it was Git-ignored. A read-only follow-up in the same completed cloud session recovered the original772-byte file (SHA-25648b111a16d1bd107b74df92961d9530cdf2d8ed26d840cbc82d8b29633f4e27a); no KnoKeep command regenerated it. All17 source inventory files, including the original partial manifest and attributes, match the recovered original inventory. Use manifest-complete.json as the authoritative export inventory.
The private cloud run identifier was removed from this report and the publication commit ancestry rebuilt from the input fixture. Original commits are retained privately; earlier public copies may remain accessible. Fixture/store bytes were not edited.
The API rejected an exact-SHA launch before creating a worker. Dispatcher then used the fixture branch after rechecking it at4d9c2d00be4e448cdefe123ffb0e13460de150ca; worker input HEAD independently agrees. One source worker ran; one read-only recovery followed. Actual route reported composer-2.5; dispatcher observed included subscription usage and no extra spend, not a token-derived dollar estimate.
Coordinator independently reran restock.py and matched the frozen source rows. Future priority-review.csv is absent. The five ordinary project files include _log_body.md with the next-action criteria, so a successor can obtain them from ordinary artifacts too; this case cannot establish incremental commercial value of KnoKeep.
Source capture and file export are supported at this scope. Native MCP, unattended capture, clean-client isolation and production reliability are not established. The cloud session has completed; the initial incomplete export remains part of the result.
