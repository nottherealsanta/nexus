# 46. Adaptive orchestration and actionable handoffs

User request (2026-11-13): improve orchestration using scout → optional plan → build, after reviewing previous
orchestrator sessions. Approved approach: adaptive prompts, not a mandatory three-agent runtime pipeline. Small,
understood tasks stay direct; substantial unfamiliar work gets a focused scout, conditional planning, bounded
build assignments and orchestrator-owned acceptance.

## Evidence from previous sessions

Read-only inspection of `~/.nexus/nexus.db` on 2026-11-13 (`session`, `message`, `turn`, `turn_tool`,
`session_snapshot`); no live session, provider or config changed:

| Session | Observation |
| --- | --- |
| Plan 35 (`ses_1a1cb0f6ba276123cbf`) | Already scout → build. Scout's first turn: 10 steps, 72 seconds. Build's first turn: 369 steps, about 87 minutes, then ConnectTimeout; later retries failed with `model_not_supported`. A separate review followed. |
| Plans 29–32 (`ses_1a1cb0bf9803b9cad90`) | Already scout → build. Three follow-ups requested outstanding verification and actual parity/interaction fixes. |
| Composer/canvas (`ses_1a1bfe0c54829566caf`) | Large build assignments bundled unrelated changes; user later explicitly requested small, fast tasks and parallel work where needed. |
| NSH Ctrl+U / Ctrl+T (`ses_1a1d31bd810196947eb`) | Scout-only investigation: parent turn about 452 seconds, child 49 tool calls. Discovery also needs a stopping rule. This child used the external agy backend; counts are not a native-scout benchmark. |

These observations support tighter scope and acceptance criteria, not a measured causal claim that another agent
stage improves speed or correctness. Provider failures need separate handling (plan 34); stages cannot fix them.

## Decisions and implementation

- [x] `builtin_agents/orchestrate.md`: identify completion criteria, do small known work directly, use a focused scout
  for unfamiliar substantial work, reuse current briefs, and plan only for meaningful design/dependency complexity.
  Investigation/planning requests do not authorize building.
- [x] Complete bounded build assignments: outcome, evidence, owned files / boundaries, dependencies, acceptance,
  validation and return. Disjoint ownership for parallel edits; one owner for integration points. Scope changes
  trigger reassessment, not endless additions to a child's assignment.
- [x] Orchestrator checks actual diffs and acceptance evidence, with focused child checks and one coordinated
  integration pass. Corrections reuse children, interruptions remain paused, provider failures follow failure advice
  rather than blind retries or silent model changes.
- [x] `builtin_agents/scout.md`: enough evidence to act, not exhaustive discovery; separate facts from hypotheses;
  report edit locations, constraints, validation commands, blocking gaps and next step. Tool budget is a ceiling.
- [x] `builtin_agents/plan.md`: consume the brief, verify critical assumptions, resolve choices and return bounded
  work items with dependencies, ownership, acceptance and integration checks. No compulsory plan file.
- [x] `builtin_agents/build.md`: consume evidence without restarting discovery, inspect current targets, respect
  ownership, surface scope growth/blockers early, and report exact checks and unmet acceptance criteria.
- [x] Update `docs/agents.md` and record the decision. No runtime, tool permissions, model configuration, session
  snapshots or client UI changes. No global/project override files overwritten.
- [x] Offline validation: prompt-contract/agent tests, snapshot and child integration suites, focused lint and diff
  whitespace checks. These check prompt delivery and harness compatibility, not LLM compliance.
- [ ] Observe subsequent real user tasks before claiming a quality/speed gain. Do not start paid/live benchmarks
  without approval. Use the existing turn/request/tool records; no new telemetry needed for this change.

## Validation (2026-11-13)

- `uv run pytest -q tests/test_agents.py`: **19 passed**, including four built-in workflow-contract cases.
- `uv run pytest -q tests/test_snapshot.py tests/test_children.py`: **41 passed**. Existing snapshot freezing,
  explicit agent reload, overrides and child orchestration remain compatible.
- `uv run ruff check tests/test_agents.py`: passed.
- `git diff --check`: passed.

Both pytest commands ran from `nexus-core/` with separate scratch `NEXUS10_HOME` directories. No live provider
calls, model-setting changes or real-session rewrites were made. These are offline compatibility checks; real-task
improvement is still unmeasured.

## Observation criteria

Compare similar tasks on the same model/effort and separate native from external-worker runs. Check correctness
first: acceptance criteria met, regressions, missing verification, and user corrections. Then inspect unnecessary
discovery repetition, child scope growth, ownership collisions, follow-ups to finish an allegedly complete task,
provider requests/tokens and elapsed time. Count child work, not only parent requests; classify provider failures
separately. Do not judge success by fewer agents or shorter replies alone.

Revisit if small known tasks routinely launch scouts/planners, every stage repeats discovery, builders still absorb
unrelated requests, or the orchestrator accepts unsupported “done” reports. The observation item remains open;
there is no controlled before/after result yet.

## Activation

New sessions use these built-ins unless an override replaces them. Existing sessions retain their frozen prompts;
an explicit agent switch reloads current files. This change does not switch or resume any live session. Named
children started later load their own current files, but an existing parent's catalog and instructions stay frozen.
