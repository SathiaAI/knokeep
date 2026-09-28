# Independent recovery-export review and reproduction

Actual Cursor authored the initial prototype; actual Devin corrected it on the existing included route; Codex independently reviewed the code, reproduced failures and tested the correction on Windows. This packet supports a synthetic experiment only, not real-store recovery or release.

| Attempt | Exact commit | Observed result |
|---|---|---|
| Devin A1 | 891ee85c89bedddf25ee7784acd0fbeb20cbe390 | Provider: 38 Linux passes. Coordinator Windows: 35 passed, two failed, one POSIX-only skip. Windows hard-link rejection was defective; a separate fsync fault-injection fixture also failed before its intended point. |
| Devin A2 | 6d8b67ad7e135907ec2e46b502f58018d9e98509 | Provider: 47 Linux passes. Coordinator: 46 Windows passes, one explicitly POSIX-only skip. Windows real hard-link regression now passes; archive/candidate budgets and malformed manifest handling also corrected. |
| Integrated experiment and review | 054a51a7ef83d873aecb2659e71edb8b6472cbfe | PR71; product code is byte-identical to Devin A2. Coordinator documentation states narrow acceptance and remaining limits. Consult exact-head CI, not an older green run. |

The [original A1 Windows CI failure](https://github.com/SathiaAI/knokeep/actions/runs/36389892493/job/108823188719) matches the local failures. `a1-windows-redacted.txt` is a declared derivative with the local workspace prefix replaced; its provenance file hashes the locally preserved original. Nothing is silently relabelled as a passing original run. The A2 log contains no local paths and is preserved byte-for-byte. Devin's original report and both source commits remain in PR71 history.

`probe_q9_lifecycle.py` independently performs three real child-process exits on newly created synthetic stores: writer exit after journal fsync before publication, a deliberately injected partial append followed by fsync/exit, and exporter exit after manifest write before rename. Each A1/A2 run passed all eight assertions. It checks original-byte preservation, explicit ambiguity, rejection and retention of incomplete exports, successful new attempts, and refusal to overwrite completed outputs. The partial-write fixture is explicitly injected; none of these is a power-loss test. Fixtures are retained; no source is repaired or service switched.

Reproduce from a checkout containing the experiment with installed Python: `python probe_q9_lifecycle.py CHECKOUT RESULT.json`. Run the provider suite with `python -m unittest discover -s experiments/recovery_export_v1 -p "test_*.py" -v`. Windows coordinator harness constrained temporary cleanup to its own disposable fixture root; product code and test assertions were not patched. One POSIX-only skip remains a skip.

Limits remain: cooperative stable filesystem, unproven hostile-path race handling and power-loss durability, unsupported Windows directory fsync, fixed size/count limits, no empty-directory topology preservation, and historical ATTEMPT.json outside the verification contract. An internally consistent archive is neither authenticated nor proof that history is complete. Candidate data stays non-authoritative.
