# Q0-HERMES-20260927-A3: Continuation Plan

This continuation plan describes the pending tests and steps required to complete the Hermes capability smoke test.

## Current Status

- **Completed**:
  - Checkout verification
  - KnoKeep CLI bootstrap
  - KnoKeep CLI session-append
  - Store persistence verification
  - Eval results
- **Pending**:
  - Fresh-session resumption test
  - Native-MCP support verification (if applicable)
  - Store export to `tests/fixtures/q0/Q0-HERMES-20260927-A3/`
  - Manifest creation

## Pending Steps

### 1. Fresh-Session Retrieval Test

**Objective**: Verify that Hermes can successfully load and resume state from the synthetic store created in the source session.

**Test Procedure**:
1. Start a new Hermes session (fresh process, no memory persistence enabled).
2. Initialize the synthetic project with `knokeep_state.py bootstrap`.
3. Verify that the resume state is loaded from the existing journal.
4. Confirm that the client can read and interpret the saved journal entries.
5. Attempt to append a new journal entry in the fresh session.
6. Verify that the new entry is persisted and readable.

**Expected Result**:
- Fresh session successfully loads the resume line from the existing journal.
- Hermes can read and interpret the synthetic probe content.
- New journal entries can be appended and persisted.

**Blocking Conditions**:
- None (this is a separate bounded test).

### 2. Native-MCP Support Verification

**Objective**: Verify whether Hermes has native MCP enrollment for KnoKeep.

**Test Procedure**:
1. Check if Hermes has an MCP server configuration (e.g., `~/.config/hermes/mcp.json` or similar).
2. Attempt to invoke Hermes's MCP integration with KnoKeep.
3. Verify that the MCP connection is established.
4. Test a simple KnoKeep operation through the MCP protocol (e.g., bootstrap or session-append).

**Expected Result**:
- If native MCP support exists, verify it works correctly.
- If no native MCP support, document this as NOT SUPPORTED.

**Blocking Conditions**:
- None (this is an informational/verification test).

### 3. Store Export

**Objective**: Export the synthetic store to `tests/fixtures/q0/Q0-HERMES-20260927-A3/` for use by future tasks.

**Procedure**:
1. Copy the entire `<A3_ROOT>\store` directory to `tests/fixtures/q0/Q0-HERMES-20260927-A3/`.
2. Create a manifest file (`manifest.txt`) listing all files with their SHA-256 hashes and sizes.
3. Verify that the copied files match the original files byte-for-byte.
4. Add a `.gitattributes` rule to the export directory to preserve line endings if needed.

**Expected Result**:
- All files from the synthetic store are exported.
- Manifest accurately lists all files with correct hashes.
- Byte fidelity is preserved.

**Blocking Conditions**:
- None.

### 4. Continuation Task Publication

**Objective**: Document this continuation plan in the evidence directory for future workers.

**Procedure**:
1. Ensure `continuation.md` is committed to the output branch.
2. No need to push or create a PR for continuation documentation.

**Expected Result**:
- Continuation documentation is available for future workers.

**Blocking Conditions**:
- None.

## Integration into the Continuation Matrix

This continuation plan should be used by the coordinator to create a separate Q0 continuation task (e.g., `Q0-HERMES-20260927-A3-continuation`). The continuation task should:

1. Use the synthetic store from `tests/fixtures/q0/Q0-HERMES-20260927-A3/` as the input.
2. Perform the fresh-session resumption test.
3. Verify native MCP support (if applicable).
4. Document the results in the evidence directory.

## Success Criteria for Continuation

- Fresh-session resumption succeeds (resume line loaded, journal entries readable).
- New journal entries can be appended and persisted in the fresh session.
- Native MCP support is verified and documented (if applicable).
- Store export is complete and byte-faithful.

## Notes

- The continuation task should use a fresh Hermes session with persistent memory disabled (or explicitly cleared) to ensure true isolation.
- The continuation task should not modify the source branch `q0/hermes/20260927-a3` beyond the evidence directory.
- The continuation task should use the same model and provider (GLM-4.7-Flash-Q4_K_M, custom) to maintain consistency.
