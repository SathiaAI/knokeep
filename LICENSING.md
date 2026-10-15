# Licensing

Copyright (c) 2026 Viaknox Holding LLC.

**Releases and commits before this PR remain available under AGPL-3.0-only. The new terms apply from this commit on.**

## Folder → license

Every file in this repository is covered by exactly one row. The most specific
row wins; `/` covers everything no other row matches. CI
(`.github/workflows/license-headers.yml`) fails if any source file's
`SPDX-License-Identifier` header does not match this table.

<!-- license-table:start -->
| Path | SPDX license | What it is |
|---|---|---|
| `/` | `Elastic-2.0 OR LicenseRef-Viaknox-Commercial` | Everything not listed below |
| `conformance/` | `Apache-2.0` | Shape checker (backend conformance suite) |
| `fixtures/` | `Apache-2.0` | Starter test cases |
| `docs/SCHEMA.md` | `Apache-2.0` | Data format |
<!-- license-table:end -->

License texts:

- `Elastic-2.0` — [`LICENSE`](LICENSE)
- `LicenseRef-Viaknox-Commercial` — [`LICENSE-COMMERCIAL`](LICENSE-COMMERCIAL)
- `Apache-2.0` — `LICENSE` inside each Apache folder ([`conformance/LICENSE`](conformance/LICENSE), [`fixtures/LICENSE`](fixtures/LICENSE)); for the single file `docs/SCHEMA.md` see [conformance/LICENSE](conformance/LICENSE) (same text)

## Third-party lines

These files contain lines from commit `0306a8ed27ce3fcd9315b89aa4566ab62d3a79c1`
(author: `coderabbitai[bot]`). Until those lines are reviewed and rewritten, the
files keep an AGPL header, not Elastic-2.0. CI accepts `AGPL-3.0-only` for these
files only.

<!-- third-party:start -->
| File | SPDX license |
|---|---|
| `store/git_backend.py` | `AGPL-3.0-only` |
| `store/objectstore.py` | `AGPL-3.0-only` |
| `store/postgres.py` | `AGPL-3.0-only` |
| `tests/test_git_backend.py` | `AGPL-3.0-only` |
| `tests/test_local_backend.py` | `AGPL-3.0-only` |
| `tests/test_objectstore.py` | `AGPL-3.0-only` |
| `tests/test_postgres.py` | `AGPL-3.0-only` |
<!-- third-party:end -->

AGPL-3.0-only text: the `LICENSE` file at any commit before this PR, or
<https://www.gnu.org/licenses/agpl-3.0.txt>.
