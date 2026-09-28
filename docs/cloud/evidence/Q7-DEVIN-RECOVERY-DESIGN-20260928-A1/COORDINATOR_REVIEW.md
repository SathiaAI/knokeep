# Coordinator review — September 28, 2026

Reviewed actual Devin A2 head `99bde386807cc66ddcd021ef2ef319a131f9d411`. Product source and existing tests are unchanged from input `380fe31e592b1aade88f7fcc7351455b1d4191af`. A1 and A2 source/results are preserved.

A1 failed immediately on Windows: the exporter reopened its own byte-locked `cas.lock` through a second handle. A2 fixed that by reading through the held handle. The original A2 Windows run reached the concurrent-writer fixture, then its **test comparator** repeated the second-handle problem while the simulated writer still held the lock.

The adjacent coordinator runner changes only that fixture's ordering: capture the export refusal while the simulated writer holds the lock, release the writer, then compare every input byte. It does not change `analyze_and_export`. With that declared correction, **24 cases pass on Windows, zero skips, process exit 0**. Devin separately reported 24 on Linux using the original A2 fixture. These are not the same exact runner; no claim is made that original A2 passes Windows unchanged.

Verified Windows cases include the held-lock byte copy, refusing busy export, partial legacy/v2 tails, ambiguous later frames, coherent rollback being labelled completeness-unproven, output overlap, preserved incomplete attempts, case-fold collisions, symlink and hard-link rejection. The original failures remain available in the coordinator's private run evidence; public result output contains no personal paths.

Accepted as **design evidence only**. It is not a repair command or a service-restoration implementation. Shipping still requires streaming I/O, durable output publication, bounded failure handling, tested platform path rules and an explicit way to approve an incomplete recovery candidate. The probe does not establish a hostile-filesystem security boundary. No archive or journal was automatically truncated.

The two A2 commits have verified neutral author and committer metadata. Earlier A1 and PR63 provider commits did not meet that requirement; existing public metadata may remain available. Future jobs must use and verify neutral per-command metadata before pushing.
