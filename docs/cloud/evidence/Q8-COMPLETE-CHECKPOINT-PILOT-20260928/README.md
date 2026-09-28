# Q8 actual-client complete-checkpoint pilot

Scored September 28, 2026 after both workers completed. The design and oracle stayed private until scoring; the preserved local hash matches. That local file/hash is not an independently timestamped preregistration. Both workers had repository/network access, so curated input is not enforced isolation.

- Actual Cursor Composer 2.5 source: [PR68](https://github.com/SathiaAI/knokeep/pull/68), publication ff6c6275c71f3d0439d6e370be73cfae7f298cd4. Exact tree of original 8e0e0c34660f025a2de7497d77d3d7278fdd1bda; six store files verified against Git blobs.
- Fresh actual Devin receiver: [PR69](https://github.com/SathiaAI/knokeep/pull/69), d0b84084805e39c7a4f35b62798710df1743cd7b. Seven store files verified, original source store and successor CSV unchanged, original journal exact prefix, prior proposal immutable. Underlying Devin model UNVERIFIED.
- Canonical proposal hashes, full returned state/log, and accepted lookup receipts matched. Source eligible owned units were cedar/ash; receiver correctly changed to cedar/oak, held ash after its signature changed, retained the rejected r2 exception, and left elm/pine disputed. Q_LOAN and its affected action carried exactly without invented resolution.
- Source ordinary same-ID retry reported unchanged journal length/hash; the final hash was independently verified. This is not a lost-response or crash test.
- Codex Windows reproduction on a copy of the actual receiver store: fresh CLI resume returned the exact new bodies; prior lookup accepted; repeating the receiver proposal reported duplicate and left the copied journal byte-identical. Original client bytes stayed unchanged.

Verdict: **PASS for this guided CLI workflow and transport; narrow semantic pass on the specified fixture.** No capture-rate estimate, incremental-value comparison, native MCP qualification, authenticated writer or completeness guarantee. Product code is PR58 at eee994ab3cf9b89407c373497387b62f6d03673a, not the new PR66 engine. The prototype remains experimental pending history/scale/auth/recovery and broader semantic testing.
