# Continuation: Q0-CLAUDE-CODE-20260927-A1

Status: PENDING. Fresh-session retrieval has not been observed.

- Repository: `SathiaAI/knokeep`
- Branch: `q0/claude-code/20260927-a1`
- Commit: the commit containing this file, as recorded by the coordinator after publication
- Project ID: `q0-claude-code-a1`
- Store export: `tests/fixtures/q0/Q0-CLAUDE-CODE-20260927-A1/store/`
- Manifest: `tests/fixtures/q0/Q0-CLAUDE-CODE-20260927-A1/manifest.json`

Retrieval commands, from a fresh checkout of that commit:

```
python docs/cloud/evidence/Q0-CLAUDE-CODE-20260927-A1/probe.py verify
python -c "import shutil; shutil.copytree('tests/fixtures/q0/Q0-CLAUDE-CODE-20260927-A1/store', 'q0-restore')"
python skill/knokeep_state.py bootstrap --store q0-restore --project q0-claude-code-a1
```

The resulting output is a mechanical retrieval check.
