# Codex cloud Q0 source-store export

Job: `Q0-CODEX-CLOUD-20260927-EXPORT-A1`

This fixture is a byte-for-byte export of the original synthetic source store that remained at `/tmp/knokeep-q0-codex-cloud-20260927T202943Z-9730d654cca4`. It preserves published data, the append-only journal, evaluation events, and durable fence-allocation metadata. The source inputs that were still available are under `source-inputs/`.

Omitted as transient or empty runtime state:

- `store/locks/advisory.lock` and `store/locks/cas.lock`: empty process-lock files recreated by `LocalBackend`.
- `store/locks/advisory/`: empty; no active advisory lease existed.
- `store/staging/`: empty; no staged write existed.

`locks/fence-alloc/` was retained because the implementation describes those files as durable cross-process fence-ownership records. Do not point a writer directly at the committed fixture. Use `verify_export.py`, which restores into a temporary directory before opening the store.

`MANIFEST.sha256` records SHA-256, byte size, and bundle-relative path for every exported source byte. It intentionally excludes documentation, the manifest itself, and executable verification/continuation helpers.
