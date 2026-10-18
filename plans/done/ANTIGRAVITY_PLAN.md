# Antigravity (`agy`) Subagent Worker — Implementation Plan

Status: **implementation and offline verification complete; live model turn not verified.** Produced from a high-tier advisor
review (the plan body below is the original, kept as written; see §14 for what was
actually built and where it differs). Companion to
`plans/in-progress/CLAUDE_CODE_ZMX_PLAN.md` (the existing external Claude Code worker,
historically called "ZMX", which ran Claude in a PTY; `agy` is headless).

## 1. Sources and verification status

Official docs reviewed by the high-tier advisor (not re-fetched by the implementer):

- antigravity.google/docs/cli/headless
- antigravity.google/docs/cli/install
- antigravity.google/docs/cli/reference
- antigravity.google/docs/cli/usage
- antigravity.google/docs/cli/permissions

Verified locally (agy 1.3.4):

- `agy --help` confirms `--input-format stream-json`, `--output-format stream-json`,
  `--print-timeout`, `--model`, `--effort`, `--mode accept-edits|plan`, `--sandbox`.
- There is **no** `auth` / `login` subcommand.
- `agy models` returns: `gemini-3.8-flash-high`, `gemini-3.8-flash-medium`,
  `gemini-3.8-flash-low`, `claude-sonnet-4-6`, `claude-opus-4-6-thinking`,
  `gpt-oss-120b-medium`.

Not yet verified locally:

- A real model-turn stream-json event sequence and terminal result. The usage panel's
  built-in TSV output from `agy -p /usage` was verified locally, but no model turn
  was run.
- Whether `--mode plan` actually prevents writes.

A live model-turn test requires explicit user approval. Offline tests and the local
CLI probes listed above are complete; live-turn behavior remains unverified.

## 2. Goal

Add an `antigravity` external subagent provider (alias `agy`) that gives Nexus
subagents parity with the existing external Claude Code worker:

- Runs headless, not under a PTY.
- Uses agy's **own** tools and agent loop. Nexus does not dispatch tools for it.
- Is a **subagent-only** provider kind. It is never the root/main agent and never the
  conversation title generator.
- Uses **explicit, exact** model IDs taken from `agy models` at runtime, not a
  hard-coded catalogue.
- Routes by tier (see §7).

Non-goals for the first pass: root/main selection, title generation, PTY/TUI
embedding, token/cost accounting, Nexus PathGuard enforcement inside agy.

## 3. Architecture

Headless process per subagent run:

```
agy --input-format stream-json --output-format stream-json \
    --model <exact-id> [--effort <e>] [--mode accept-edits|plan] \
    [--sandbox ...] --print-timeout <s>  (cwd = child's workspace)
```

- Prompt is written to **stdin** as a bounded, single stream-json input message.
  Never passed on argv.
- stdout is parsed as NDJSON line by line with a size cap per line.
- stderr is captured, ANSI-stripped, redacted, and size-capped. It is never shown
  raw.
- The child is started in its **own process group**. On completion, timeout,
  cancel, or parent exit, the whole group is terminated (SIGTERM, then SIGKILL after
  a grace period).
- A hard deadline is enforced by Nexus in addition to `--print-timeout`.

### 3.1 Reuse before refactor

- Reuse the existing `WorkerResult` duck contract in
  `nexus/agents/claude_code_worker.py:580` and its helpers if they are adequate.
  Do not create a shared abstraction unless the existing one demonstrably cannot
  carry agy's data.
- Reuse the external-worker UI helpers in `nexus/ui_support/external_worker.py`
  (`is_external`, `header_rows`, `footer_rows`, `external_blocks`, `task_note`)
  rather than forking them. Labels must become provider-generic (see workstream 4).
- Runtime external plumbing already exists in `nexus/runtime.py` (`_plan_external`,
  `_refuse_inherited_external`, `_run_external`) and `nexus/agents/runner.py`
  (`_external_dict`, `_external_partial`). Extend these; do not duplicate them.

## 4. Worker behaviour (workstream 1)

### 4.1 Process and I/O

