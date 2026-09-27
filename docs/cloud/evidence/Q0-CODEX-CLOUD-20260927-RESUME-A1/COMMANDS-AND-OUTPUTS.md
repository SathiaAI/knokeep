# Exact commands and outputs

Commands are listed in execution order. `$RESTORE` resolved to `/tmp/knokeep-q0-codex-resume.m9C9T3` for this run.

## 1. Input identity and continuation coordinates

```bash
git rev-parse HEAD && cat tests/fixtures/q0/codex-cloud-source-20260927/CONTINUE.md
```

```text
5637ba7e6f5b980368cdc80a77562c832df0a3d6
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
```

## 2. Restore and bootstrap

```bash
set +e
RESTORE="$(mktemp -d /tmp/knokeep-q0-codex-resume.XXXXXX)"
printf '%s\n' "$RESTORE" > /tmp/knokeep-q0-resume-path
cp -a tests/fixtures/q0/codex-cloud-source-20260927/store/. "$RESTORE/"
python skill/knokeep_state.py bootstrap \
  --store "$RESTORE" \
  --project q0-codex-cloud-synthetic \
  >"$RESTORE/bootstrap.stdout" 2>"$RESTORE/bootstrap.stderr"
rc=$?
printf '%s\n' "$rc" > "$RESTORE/bootstrap.exit-code"
```

```text
RESTORE=/tmp/knokeep-q0-codex-resume.m9C9T3
STDOUT={"version_hash": "87164697d7b3b601b59df5c38c30dc4ea3dacc336f122c864231bc37c990f53f", "revision": 1, "log_hash": "fa80e5a2262bca138a5ff159cb548e30c84a654c361a09acea0b42fc6d83384e", "active": "Q0 Codex cloud smoke test evidence collection.", "next": "In a genuinely separate Codex cloud session, bootstrap this exact store and report marker violet-orbit-7319 before reading this evidence report.", "conflicts": [], "conflict_count": 0, "settled_count": 0, "resume_line": "resuming: Q0 Codex cloud smoke test evidence collection. / next: In a genuinely separate Codex cloud session, bootstrap this exact store and report marker violet-orbit-7319 before reading this evidence report. / v87164697d7b3"}
STDERR=
EXIT_CODE=0
```

## 3. Pre-disclosure receipt hashes

```bash
sha256sum "$RESTORE/data/q0-codex-cloud-synthetic/system_state" "$RESTORE/data/q0-codex-cloud-synthetic/session_log"
```

```text
87164697d7b3b601b59df5c38c30dc4ea3dacc336f122c864231bc37c990f53f  /tmp/knokeep-q0-codex-resume.m9C9T3/data/q0-codex-cloud-synthetic/system_state
fa80e5a2262bca138a5ff159cb548e30c84a654c361a09acea0b42fc6d83384e  /tmp/knokeep-q0-codex-resume.m9C9T3/data/q0-codex-cloud-synthetic/session_log
```

## 4. Independent manifest verification after disclosure

The first attempt used `sha256sum -c MANIFEST.sha256`; it failed because this repository's manifest has a third byte-size column, which is not `sha256sum` check-file syntax. That was a command-format error, not a data mismatch. The corrected first retry was:

```bash
set -euo pipefail
BUNDLE=tests/fixtures/q0/codex-cloud-source-20260927
count=0
while IFS= read -r line; do
  expected_sha="${line%%  *}"
  rest="${line#*  }"
  expected_size="${rest%%  *}"
  rel="${rest#*  }"
  actual_sha="$(sha256sum "$BUNDLE/$rel" | cut -d' ' -f1)"
  actual_size="$(stat -c %s "$BUNDLE/$rel")"
  test "$actual_sha" = "$expected_sha"
  test "$actual_size" = "$expected_size"
  printf 'MATCH sha256=%s size=%s path=%s\n' "$actual_sha" "$actual_size" "$rel"
  count=$((count + 1))
done < "$BUNDLE/MANIFEST.sha256"
printf 'MATCHED_MANIFEST_FILES=%s\n' "$count"
```

