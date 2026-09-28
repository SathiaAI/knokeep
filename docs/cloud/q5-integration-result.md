# Q5 reviewed integration candidate

September 28, 2026. This combines PR57 recovery-before-CAS, PR60 journal length bounds, PR62 read-only health inspection and PR53 capture/resume documentation. It does not include the experimental checkpoint API from PR58 and is not a production release.

The combined assembly at bda1fa5 had Git tree `fdf5f2d9aaaabdb5cf23ba22e813aed254874092`. Publication commit `299fe8834336b0c83e8d45df36fb0948e80db9b8` preserves that exact tree with coordinator author metadata. Original component histories remain preserved. The only subsequent changes are this document and a stronger health regression that tests malformed, missing, numeric and null error timestamps separately. Product source is unchanged from the tested assembly.

## Verification

- Full workflow V2 test list on Windows: **701 passed, 40 skipped**, process exit0, 341.94 seconds. Explicit pytest targets were conformance plus boundary, gate hardening/supplement, plugin root, client attribution, local/git/objectstore/postgres backends, MCP, migration, three reconciler suites, resume sections and session-append idempotency. Skips are not passes; GitHub's Linux service job supplies backend coverage unavailable in this local run.
- Skill acceptance: **33/33 V1**, concurrency passed, **10/10 evaluation**, **7/7 shared CLI/MCP store**. Health initially passed49/49 on the combined tree; after strengthening the test, it passed **58/58**.
- The first audit script exited0 with a scanner-unavailable skip. It was rerun with the already installed scanner on that process's PATH and passed **5/5**. No global PATH change or installation was made.
- Codex's independent health probes passed **11/11** on the Cursor component, including the isolated malformed timestamp that failed before its correction. The strengthened shipped test now exercises that path without another invalid row masking it.
- Actual Devin independently reviewed the parser component in PR63; Codex reproduced all **17 passing probe cases on the fix and six failures on its prior base**. Both byte preservation and input size bounds were checked without large allocations. Devin's unverified historical-key and concurrent-startup-lock questions remain open.
- PR57, PR60 and PR62 had green exact-head cross-platform checks. This integration branch requires its own checks; prior component results are not substitutes.

## Unresolved release conditions

A torn journal tail still blocks all later writes without a safe recovery procedure. Original ambiguous bytes must be preserved; automatic truncation is not authorized by this candidate. Long-lived readers and concurrent startup replay need further qualification. Health assumes a cooperative offline snapshot; it is not a security sandbox or a linearizable read. Client instructions do not enforce complete semantic capture, and hashes/receipts do not prove meaning or completeness. The local-model comparison qualifies only a small, checked task role. Fresh Cowork enrollment, decision authentication, realistic multi-client continuation and external demand evidence remain separate work.
