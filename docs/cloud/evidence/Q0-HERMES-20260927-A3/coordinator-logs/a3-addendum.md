

## Coordinator run notes for this attempt (A3)

- You are the actual Hermes client, running locally on Windows (PowerShell terminal tool). Your model/provider: local llama.cpp server, model `GLM-4.7-Flash-Q4_K_M`, provider `custom`. Paid fallbacks are empty and persistent memory is disabled in this isolated profile; record these as coordinator-configured settings.
- Your working directory is the isolated checkout, already on branch `q0/hermes/20260927-a3` at the required input commit. Verify it yourself.
- Use a synthetic store OUTSIDE the checkout at `<A3_ROOT>\store` and project ID `q0-hermes-20260927-a3`. The KnoKeep CLI is `python skill/knokeep_state.py`; read its `--help` and the relevant subcommand `--help` before using it. Use `--client hermes`.
- Keep usernames and machine paths out of committed files: write the store path in committed evidence as `<A3_STORE>`.
- Publication for this attempt: commit locally on the output branch only. Do NOT push, do NOT open a PR, do NOT run `gh`. The coordinator will verify, push and open the draft PR, attributed to the coordinator. Record publication as "LOCAL COMMIT; PUSH/PR BY COORDINATOR".
- Do not modify files outside the checkout and the store directory. No installs. Network: none needed.
- Finish within your turn budget; if a step fails twice, preserve both and stop that step.