```text
MATCH sha256=9ae7bf4baa5d2f1d5d2cd4421d978651e074acea67d6dd33bff7d2576a616f8b size=348 path=source-inputs/session-log.md
MATCH sha256=dfe6bc98e4082206dabd1398b1a4630191f0f9d77d901b00c1275c45e2a36de3 size=314 path=source-inputs/system-state.md
MATCH sha256=3013bd65c3ee8409b5d2b1559fb1644069270e87fcb3f4871b309ea703edab07 size=767 path=store/.knokeep-eval/events.jsonl
MATCH sha256=a6f9d7a732af6adb07cc99e59691da0f771c69d71d3177824bff610377a2b48b size=44 path=store/data/q0-codex-cloud-synthetic/mcp_probe
MATCH sha256=fa80e5a2262bca138a5ff159cb548e30c84a654c361a09acea0b42fc6d83384e size=453 path=store/data/q0-codex-cloud-synthetic/session_log
MATCH sha256=77692f66feb86a79f3e7c2791e3e74df1a4538ac5c7077f6dd40cf3090273399 size=292 path=store/data/q0-codex-cloud-synthetic/sessions/q0codexcloudone
MATCH sha256=87164697d7b3b601b59df5c38c30dc4ea3dacc336f122c864231bc37c990f53f size=434 path=store/data/q0-codex-cloud-synthetic/system_state
MATCH sha256=bdd430d339a2fc276563fefb90baa61c2dcdd7d17707d69c4629c89e9aeb7908 size=1603 path=store/journal/journal.log
MATCH sha256=ce669b693c66a5e9b4d2f057faf019342bd421b034e42b9cac787254a2583aa3 size=53 path=store/locks/fence-alloc/3e004db5d5a596204cf382b60d65da06c6cba8ec2719f854a7271d58c35326c0.fence
MATCH sha256=91ad17524a5bf7ab5684f13635bf4686e213f045fa95f45b14d4a0eb02b93d7c size=51 path=store/locks/fence-alloc/4ff973e95ea1b1b80ab9a1268a096cdba578b974e4161375e26be5283ea77f14.fence
MATCH sha256=104210ba1550cb67b81b2c6f7ae6bb69cdd3ce477a48d1aab313512f92601080 size=53 path=store/locks/fence-alloc/78ebb9f9ef75cb96812baac6eec7bba8dceb4190056124bc3bfdcb3f2e8e1e6b.fence
MATCH sha256=855b1bdc2c8de0d9a607c78ac2583839bfd57a45cc5eb3eede9a05e7427293c8 size=53 path=store/locks/fence-alloc/f254dc343ea96d28c9259ec1c699d16590f027a8075a7328dc718064dd4810df.fence
MATCHED_MANIFEST_FILES=12
```

## 5. Restored bytes, receipts, verifier, and fixture cleanliness

```bash
while IFS= read -r line; do
  rest="${line#*  }"; rel="${rest#*  }"
  case "$rel" in store/*) cmp --silent "$BUNDLE/$rel" "$RESTORE/${rel#store/}" && printf 'MATCH %s\n' "$rel";; esac
done < "$BUNDLE/MANIFEST.sha256"
sha256sum "$RESTORE/data/q0-codex-cloud-synthetic/system_state" "$RESTORE/data/q0-codex-cloud-synthetic/session_log" "$RESTORE/data/q0-codex-cloud-synthetic/mcp_probe"
python "$BUNDLE/verify_export.py"
git diff --exit-code -- "$BUNDLE"
git status --short -- "$BUNDLE"
```

```text
MATCH store/.knokeep-eval/events.jsonl
MATCH store/data/q0-codex-cloud-synthetic/mcp_probe
MATCH store/data/q0-codex-cloud-synthetic/session_log
MATCH store/data/q0-codex-cloud-synthetic/sessions/q0codexcloudone
MATCH store/data/q0-codex-cloud-synthetic/system_state
MATCH store/journal/journal.log
MATCH store/locks/fence-alloc/3e004db5d5a596204cf382b60d65da06c6cba8ec2719f854a7271d58c35326c0.fence
MATCH store/locks/fence-alloc/4ff973e95ea1b1b80ab9a1268a096cdba578b974e4161375e26be5283ea77f14.fence
MATCH store/locks/fence-alloc/78ebb9f9ef75cb96812baac6eec7bba8dceb4190056124bc3bfdcb3f2e8e1e6b.fence
MATCH store/locks/fence-alloc/f254dc343ea96d28c9259ec1c699d16590f027a8075a7328dc718064dd4810df.fence
87164697d7b3b601b59df5c38c30dc4ea3dacc336f122c864231bc37c990f53f  /tmp/knokeep-q0-codex-resume.m9C9T3/data/q0-codex-cloud-synthetic/system_state
fa80e5a2262bca138a5ff159cb548e30c84a654c361a09acea0b42fc6d83384e  /tmp/knokeep-q0-codex-resume.m9C9T3/data/q0-codex-cloud-synthetic/session_log
a6f9d7a732af6adb07cc99e59691da0f771c69d71d3177824bff610377a2b48b  /tmp/knokeep-q0-codex-resume.m9C9T3/data/q0-codex-cloud-synthetic/mcp_probe
PASS manifest+receipts+restore: 12 files
```

Both fixture-cleanliness commands produced no output and exited 0.
