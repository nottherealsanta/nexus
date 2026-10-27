# Nexus-owned interactive Claude Code workers

Status: worker, host/native UI (5C except human handoff), crash recovery on reopen, Haiku live run, and a subagent backend switch (worker | sdk) are done. Remaining: human handoff into the live PTY, root escalation/pause, Sonnet/Opus live IDs, broader race tests, platform decision, orphan cleanup. Revised to independent Claude Code tools.
This ledger supersedes the original ZMX-specific plan. No ZMX dependency is
currently required. The text-only bridge prototype was replaced by the independent worker below.

## 1. Agreed product contract

- Only explicitly configured `claude-code/<model>` roles use external workers.
  Existing Nexus roles/subagents remain unchanged; there is no global switch
  making all children Claude Code workers.
- Claude Code models are hidden from the main-model picker by default.
  `[models] claude_code_main_enabled = true` enables main selection. Main agents
  use the Agent SDK path; external subagents use genuinely interactive Claude
  Code through a Nexus-owned PTY, not `claude -p` or the SDK.
- Claude Code workers run their OWN tools and permission flow, independently of
  Nexus's tool dispatcher. Nexus delegates a task and reads activity/results;
  it does not translate Claude tool intentions into Nexus tool calls.
- Nexus owns scheduling, child identity, deadlines, cancellation, durable
  readback, and user-facing attention states. Claude Code owns its internal
  execution loop, built-in tools, and explicitly configured extensions.
- The device's existing Claude Code login is used through the unmodified CLI.
  Nexus never extracts subscription tokens or proxies model HTTP requests.
- Model selection is exact and explicit. Requested Opus/Sonnet/Haiku 5.5 IDs
  must be verified against the installed CLI/account before advertised as
  available. Do not fabricate catalogue entries or silently substitute aliases.
- Subscription efficiency and commercial entitlement are not guaranteed by a
  PTY; measure usage separately and confirm applicable product-use terms.

