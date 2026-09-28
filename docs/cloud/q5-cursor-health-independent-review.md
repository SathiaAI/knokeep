# Independent review of the Cursor health change

September 28, 2026. Original Cursor head `1b1bbf08e3bbe31dff28ce4f9fc2779f945be9c3`. Metadata-clean derivative `2bf3bcf067129e5ce7f51748f8f312f73cdb1ac9` has the identical Git tree `c199f53b29d6953d532a996f4aa69a6caa1857f3`. Earlier provider commits remain public; this is not an erasure claim.

Actual Cursor Composer 2.5 implemented this change under direct Codex orchestration. Codex reviewed the final code and independently reproduced defects across four versioned passes. Initial failures were retained, then corrected on the same worker branch.

- Independent coordinator probes: **11/11 pass** on Windows. These cover missing journal, concurrent journal growth, indeterminate storage, traversal key, invalid UTF-8, malformed JSON row shapes, bad error timestamp, and dangling telemetry symlink. The timestamp case failed on the previous head before the final one-line correction.
- Product acceptance: **49/49 health checks**, **10/10 evaluation checks**, executed independently on Windows at the original final head.
- Health is inspected without constructing LocalBackend, so it does not create a missing store or replay journal records into published data. Corrupt framing, unpublished durable records and malformed telemetry produce attention.
- The preceding round passed all four CI checks. CI for this exact final/publication head is recorded separately; do not substitute prior-head checks.

## Scope and limits

This is a LocalBackend diagnostic, not repair or a security sandbox. Inspection assumes a cooperative offline filesystem snapshot. Journal-size change detection does not make reads linearizable. Active writers, replacement races and all Windows reparse variants are not qualified by these tests. A missing external secret scanner is disclosed as unavailable. Ordinary evaluate behavior is unchanged. PR57 still leaves torn journal tails write-blocking; PR60 hardens parser length checks separately. No production-readiness claim or main merge follows from this review.

One worker test combines invalid timestamp rows; the independent coordinator case isolates a malformed timestamp, so its passing result directly exercises the formerly broken return path.
