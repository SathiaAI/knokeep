# Q3: interruption, retry and competing writers

Measured on Windows, September28 UTC, product `8fb9ef7ac4290b43352a40a9690c13c1f32a8917`. These are disposable, deterministic process/storage probes, not two cloud clients or network fault injection. Original Q1 evidence is in PR42; no original store was modified.

| Probe | Observed result | Meaning |
|---|---|---|
| Process exits73 immediately after the real journal append/fsync, before create reply | Fresh backend recovers exact body; identical create retry returns OK without growing journal | Per-document durability and identical create replay survived this boundary |
| Same crash boundary during session append | Recovered entry appears once; retry of identical request makes two entries | Lost-ack append retry duplicates; issue43 and fixPR44 |
| Two independent local writer processes use one expected log hash | One succeeds; the other's exact body is parked and bootstrap reports one conflict | This stale-write case preserves both writers' content |
| Replay original Q1 journal prefix ending at accepted state68c736be… | Architecture permits approved holds; Hard Constraints forbids all holds; bootstrap has no warning and still displays old completed state | Durable documents do not make an entire milestone atomic or semantically consistent |
| Eight Git writers, first acquired helper lease paused6seconds before release | One OK, seven BackendBusyError at approximately5seconds; final bytes equal winner | Reproduces issue33 setup-budget mechanism on both baseline and integrated code |

The append crash test changes the disposable process's lease TTL to0.1s solely to shorten expiry recovery; it does not change production configuration. No cloud transport is involved. New stable operation IDs in PR44 are scoped to a retained project/session journal; old no-ID calls remain at-least-once.

The original accepted prefix is exactly3789bytes of `source-journal.bin`, whose full SHA-256 is `16ecf88551dea073b7afcf5c2a3ab1abf21ef0503f1279b45e060ada9cdd5924`. Replayed state hash is `68c736be709c57740c6343b976e8f7961ac879ac86f6167528d216c633b495e8`. This prefix reconstruction is labelled explicitly; it is not an observed crash in Claude's original session. The other two crash probes actually terminate their subprocess after fsync.

For the multi-write risk, write all related sections of one document in a single guarded update. Across documents, a future complete-checkpoint marker must bind the exact state/log versions before calling a milestone resumable; changing prose alone cannot supply cross-document atomicity. Bootstrap currently does not detect arbitrary contradictory prose. Keep this gate unresolved until an interrupted source and successor test passes.

The Git contention probe runs the same controlled scheduling pause on baseline `cf01c007054a5a91de4bd0666c623b66274f0dc0` and integrated `8fb9ef7ac4290b43352a40a9690c13c1f32a8917`; both produce seven explicit BUSY exceptions. This demonstrates a mechanism consistent with the original Windows failure, not proof of that runner's exact scheduling history or a production contention rate. Keep the original failed CI attempt. Isolate advisory-lease setup from the CAS race while keeping separate BUSY/TTL coverage; do not hide exceptions or extend arbitrary retry budgets.

`probe.py` is a path-adapted version of the executed local harness. Run from this repository root with stdlib Python; it creates a new `q3-probe-output` folder and refuses to overwrite it. `git_contention_probe.py <repository> <new-output-directory>` requires Git and runs eight local workers against a new bare repository. Each Git command has a10-second limit, but this original diagnostic lacked a separate overall supervisor; both observed executions finished in about8seconds. Future repeated qualification should use the approved outer supervisor. Original observed results are `results.json` and the two contention JSON files. No production host, credential, billing setting or real project data was involved.
