# Q1 coordinator tools

These tools implement the diagnostic protocol in PR #38 / job #36. They do not add product capture hooks or claim production reliability.

`claude_turn.py --spec PRIVATE.json` runs one actual Claude Code turn using verified Max subscription authentication, an explicit model and new UUID or explicit resume UUID. The operator supplies an approved supervisor directory from PR #37. The adapter scrubs API/provider overrides, disables customizations/MCP/browser/slash commands, and permits file tools plus Python commands. The product and store directories are explicitly allowed; the task workspace contains only ordinary task files. Shell-capable workers are not a formal isolation boundary. Prompts, process arguments and stream contain private local paths and stay private until sanitized publication.

The spec must name and hash the real native executable: shell shims are refused because they can truncate multiline prompts on Windows. The run's actual API-key-source and tool set must match the packet, and product HEAD/clean tracked files/no untracked artifacts are checked before and after. A4's observed Max route reports `apiKeySource: none`; this is a runtime consistency check combined with authenticated Max preflight, not a standalone billing attestation.

`checkpoint.py --spec PRIVATE.json` copies the whole project and store without calling the store API. It compares original and copied byte manifests, reads both journal formats without replaying them, and independently runs the predetermined report command on a separate copy. It removes only that copy's prior report so stale output cannot pass. Source verification, receipt attribution, semantic capture and chronology require separate evidence review; a coordinator output match never grants those passes automatically. Receiver controls include every ordinary source artifact from the project snapshot.

The coordinator chooses one turn at a time and enforces the 20-minute session budget. A changed session ID, missing terminal event, timeout or critical capture failure stops expansion; no retries happen inside these tools. An OS exit zero is necessary but insufficient. Fresh output directories are mandatory.

Before any model launch, freeze the complete private packet and these tool hashes, publish its salted commitment, and check the supplied policy bytes match the pinned product Git object. Bind model/route, input exclusions, random receiver order and all prompts at that point. No private expected rows are passed to the source adapter.

Harmless checks: `python -m unittest discover -s tools/q1 -p test_capture_driver.py`. They exercise wrong-session/model detection, byte copies/refusal to overwrite, mixed/torn/corrupt journal evidence, and provider environment cleanup. They do not validate real model transport or capture.
