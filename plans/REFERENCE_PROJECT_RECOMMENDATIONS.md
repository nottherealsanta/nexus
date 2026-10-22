# Reference project recommendations

Status: **proposal**, not a commitment. `docs/` is the source of truth and the code wins over
this file (see `AGENTS.md`). Nothing here changes behavior; accepted items need their own plan,
tests and a `docs/decisions.md` entry.

Review date: 2026-10-22. Reviewer: a task agent with read/search tools only (no shell, so no
`git log`, no running of any reference project, no network).

Conventions in this document:

- **Verified** = read in a file named in the text, on the revision listed below.
- **Inference** = my reading of what those files imply; not run, not confirmed with upstream.
- Paths are repo-relative to the Nexus root. `references/` is a git-ignored local clone
  directory (`.gitignore:15`). No code was copied; ideas are described in prose only.
- Licensing: reference code must not be copied into Nexus. Where an idea is adopted, Nexus
  should re-derive it from its own contracts and record attribution in `NOTICE` only if
  something more than a general idea is taken (as `nexus/live/NOTICE` already does).

## 1. Method and revisions

| Reference | Path | Upstream (from the task) | Revision read (branch HEAD ref in `.git/refs/heads/`) |
| --- | --- | --- | --- |
| CodexBar | `references/CodexBar` | steipete/CodexBar | `3bfbf4a51d3af6c7307d5aa189e85b3e94390fe8` (main) |
| grok-build | `references/grok-build` | xai-org/grok-build | `2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8` (main, shallow clone); `SOURCE_REV` records monorepo commit `559751fdcec02d413e4c57c8832ab275e4f44980` |
| opencode | `references/opencode` | anomalyco/opencode | `6a7cedcbf1823eeb1c8dfc4baf5a7951854f578b` (**dev**, not main) |
| pi | `references/pi` | badlogic/pi-mono (the README now names `earendil-works`) | `c268154ca1171c2f9eefe75d3d7372a68aada2d5` (main) |
| t3code | `references/t3code` | pingdotgg/t3code | `96bae5465bc5dfaedc890998a7a71672fb470ae2` (main) |

All five clones are present (verified: each has a README and `.git/HEAD`). Commit dates were not
read. The working trees were assumed clean and equal to these refs; not checked.

What I read for Nexus (the baseline for "already present"): `AGENTS.md`, `docs/architecture.md`,
`docs/decisions.md` (first 751 of 1360 lines read end to end; later sections were grepped during
sanity checks but not fully read), `docs/gateway.md` (top), `docs/cli.md`, `docs/sessions.md`,
`docs/testing.md`, `docs/devtools.md` (by search), and greps across `docs/` and `nexus/`. I did
**not** read `docs/loop.md`, `docs/context.md`, `docs/tools.md` end to end, the Rust TUI, or the
second half of `docs/decisions.md` in full, so "Nexus lacks X" below means "a search of `docs/` and
`nexus/` for the obvious terms found nothing", which is weaker than a proof.

Per-reference method: README, then architecture/design docs and the packages or crates most
relevant to Nexus's principles (durable log, host contract, context presentation, native TUI,
testing), then a targeted search in Nexus for the same capability.

## 2. What the references are

### 2.1 CodexBar (Swift menu-bar app and CLI)

Purpose: show AI-provider limits, reset times, credits and local cost (`README.md`).

Inspected: `README.md`, `docs/architecture.md`, `docs/agent-sessions-design.md`,
`docs/credential-notifications.md`, `docs/cost-reporting-periods.md`, `docs/cli.md` (top),
`docs/codex.md` (top). Not inspected: the Swift sources, the ~100 per-provider pages, the Linux
app, widgets, Sparkle/update plumbing.

Verified strengths:

- Core/UI split: `Sources/CodexBarCore` fetch and parse, `Sources/CodexBar` state and UI,
  `Sources/CodexBarCLI` a CLI that mirrors the app (`docs/architecture.md`). One data path feeds
  menu, widgets and CLI.
- Each provider has an ordered fallback of sources (OAuth API, then CLI RPC, with web extras as a
  separate follow-up) and failures keep their original error class (`docs/codex.md`).
