# Preserved recovery-export drafts and independent results

These are original actual Cursor file bytes, stored with .txt suffixes as evidence. They are untrusted, unqualified drafts, not files to execute or a product release. Both private deliveries had all four SHA-256 values verified before materialization. No Cursor source branch was published. Coordinator-produced [PR71](https://github.com/SathiaAI/knokeep/pull/71) is a separate explicitly unqualified experiment.

A1: provider reported 12 Linux passes. Original Windows probes all failed with errno9 at a redundant fsync on a read-only reopened destination. A diagnostic-only in-memory suppression of that second fsync (the copy already fsynced its writable handle) exposed four additional failures. This intervention is not an original-A1 success claim and no source draft bytes were changed.

A2: provider reported 16 Linux passes; Codex independently ran all 16 on Windows, then the four discriminating cases passed without patching product code. Original source bytes and failed attempts were retained. Remaining boundedness, malformed-input/link/incomplete verification and publication-durability findings still block acceptance. See the PR71 coordinator review. The internal manifest hash is not authentication or a completeness proof. Neither draft restores service or authorizes a recovery choice.

The preceding read-only Cursor design review is separately preserved under Q9-CURSOR-RECOVERY-REVIEW-20260928-A1. No hidden reasoning, private provider identifiers or account metadata are included here.
