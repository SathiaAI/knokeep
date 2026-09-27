# Separate-session continuation coordinates

Repository: `/workspace/knokeep`
Committed store: `tests/fixtures/q0/codex-cloud-source-20260927/store`
Project: `q0-codex-cloud-synthetic`

In a genuinely separate Codex cloud session, restore the read-only fixture to a disposable directory and bootstrap it:

```bash
cd /workspace/knokeep
RESTORE="$(mktemp -d /tmp/knokeep-q0-codex-resume.XXXXXX)"
cp -a tests/fixtures/q0/codex-cloud-source-20260927/store/. "$RESTORE/"
python skill/knokeep_state.py bootstrap \
  --store "$RESTORE" \
  --project q0-codex-cloud-synthetic
```

Record the command output and session identity. Do not read the evidence report before recording the retrieved result. Do not write to the committed fixture.
