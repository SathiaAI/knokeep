# Q8 complete-checkpoint prototype: source client brief

Job: Q8-CURSOR-CHECKPOINT-SOURCE-20260928-A1. This is a guided experimental workflow test, not a product release or a native MCP test. Use the frozen input SHA specified by the coordinator. Output branch: q8/cursor-checkpoint-source-a1. At most six minutes, then report unfinished items honestly and stop.

Use only the existing Python standard library and repository code. Do not install packages, change product code, access credentials, alter configuration/security/billing, merge main or deploy anything. Read `experiments/checkpoint_v1/README.md`. The experiment's existing limits remain in force. Only add files under `docs/cloud/evidence/Q8-CURSOR-CHECKPOINT-SOURCE-20260928-A1/`.

## Work to do

The attached synthetic inventory describes exhibition display units. The approved release rule requires BOTH revision r3 AND a signed check of yes. Units that fail either condition are held. A proposed exception to allow signed r2 units was rejected; do not make it current policy.

One decision is unresolved: "May loan display units be released when they meet revision r3 and signed-check requirements?" Keep it open with ID `Q_LOAN` and affected action `release-loan`. No approval to resolve this question is supplied. Compute the uncontested eligible owned units and identify disputed loan units separately. Do not complete or authorize loan release.

1. Read `q8-display-units.csv`, compute the current result and write a concise summary plus machine-readable CSV of the uncontested eligible owned units. Preserve the source CSV.
2. Use the experimental checkpoint API/CLI to save one complete checkpoint to an evidence-local store, project `q8-display`, milestone `display-source-1`, writer `cursor`. The state and log must carry the accepted rule, rejected proposal, work performed and unresolved question. Use the supplied question ID/text/action exactly in `open_questions`. Mark only work actually completed. Use `build_proposal` to build correct hashes; retain the full input JSON and CLI stdout receipts.
3. Call fresh CLI `resume` and verify both complete bodies are present. Retry the EXACT same proposal JSON and retain the second receipt. Show whether journal bytes changed across the duplicate retry. Do not invent a crash or claim lost-ack coverage from this ordinary duplicate retry.
4. Export the WHOLE original evidence-local store, including ignored journal/data/locks metadata, with a relative-path SHA-256 manifest. Git must contain the original bytes: protect these evidence files against automatic newline conversion (a scoped `.gitattributes` using `-text` is permitted inside the evidence directory). Record source CSV hash, final implementation SHA, commands, exit codes and actual client/model in REPORT.md. Reports may state limits; do not include private provider task links, account identifiers, personal paths, credentials or hidden reasoning.
5. Commit and push only your evidence branch, using neutral author and committer metadata `KnoKeep Worker <worker@knokeep.invalid>` with no coauthor trailer. Verify metadata before public push; if the platform overrides it, stop and supply a patch to the coordinator instead. Do not force-push or rewrite prior commits. No PR creation needed.

The coordinator will check original Git blobs and business meaning independently. This brief supplies task facts, not scored results. A receipt only establishes the bytes accepted by this experiment; it does not certify that your prose accurately reflects the task.