- Bounded stdin prompt (hard byte cap; refuse oversized prompts with a clear error).
- Process group spawn and cleanup as in §3.
- Deadline: Nexus wall-clock deadline distinct from `--print-timeout`.
- Stderr: ANSI stripped, secrets redacted, size-capped, included in diagnostics only.
- NDJSON parser: tolerant of unknown event types (retained as-is for debugging,
  not interpreted); malformed lines counted and reported, not fatal unless a
  terminal event is also missing.

### 4.2 Pre-flight checks

- **Init cwd check**: the child's cwd must equal the intended workspace. Refuse
  otherwise.
- **Mode check**: `accept-edits` or `plan` only. Anything else is refused.
- **Read-only refusal**: a read-only request is refused until `--mode plan` is
  shown to enforce no writes in a local test. Until then, read-only subagents
  for agy are rejected with an explicit message.
- **Budget / worktree / no-shell profiles**: refuse these until they are
  implemented and enforced. Do not silently ignore them.
- **Bypass flags refused**: any bypass/yolo flag and any global "always proceed"
  setting from agy's own config is refused.

### 4.3 Event and result model

- Emit structured external events: `external.started`, `external.tool`,
  `external.finished`. Tool events carry name and status only, not raw payloads.
- **Authoritative result status** comes from an explicit terminal event in the
  stream or the exit condition. **EOF is never success.** A stream that ends without
  a terminal event is reported as `failed` (or `incomplete`), with whatever partial
  answer was collected.
- **Partial answer** is preserved and returned with the non-success status.
- **Files snapshot verification**: before and after the run, snapshot the workspace
  file set (paths, sizes, mtimes or hashes, bounded). Report files changed and
  flag any change outside the expected set. Do not roll back automatically.
- **Token usage and cost**: report as `unknown` unless the stream actually provides
  them. Never estimate or fabricate.

## 5. Settings, auth, and consent (workstream 3)

- **CLI owns login and credentials** (keychain or equivalent). Nexus never reads,
  extracts, stores, or forwards agy tokens, and never signs out.
- **Settings page**: shows install/version state and an instruction to run `agy`
  interactively to sign in. A "recheck" action re-runs the local probes
  (`agy --version`, `agy models`, and `agy -p /usage` if supported). No in-app
  login flow is built on assumptions about auth commands that do not exist.
- **Explicit acknowledgment**: enabling the provider requires an explicit
  `worker_inherits_agy_settings` acknowledgment. The UI and docs must disclose:
  - agy's own user/global settings are inherited;
  - agy's MCP servers and plugins are inherited;
  - Nexus PathGuard is **not** applied inside agy.

### 5.1 Usage (`/usage`)

- Invoke standalone as `agy -p /usage`. Documented in the usage doc above.
- Parse with a **bounded parser based only on observed output**. Do not invent a
  schema from docs.
- Unrecognised lines are retained as labelled `unknown` fields.
- No quota numbers are fabricated. If a value is not observed, show "unknown".

## 6. Provider kind and config (workstream 2)

- New provider kind `antigravity`, alias `agy`, valid **only** for subagent backends.
  Rejected for root/main selection and for title generation.
- Subagent backend switch: extend the existing `claude_code_subagent_backend`
  style selection (see `nexus/config/schema.py:195`,
  `nexus/host_support/model_settings.py:347-373`) to accept the agy provider kind,
  or add a parallel setting. Choose the one that keeps Claude regressions at zero;
  record the decision in `docs/decisions.md`.
- Models: exact IDs from `agy models` at runtime. **Do not** hard-code the list as
  permanent catalogue; the values above are observed on 1.3.4 only. Unknown or
  stale IDs are rejected with a refresh hint.
- Touchpoints (verify before editing): `nexus/config/schema.py`,
  `nexus/host/facade.py`, `nexus/host/protocol.py`, `nexus/runtime.py`,
  `nexus/host_support/model_settings.py`, `nexus/ui/ratatui/settings_pages/models.py`,
  `nexus/client/protocol.py`.

## 7. Tier routing (workstream 2)

- Subagent tiers map to explicit agy model IDs via config, with defaults chosen at
  implementation time from the runtime `agy models` list (not baked in).
