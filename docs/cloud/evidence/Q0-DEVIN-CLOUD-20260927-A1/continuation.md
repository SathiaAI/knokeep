# Continuation coordinates — Q0-DEVIN-CLOUD-20260927-A1

Neutral handoff. Coordinates and retrieval commands only; no narrative, no answer key.

- Project ID: `q0-devin-cloud-20260927-a1`
- Job ID: `Q0-DEVIN-CLOUD-20260927-A1`, attempt generation 1, node F
- Input commit: `49e4c1cc6ad75113c97de856a069a670bf0696df` on `docs/cloud-handoff-2026-09-27`
- Output branch: `q0/devin-cloud/20260927-a1`
- Evidence path: `docs/cloud/evidence/Q0-DEVIN-CLOUD-20260927-A1/`
- Store export path: `tests/fixtures/q0/Q0-DEVIN-CLOUD-20260927-A1/store/`
- Manifests: `tests/fixtures/q0/Q0-DEVIN-CLOUD-20260927-A1/MANIFEST.sha256`, `MANIFEST.sizes`
- Store keys present: `q0-devin-cloud-20260927-a1/system_state`, `.../session_log`, `.../sessions/Q0-DEVIN-CLOUD-20260927-A1`, `.../mcp_harness_note`

Retrieval:

```bash
git clone https://github.com/SathiaAI/knokeep.git && cd knokeep
git checkout q0/devin-cloud/20260927-a1
cd tests/fixtures/q0/Q0-DEVIN-CLOUD-20260927-A1/store && sha256sum -c <(sed 's|  ./|  |' ../MANIFEST.sha256) ; cd -
RESTORED=$(mktemp -d); cp -a tests/fixtures/q0/Q0-DEVIN-CLOUD-20260927-A1/store/. "$RESTORED"/
python3 skill/knokeep_state.py --store "$RESTORED" --project q0-devin-cloud-20260927-a1 bootstrap
```

No shared `/tmp` between hosts is assumed; the export travels in Git.

Status: fresh-session retrieval is **PENDING**. It requires a genuinely separate task/session dispatched by the coordinator after the coordinator has verified this export is fetchable from GitHub. A new session ID alone is not isolation, and a same-session process restart is not fresh-session resume. Any resulting restore from this public packet is a mechanical retrieval check, not a blind behavioral test.
