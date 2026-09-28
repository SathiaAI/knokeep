# Hermes to fresh Claude Code: useful continuation

Job #32, September 28, 2026 UTC (September 27 local). Coordinator: Codex.

**PASS for one guided, file-carried continuation.** Actual Hermes saved the synthetic project; a new actual Claude Code session read the published record and produced the correct packing report without the source chat or a repeated list of rules. This does not prove automatic capture, native MCP enrollment, cloud-client operation, or production reliability.

## Inputs and independence

- Source evidence: PR #34 at `7c0e4316bc411d058ffd44b48703efb1a3bc04c2`.
- Helper code used by source: `8fb9ef7ac4290b43352a40a9690c13c1f32a8917`, integrating reviewed #29/#30/#31.
- Codex fetched the published Git objects and compared all 11 manifest entries (10 original store files plus CSV) against their SHA-256, size and original live bytes. No omitted/extra store files. Receipt hashes matched; targeted metadata scan and installed gitleaks scan passed.
- Receiver input directory contained only those 11 verified files plus `VERIFIED-INPUT.json`. No Git checkout, source chat, source report, source prompt or expected answer was supplied. The coordinator prepared the expected answer before receiver launch and kept it outside the allowed directory. It was not externally cryptographically committed beforehand.
- Actual Claude Code ran a new session with `--safe-mode --restricted --strict-mcp-config`, tools limited to `Read,Write,Glob,Grep`, `dontAsk` permissions and a 300-second supervisor timeout. No shell, web or MCP tool was available. Init trace confirms the tool list and `claude-opus-5-5`. Existing Max subscription authentication was checked with API/provider overrides removed; no new paid API route was requested. Provider token-cost estimates are not invoices.

## Observed result

Receiver ran from 01:15:44Z to 01:16:12Z, 27.7 seconds. The supervisor captured operating-system exit code 0 and the CLI returned success. `packing-report.csv` exactly matches the privately held expected rows when parsed as CSV:

```csv
sku,units,cartons
blue_mug,6,2
green_tea,6,2
```

All 11 input files still have their original hashes. The tool trace shows reads of the stored state/log/session and orders, followed by writes of the report and continuation explanation. The receiver applied the recorded filter, SKU normalization, four-unit carton size, rounding and rejected held-order approach. It calculated seven rows by hand; no general reporting program or large-dataset test is claimed.

## Artifacts and limits

`packing-report.csv` and `continuation-evidence.md` are unmodified receiver outputs. `evaluation.json` is the coordinator's comparison; `source-verification.json` and `input-manifest.json` preserve the byte checks. `receiver-prompt.txt` is the actual receiver instruction. `tool-trace.jsonl` retains user-visible text and tool actions/results; private machine paths/session identifiers and internal thinking blocks are omitted. Original runtime logs remain private. This is procedural fresh-session isolation, not a general security audit of the vendor runtime.

Hermes source reported `exit_code: 0` in its result stream; its PowerShell metadata failed to capture an independent OS exit code. That instrumentation limitation remains. Its prompt supplied every save command and body text, so this is guided capability, not spontaneous capture.

The integrated Windows CI failed once in an unchanged Git race test, then its single Windows-only retry passed. Issue #33 remains open; a green retry does not fix contention reliability. All changes/evidence remain on draft branches; no main merge or deployment occurred. Next work is unprompted capture and additional actual-client handoffs, with the concurrency finding addressed before reliability claims.