- Routing applies only to ordinary child subagents.
- **Excluded**: root/main agent, and conversation title generation. These continue
  to use their existing paths unchanged.

## 8. UI (workstream 4)

- After the worker contract (workstream 1) is stable, make external-worker labels
  provider-generic. Current labels in `nexus/ui_support/external_worker.py` and
  `nexus/ui/cli/details.py` must not assume Claude Code.
- Show agy status, model, and files-changed summary. Show "unknown" for token/cost.

## 9. Docs (workstream 5, after implementation)

- Update `docs/host.md`, `docs/models.md`, `docs/agents.md`, `docs/security.md`,
  `docs/decisions.md`, `docs/module-map.md`.
- Document the acknowledgment, inherited settings/MCP/plugins, PathGuard
  non-application, and refusal list.
- Keep layering rules in `docs/module-map.md` intact (worker in `nexus/agents/`,
  presentation in `nexus/ui*`).

## 10. Parallel workstreams

Boundaries are chosen so they can be worked independently and merged without
overlapping files.

1. **Worker + fake tests**: agy subprocess worker, NDJSON parser, process group,
   deadline, stderr redaction, pre-flight checks, result model, files snapshot.
   Tests use a fake `agy` executable; no real model turn.
2. **Config / registry / runtime / facade / model_settings routing + tests**:
   provider kind, alias, subagent-only enforcement, tier routing, exact-model
   validation.
3. **CLI helper / auth / usage / setup + native auth flow + tests**: `agy` version
   and models probes, `/usage` parser, Settings recheck, acknowledgment flow.
   No token handling.
4. **UI generic external labels**: after workstream 1 contract is settled.
5. **Docs**: after implementation is validated.

All currently dirty files in the working tree contain additive changes and must be
preserved. Each workstream rebases on them without reverting.

## 11. Validation

- Worker and fake-agy tests: EOF-is-not-success, timeout kills whole group, oversized
  stdin refused, stderr redaction, cwd/mode refusal, partial answer retained,
  snapshot diff.
- Auth and usage tests: `/usage` parser on recorded fixtures only; unknown fields
  preserved; no fabricated quota.
- Config and routing tests: agy kind rejected for root and title; exact-model
  validation; Claude backend unchanged.
- Claude regressions: existing `tests/test_claude_code_routing.py`,
  `tests/test_host_claude_code_settings.py`, `tests/test_model_tiers_settings.py`,
  and `tests/test_subagent_runner.py` must pass unchanged.
- Layering/docs checks and `ruff`.
- Native PTY tests (`tests/test_ratatui_pty_settings.py`) only if they already exist
  and cover the settings path; do not add new PTY tests for agy.
- **No model-turn live test** without explicit user approval. Probes limited to
  `--version`, `models`, and `/usage`.

## 12. Open questions

- Confirm stream-json event names and terminal event on agy 1.3.4 (via a non-model
  probe where possible; model-turn tests need approval).
- Confirm `--mode plan` write enforcement before lifting the read-only refusal.
- Choose between extending `claude_code_subagent_backend` and adding a parallel
  setting.

## 13. Execution ledger

| # | Workstream | Status |
|---|------------|--------|
| 1 | Worker + fake tests | implementation and offline verification complete |
| 2 | Config / routing + tests | implementation and offline verification complete |
| 3 | CLI helper / auth / usage / setup + tests | implementation and offline verification complete; local probes and built-in `/usage` verified |
| 4 | UI generic labels | implementation and offline verification complete |
| 5 | Docs | written; docs tests passed |
| V | Offline validation (worker, auth, usage, config, routing, Claude regressions, layering, ruff) | **complete**; see final ledger below |

## 14. Implementation notes (post-code phase)

Evidence the parent session gathered: it fetched
`https://antigravity.google/docs/cli/headless` and ran local `agy --version`,
`agy --help` and `agy models` (agy 1.3.4). `antigravity.google/docs/cli/usage`
returned **404**, so nothing is claimed to be read from a usage page. §1's other
doc URLs were not re-verified by the implementer. **No real model turn was run and
no credentials were used.**