References: [agents](../../docs/agents.md), [models](../../docs/models.md),
[security](../../docs/security.md), [Claude Code terms](https://code.claude.com/docs/en/legal-and-compliance).

## 2. What exists now

These changes are working-tree implementation, not a released feature. Behavior
is documented in [agents.md](../../docs/agents.md#external-claude-code-workers).

- [x] Provider/adapter identity `claude-code`, including registry projection from
  the existing Claude subscription model catalogue.
  Files: `nexus/model/registry.py`, `nexus/host_support/provider_auth.py`.
- [x] Main-model visibility configuration, default off, with picker filtering
  and direct main selection rejection while disabled.
  Files: `nexus/config/schema.py`, `nexus/host/facade.py`, `nexus/runtime.py`.
- [x] Main-agent routing through `ClaudeAgentProvider` when explicitly enabled.
  Explicit children never reach it: `_ChildRuntime` plans an external run.
  File: `nexus/runtime.py`.
- [x] `ClaudeCodeWorker` (`nexus/agents/claude_code_worker.py`): own PTY,
  executable/version/flag preflight, explicit `WorkerPolicy`, isolated hook
  script, incremental bounded transcript readback, startup/permission attention
  detection, deadlines, cancellation, process-group cleanup, before/after
  file-change snapshots, private `~/.nexus/claude-code/` state directory.
- [x] `[providers.<name>]` `worker_*` keys with load-time validation
  (`nexus/config/schema.py`); `external.*` events, `ToolCallView.provenance`,
  `AgentView.usage_unknown`, `ConversationView.external`, a `Provenance` detail
  row, and unknown-usage / authority / files-changed notes in `SubagentOutcome`.
- [x] Offline tests: `tests/test_claude_code_worker.py`,
  `tests/test_claude_code_routing.py`, `tests/test_view_external.py`, with fakes
  in `tests/claude_code_fakes.py`.
- [x] Live startup attempted earlier with a requested Haiku 5.5 identifier: blocked
  by the Claude Code folder-trust prompt before inference. No successful model
  response or model availability verification, and no live run of the new worker.

### Prototype mismatch: replaced

The earlier `ClaudeCodeProvider` (`nexus/model/providers/claude_code.py`, a
text-only model bridge feeding JSON tool intentions into a Nexus child loop) and
its test were deleted. The PTY/version/cleanup/diagnostic ideas were reused in the
worker; the prompt protocol, private non-project cwd, tool-disable flags and
child-loop placement are gone.

## 3. Target architecture

```text
Nexus subagent assignment (explicit claude-code role)
  -> SubagentRunner reservations / external execution authorization
  -> dedicated ClaudeCodeWorker controller (outside Nexus run_turn tool loop)
       -> actual assigned workspace or isolated worktree
       -> interactive Claude Code PTY, Claude's own tools and approvals
       -> lifecycle hooks + transcript reader + terminal capture
  -> durable child events, attention UI, final/partial SubagentOutcome
```

Do not implement the worker as repeated `ModelProvider.stream()` calls for each
Nexus iteration. A delegated task must be executed by Claude's own agent loop.
Keep the SDK model provider for main-agent requests separate from this controller.
The daemon owns the worker; lower layers depend on narrow injected protocols.

## 4. Independent tools and the authority boundary

Independent tools do not mean permission bypass or hidden authority escalation.
Nexus tool allowlists, PathGuard, read-only role rules, and tool budgets do not
automatically constrain external Claude tools. The UI and delegation result must
say this clearly. Do not pretend an external advisor is Nexus-enforced read-only.

- [x] Define an explicit external-execution authorization policy/configuration:
  permitted workspace, effective Claude permission mode, available tool/plugin/MCP
  configuration (network access is only the `--restricted` WebFetch/Bash gate).
- [x] Refuse assignments whose mandatory inherited denials/read-only constraints
  cannot be enforced by a verified Claude policy or OS boundary (done for read-only
  roles and budgets; Nexus write roots/PathGuard are NOT mapped, only disclosed). Never waive an
  absolute security denial merely because the user enabled this backend.
- [ ] PARTIAL (the no-bypass, no-auto-answer, no-implicit-plugin/MCP parts are
  done): Do not pass permission-bypass flags, auto-answer approvals, or accidentally
  load global/project plugins, hooks, or MCP servers without disclosure and an
  explicit configuration policy. Independence is not blanket inheritance of all
  local extensions.
- [ ] Route Claude approval/auth/trust prompts into a user attention workflow.
  (Implemented only as the `needs_attention` result naming a manual command; no
  host attention/handoff command or UI.)
  Human input pauses automation; task submission resumes only after handoff.
- [x] Distinguish Nexus scheduling limits from Claude execution limits. Do not map
  Nexus iteration/tool caps to unsupported Claude equivalents. Reject mandatory
  token/cost caps if accurate accounting/enforcement is unavailable.
- [x] Surface unknown external usage as unknown, not measured zero. Verify file
  changes separately from what the model claims.

## 5. Implementation checklist (A and B done offline; C, D and crash recovery remain)

### A. Replace the model bridge with an external child controller

- [x] Add a dedicated worker execution interface and select it before building a
  normal Nexus child `run_turn` loop for explicit Claude Code assignments.
- [x] Preserve effective model resolution, scheduling/depth/concurrency checks,
  child ownership, cancellation, and parent handoff behavior (handoff for failed/
  interrupted; concurrency is the runner's existing bound, not worker-specific).
- [x] Launch in the assigned workspace/worktree with Claude tools enabled under
  the explicit external policy; remove JSON Nexus-tool-intention prompting.
- [x] First release: one fresh Claude session per assignment, bounded concurrency;
  defer conversation reuse and detach/reattach persistence.

### B. Reliable readback and lifecycle

- [x] Read Claude's persisted transcript incrementally for assistant responses
  AND tool activity; correlate session ID, transcript offsets, and assignment.
- [x] Use supported lifecycle hooks as signals, reconcile transcript flushes, and
  verify semantics for stop/failure/interruption. A quiet PTY is never success.
- [x] Retain bounded terminal diagnostics for startup/approvals; sanitize ANSI/OSC
  sequences and redact secrets before durable storage or parent presentation.
- [x] Return a bounded answer/partial answer, transcript reference, status, unknown
  usage, clipping notices, and verified change summary where available.
- [ ] PARTIAL: events and messages are in the child's durable log and replay through
  the reducer; daemon-crash recovery is NOT implemented. Persist lifecycle/output/ownership so replay shows the same information.
  Reconnect must not duplicate submission; initial daemon-crash recovery marks
  interrupted rather than claiming the process survived.

### C. Host and native UI

- [x] PARTIAL: `AgentCancel(session, agent_id)` cancels one child alone (per-child
  token in `agents/runner.py`; the worker kills its process group). Inspect already
  exists as `AgentTranscript` (its view now carries `external`). Attention is
  surfaced as the `needs_attention` result/card only; a human-handoff command into
  the live Claude PTY is DEFERRED (no attention-resolve command either).
- [x] Native Ratatui shows provider/model, independent tool authority, effective
  workspace, Claude tool rows marked with provenance, final status, transcript path,
  `needs_attention` as its own state with the manual instruction, and usage
  unknown (`ui_support/external_worker.py`, Rust block kind `external`; see
  ratatui-parity.md). Live PTY check against a real worker is NOT done (5D).
- [x] Discoverable Settings -> Models toggle for `claude_code_main_enabled`
  (`ClaudeCodeSettings(Set)`), with the SDK-only / per-role note.
- [x] No web feature added; all clients stay behind the host contract.

### D. Models, setup, verification, release

- [x] Haiku 5.5 verified live (2026-10-13): `claude-haiku-5-5` accepted, Claude's own
  `Read` tool ran in a trusted folder, transcript tool/text readback and the `Stop`
  hook worked, status `completed`, 1 tool call.
- [ ] Sonnet/Opus IDs: deliberately NOT tried (user chose Haiku only); do not claim them.
- [ ] PARTIAL (offline fakes only): Add tests for own-tool execution, permission attention, slow tools, hook loss,
  stale transcript entries, multiple assistant messages, cancellation races,
  truncated output, model rejection, and default Nexus behavior unchanged.
- [ ] Run provider/routing tests, existing child/permission/host/registry/layering
  suites, docs checks, and native PTY checks for UI changes; record actual results.
- [x] Update docs/agents.md, docs/models.md, docs/config.md, docs/security.md,
  docs/events-and-view.md, docs/host.md, docs/module-map.md, and native-client docs
  with implemented behavior only. Record the independent-tool design decision.
  (Done, including docs/host.md and the native-client docs after 5C.)
- [ ] Decide whether Python PTY primitives meet supported platforms or a packaged
  native helper is needed. Do not add ZMX/wrapper bundling without evidence.

### Direction change (2026-10-13): `auto` and root-first attention

- [x] `worker_permission_mode = "auto"` accepted (opt-in); `bypassPermissions` still
  rejected; read-only roles still narrowed to `dontAsk`. Live Haiku: write+read ran
  with no prompt; a denial reads "denied by the Claude Code auto mode classifier".
  NOT verified live: the classifier-denial path itself (offline fake only).
- [x] Attention for every subagent goes to the root (`SubagentOutcome.attention`,
  render guidance), for Claude workers and Nexus children; the root escalates to the
  human by ending its reply. Docs: agents.md "Attention goes to the root".
- [ ] Not done: a dedicated root escalation tool, pause/resume of a blocked child,
  `agent.attention` as a live event/UI row (today it is in the Task result only).

- [x] Tier routing (2026-10-13): `[models.tiers]` may list `claude-code/<model>`;
  children route a tier's first usable entry to the worker (before this, a tier
  entry was silently skipped and the child ran on the next provider). Live: a
  `low` subagent on the user's config ran as an external Haiku worker in `auto`.
  Known: `redact_secrets` masks any 40+ char path as an opaque token (`/***.txt`);
  not changed here. Sonnet 5.5 is configured in the medium tier but NOT verified live.

## 6. Verification ledger

- Earlier documentation-only checks of the prototype plan: `tests/test_docs.py`
  30 passed; they verified no runtime behavior.
- Offline, after the read-only MCP/settings refusal fix (2026-10-13): worker +
  routing tests 52 passed; full offline suite 5464 passed, 312 skipped, 4
  deselected; `ruff check nexus tests` clean; `tests/test_docs.py` 30 passed
  after the docs pass.
- After 5C (host AgentCancel, Ratatui external rendering, settings toggle) and
  removal of the `claude --resume` attention hint (2026-10-13): full offline
  suite 5486 passed, 312 skipped, 4 deselected; ruff clean; `rust/tui` builds;
  native PTY external tests pass. Playwright Ratatui screenshot check not run.
- Follow-up: session reopen durably marks unfinished external workers and
  unclosed child-task rows `interrupted`, with unknown usage. Replay retains
  worker context. Recovery is lease-protected and idempotent, including the
  pre-turn crash window. It does not retry tools or kill orphan Claude processes;
  the warning explicitly says the process may still be running.
  Replay, sibling isolation, live-lease and abrupt owner-process-exit tests pass.
- Follow-up verification: 356 targeted Python tests passed (sessions, worker,
  routing, cancel/settings, terminal rendering/PTY, layering and docs); Rust
  suite passed (154 passed, 2 ignored); Ruff and `git diff --check` passed.
  `tests/playwright_ratatui_check.py` passed and generated small/large screenshots
  for default and red themes under `artifacts/`. That scenario does not include
  an external Claude worker, so it is not visual proof of the worker card.
  No real Claude invocation or real-user config changes in this follow-up.
- Live Haiku startup (old prototype): trust prompt blocked; no automatic approval
  was sent.
- Live Haiku run of the new worker (2026-10-13, Claude Code 2.1.296): trust prompt
  stopped an untrusted folder as `needs_attention` (nothing approved); in the already
  trusted repo's ignored `artifacts/` subfolder the run completed with a Claude-owned
  `Read`. Transcript path and Stop hook confirmed.
- NOT VERIFIED: permission-prompt wording; StopFailure payload; Sonnet/Opus 5.5 IDs;
  diagnostic text still lacks word spacing in Ink output (cosmetic); subscription
  efficiency and commercial terms.

- Review fixes (2026-10-14): `files_changed` also lists files touched by commits
  made during the run (`HEAD` diff); gitignored files disclosed as unchecked.
  `AskUserQuestion` always disallowed (it would stall to the deadline). The context
  snapshot lists "Claude Code default set" instead of 0 tools. No silent default
  model (`claude-sonnet-4-6` fallback removed; a bare exact ID on a provider-only
  role is kept). Model IDs accept any `claude-<family>-N` (e.g. Fable). Hooks are
  checked before the deadline; permission detection removes Claude's own replies.
- Subagent backend switch (2026-10-14): `[models] claude_code_subagent_backend`
  `worker` | `sdk`, Settings -> Models "Claude Code subagents run via"
  (`ClaudeCodeSettingsSet(subagent_backend)`). `sdk` runs the child in the Nexus
  loop on a per-child `ClaudeAgentProvider`; main selection is unaffected.
  Verified offline only; no live SDK-backend subagent run.
- Verification (2026-10-14): full offline suite 5991 passed, 312 skipped, 1 failed
  (`tests/test_cli.py::test_parser_exposes_the_canonical_command_set`, caused by the
  unrelated in-progress `gateway` CLI work); ruff clean. No PTY/Playwright check of
  the new Settings row.

### Remaining work

1. 5C remainder: human handoff / attention-resolve into the live Claude PTY
   (deferred). Done 2026-10-13: `AgentCancel`, native rendering of `external.*`,
   the `claude_code_main_enabled` toggle, docs/host.md and docs/ratatui-parity.md.
2. 5D: live verification on a real account (trust setup, a small authorized task
   exercising a Claude-owned tool, exact model IDs, prompt wording, hook fields,
   transcript path), then adjust the unverified detectors; native PTY checks.
3. Crash reconciliation is implemented on session reopen, not an eager scan of
   every unopened session at daemon startup. Orphan-process cleanup remains
   separate from interrupted-status recovery. A Claude-worker-specific browser
   screenshot scenario remains open (the generic terminal check passed).
4. Decide whether Python PTY primitives meet supported platforms (POSIX only today).

## 7. Release acceptance

An explicitly configured Claude role runs one delegated task through interactive
Claude Code using its own tools in an authorized workspace. Nexus can show its
activity and answer durably, interrupt it, request human attention, and distinguish
success from partial/interrupted work. Existing subagents remain unchanged, main
Claude selection is default-hidden and SDK-only when enabled, and the interface
makes the external authority boundary explicit.
