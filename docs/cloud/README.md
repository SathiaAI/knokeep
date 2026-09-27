# KnoKeep cloud handoff

September 27, 2026. This is a technical handoff prepared for repository-based cloud work. It summarizes the reviewed direction; it does not certify that the planned integrations or tests have run.

## Start here

1. Record the checked-out commit and the actual client, execution surface, model and tool access.
2. Read [orchestration](orchestration.md), [continuity acceptance](continuity-acceptance.md) and [design constraints](design-constraints.md).
3. Read the repository's current [README](../../README.md), [CI workflow](../../.github/workflows/ci.yml) and relevant implementation before proposing changes. Verify actual state rather than treating a historical test count as a current pass.
4. Begin with the bounded synthetic capability probe assigned to this client. Report missing tools or permissions explicitly. A local runtime must not be relabeled as a cloud runtime.

The goal is that a person can change clients on the same project and continue with the correct files, decisions, unresolved conflicts and next action, without reconstructing the project manually.

## Repository state used to prepare this handoff

The preparation base was `b05fd870f2a396ead946693506c8da09ccfc5311` on `main`. Its title is “Align product description to Viaknox canonical descriptor.” The earlier Phase 3 handoff references merge `fc8fb4687d82675edfc309aed1498a61100f4ab6` and reports 683 passing tests, 36 skips and five passing standalone scripts. Those results are historical reports, not a fresh test run performed for this documentation change.

The current implementation includes local, Git, object-store and Postgres backends, an MCP server and migration/reconciliation code. A hosted HTTP record service and the complete cross-client capture plan remain proposed work. Do not replace the working implementation merely because a pivot document describes a different future design.

## Running the existing checks

Use the explicit test lists and dependencies in [CI](../../.github/workflows/ci.yml). Its ordinary pytest suite and five standalone scripts are separate. A bare repository-wide pytest invocation is not the documented CI recipe: some script-style tests exit during collection.

The five standalone scripts are `tests/test_v1.py`, `tests/test_concurrency.py`, `tests/test_eval.py`, `tests/test_audit.py` and `tests/test_skill_mcp_shared_store.py`. Run them separately as CI does. Record skips and missing services; a skipped Postgres test is not evidence that Postgres behavior passed. Never use live project stores for destructive or failure-injection tests.

No application tests were rerun solely to add these Markdown documents. Documentation validation covers internal links, patch whitespace, publication scope and secret scanning.

## Scope of this public packet

This packet contains technical design constraints, client assignments and proposed acceptance checks. Personal account records, credentials, private chat links, machine-specific directories, spending authorizations and the original internal review bundle are deliberately excluded. Obtain any necessary runtime permissions and budget from the task's actual authorization; this document grants none.

Keep the working repository and any cloud checkout tied to explicit commits. A cloud environment receives repository contents from the selected branch; an unsaved local document does not become cloud context automatically.