Differences from the plan body above:

- **Event shapes are now documented ones:** `init`, `step_update`, `result`, with the
  prompt on stdin as `{"event":"user","message":{"content":...}}`
  (`--input-format stream-json`). `-p` is not passed; `--print-timeout` takes a
  duration (`<n>s`), not a bare integer. Success needs `init` then `result.status ==
  "SUCCESS"`; any other status fails with the partial answer kept. Shapes live in
  `antigravity_worker.SCHEMA`; still unverified by a real run.
- **Config:** new kind `antigravity` (alias `agy`), a parallel provider kind rather than
  an extension of `claude_code_subagent_backend`. `worker_inherits_agy_settings = true`
  is required; `worker_permission_mode` is `accept-edits` (default) or `plan`; only
  `executable` and `timeout_seconds` are otherwise accepted. Minimal config:
  `[providers.antigravity]` / `kind = "antigravity"` / `worker_inherits_agy_settings = true`.
- **Models:** exact IDs from `agy models` at runtime; routing is by explicit
  `antigravity/<id>` on a role, `Task` call or tier. No price ranking. Never root or title.
- **Refused as unsupported (no mediation possible):** read-only roles, tool restriction
  profiles, MCP/setting-source fields, worktree children, token/cost budgets. Nexus
  PathGuard does not apply inside agy. `--sandbox` and `--effort` are not passed.
- **Auth / usage:** `agy` has no auth command, so Settings shows a manual `cli` method
  and `auth_verified` stays false. The usage panel runs configured `agy -p /usage`
  (built-in TSV verified on agy 1.3.4), inverts remaining percentages into used windows,
  reports malformed rows as bounded notes only, and shows CLI failures as unavailable rows;
  no live model turn was tested and no quota numbers are fabricated. Run usage is the terminal
  `result.usage` or `unknown`; cost is `unknown`.
- **Files added:** `nexus/agents/antigravity_worker.py`,
  `nexus/model/providers/antigravity_cli.py`, and `tests/test_antigravity_*.py`.
- **Docs updated:** `docs/{models,config,agents,provider-onboarding,surfaces,module-map,
  decisions,security,ratatui-parity}.md`; `nexus/host/protocol.py` docstring only.

Still open: a real, user-approved agy run to confirm stream/result schema,
permission/trust wording and `--mode plan` write behaviour; whether to lift the
read-only refusal. No live model turn or quota result was tested. The high-tier
planning review completed, but a high-tier implementation review was unavailable
because the provider failed; do not treat that review as completed.

## 15. Final verification ledger

- `agy` 1.3.4 local `--version`, `--help`, and `models` probes passed; the built-in
  `-p /usage` output was verified. No agent/model turn was invoked.
- The requested offline regression suite passed: **595 passed in 48.75s**.
- Ruff passed for the touched implementation and new tests.
- Implementation and offline verification are complete. Live-turn behavior remains
  explicitly unverified; high-tier implementation review was not completed.
- Pre-existing unrelated dirty files were preserved.
- Task-related paths: `nexus/agents/antigravity_worker.py`,
  `nexus/model/providers/antigravity_cli.py`, `nexus/host/protocol.py`,
  `tests/test_antigravity_*.py`, and the Antigravity documentation updates listed
  in §14. No claim is made that the implementation received a high-tier review.

### Antigravity visibility and safe discovery

An installed `agy` is discovered without a handwritten provider section using a
bounded `agy models` probe only. Configured `agy` aliases and custom executables
are respected. The host model list exposes these rows labelled **Subagents only**
(including native clients); selecting one as the main model is refused with an
explanation, while explicit subagent tier/role/Task references remain supported.
Discovery does not acknowledge inherited settings: worker execution still requires
`worker_inherits_agy_settings = true` in an explicit provider section.

Providers Settings always includes Antigravity, with manual CLI sign-in: run `agy`
in your terminal, then **Re-check and refresh models**. CLI presence is not verified
authentication. Nexus does not run a login subcommand, manage CLI credentials, or
perform a model turn to discover availability. A missing CLI is reported honestly.