- Credential-expiry alerts are typed, opt-in, once per failure episode, reset only by a
  successful fresh fetch, and contain no account data (`docs/credential-notifications.md`).
- Cost reporting states what it can prove: "recorded spend, not an authoritative bill", lower-bound
  markers for incomplete sources, missing hours distinct from recorded zero
  (`docs/cost-reporting-periods.md`).
- Privacy discipline in session discovery: bounded reads, no process environment capture, no
  transcript bodies (`docs/agent-sessions-design.md`).
- A provider status poll surfaces incident badges (`README.md`, "Live status").

Tradeoffs: macOS/Swift, scrapes browser cookies for many providers (a posture Nexus has explicitly
refused for Claude; `docs/decisions.md`, "Provider usage reuses the credentials Nexus already
holds"), and its scale (100+ providers) is a maintenance cost Nexus should not copy.

### 2.2 grok-build (Rust TUI and agent runtime, Apache-2.0)

Purpose: SpaceXAI's terminal coding agent (`README.md`). Source is synced from a monorepo; the
root `Cargo.toml` is generated; external contributions are not accepted.

Inspected: `README.md`, `SOURCE_REV`, the whole `crates/codegen/xai-grok-pager/docs/user-guide/`
index, and guides 15 (agent mode/ACP), 17 (sessions), 18 (sandbox), 19 (plan mode), 20
(background tasks), 22 (permissions), 23 (dashboard), 24 (monitoring/OTel), 25 (status line), 13
(memory, top). Not inspected: any Rust source (including `xai-chat-state`, `ptyctl`,
`xai-acp-lib`, `xai-codebase-graph`), hooks, plugins, subagents and workflows guides, theming.
Everything below about grok-build is therefore **documented behavior, not verified code**.

Verified (documented) strengths:

- **Rewind points**: one per user prompt, `/rewind` or Esc-Esc, truncates conversation history
  but explicitly does not restore files (guide 17).
- **OS sandbox profiles** (`off`, `workspace`, `read-only`, `strict`, custom with `deny` globs)
  using Seatbelt/Landlock/bubblewrap, and write-deny of its own config/hook files under those
  profiles (guide 18). Child-network blocking is Linux-only per the guide.
- **Permission modes** including a classifier-backed `auto` that surfaces a prompt for what it
  will not auto-allow and fails the call in non-interactive runs (guide 22).
- **Plan mode** gated by the tool layer: only the plan file is writable, in every mode (guide 19).
- **Status line** as an opt-in row fed by built-in items or a user script given JSON on stdin,
  with caps on output, a timeout and a debounce (guide 25).
- **Agent mode over ACP** (stdio or WebSocket) so editors can drive the agent (guide 15).
- **Background commands, a `/loop` recurring prompt, and a dashboard** of top-level sessions by
  state (guides 20, 23).
- **Opt-in, content-free OpenTelemetry export** with a double opt-in (guide 24).
- Session directory records the rendered system prompt, prompt inputs and the tool definitions
  sent on the latest model call (guide 17 storage layout), which supports the "show what the
  model saw" idea.

Tradeoffs: session state is a directory of several JSON/JSONL files rather than one
transactional store; sandboxing is platform-specific with documented gaps; rewind does not touch
files.

### 2.3 opencode (TypeScript/Effect monorepo; branch `dev`)

Purpose: open-source coding agent with TUI, desktop, web and SDK (`README.md`). Nexus already
integrates it as an ACP provider (`docs/decisions.md`, "OpenCode is integrated over ACP only")
and copied its transcript row layout (`docs/decisions.md`, "Native transcript copies the
OpenCode row layout").

Inspected: `README.md`, the package list (30 packages), `packages/core/src/snapshot.ts` (top 60
lines), `packages/core/src/session/revert.ts` (top 80 lines), `packages/http-recorder/README.md`,
`packages/codemode/README.md`. Not inspected: the TUI package, server/protocol/sdk packages, the
plugin system, `simulation` (only file names seen), `httpapi-codegen` (only `package.json`),
enterprise/console.

Verified strengths:

- **Snapshot-based revert**: a content-addressed capture of the project file tree, per-file
  restore, per-file diff between two captures, and a staged/cleared/committed revert lifecycle
  published as session events (`snapshot.ts`, `revert.ts`). Capture returns nothing instead of
  failing the turn when snapshots are disabled or unsupported.
- **HTTP/WebSocket record-and-replay** test cassettes for provider integrations
  (`packages/http-recorder/README.md`): first local run records against the real API, later runs
  replay deterministically.
- A code-mode interpreter whose programs reach the outside only through host tools
  (`packages/codemode/README.md`).

Tradeoffs: Effect-heavy TypeScript; behavior of the revert UI and cassette redaction not read.

### 2.4 pi (TypeScript monorepo, minimal harness)

Purpose: "minimal, extensible agent harness"; deliberately omits sub-agents and plan mode,
pushing them into extensions (`README.md`).

Inspected: `README.md`, package list, `packages/durable/README.md` (top 70 lines),
`packages/evals/README.md`, `packages/env/README.md`, `packages/telemetry/README.md` (top),
`packages/coding-agent/docs/virtual-models.md`, `sessions.md`, `containerization.md` (top). Not
inspected: `chord`, `ai`, `tui`, `mcp`, `codemode` internals, extensions API, RPC docs, session
format spec, any source file.

Verified strengths:

- **Session tree**: entries stored as a tree; `/tree` moves within a file, `/fork` starts a new
  session from an earlier user message, `/clone` copies the active branch; leaving a branch can
  attach a summary of it to the branch entered (`sessions.md`). Compaction never deletes entries.
- **Virtual models** that route per request while recording both selection and dispatched model;
  footer shows `selection → routed`; context limits follow the physical model
  (`virtual-models.md`).
- **Durable harness** claim: conversations, turns and tool calls are committed before display and
  resume after a crash (`packages/durable/README.md`; marked experimental).
- **Evals** that run paired Docker arms (with and without docs) and report lift
  (`packages/evals/README.md`).
- **Remote execution env**: tools run on another machine through a small daemon over SSH while
  storage and credentials stay local; strict host-key checking, no forwarding
  (`packages/env/README.md`).
- **Telemetry contracts** that are explicit-context and backend-neutral, with a no-op default
  (`packages/telemetry/README.md`).
- **Isolation guidance** that spells out what stays exposed under each container method
  (`containerization.md`).

Tradeoffs: minimalism pushes policy into extensions, which conflicts with Nexus's "context is
shown clearly and by default" stance; `durable` and `env` are explicitly experimental or new.

### 2.5 t3code (TypeScript/Effect; web, Electron, mobile)

Purpose: a control surface that drives provider CLIs (Codex, Claude Code, Cursor, Grok, OpenCode,
Antigravity) over a WebSocket server (`README.md`, `AGENTS.md`).

Inspected: `README.md`, all of `AGENTS.md`, the package list, `packages/client-runtime/src/` and
`packages/contracts/src/` file lists, `apps/server/src/checkpointing/CheckpointStore.ts` (top
70 lines), `apps/server/src/orchestration-v2/CheckpointRestoreSafety.ts` (top 80 lines),
`apps/server/src/scheduledTasks/ScheduledTaskService.ts` (imports only),
`packages/contracts/src/orchestratorMcp.ts` (imports only). Not inspected: web/mobile/desktop
apps, `docs/internals/*`, the relay/tunnel code, provider adapters.

Verified strengths:

- **Event-sourced server**: a pure orchestrator decides events, one transaction commits events,
  projections, a command receipt and outbox effects, and an effect worker runs side effects
  (`AGENTS.md`, "How it works").
- **Per-turn checkpoints**: each turn ends with a hidden git ref; the server can diff and restore
  (`AGENTS.md`; `CheckpointStore.ts` exposes capture, restore, diff between refs,
  list-authored-paths, delete). **File restore is refused unless the checkout is an isolated
  worktree**, with the message "Rewind the conversation without restoring files instead"
  (`CheckpointRestoreSafety.ts`).
- **Scheduled tasks** with schedules, run-now, enable/disable and webhook triggers with signature
  verification and dedupe (file names and imports in `scheduledTasks/`).
- **Typed contracts package** shared by server and all clients; protected RPC methods carry
  permission scopes the client checks and the server enforces (`AGENTS.md`, "Taste").
- **Agents reach app capabilities through MCP tools** backed by the same service methods as the UI
  (`AGENTS.md`, "Hit every surface"; `contracts/src/orchestratorMcp.ts` exists).
- **A "hit every surface" checklist** (entry points, clients, providers, contracts, reverse
  states, connection modes) and strict test rules: no sleeps, no wall-clock, no polling in tests
  (lint-enforced) (`AGENTS.md`).
- Dev safety rules for agents (never kill by pattern, never open the live DB read-write, seed
  dev DB from a read-only copy).
- Docs rule: internal docs only record decisions and traps the code cannot show
  (`AGENTS.md`, "Documentation").

Tradeoffs: three UI surfaces plus remote modes is a different product shape from Nexus's
terminal-first, loopback-only posture; Effect-based services do not transfer.

## 3. What Nexus already has (so it is not proposed as new)

Verified from `AGENTS.md`, `docs/decisions.md` and `docs/architecture.md` unless noted.

| Capability | Nexus today |
| --- | --- |
| Durable, append-only log, reducers, replay | SQLite `~/.nexus/nexus.db`, `view/reduce.py`, monotonic `seq`; assistant message persisted before tools; recovery closes dangling turns |
| Host contract and daemon per workspace | `host/protocol.py` → `host/facade.py`; every surface is a client |
| Branching | `SessionFork` / `/fork [at_seq]` (parent_id, fork_seq); `docs/sessions.md`, `docs/cli.md:83` |
| Compaction and handoff | `context.on_overflow = "handoff"` default; snapshots are derived caches |
| Code mode | `code` tool on a Monty sandbox (`docs/tools.md`), with an offline evaluation harness (`benchmark/codemode_eval.py`, `docs/devtools.md`) |
| Permissions | deny-absolute, whole-batch checks, PathGuard, compound-shell-command rules; decision to enforce **no** OS sandbox and **no** structural read-only (`docs/decisions.md`) |
| Provider quota / usage view | `/usage` and `Ctrl+U`, reserve = elapsed share minus used share, usage-aware subagent routing that fails open (`docs/decisions.md`; `host_support/provider_usage.py`) |
| Per-turn usage and cost | `/cost`, "Usage by turn" in the Context dialog, tiered pricing |
| Tiers (a form of model routing) | `model/tiers.py`; subagent roles own allowed tiers |
| Titles, toasts, logs, session search, archive/trash, export | `json` / `markdown` / `jsonl` export (`docs/sessions.md`) |
| Remote reach for devices | opt-in loopback gateway with pairing, scopes and Tailscale Serve guidance (`docs/gateway.md`) |
| External agents | OpenCode over ACP as a *client*; Claude Code and Antigravity workers; MCP |
| Test seams | `ScriptedProvider`, recorded fixtures, provider conformance suite, mock scenarios, PTY and Playwright checks, opt-in `live` tests (`docs/testing.md`) |
| Native TUI performance work | schema-3 incremental mirror, frame pacing, stall log |

Not found in `docs/` or `nexus/` by search (treat as gaps, subject to the caveat in §1):
per-turn file checkpoint and restore; a conversation rewind/branch-in-place command beyond
`/fork`; plan mode; Nexus acting as an ACP *server*; a user-scriptable status line; user-facing
scheduled or recurring prompts; provider-incident/status polling; credential-expiry
notifications; OpenTelemetry export; HTTP record/replay in tests; remote tool execution.

## 4. Prioritized opportunities

Ordering weighs fit with Nexus principles (context shown clearly, host contract, durable log,
layering, native TUI first), size, and risk. Web is deprecated and GPUI is paused, so no item
below asks for work in `nexus/ui/web/` or `rust/desktop/`; where a feature needs a client, the
native TUI is the target and the web/desktop get nothing new.

### P1. Per-turn workspace checkpoints with a labelled diff, and restore only when safe

- **Source of idea**: t3code (`CheckpointStore.ts`, `CheckpointRestoreSafety.ts`); opencode
  (`snapshot.ts`, `revert.ts`). grok-build documents the opposite choice (rewind never touches
  files).
- **Gap (verified by search)**: Nexus has `/diff` (workspace Git diff) and a Changes section
  (formerly labelled `MODIFIED FILES`, though docs disagree), but no record of what the tree
  looked like at each turn boundary, so "what did this turn change" and "undo this turn's file
  edits" are not answerable from the log.
- **Proposal**: at turn end, optionally record a bounded workspace snapshot reference as a
  durable event, so the TUI can show a per-turn diff (a labelled structured view, not a raw
  patch dump) and the user can restore. Restoring must be an explicit host command with the
  exact file list shown first, never an agent tool.
- **Rationale**: it directly serves "context clearly presented" (the user sees what each turn
  did to disk) and fits the existing rule that worktree integration applies "frozen bytes ...
  with a rollback journal" and the user "acknowledges an exact review" (`docs/decisions.md`).
- **Design cautions (inference)**: take t3code's lesson that restore is only safe when the
  checkout is not shared; Nexus sessions routinely share a workspace across tabs and subagents
  (global turn cap of 4), so the default should be diff-only, restore only for worktree
  sessions. Snapshots must not use Git commit/reset/stash/checkout on the user's branch (an
  existing decision); a separate object store or private ref is the open design question.
  Events must stay bounded and replayable (store references and counts, not file bodies).
- **Risks**: disk growth, large repositories, dirty trees, secrets in snapshots, and overlap with
  the existing worktree journal. **Non-goal**: automatic rollback when a turn fails.
- **Size**: large; needs a plan under `plans/`, a host command pair, a reducer field, and tests.

### P2. Conversation rewind in place (history-only), built on the existing fork

- **Source**: grok-build guide 17 (`/rewind`, one point per user prompt, Esc-Esc); pi
  `sessions.md` (`/tree`, `/fork`, `/clone`, branch summaries).
- **Gap**: Nexus can fork at a `seq` but, from what I read, a fork is a *new* session. There is
  no in-place "go back to this prompt, edit it, continue" flow in the native TUI (no picker or
  rewind flow was found in `rust/tui/src/`).
- **Proposal**: a native TUI picker over user prompts that calls `SessionFork` at the chosen
  `seq` and opens the child with the prompt text in the composer. Carrying prompt text into
  the composer is client-side work and is not provided by `SessionFork` for free. This reuses
  the append-only rule (nothing is deleted; the old branch remains) and needs no new storage.
- **Rationale**: lowest-cost use of a mechanism Nexus already has; keeps "durable log first".
- **Risks**: tab/session clutter; naming and lineage display (`docs/sessions.md` says fork
  lineage is not task ancestry). **Non-goal**: truncating the log (grok-style) or pi-style
  branch trees inside one session, which would change the reducer contract.
- **Size**: small to medium; mostly client work plus tests.

### P3. Content-free tests for providers via recorded HTTP cassettes

- **Source**: opencode `packages/http-recorder`; pi `packages/evals` for the evaluation shape.
- **Gap**: Nexus has hand-built fixtures and a provider conformance suite, plus opt-in live
  tests; there is no documented record/replay layer, and several provider docs say "not
  observed live" (xAI refresh, Copilot device flow, Antigravity). A recorder would turn each
  one-off live check into a replayable fixture.
- **Proposal**: a test-only record/replay transport for the `httpx`-level adapters, with a
  redaction pass and a "no credentials, headers, encrypted reasoning or private output"
  check that enforces the existing rule in `docs/testing.md`.
- **Rationale**: raises confidence in provider adapters without making CI need credentials.
- **Risks**: cassettes go stale when upstream changes; redaction mistakes leak secrets (hence
  an enforced scrubber test); some providers (WebSocket, SSE, device flows) need care.
  **Non-goal**: running live traffic in CI.
- **Size**: medium; test-only code, no runtime change.

### P4. Provider health and credential-expiry signals in the usage page and toasts

- **Source**: CodexBar `docs/credential-notifications.md`, README "Live status".
- **Gap**: Nexus shows usage windows and errors per provider row, but I found no incident polling
  and no "sign in again" episode logic.
- **Proposal**: reuse `host_support/provider_usage.py` refresh to classify only *typed*
  authentication failures (never string-matching "token" or "login", CodexBar's explicit rule)
  and raise one toast per failure episode, reset by a successful fetch. Optional, off by
  default, mirrored to Logs like every toast (`docs/decisions.md`, toasts).
- **Rationale**: matches "problems-first logs" and the toast contract; no new credential reads.
- **Risks**: false alerts; provider status pages are third-party network calls and must go
  through `net/` with the public-address-only rule or be left out. **Non-goal**: scraping
  browser cookies, which Nexus has refused.
- **Size**: small for credential episodes; medium for incident status (new network surface).

### P5. A bounded, user-scriptable status line for the native TUI

- **Source**: grok-build guide 25.
- **Gap**: Nexus has a built-in status line (`ui/cli/details.py`, `turn_status.py`) but no user
  hook. A user script is a code-execution surface.
- **Proposal**: if wanted, accept a script as a *user-level* setting only (never from project
  config, the same rule as `worker_dangerously_skip_permissions` in `docs/decisions.md`), pass
  it a documented JSON subset, cap output, strip control sequences, apply a timeout and
  debounce, and show the script's identity and last error in the UI so nothing is hidden.
- **Rationale**: low-cost personalization. **Risks**: this is the weakest fit with "context is
  clearly presented", because script output is opaque to Nexus, and it adds a spawn path from
  the TUI. Recommend deferring until there is a concrete user request.
- **Size**: small to medium.

### P6. Plan mode as host policy, not a prompt convention

- **Source**: grok-build guide 19 (plan file is the only writable path in every mode); pi chooses
  the opposite by leaving plan mode to extensions.
- **Gap**: there is no Nexus-enforced plan mode for root or native agents. Nexus does have legacy
  subagent roles (`plan`/`planner`, now falling back to read-only `advisor`), and `plan` permission
  mode is passed through to external Claude Code and Antigravity (`agy`) workers per
  `docs/security.md:104-111`, but native tool calls have no plan-restricted mode. Nexus decided on
  2026-10-17 that "read-only" is guidance only and never enforced (`docs/decisions.md`). A
  tool-enforced plan mode for root/native agents would contradict that recorded decision, so it
  **needs an explicit user decision first**.
- **Proposal (if the decision changes)**: a session mode that the permission engine treats as
  deny-all-writes except one named plan file, shown in the header and recorded as an event.
  Otherwise, a prompt-level "plan first" skill needs no core change and is already possible
  through the skills mechanism.
- **Size**: small if prompt-only; medium if enforced.

### P7. Nexus as an ACP server (editor integration)

- **Source**: grok-build guide 15 and `crates/codegen/xai-acp-lib` (file names only).
- **Gap**: Nexus is an ACP client for OpenCode; no ACP server by search. A server would be one
  more host *client* translating ACP to host commands, so it fits the layering rule
  (`nexus/ui/**` imports only host/view/events/client), and permissions would remain
  daemon-side.
- **Risks**: protocol churn; approval and context-presentation semantics in an editor may hide
  information from the user, which cuts against the product's core idea; the effort is
  unrelated to the TUI priority. **Non-goal** for now unless editor integration becomes a goal.
- **Size**: large. Listed for completeness, lowest priority.

### P8. Scheduled and recurring prompts

- **Source**: t3code `scheduledTasks/` (schedules, run-now, webhooks); grok-build `/loop`.
- **Gap**: nothing user-facing found by search. Nexus's rule that "a turn outlives its viewers"
  and the daemon's per-session FIFO make a scheduler technically natural: it would enqueue
  through the same `SessionEnqueue` path.
- **Risks**: unattended runs interact with the approval model (first-responder approvals in
  `host/presence.py`, and the unattended policy in `tools/permissions.py` and `config/schema.py`);
  webhooks add an inbound network surface and conflict with "loopback only". Recommend only a
  local, time-based, off-by-default form, with every
  fired run labelled in the transcript by origin (as `input.queued` already carries an `origin`).
  **Non-goals**: webhooks, public endpoints.
- **Size**: medium; needs a security review first.

### P9. Optional OS-level sandbox profile for shell jobs

- **Source**: grok-build guide 18; pi `containerization.md` (for honest exposure tables).
- **Conflict**: `docs/decisions.md` and `docs/security.md` state "no OS sandbox ... a false
  sense of isolation is worse than a stated boundary". Adopting this would reverse a recorded
  decision, so it is **not recommended without an explicit user decision**. If ever pursued, the
  lesson worth taking is grok-build's behavior of refusing to start when a profile cannot be
  applied, and pi's practice of listing what remains exposed. Cheaper first step: add a
  `docs/security.md` section documenting how to run Nexus inside Docker or a VM, in the style of
  pi's table.
- **Size**: docs only (small), or very large for real enforcement.

## 5. Smaller observations (no action proposed yet)

- **Agent-facing contributor rules** from t3code (`AGENTS.md`): the "reverse states" check ("if
  you added a way in, add the way out") and the no-sleep/no-polling test rules are portable
  ideas for `docs/testing.md`. Nexus's `AGENTS.md` already has the dirty-worktree rule; the
  "never kill by process-name pattern" rule is not there and is cheap to consider.
- **Virtual models (pi)**: Nexus tiers already resolve per request and clamp downward; pi's
  contribution is recording *selection* separately from *dispatch* and showing `selection →
  routed` in the footer. Whether Nexus's tier resolution is already surfaced that way was not
  verified; worth a quick check against `docs/models.md`.
- **Typed wire contracts**: t3code centralizes them in one package and opencode has an
  `httpapi-codegen` package (only its `package.json` read). Nexus mirrors Rust wire structs by
  hand with parity tests (`tests/test_desktop_wire.py`); a schema-generation approach would
  address drift but is a large change to a part that currently works. Not recommended now.
- **Remote execution (pi `env`)**: tools on another machine over SSH with a hash-verified
  daemon is interesting but conflicts with "credentials never leave the daemon" unless the
  design keeps the whole Nexus daemon local; out of scope.
- **OpenTelemetry (grok-build guide 24, pi `telemetry`)**: both stress content-free defaults and
  explicit opt-in. Nexus has durable events and `~/.nexus` logs already; an exporter would be a
  new outward network path. Defer unless an operator use case appears.
- **Memory (grok-build guide 13)**: Nexus has `MEMORY.md` frozen per turn; grok's topic files plus
  consolidation pass is a different model. I did not evaluate Nexus's memory flow, so no
  recommendation.

## 6. Not done / limits of this review

- No reference project was built or run; no behavior was exercised.
- No Rust, Swift or TypeScript source of grok-build or CodexBar was read; grok-build claims come
  from its user guide only.
- pi's `chord`, `ai`, `tui`, `mcp` packages, opencode's TUI/server/plugin/simulation packages, and
  t3code's web, mobile, desktop, relay and provider adapters were not inspected.
- `references/t3code/.repos/` holds vendored third-party repos (for example `alchemy-effect`);
  these are not t3code itself and were ignored.
- Commit dates, release tags and any local modifications in the clones were not checked.
- `docs/decisions.md` lines 752–1360 were not read in full (only targeted greps during sanity
  checks; do not treat the remainder as fully reviewed); an unread decision may already cover
  or nuance one of the "gaps" above. Re-check before opening a plan.
- Upstream licenses were not reviewed in depth; grok-build's `README.md` says its notices list
  in-tree ports of openai/codex and sst/opencode tool implementations, which is a reminder to
  treat all of these as read-only inspiration.

## 7. Suggested next step

Pick P1 and P2 together as one plan (they share the "what did this turn do, and go back to it"
theme), then P3 as an independent test-infrastructure plan. Decide P6 and P9 as *policy*
questions with the user before any engineering work, because both reverse recorded decisions.
Everything else can wait for a concrete request.
