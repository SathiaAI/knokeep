# Publication status — Q0-DEVIN-CLOUD-20260927-A1

Recorded separately, as required. Each line is an observed result, not an intent.

| Step | Status | Observed |
|---|---|---|
| Local commit | PASS | evidence commit on `q0/devin-cloud/20260927-a1` |
| Branch push | PASS | `q0/devin-cloud/20260927-a1` pushed to `origin` (`SathiaAI/knokeep`) |
| Draft PR creation | PASS | https://github.com/SathiaAI/knokeep/pull/15 — draft, base `docs/cloud-handoff-2026-09-27`. Created through the platform's Create-PR publication step, not by a local commit alone. Not merged. |
| Remote verification | PASS | fresh `git clone --branch q0/devin-cloud/20260927-a1 --depth 1` into a clean directory; head `b528bbe4642c4055e7039913a892ffc3ab07fe4c`; `sha256sum -c` over `MANIFEST.sha256` returned OK for all 5 members; byte sizes matched `MANIFEST.sizes` exactly |

Verification command run against the clean remote checkout:

```bash
git clone -q --branch q0/devin-cloud/20260927-a1 --depth 1 https://github.com/SathiaAI/knokeep.git q0verify
cd q0verify/tests/fixtures/q0/Q0-DEVIN-CLOUD-20260927-A1/store
sha256sum -c <(sed 's|  ./|  |' ../MANIFEST.sha256)
find . -type f -printf '%s\t%p\n' | sort -k2 | diff - ../MANIFEST.sizes
```

This `publication.md` was added in a follow-up commit on the same branch, so the head commit recorded above is the one verified for manifest fidelity; later commits on the branch add documentation only.

Pending: fresh-session retrieval (coordinator-dispatched separate session), `flush-log` diagnostic review, ACU/cost accounting (UNAVAILABLE from inside the runtime).
