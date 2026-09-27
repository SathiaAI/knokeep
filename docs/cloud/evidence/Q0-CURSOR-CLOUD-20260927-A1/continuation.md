# Continuation coordinates (Q0-CURSOR-CLOUD-20260927-A1)

Immutable inputs for a **separate** fresh-session verification task (not executed in this run).

## Checkout

- Repository: `SathiaAI/knokeep`
- Evidence branch: `q0/cursor-cloud/20260927-a1` (verify remote head after coordinator push)
- Product input commit: `49e4c1cc6ad75113c97de856a069a670bf0696df`
- Original evidence commit (immutable): `1a11fbc4ab0fd0a1254c04922ccbbdb8f54a699b`
- For the corrected verifier, use the immutable reviewed PR head supplied by the coordinator; record it before execution.
- Evidence directory: `docs/cloud/evidence/Q0-CURSOR-CLOUD-20260927-A1/`

## Synthetic store export

- Export path: `tests/fixtures/q0/Q0-CURSOR-CLOUD-20260927-A1/store-export/`
- Manifest: `tests/fixtures/q0/Q0-CURSOR-CLOUD-20260927-A1/MANIFEST.sha256`
- Project slug inside store: `q0cursor`
- CLI system_state content hash after probe: see `logs/probe_summary.json` → `cli_system_state_hash`

### Restore to a disposable LocalBackend root (successor session)

```bash
JOB=Q0-CURSOR-CLOUD-20260927-A1
EXPORT="tests/fixtures/q0/${JOB}/store-export"
RESTORE_ROOT="$(mktemp -d /tmp/q0_restore_XXXXXX)"
cp -a "${EXPORT}/." "${RESTORE_ROOT}/"
python3 docs/cloud/evidence/Q0-CURSOR-CLOUD-20260927-A1/verify_export.py || exit 1
python3 skill/knokeep_state.py bootstrap --store "${RESTORE_ROOT}" --project q0cursor
python3 -c "
import json, sys
sys.path.insert(0, '.')
from store.local import LocalBackend
b=LocalBackend('${RESTORE_ROOT}')
r=b.read('q0cursor/system_state')
b.close()
print(json.dumps({'found': r is not None, 'version_hash': r.version_hash if r else None}))
"
```

Expected: `found` true; `version_hash` matches `cli_system_state_hash` in probe summary.

## Pending observations (coordinator / successor)

| Track | Status |
|---|---|
| Fresh-session retrieval | **PENDING** — requires new agent session with no shared `/tmp` |
| Native KnoKeep MCP on Cursor Cloud | **BLOCKED** — no `knokeep_*` tools in host MCP catalog (KnoTrack `kt_*` is separate) |
| Automatic capture | **NOT TESTED** — Q0 scope |
| Draft PR publication | **PENDING** — dispatcher disabled auto-PR; coordinator opens draft to `docs/cloud-handoff-2026-09-27` |

## Client identity note

Probe metadata used `client` values emitted by `knokeep_state.py` on this cloud runtime. Successor should compare persisted `client` fields in exported blobs with the actual successor runtime and report mismatches without rewriting source evidence.

Coordinator correction: the original saved state says `client: cowork`; see `coordinator-review.md`. Compare this against the original source runtime (Cursor Cloud), not against a successor that merely reads the record.
