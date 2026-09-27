# Continuation coordinates — Q0-COWORK-CLOUD-20260927-A1

- Repository: https://github.com/SathiaAI/knokeep.git
- Input commit: `49e4c1cc6ad75113c97de856a069a670bf0696df`
- Output branch: `q0/cowork-cloud/20260927-a1`; output commit: see `report.md` Publication section of that branch
- Store export: `tests/fixtures/q0/Q0-COWORK-CLOUD-20260927-A1/store/` with `MANIFEST.sha256-size.txt`
- KnoKeep project ID: `q0-cowork-cloud-a1-plan`

Retrieval:

```
git clone https://github.com/SathiaAI/knokeep.git && cd knokeep
git fetch origin q0/cowork-cloud/20260927-a1 && git checkout FETCH_HEAD
(cd tests/fixtures/q0/Q0-COWORK-CLOUD-20260927-A1 && while read h s p; do echo "$h  store/$p"; done < MANIFEST.sha256-size.txt | sha256sum -c)
RESTORE=$(mktemp -d)/store && cp -a tests/fixtures/q0/Q0-COWORK-CLOUD-20260927-A1/store "$RESTORE"
python3 skill/knokeep_state.py bootstrap --store "$RESTORE" --project q0-cowork-cloud-a1-plan
```
