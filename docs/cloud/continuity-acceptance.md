# Continuity acceptance plan

Status: proposed experiments, September 27, 2026. None of the following scenarios is marked passed by this document.

## Enroll actual surfaces

The first proposed matrix contains A Claude Code, B Claude Cowork Cloud, C Codex cloud, D Cursor cloud, E Hermes and F Devin cloud. Freeze the exact versions, model, account route, tools and memory settings. Codex desktop, ChatGPT and Cursor IDE require separate enrollment for claims about those surfaces.

Six nodes produce 30 directed cross-client paths; A to B and B to A are distinct. Nine enrolled surfaces produce 72 paths. Optional reviewers do not become compatibility nodes automatically.

## Staged ladder

| Stage | Scope | Required observation |
|---|---|---|
| Q0 capability smoke | Six client scenarios | Assignment delivered; synthetic input read; save attempted; receipt obtained; fresh-session retrieval/resume or explicit blocked step |
| Q1 source behavior | Six source scenarios, initially ten ordinary turns each | Independently expected events compared with captured events; no per-turn reminder to save |
| Q2 successor behavior | Six successor scenarios | Correct first action from a known-good record and the intended artifacts |
| Q3 ring and controls | A→B→C→D→E→F→A plus six same-client controls | Matched baseline and KnoKeep conditions, with contamination controlled |
| Q4 full campaign | Thirty paths × two projects × three conditions | Route-level results before broad arbitrary-switching claims |

Q0–Q3 contain 30 scenario entries, not 30 billable model executions. Fresh source/successor sessions, matched conditions and retries add executions. Freeze a manifest and budget forecast before each stage. Failed prerequisites pause dependent scenarios.

Use synthetic coding and document/planning projects. Include changed requirements, rejected approaches, unresolved work and precise artifact versions. Introduce facts during the source run that cannot all be reconstructed from the initial brief. For the coding fixture, include required uncommitted work; for planning, include a changed decision and an obsolete claim.

The baseline is the existing handoff-file workflow. Freeze each source at the handoff point and evaluate fresh successors under baseline and KnoKeep conditions. Fix model/configuration within each comparison, randomize condition order and use fresh variants. Baseline materials must be legitimate baseline output, not a private copy of KnoKeep's context.

Q4 is 180 fixtures and 360 successor runs, plus up to 180 source runs. Calibration, same-client controls, fault drills and reruns are additional. This is not permission to launch the campaign without the task's verified budget.

## Score outcomes

Measure capture completeness, correct first useful action, artifact availability, user effort, failure recovery and cost per successful handoff. Fix the rubric before running the test. Prefer deterministic checks where possible and independent, blinded grading for ambiguous qualitative outcomes.

Count expected events even when no save was attempted. Hook execution, a save call, durable storage and useful resume context are four different observations. An agent saying “saved” is not a receipt. A file hash identifies content but does not make the file available in the successor's environment.

Separate coding and document results. A proposed value target is a 30% median reduction in handoff effort versus baseline without a material completion regression. This is an experimental target, not an achieved result or a reliability standard.

Ten perfect turns provide only a smoke test. For a defined representative population, 299 independent successes with zero failures yield a one-sided 95% lower confidence bound of approximately 99%, because the exact lower bound is `0.05^(1/n)`. Correlated runs do not count as independent, and a pooled result does not certify every route separately. Publish numerators, denominators, failures, versions and uncertainty.

## Required fault cases

- Commit succeeds but the acknowledgement is lost; retry returns the original outcome without a duplicate logical write.
- Two sources race or a stale source keeps writing after a handoff; valid proposals survive and conflicts are visible.
- Duplicate/reordered requests, cancellation, context compaction and provider quota exhaustion.
- Network loss or desktop-bridge drop during a save; display saved, pending, failed and unknown honestly.
- A human changes a file between sessions; Windows/Linux paths differ; an artifact cannot be retrieved.
- A secret filter incorrectly rejects legitimate material; the client reports the omission.
- Poisoned repository or memory text claims fake authorization or fake test success; source content does not gain instruction authority.
- Expired/revoked tokens and cross-project access attempts are rejected.
- Restore from mismatched backup/key generations; validate receipt, deduplication and deletion behavior.
- Coordinator restart, duplicate completion and delayed results from an obsolete job generation.

Use isolated disposable stores and agent instances. Do not disable a live desktop bridge or interrupt a working agent merely to simulate a failure. Demonstrate a bridge-independent path through isolated configuration and observed tool use, or report it blocked.

## Exit conditions

An internal prototype can exit after the ring and controls with critical defects fixed and retested. Describe only the tested routes as supported. Broad six-surface switching claims require the full matrix and published remaining failures. Capture, conflict preservation, privacy boundaries and artifact continuity must pass explicit checks; model agreement is not acceptance evidence.
