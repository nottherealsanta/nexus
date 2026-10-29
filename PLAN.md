# Nexus 0.10 plan (branch nexus10)

Nexus is being rebuilt from scratch, starting at the provider wire: the exact JSON
that goes out and every event that comes back, token by token. The work is now
mostly porting. Old Nexus (`~/repos/nexus`, branch `main`; AGENTS.md calls it
`nexus01`) already has most of the remaining features and their tests. Copy its
code, adapt it to the nexus10 Core API and the one-timeline rule, and write down
every behavior that changes.

Branch `nexus10` is an orphan branch, checked out as a worktree at
`~/repos/nexus10`. It is released as **0.10.0** once it replaces the old code
("10" is binary for 2).

Rewritten on 2026-10-29. The previous plans are archived in git:

| File | Read it with | Holds |
| --- | --- | --- |
| PLAN.md before this rewrite | `git show fdb1860:PLAN.md` | Step 12 polish notes, 12a/11a lists, dated UI notes |
| DESIGN_PLAN.md | `git show fdb1860:DESIGN_PLAN.md` | Decisions D01–D18 and increments I0–I10. Code comments still cite these tags |
| INTERFACE_PLAN.md | `git show fdb1860:INTERFACE_PLAN.md` | The old prose interface spec. Its rules for the composer, handoff, one-turn sessions and settings are carried into the work items below |
| Step-by-step design notes for steps 0–11 | `git show 13651aa:PLAN.md` | Verification logs and delivery reviews |

**In progress, separate plan:** [TURN_SUMMARY_PLAN.md](TURN_SUMMARY_PLAN.md).
Core steps 1, 2 and 8 are done in the working tree (turn rows carry model,
effort, usage and reasoning time; `session_usage`; `GET /api/session/{id}/turn`;
`turn.summary`; recovery reads `turn.ended`). Steps 0, 3–7 and 9 are open: the
TUI and NSH switch to summaries, then deltas become ephemeral behind a live
partial. Work in this plan that touches turns, deltas or the event log has to
fit that design. The overlaps are listed in [Interactions with turn
summaries](#interactions-with-turn-summaries).

## Contents

1. Principles
2. Layout
3. Done
4. How to port
5. Decisions in this plan
6. Work items
   - A. Foundations: config file, mock provider, token counting
   - B. Steer and queue
   - C. Prompt history
   - D. Tools
   - E. Hot reload: user tools and agents
   - F. Skills
   - G. MCP
   - H. Hooks
   - I. Voice: speech to text and text to speech
   - J. Providers
   - K. Context and sessions (handoff, titles, `/btw`, drafts, search, paging)
   - L. Worktree review and integration
   - M. Interface (TUI open items, NSH parity, Mermaid)
   - N. Operations: doctor, install, update
   - O. Release gate
7. Order
8. Interactions with turn summaries
9. Not in this plan
10. Ledger
11. Known gaps
12. Open questions

## 1. Principles

1. **Context is visible.** The user can see everything the agent sees: the
   request body, every stream event, every tool argument and result, labelled.
   Any clipping is announced.
2. **Raw and normalized, side by side.** Each part a provider returns keeps its
   exact wire item (`raw`) and the provider that made it (`origin`). A replay to
   the same provider is lossless (reasoning `encrypted_content`, item ids). Other
   providers use the normalized fields.
3. **Task tools vs. control.** Tools act on the workspace: read, bash, edit,
   write, and code to script them. Changing Nexus itself (which session, workspace
   or worktree, which model, forking, subagents) is a separate control plane in a
   `nexus` namespace. Those are commands with their own event type and
   permissions. The user can run them directly, and the agent gets them only when
   granted. A worktree is an optional property of a session, never a mandatory
   tool.
4. **One step at a time.** Each step ends with something you can run and see.
5. **Separate state.** The command is `nexus`. State and the one config file stay
   in `~/.nexus10` (`$NEXUS10_HOME`) so nothing is shared with old Nexus
   (`~/.nexus`), which still runs from its own checkout. When 0.10.0 replaces
   old Nexus the home becomes `~/.nexus` (release gate, O3).
6. **uv only.** `uv sync`, `uv run`, `uv add`. Python is pinned in
   `.python-version` and dependencies are locked in `uv.lock`.
7. **One timeline.** The timeline the user sees and the context the agent sees
   stay as close as possible. Every item the model gets (system prompt, messages,
   tool results, notices, context insertions, handoffs, steered messages, skill
   bodies, hook output) is shown to the user in order. A child launch appears in
   its parent's context line; opening it shows the child's own context line.
   Activity indicators (running, waiting, queued, done) may update without
   becoming model context. Execution details are labelled separately from what
   the model received.
8. **Append-only context.** Context already sent to a provider is never edited:
   it is cached, and a change invalidates the cache. A session's system prompt is
   fixed when the session starts. Later changes (a new directory, a switched
   setting, a new skill or MCP tool) go forward as new items. Actions that will
   invalidate the cache anyway (switching model or agent) warn first.
9. **Copy first.** When old Nexus has it, port it with its tests. Do not
   redesign what already worked; change only what the nexus10 API, the
   timeline rule or a recorded decision requires.

## 2. Layout

Each part of the repo is a self-contained project with its own toolchain. The
root holds only plans and docs. Modules marked *(planned)* come from the work
items below.

```
docs/             how the code works; start at docs/README.md
interface/        shared manifest, theme tokens, capability matrix, design language
scripts/nexus     repo launcher (nexus, nexus tui, nexus web, nexus check …)
nexus-core/       Python (uv): everything that is not a UI. Import name `nexus`, command `nexus`
  nexus/
    llmhub/         providers normalized to one conversation + stream shape
      types.py, sse.py, responses.py, chat.py, providers.py, catalog.py, hub.py
      messages.py   (planned, J) Anthropic Messages dialect
      auth/         codex, copilot, grok, opencode; api_key.py (planned, J)
    tools/          read, bash, edit, write, ask, code
                    grep, glob, ls, multiedit, web_search, web_fetch, skill (planned, D/F)
    config.py       the one config file: schema, load, write, watch
    mock/           dev-gated scripted provider and scenarios for tests and demos
    counting.py     token estimates and the context budget
    inputs.py       (planned, B) steer and queue: pending inputs per session
    skills/         (planned, F) discovery, frontmatter, index, invocation, resources
    mcp/            (planned, G) client, manager, bridge, search
    hooks.py        command hooks (H)
    extensions.py   user tools loaded from disk, run in a child process
    voice/          (planned, I) HF cache lookup, STT engine, TTS worker, audio checks
    workspace.py, worktrees.py, kernels.py, bash_jobs.py, snapshot.py,
    observations.py, questions.py, files.py, images.py, agents.py,
    builtin_agents/, loop.py, codec.py, export.py, store.py, bus.py,
    eventlog.py, recovery.py, continuation.py, retry.py, retention.py,
    runtime.py, server.py, inspector/, daemon.py, client.py, cli.py
  tests/
nexus-search/     local SearXNG container (Docker and Apple container)
nexus-browser/    default browser-use MCP configuration
nexus-tui/        Ratatui terminal client (Rust)
nexus-nsh/        NSH client (TSX): browser + desktop
nexus-design-mockup/  temporary; delete once the NSH side of the design pass lands (M2)
```

Nexus Core runs as a daemon and exposes everything over HTTP (`docs/api.md`).
Every surface is a client of that API. No surface reads Nexus state files,
the config file or the Hugging Face cache directly.

## 3. Done

| Step | What landed | Read |
| --- | --- | --- |
| A1–A3 | Single-file config and shared editable Settings, provider login/logout, dev-only mock scenarios and Core context budget (2026-10-29) | docs/config.md, docs/llm-providers.md |
| 0–2 | Provider wire, normalized conversation types, the agent loop | docs/llm-providers.md, docs/loop.md |
| 3 | codex, grok, copilot, opencode providers; models.dev catalog; `provider/model` refs | docs/llm-providers.md, docs/models.md, docs/auth.md |
| 4 | Daemon, projects, sessions, turns, approvals over HTTP | docs/daemon.md, docs/api.md |
| 5 | Agent files (orchestrate, build, quick, scout, plan) | docs/agents.md |
| 6 | Session trees: child sessions with `launched_by`/`parent_call_id` | docs/daemon.md |
| 7 | Inspector page and the stored event log | docs/inspector.md |
| 8 | The `code` tool on daemon-owned Monty kernels | docs/tools.md |
| 9 | Tool descriptions from `Tool.guidance`, per-session tool switches | docs/tools.md |
| 9a | Background bash jobs (`bash_output`, `bash_kill`) | docs/tools.md |
| 10 | Frozen session snapshot and prompt, `AGENTS.md`, `ask`, approval-free in-directory edits, read-before-edit observations, workspace controls and worktrees, archive, export | docs/daemon.md, docs/api.md |
| 11, 11.1–11.7 | Durable events and cursors, crash recovery, manual continuation, idempotent mutations and revisions, bounded provider retries, `/api/snapshot` resync, retention | docs/api.md, docs/stream-events.md |
| 12.1 | Shared interface data: manifest, capability matrix, reference inventory | interface/README.md |
| 12.3 | TUI shell: connection supervisor, header, sidebars, palette, alerts and terminal notifications, waiting card with `ask` answers, composer with `/` and `@` completion, full timeline, Files tab with search, Git groups and diffs, settings modal, saved client preferences | nexus-tui/README.md |
| 12.5 (web) | NSH web client with the shell, timeline, pickers and export, behind a hardened host proxy | nexus-nsh/README.md |
| Design pass | TUI increments I0–I10 (chrome band, header tabs, timeline rhythm, Markdown and diffs, composer card, sidebars, one modal recipe, waiting card and alerts, starting context, files, narrow layouts, empty and offline states); NSH I0–I8 | `git show fdb1860:DESIGN_PLAN.md`, interface/design-language.md |
| Images | Clipboard image paste in both clients, inline `Image` parts durable in SQLite, lettered references, replay in both dialects | docs/stream-events.md#user-images |
| Turn summaries 1, 2, 8 (Core) | Model, effort, per-attempt usage, reasoning time and `turn.ended` on the turn rows; the `session_usage` ledger; one-time backfill; `GET /api/session/{id}/turn` and the `turn.summary` event (`api_version` 1.2); recovery reads `turn.ended` (2026-10-29). Clients still read events until TURN_SUMMARY_PLAN.md steps 3–6 | TURN_SUMMARY_PLAN.md, docs/api.md#turn-summaries |
| Turn summaries 0, 3–5 (TUI) | The TUI loads messages, starting context and every `/turn` page together and no event history; footers, tool timing, retry and error rows, context %, header model, sidebar tokens and cost (ledger, `Since <date>`), the continue hint and a floor row come from summaries; the running turn's live events are scratch until `turn.summary`; `/turn?after=` after a reconnect; `CLIENT_API` 1.2. Measured below (2026-10-29) | TURN_SUMMARY_PLAN.md, nexus-tui/README.md |
| I1–I2 (Core) | HF cache reader and `GET /api/voice` status; `[voice] stt_model` / `[speech] tts_model` (defaults Bark and phonon); `POST /api/voice/speak` streams a WAV from a warm offline ptts worker, `/speak/stop`; `voice` extra (installed by `nexus voice init`). Real synthesis checked with the cached phonon model (2026-10-29). TUI `/speak` and stop (rodio, streamed) done; NSH `/speak` (Web Audio) written, not yet heard in a browser; open: a stop key and I4–I5 | docs/voice.md |
| I3 (Core) | Lazy offline Bark STT, explicit verified HF-cache download, `POST /api/voice/transcribe` and progress events; fake-engine tests (2026-10-29) | docs/voice.md, docs/api.md |
| I4–I5 (TUI, NSH) | Dictation: `voice.dictate` (`/voice`, `Ctrl+D`, `Ctrl+X V`) in the TUI (`cpal`, waveform strip, cues) and a composer mic button in NSH (`getUserMedia`); 16 kHz PCM16 WAV, paced live previews, consent before the STT download; Enter or Stop inserts the transcript without sending, Esc cancels. `composer.dictate` became `voice.dictate`. Real microphone capture unverified (2026-10-29) | docs/voice.md |
| I6 (Core, TUI, NSH) | Settings → Voice and speech: model status, consented Download and confirmed Remove for both models; Core adds `POST /api/voice/tts/download` and `DELETE /api/voice/{stt or tts}`; `nexus voice init` installs the runtime. Unverified with real downloads and in a browser (2026-10-29) | docs/voice.md |
| Section I accepted unverified | Voice (I1–I6) is accepted without a run against real audio, a real Bark download or a browser; only fake-engine, API and unit tests, plus one real phonon synthesis, back it (2026-10-29, user decision) | docs/voice.md |

## 4. How to port

Each work item names its old Nexus sources. Paths are relative to
`~/repos/nexus` unless they start with a nexus10 directory.

1. **Read the old doc and the old tests first.** They describe the behavior that
   was verified. The old docs are listed per item (`docs/extensions.md`,
   `docs/voice.md`, `docs/loop.md`, `docs/tools.md`, `docs/context.md`,
   `docs/surfaces.md`).
2. **Copy the module and its tests together.** Put `Adapted from old Nexus
   <path>` in the module docstring, as `interface/reference-inventory.md` asks.
3. **Adapt the edges, keep the core.** The usual changes:
   - old `msgspec` host protocol structs (`nexus/host/protocol.py`) → HTTP routes
     in `server.py` and events through `bus.py`
   - old `ToolSpec`/`RegisteredTool` (`nexus/tools/spec.py`) → nexus10 `Tool`
     (`nexus-core/nexus/tools/base.py`, with `guidance`)
   - old config layers (`nexus/config/layers.py`, project + global) → the one
     config file (A1)
   - old `~/.nexus` / `.nexus/` paths → `$NEXUS10_HOME` and the project's
     `.agents/` folder
   - old event records → nexus10 durable events (`eventlog.py`) and state tables
     (`store.py`)
   - old Python TUI bridge (`nexus/ui/ratatui/*.py`) → logic in Core or in
     `nexus-tui` Rust; the TUI no longer has a Python half
4. **Drop what nexus10 does not have:** the gateway and paired devices, GPT Live,
   the manager session, model tiers, the iOS app, old host commands with no
   client.
5. **Write the ledger row** when a behavior changes, in the same change.
6. **Update the matching doc** in `docs/` and the capability matrix row in
   `interface/capability-matrix.md`.
7. **Check it in the real app**: `uv run pytest -q`, `uv run ruff check nexus
   tests`, `nexus check`, and TUI screenshots through `nexus-tui/tools/webterm`
   or `tools/shots.sh`.

## 5. Decisions in this plan

| # | Decision | Why |
| --- | --- | --- |
| P1 | **One config file**: `$NEXUS10_HOME/config.toml` (`~/.nexus10/config.toml`; `~/.nexus/config.toml` after 0.10.0). No project config layer, no `nexus.toml`, no separate `mcp.json` or `hooks.toml`. Settings in both clients read and write this file through Core, and an edit by hand shows up in Settings. | One place for every setting. Old Nexus had five layered files and two homes, and Settings and the host disagreed about `NEXUS_HOME` (old `docs/extensions.md`, "Adding an MCP server") |
| P2 | Project *content* is still discovered: `.agents/agents/`, `.agents/skills/`, `.agents/tools/` and `AGENTS.md` in the project. These are files the user writes, not settings. | Agent and skill files belong with the code they describe |
| P3 | No model tiers (no low/medium/high model routing). Reasoning effort per session stays. | User decision |
| P4 | **Enter steers and Tab queues** while a session is working, and only when the composer has content. Idle: Enter sends as today and Tab keeps its current role. | User decision; replaces old `Ctrl+Enter` queue (old `docs/surfaces.md` line 199) |
| P5 | Voice models are read from the **Hugging Face cache** when they are there, and downloaded into that cache (after consent) when they are not. Nexus keeps no model copy of its own. | User decision; old STT used a private copy in `~/.nexus/models/voice` |
| P6 | Hot reload: user tools and agent files are re-read from disk. A **new** session uses the current files. A running session keeps its frozen snapshot (Principle 8). | User decision; keeps the prompt cache valid |
| P7 | Every old tool is ported: `grep`, `glob`, `ls`, `multiedit`, `web_search`, `web_fetch`, `skill`, plus `escalate` as a control tool for children. | User decision |
| P8 | Out of this plan: GPT Live, the iOS app and gateway, the manager session, model tiers. | User decision |

## 6. Work items

Each item lists **Old** (the files to copy), **New** (where it goes), **Changes**
(what differs from old Nexus and needs a ledger row), **Tests** and **Done when**.

### A. Foundations

These come first because B, F, G, H and I depend on them.

#### A1. The config file and Settings over the API

**Landed 2026-10-29:** comment-preserving atomic config writes, schema and
per-key errors, revision conflicts, serialized daemon patches, two-second
outside-edit polling, config events, masked MCP credential round trips and
`nexus config import-mcp`. Root sessions apply defaults; explicit creation
fields win and existing snapshots stay frozen. TUI and NSH Settings edit model,
effort, agent, permission and disabled tools and refresh on outside edits.
Detail and reduced-motion preferences are shared through `[ui]`.
Provider device/API-key login and logout are exposed over HTTP and in both
clients. Later feature items add their own sections. External writers can race
a daemon write and rapid outside edits can coalesce between polls.


**Old:** `nexus/config/schema.py` (1455 lines, the typed schema and defaults),
`nexus/config/layers.py` (merge order, which is dropped), `nexus/config/paths.py`,
`nexus/host_support/settings_inventory.py` and `settings_scope.py` (path-checked,
hash-checked writes, secret masking and restore), `nexus/ui_support/settings_page.py`,
`settings_help.py`. Old tests: `tests/test_config_layers.py`, `test_config_secrets.py`.
Old docs: `docs/config.md`, `docs/extensions.md` "Settings files (host)".

**New:** `nexus-core/nexus/config.py` and `docs/config.md`.

- File: `$NEXUS10_HOME/config.toml`. A missing file means defaults. Core writes
  it atomically (temp file and rename, the same pattern as `auth/store.py`) and
  keeps comments and order with `tomlkit` (`uv add tomlkit`).
- Sections (the first version; each work item adds its own):

  ```toml
  [defaults]       # model, effort, agent, permission ("allow" | "ask")
  [context]        # handoff_percent = 80 (K1)
  [tools]          # disabled = [...]; per-tool permission rules (D7)
  [web]            # search_url (nexus-search), fetch limits
  [voice]          # stt model repo/revision, device, max_seconds, auto_send
  [speech]         # tts model repo, enabled
  [mcp.servers.<name>]   # G; same keys as old mcp.json entries
  [[hooks.<Event>]]      # H
  [ui]             # values both clients share (detail level, reduced motion)
  ```

- API: `GET /api/config` returns `{revision, values, defaults, schema, errors}`
  with secrets masked. `PATCH /api/config` takes `{expected_revision, set: {...},
  unset: [...]}` and returns 409 when the file changed. A `config.changed` event
  is published on every write and on every outside edit (Core polls the file's
  mtime every 2 s; no watcher dependency).
- An invalid file never stops the daemon. Defaults are used for the broken keys,
  the errors are listed in `/api/config` and in Settings, and nothing else
  changes.
- The client-only preferences in `~/.nexus10/tui.json` stay where they are
  (window layout, open tabs). Anything both clients should agree on moves into
  `[ui]`.
- Settings in the TUI and NSH become editable. The manifest's
  `unavailable_reason = "Core has no default setting for this yet…"` rows in
  `interface/manifest.toml` turn into real settings.
- Sign-in and sign-out over the API (`POST /api/provider/{id}/login` running the
  same device flow as `nexus login`, `DELETE` to sign out) land here too, from
  old Step 12a.

**Changes:** one file instead of layered project/global files; MCP servers and
hooks move into it. `nexus config import-mcp <path>` converts an existing
`mcp.json` (any dialect from G) into `[mcp.servers.*]` tables.

**Tests:** load and defaults; invalid keys reported without failing; a write
keeps comments; a stale revision gets 409; an outside edit publishes
`config.changed`; secrets are masked in `GET` and restored on an unchanged masked
`PATCH`.

**Done when:** changing the default model in TUI Settings changes
`~/.nexus10/config.toml`, and editing the file by hand updates Settings in a
running TUI and NSH.

#### A2. Mock provider and scenarios

**Landed 2026-10-29:** dev-gated `mock` provider with all eleven scenarios,
normalized and wire events, catalog entries and a real-loop tool marathon test.
The B-specific steer/queue assertions land with B, whose input API is not yet
implemented.

**Old:** `nexus/devtools/mock/provider.py`, `dsl.py`, `directive.py`,
`runner.py`, `checks.py`, `sandbox.py`, `scenarios/*.py` (hello, bash_wait,
cancel, parallel_tools, question, streaming_rich, stress, tool_marathon,
provider_failure, parallel_subagents, context_pressure). Old doc:
`docs/devtools.md`.

**New:** `nexus-core/nexus/mock/`, registered as provider `mock` only when
`NEXUS10_DEV=1`. Models are scenario names (`mock/hello`). It produces real
`llmhub` stream events (text, reasoning, tool calls, usage), so the loop, store,
turn summaries and both clients run unchanged.

**Changes:** it plugs into `llmhub/providers.py` as a dialect instead of the old
`model/providers/scripted.py`. Scenarios that used manager or live features are
not ported.

**Done when:** `NEXUS10_DEV=1 nexus chat -m mock/tool_marathon` runs a turn
with no network, and the steer/queue tests in B use it.

#### A3. Token counting and context budget

**Landed 2026-10-29:** `nexus/counting.py`; the context endpoint adds top-level
`used`, `window`, `exact` while preserving request inspection. Usage comes from
the newest measured request only when its model matches; otherwise it is an
announced character estimate. Unknown catalog windows remain null. Both client
meters consume the endpoint.

**Old:** `nexus/context/counting.py` (170 lines), `nexus/context/budget.py`
(267 lines), tests `test_context_counting.py`, `test_context_budget.py`. Old
doc: `docs/context.md` "Budget", "Token counting".

**New:** `nexus-core/nexus/counting.py`. Uses the model's window from the
models.dev catalog and the newest request's real input tokens from turn
summaries (`turn_request.input_tokens`) when known. Estimates are labelled as
estimates.

**Used by:** the composer meter (exact once a request has usage), session drafts
(K4), the handoff threshold (K1), the steer/queue view (how much a pending
message adds).

**Done when:** `GET /api/session/{id}/context` returns `{used, window, exact}`
and the TUI meter reads it.

**Section A verification (2026-10-29):** Core 404 tests and Ruff pass;
full TUI tests pass, Clippy with warnings denied and formatting pass; shared
interface checks and NSH type/generation checks pass. Real offline CLI marathon
completed all 36 calls and its 5/5 verdict using an isolated home, workspace and
port (an isolated home alone still defaults to port 4747). TUI Settings was
rendered and inspected. NSH host tests and a focused browser Settings PATCH test pass; the broader browser shell suite's
finite mock SSE transport causes reconnect-related failures, so it is not
recorded as green.

### B. Steer and queue

**Landed in code (2026-10-29), pending review and real-app check:** durable Core
inputs, recovery and hold-after-interrupt, both client composers and pending
stacks, independent mock integration tests (Core 416 tests, TUI tests, Clippy
and format pass). The TUI tags a delivered steer and no longer treats it as a
turn start. Open: NSH browser tests for Enter/Tab/stack (see Known gaps: the suite
runs only with `--browser`, and 11 tests fail), and the "done when" run was
done in both clients on 2026-10-29 with `copilot/gpt-6-luna`. Section A was committed as `eb7168f`.

Today a prompt while a turn runs gets 409 (`server.py` route table, `runtime.py`
`self.turns`). The TUI already renders `steer` and `queued` message tags, but
Core sends none (archived DESIGN_PLAN "Known gaps").

**Old:**
- `nexus/session/session.py`: `enqueue` (line ~1647), `consume_steering`
  (~1818), `add_context` (~1685), and the queue ordering helpers after them.
  The durable events are `input.queued`, `input.consumed`, `input.dropped`,
  `input.moved` and `input.mode_changed`.
- `nexus/core/loop.py`: steering is consumed at a safe boundary (~2290), and a
  step with no tool calls continues when steering is waiting (~2596).
- `nexus/session/manager.py`: `queue_depth`, and delete refusing a pending queue
  (~880).
- `nexus/host/protocol.py`: `SessionEnqueue` (`mode: queue | steer |
  interrupt`), `SessionQueueMove`, `SessionQueueRemove` (returns the text to the
  composer), `SessionQueueSetMode`.
- TUI: `rust/tui/src/bridge.rs` (queued section, `steer_restore_applies`),
  `rust/tui/src/live_bar.rs` (`turn #N · steer` rows).
- Docs: old `docs/loop.md` "Steering, queue, interrupt", old `docs/surfaces.md`
  around line 199.

**New:**
- **Core** (`nexus-core/nexus/inputs.py`, `store.py`, `runtime.py`, `loop.py`,
  `server.py`):
  - A `pending_input` table: `id, session_id, mode ("steer" | "queue"), parts
    (JSON, text and images), created, position, state ("pending" | "delivered" |
    "removed")`. Pending input is durable, so a client can close and a daemon
    restart keeps it.
  - `POST /api/session/{id}/prompt` takes `mode: "send" | "steer" | "queue"`
    (default `send`). `send` while busy still returns 409, so old clients keep
    working. `steer` or `queue` while idle starts a turn at once (the same as
    `send`). It returns `{input_id, mode, state}`.
  - `GET /api/session/{id}/input` lists pending inputs (also included in
    `/api/snapshot`). `PATCH /api/session/{id}/input/{iid}` edits the text or
    changes the mode (`queue → steer`). `DELETE` removes one and returns its
    parts so the client can put them back in the composer. `POST
    .../input/{iid}/move` takes `{before: iid}`.
  - **Steer delivery:** the loop already takes notices from `inbox` before each
    model call (`loop.py`, `self.inbox()` near line 200, which also delivers
    child results and `message_session` steers). Steered inputs join that path:
    at the next model call, after the current tool results, each pending steer
    becomes a user message with `delivery: "steer"` metadata. A steer that
    arrives during the final answer (no tool calls) makes the loop take one more
    step instead of ending the turn, as old `loop.py` ~2596 did. Delivery never
    splits a tool call from its result.
  - **Queue delivery:** when a turn ends (completed, failed or interrupted),
    the oldest queued input starts the next turn. The user message carries
    `delivery: "queued"`. After an interrupt the queue is **held** (not
    started), and the timeline says so, so Esc does not trigger the next queued
    prompt by surprise. "Send next" in the queue view releases it.
  - **Crash recovery:** pending inputs survive. A pending steer whose turn was
    recovered as interrupted becomes a queued input (it never reached the model)
    and the recovery notice says so. Nothing starts by itself after a restart.
  - **Events** (durable): `input.added`, `input.updated`, `input.removed`,
    `input.delivered` (with `message_id` and `turn_id`). Delivery is the moment
    the input becomes model context, and only then does it show as a message.
    Until then it is an activity indicator in the queue view.
  - Session delete and archive refuse while inputs are pending, unless the call
    says `drop_inputs: true` (from old `manager.py` ~880).
- **TUI** (`nexus-tui/src/composer.rs`, `app.rs`, `render.rs`, `api.rs`,
  `reduce.rs`):
  - Busy, with content: **Enter steers**, **Tab queues**. With an open `/` or
    `@` completion, Tab still accepts the completion first. With an empty
    composer, Tab keeps its current role.
  - A pending-input stack above the composer card, newest at the bottom, each
    row `steer` or `queued` with the first line of text. Up/Down selects, Enter
    edits (back into the composer), `Backspace` removes and restores to an
    empty draft (old `steer_restore_applies`), and `s` turns a queued input into
    a steer.
  - Delivered inputs appear in the timeline at the point they were inserted,
    with the `steer` or `queued` tag the renderer already has.
  - Esc interrupts the turn, and the control row shows no stop action (spec
    change of 2026-10-29). The keys `enter steer · tab queue` are shown in the
    pending stack's heading once it has an entry, and in help and the palette,
    not on the control row.
- **NSH** (`nexus-nsh/src/ui/composer.tsx`, `store.ts`, `core/reduce.ts`): the
  same keys. Tab is captured only while busy with content, so keyboard focus
  navigation still works otherwise. Shift+Tab is never captured. The same
  pending stack, plus buttons for mouse users.
- **Manifest:** add `composer.steer` and `composer.queue` commands with their
  `when` conditions (`busy && has_draft`) in `interface/manifest.toml`, then
  regenerate `nexus-nsh/src/gen/manifest.ts`.

**Changes:** the default key for queue is Tab, not old `Ctrl+Enter`; no
`interrupt` mode (Esc stops, then the queue is held); pending inputs live in a
state table, not in replayed events.

**Tests:** using A2's `mock/tool_marathon` and `mock/bash_wait`: a steer during
a tool call is delivered after that result and before the next request (check
the request body); a steer during the final answer adds a step; two steers keep
their order; a queued input starts the next turn after a completed turn and is
held after an interrupt; a restart keeps pending inputs and turns an undelivered
steer into a queued one; `send` while busy is still 409; removal returns the
parts; delivered inputs have `input.delivered` and a message with `delivery`;
TUI key tests for Enter, Tab, Tab with completion open, and empty Tab.

**Done when:** in the real TUI, during a long bash call, Enter on "also run the
linter" shows the message in the timeline right after the bash result, and Tab
on "now write the changelog" starts a new turn when the first one finishes.

### C. Prompt history

**Landed 2026-10-29 (Core, TUI, NSH), pending the real-app check:** Core table
`prompt_history` and `GET /api/project/{id}/prompts` (`q`, `limit`, `before`;
per project, duplicates collapsed, notices excluded, one-time backfill for old
sessions, docs/api.md). The TUI refills `editor.history` from it on session open,
after a send and on `turn.summary`; Up recalls on the first line and Down on the
last, so Up inside a multi-line draft or recalled entry moves the cursor; images
show as `[N images]`. NSH recalls with Up/Down only while the draft is empty or
exactly the recalled entry (the caret is not readable there). Core 419 tests,
TUI tests, Clippy and `nsh check` pass. **Open:** Ctrl+R search list (both
clients; `q` is a plain substring until K5's FTS5), NSH browser tests (see Known gaps), and the "done when" run with both real clients. The text below is the
original spec.

nexus10 has history in the TUI only (`nexus-tui/src/editor.rs`
`history: Vec<String>`, 500 entries, Up/Down at `app.rs` ~4104). It is kept in
memory, so it is lost when the TUI restarts, and it holds only what this client
sent. NSH has none. Old Nexus stored it in `~/.local/state/nexus/prompt_history`
(`nexus/ui_support/prompt_history.py`, test `tests/test_prompt_history.py`), one
global file for every project, and the user reports that it did not work well.

**New:** history comes from Core, built from what was actually sent.

- `GET /api/project/{id}/prompts?q=&limit=50&before=` returns the user's own
  messages (typed, steered or queued; not notices, child results or hook output)
  from every session in the project, newest first, with exact duplicates
  collapsed. Each entry is `{text, image_count, session_id, created}`. The
  FTS5 index from K5 speeds up `q`.
- TUI and NSH: Up on the first line of the composer (or in an empty composer)
  goes to older entries and Down goes to newer ones. The draft being typed is
  kept and comes back after the newest entry. Multi-line prompts come back
  whole. Ctrl+R opens a search list over the same endpoint.
- The in-memory `editor.history` becomes a cache of the endpoint, refreshed on
  session switch and after each send.

**Problems to test for**, taken from how the old one and the current one fail:
multi-line entries recalled as one entry; Up in the middle of a multi-line draft
moves the cursor instead of recalling; the draft is not lost; history survives a
TUI restart; a prompt sent from NSH is in the TUI's history; history is per
project, not global; images are mentioned (`[2 images]`) but not re-attached.

**Done when:** after restarting both clients, Up in the TUI recalls a prompt
sent from NSH in the same project, and the tests above pass in
`nexus-tui/tests/behaviour.rs`.

### D. Tools

**Landed 2026-10-29: D1–D4** (grep, glob, ls, multiedit) and **D5–D6**
(`web_search`, `web_fetch`; Core 469 tests, Ruff, TUI tests pass; docs/tools.md).
**D7 landed 2026-10-29** (permission rules: `nexus/permissions.py`,
`[tools.permission]`, agent `permission:`; docs/tools.md "Permission rules").
**D8 landed 2026-10-29** (`escalate`, a child-only control tool; Core 492 tests;
docs/daemon.md). **Section D is complete.** D5 was smoke-tested against a real
container on 2026-10-29: `nexus-search/`'s Docker Compose starts SearXNG and
serves `/search?format=json`, and `web_search` returned real results (image
`searxng/searxng@sha256:eb2a404d…`, recorded in its README; still to be pinned
in compose, O6). The Apple `container` path is untried. Port 18765 was held by
old Nexus's own `websearch-searxng-1` container, so the compose was run on a
temporary port. The tool is tested against a local
fake SearXNG. D5/D6 decisions: search reads `[web] search_url` and only JSON; the
HTML fallback, remote-instance allowlists and the old 1200-line outbound service
are dropped; `web_fetch` follows only same-origin redirects and refuses
non-public addresses, with the connection pinned to the checked address
(`nexus/net.py`); both tools need approval in `ask` sessions. Decisions made while porting:
grep uses `rg` or a killable Python scan, paths are relative to the workspace,
and neither grep nor glob nor ls records read observations (a match is not a
read); `ls` shows the directory as it is on disk (no `.gitignore`); `multiedit`
needs a complete observation, since its intermediate positions do not map to
lines read. `scout` and `orchestrate` get grep, glob and ls. Not yet checked in
a real session with a model; the TUI preview lists the new tools.

All are task tools (`nexus-core/nexus/tools/`), listed in `docs/tools.md`,
with per-session switches. Each port keeps the old output limits and announces
clipping.

| Tool | Old | Notes |
| --- | --- | --- |
| D1 `grep` | `nexus/tools/builtin/grep.py`, `_grep_scan.py` | Uses `rg` when it is on PATH, otherwise the Python scan. Records read observations for matched files only when it returns full lines (check against `observations.py`) |
| D2 `glob` | `glob.py` | Respects `.gitignore` like `files.py` already does |
| D3 `ls` | `ls.py` | Shares the walk in `files.py` |
| D4 `multiedit` | `multiedit.py` | Several replacements in one file, applied all-or-nothing with the same read-before-edit check as `edit` |
| D5 `web_search` | `websearch.py`, `nexus/net/local_search.py` | Talks to `nexus-search/` (SearXNG). Smoke-test the container and pin its image first (it was never run). URL from `[web] search_url` |
| D6 `web_fetch` | `webfetch.py`, `_html_markdown.py`, `nexus/net/outbound.py` (SSRF guard, redirects, size limits) | Fetched content is untrusted data and is fenced the same way as MCP output (G) |
| D7 permission rules | `nexus/tools/permissions.py` (rule grammar and matcher), `tests/test_permissions.py` | `allow` / `ask` / `deny` per tool and per pattern (`bash(git push*)`), in agent frontmatter (`permission:`) and `[tools.permission]` in config. Agent rules narrow, never widen. Closes `docs/agents.md` "Not yet" |
| D8 `escalate` | `escalate.py` | A **control** tool for child sessions only (Principle 3): the child reports it is blocked, its answer to the parent carries `attention`, and the parent's timeline shows it |

**Tests:** copy `tests/test_builtin_websearch.py`, `test_builtin_webfetch.py`
and `test_permissions.py`; new tests for grep, glob, ls and multiedit
(confinement to the session directory, clipping notices, read observations).

**Done when:** every tool shows in the starting-context tools grid with a
switch, and `docs/tools.md` documents each one.

### E. Hot reload: user tools and agents

**Landed 2026-10-29 (Core), pending a real-session check and client UI:**
`agents.changed` from a two-second poll, `color` in frontmatter and `mode`/`color`
in `/api/agent`, hot-reload tests for global and project agent files
(`tests/test_agents_reload.py`), and user tools (`nexus/extensions.py`,
`GET /api/extension`, `extensions.changed`, `extension.failed`,
`tests/test_extensions.py`, docs/tools.md "User tools"). Core 504 tests and Ruff
pass. Decisions made while porting: metadata is read with `ast.literal_eval`
so the daemon never executes a file; each call runs in a child `python -I`
process; the snapshot stores the granted tools' source (`snapshot.extensions`)
so a running session keeps its code, even across a daemon restart, and a result
says when the file changed on disk. Not done: showing agent and tool problems
in the clients' pickers and Settings, a changed-file notice item (the result
line stands in), memory limits for a tool process, and `[tools] disabled`
validation of project-only tool names.

**Agents.** `agents.load()` already reads the files on every call
(`nexus-core/nexus/agents.py:157`, `runtime.py:353`), and the session snapshot
freezes the prompt (Step 10). So "an edited agent file is used by the next
session" mostly works already. What is missing:
- A test: edit `.agents/agents/build.md` and `~/.nexus10/agents/x.md`, and check
  that a new session's frozen prompt has the new text while a running session
  keeps the old one.
- An `agents.changed` event (mtime poll with the config file, A1) so the agent
  picker in both clients refreshes without a reconnect.
- An invalid agent file is listed with its error (agent picker and Settings),
  not dropped silently.
- `color` and `mode` in frontmatter and `/api/agent/{name}` (old 12a item).

**User tools.**
**Old:** `nexus/ext/manager.py` (3008 lines; discovery, load, reload, trash),
`ext/manifest.py`, `ext/quarantine.py` (runs a user tool in a child process),
`ext/template.py`, `nexus/tools/loader.py`; tests `test_ext_manager.py`,
`test_ext_manifest.py`, `test_ext_quarantine.py`, `test_hot_extension_e2e.py`,
`test_hot_extension_stress.py` (200 reloads, no leak). Old doc:
`docs/extensions.md` "Hot reload".

**New:** `nexus-core/nexus/extensions.py`. Tools are read from
`~/.nexus10/tools/*.py` and `<project>/.agents/tools/*.py` (P2). Port the
quarantine: a user tool runs in a child process, never in the daemon. Following
P6, a new session snapshot takes the current tool set and a running session
keeps its own; a tool whose file changed mid-session keeps running the code it
started with until the session ends, and the timeline says the file changed. A
load error is an `extension.failed` event and a row in Settings.

**Changes:** no `[[ext]]` table in a project config (P1); no Python hooks
(H); the 3000-line manager is cut down to what tools need.

**Done when:** writing `~/.nexus10/tools/hello.py` and starting a new session
lists `hello` in the tools grid and the model can call it, and the stress test
passes.

### F + G. Revised design (2026-10-29, agreed in review; supersedes the notes below where they differ)

**One rule for both:** a running session keeps what it started with (P6, Principle 8).
A skill or MCP server added, removed or edited later is **not** injected into running
sessions. Clients show a notification ("available in new sessions"); only a new session
has it. The only mid-session changes are the user's switches (below).

- **Skills.** Global folder `~/.agents/skills/` and project folder `<project>/.agents/skills/`
  (never `~/.nexus10`). The `skill` tool and the `name: description` index exist only in
  sessions that started with at least one skill on. The late-add notice (`skill.announced`)
  is removed. `allowed-tools` is ignored for now (Open questions).
- **MCP modes are global, not per server and not per session**, in `config.toml`:
  `[mcp] full_definitions = false`, `[mcp] max_listed_tools = 25`. `tool_loading` per
  server is dropped. Tools are `mcp_tool_search` and `mcp_call` only (`mcp_show_tool_def`
  is folded into search: a search returns the definition JSON). They exist only in
  sessions that started with at least one MCP server on.
  - `full_definitions = true`: the session's context holds every enabled server's tools
    with their full definitions (no limit).
  - `full_definitions = false`: with `max_listed_tools` or fewer tools in total (all
    servers together) the context lists every tool as `server/tool: description`; with more,
    it lists only each server (its `instructions`, else its name, and the tool count) and
    the model finds tools with `mcp_tool_search`, which returns definitions, then `mcp_call`.
  - Everything from a server is fenced untrusted data. The listing is part of the frozen
    system prompt, so it never changes mid-session.
- **A session starts only after every enabled MCP server is online** (connected in
  parallel, each within its connect timeout; no disk cache). A server that cannot connect
  does not block the session: it is listed as unavailable, switched off for that session,
  and reported (`mcp.failed`, a toast).
- **Switches in the starting-context header** (Skills and MCP sections, both clients):
  per session, `skill:<name>` and `mcp:<server>` entries in `disabled_tools`. Only what
  was present at the session's start can be switched; the rest says "new session".
  A mid-session switch injects one JSON item, a visible notice, and enforces it:
  `{"skill_unavailable": {"name": …}}`, `{"skill_available": {"name": …}}`,
  `{"mcp_unavailable": {"server": …}}`, `{"mcp_available": {"server": …}}`. A switched-off
  skill or server is refused by `skill`, `mcp_tool_search` and `mcp_call`.
- **`/learn`** takes three steps: a modal for project or global, the fetch, then a checklist
  of the skills found (Select all, Select none, Invert; installed ones are tagged and ticking
  one replaces it). Core: `POST /api/skill/preview` returns `{fetch_id, found}`; `POST
  /api/skill/install` takes `{fetch_id, skills, scope, overwrite}`.

### F. Skills

**Landed 2026-10-29 (Core; clients in progress):** `nexus/skills/` (lenient frontmatter,
fail-closed resources, project-over-global discovery with reported clashes), the `skill` tool
(body and bundled files, read from disk at call time), `GET /api/skill[/{name}]`,
`skills.changed`, `skill:<name>` switches with a visible JSON notice for a mid-session switch
and `locked` rows for what the session did not start with, session JSON `skills` rows, and
`/learn` (`POST /api/skill/preview` then `/install`, with a checklist and overwrite)
(`tests/test_skills.py`, `test_skills_install.py`, docs/tools.md "Skills"). A skill added later
is not injected into running sessions (see the revised design above). Open: `allowed-tools`
(Open questions), default-on settings for skills, a real-model run.

**Old:** `nexus/skills/` (`manager.py` 636, `frontmatter.py` 541, `model.py`
368, `resources.py` 485, `activation.py` 246, `invocation.py` 135, `errors.py`),
`nexus/tools/builtin/skill.py` (652), `nexus/host_support/skills_install.py`
(372, `npx skills` import). Tests: `test_skills_manager.py`,
`test_skills_model.py`, `test_skill_resources.py`,
`test_skill_activation_scope.py`, `test_builtin_skill.py`,
`test_context_skills.py`, `test_host_skill_inspect.py`, `test_skills_install.py`.
Old doc: `docs/extensions.md` "Skills".

**New:** `nexus-core/nexus/skills/` and `tools/skill.py`.

- Discovery: `<project>/.agents/skills/<name>/SKILL.md`, then
  `~/.agents/skills/<name>/SKILL.md` (the shared home-level folder, not `~/.nexus10`). The project wins on a name clash, and the
  clash is reported.
- Progressive disclosure, as in old Nexus: the session's frozen prompt gets an
  index of `name: description` lines, and the `skill` tool loads the body as a
  tool result. The body is visible in the timeline as model context. Bundled
  `scripts/` and `references/` resolve through the fail-closed resolver
  (`resources.py`).
- Per session: switches in the starting context's Skills section (it is listed
  as unavailable today). Off skills leave the index.
- A skill added or changed after the session started is not in its frozen
  index. The session gets a notice item ("skill `x` is now available") the
  next time the model is called, which follows Principle 8 and keeps the
  timeline honest.
- API: `GET /api/skill` (name, description, scope, path, error), `GET
  /api/skill/{name}` (frontmatter and body).
- `npx skills` import (old `skills_install.py`) is ported last, behind a
  confirmation that says it runs third-party code.

**Changes:** frontmatter is parsed leniently. Unknown keys (for example
`license`) are kept and shown as warnings instead of rejected. The old strict
parser turned away public skills. `allowed-tools` still only **narrows** the
tool set while a skill is active, and never grants.

**Done when:** a skill in `~/.agents/skills/` shows in the TUI starting
context, the model loads it with `skill`, and its body is readable in the
timeline.

### G. MCP

**Landed 2026-10-29 (Core; clients in progress):** `nexus/mcp/` on the `mcp` SDK (stdio,
Streamable HTTP, SSE): entry dialects and `${env:…}`, lazy single-flight connections that a new
session waits for, health, backoff, restart, stderr logs; `mcp_tool_search` and `mcp_call`; the
two global settings `[mcp] full_definitions` and `max_listed_tools` and the three context
listings; approval and `[tools.permission]` rules decided for the target `mcp__<server>__<tool>`;
everything a server says sanitized, bounded and fenced in `<untrusted-mcp-data>`;
`GET /api/mcp[/{name}]`, `POST …/restart`, the `mcp.*` events incl. `mcp.changed`;
`mcp:<server>` switches with the same JSON notices and locked rows as skills; pinned
browser-use offered disabled (`tests/test_mcp.py` runs a real stdio server; docs/mcp.md). The
pinned browser-use 0.13.14 was launched through the manager: 16 tools, and a real
`browser_navigate` plus `browser_get_state` of example.com. Open: resources and prompts, image
results (K8), Settings screens for adding servers, and a run in a real session with a model
(the "done when" below).

**Old:** `nexus/mcp/client.py` (2529 lines: stdio with process groups,
Streamable HTTP and legacy SSE, deadlines, bounded and redacted stderr),
`manager.py` (1749: lazy single-flight connect, health, backoff, circuit
breaker, list cache, hot apply, one immutable snapshot), `bridge.py` (1878:
`mcp__<server>__<tool>`, `mutates` unless `readOnlyHint`, resources, prompts),
`search.py` (88: `mcp_search` / `mcp_call` search loading), `errors.py`.
Tests: `test_mcp_client.py`, `test_mcp_manager.py`, `test_mcp_bridge.py`,
`test_mcp_dialects.py`, `test_mcp_integration.py`,
`test_mcp_loading_config.py`, `test_mcp_search_*.py`,
`test_session_mcp_loading.py`, `test_host_mcp_show.py`, fixture
`tests/fixtures/mcp_server.py`, `nexus/devtools/mock/mcp_server.py`. Old doc:
`docs/extensions.md` "MCP", "MCP config dialects", "MCP search loading".

**New:** `nexus-core/nexus/mcp/` (`uv add mcp`, pinned).

- Config: `[mcp.servers.<name>]` in `config.toml` (P1). The entry keys are the
  old dialect keys (`command`, `args`, `env`, `url`, `headers`, `transport`,
  `enabled`, `tool_loading`, `includeTools`, `excludeTools`, timeouts) and the
  same `${env:VAR}` interpolation. `nexus config import-mcp` (A1) reads any
  dialect from old `normalize_server_entry`. Settings → MCP adds, edits, enables
  and disables servers through `PATCH /api/config`.
- Default server: browser-use from `nexus-browser/mcp.json`, written into the
  default config disabled until the user enables it. Pin its version and launch
  it once before it is enabled by default.
- Tools are task tools named `mcp__<server>__<tool>`. Default loading is
  `search`: the model gets `mcp_search` and `mcp_call`, and full schemas stay
  out of the request until selected. `tool_loading = "all"` puts them in.
- Approval: a tool that mutates (no `readOnlyHint: true`) follows the session's
  permission and D7's rules.
- Everything a server returns is untrusted data: sanitized, bounded, fenced in
  `<untrusted-mcp-data>`, and shown in the timeline exactly as the model
  received it.
- Per session: servers are switches in the starting context's MCP section
  (listed as unavailable today). The tool list is frozen in the snapshot. A
  server whose tools change mid-session adds a notice item instead of rewriting
  the tool list (Principle 8). A server that dies never fails a turn: its
  tools return an error result and an `mcp.failed` event is published.
- Logs: stderr goes to `~/.nexus10/logs/mcp/<server>.log`, never into context.
- API: `GET /api/mcp` (server, scope, status, tools count, error), `GET
  /api/mcp/{name}` (read-only detail as in old `McpServerShow`: no connecting,
  no argument values), `POST /api/mcp/{name}/restart`. Events `mcp.connected`,
  `mcp.disconnected`, `mcp.failed`, `mcp.tools_changed`.
- Images in MCP results need artifact storage (K8). Until then an image result
  is replaced by a labelled placeholder, and the placeholder says so.

**Changes:** the config lives in `config.toml` instead of `mcp.json`;
project-scoped MCP config is dropped (P1); OAuth is still not implemented.

**Done when:** the browser-use server, enabled in Settings, runs a page fetch in
a real session, the TUI MCP section shows it connected, and killing the server
process shows `mcp.failed` while the turn continues.

### H. Hooks

**Landed 2026-10-29 (Core), pending a real-session check and client UI:**
`nexus/hooks.py` (parse, runner, limits), the loop firing `UserPromptSubmit`,
`PreToolUse`, `PostToolUse`, `TurnEnd` and (first turn) `SessionStart`,
`SessionEnd` fired on session delete, `hook_fired` events, hooks listed in
`GET /api/config` with their errors, `tests/test_hooks.py`, docs/hooks.md.
Decisions made while porting: one module instead of a package; `on_nonzero`
defaults to `warn`; a blocked prompt stays in the context and ends the turn
with stop `blocked` and a visible hook message; a `modify` keeps the assistant's
original call in the context and adds a notice with the new input (Principle 8),
and the tool's own validation and approval then run on the new input; hook
messages and context are delivered as a user message at the next model call
(a `TurnEnd` hook's goes in at once, before `TurnEnd`). Open: editing hooks in
Settings (`PATCH` takes no `hooks` key; edit the file), the hook rows in the
timeline renderers of both clients, `SessionEnd` on archive, `bash(...)` matcher
tests against compound commands, running `PostToolUse` after a denied approval
(it fires for every known tool call, including declined ones), and the
"done when" run with a real model.

**Old:** `nexus/hooks/manager.py` (1663 lines), `hooks/model.py` (611),
`examples/hooks.toml`, test `test_hooks_manager.py`. Old doc:
`docs/extensions.md` "Hooks".

**New:** `nexus-core/nexus/hooks/`, **command hooks only**, configured as
`[[hooks.<Event>]]` in `config.toml`.

- Events: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
  `TurnEnd`, `SessionEnd` (old `ContextAssembled`, `PreCompact` and
  `ExtensionLoaded` wait for K1 and E).
- Decisions: `allow`, `warn`, `block` (a blocked tool becomes an error result
  the model sees), `modify` (replaces the input, which is then re-validated
  and re-approved; a hook is policy, not permission).
- The old limits stay: argv with no shell unless `shell = true`, timeout
  10 s default and 600 s max, stdin JSON at most 256 KiB, stdout at most
  64 KiB, a fixed safe environment plus `NEXUS_WORKSPACE` and
  `NEXUS_PROJECT_ROOT`.
- Timeline: anything a hook adds to context (a block message, modified input,
  added text) is a visible item labelled with the hook. A hook that only
  allowed is an execution detail (`hook.fired`), not context. This closes
  capability-matrix row T18.

**Changes:** no Python hooks (they needed the quarantine; revisit after E); no
project `hooks.toml` (P1).

**Done when:** a `PreToolUse` hook that blocks `bash(rm *)` shows its block in
the timeline and the model gets the error result.

### I. Voice: speech to text and text to speech

**Old:**
- STT: `nexus/voice/manager.py` (450, serialized lifecycle, 1 running + 2
  waiting), `engine.py` (209, `BarkEngine` → Transformers `ParakeetForTDT`),
  `bark.py` (265, the strict `.bark` container reader), `store.py` (178,
  pinned manifest, download), `audio.py` (112, WAV checks: mono, 16 kHz, PCM16,
  0.2–120 s), `model.py`, `NOTICE` (CC-BY-4.0 attribution).
- TTS: `nexus/host_support/speech.py` (569, the `ptts` worker for
  `gradium/phonon-31m-en`, q8, CPU, sentence chunks, warm worker unloaded after
  10 idle minutes, `HF_HUB_OFFLINE=1` when playing).
- Client side: `nexus/ui_support/voice_capture.py` (capture and start/stop
  cues), `ui/ratatui/voice.py`, `rust/tui/src/live_wave.rs` (waveform),
  `ui_support/speech_settings.py`, `voice_settings.py`,
  `speech_download.py`.
- Tests: `test_voice_audio.py`, `test_voice_bark_format.py`,
  `test_voice_engine.py`, `test_voice_manager.py`, `test_voice_store.py`,
  `test_voice_config.py`, `test_voice_cues.py`, `test_speech.py`,
  `test_speech_settings.py`, `test_ratatui_voice.py`.
- Measurements: `memory usage/speech-to-text/measurement.md`,
  `memory usage/text-to-speech/measurement.md`.
- Old doc: `docs/voice.md`.

**Where the Hugging Face cache is** (checked 2026-10-29 against the
[Hub cache docs](https://huggingface.co/docs/hub/local-cache) and the
[huggingface_hub environment variables](https://huggingface.co/docs/huggingface_hub/v1.1.1/en/package_reference/environment_variables)):

1. `$HF_HUB_CACHE` if set,
2. else `$HF_HOME/hub`,
3. else `$XDG_CACHE_HOME/huggingface/hub`,
4. else `~/.cache/huggingface/hub`.

Layout: `models--<org>--<name>/refs/<revision>` holds a commit hash,
`snapshots/<hash>/<file>` are links into `blobs/`, and a `blobs/*.incomplete`
file means a download is still running or was cut off. This machine has
`~/.cache/huggingface/hub` with `models--gradium--phonon-31m-en` (the old TTS
model), `models--hexgrad--Kokoro-82M`, `models--sahilmahendrakar--Paradee-8M-v1.0`,
`models--mlx-community--whisper-large-v3-turbo`, `models--mlx-community--whisper-base-mlx`,
`models--nvidia--canary-180m-flash` and several Parakeet GGUF repos. The old STT
weights (`ConwayResearch/Underdog-Bark-3.0-Clean`) are **not** in the HF cache
(old Nexus kept a private copy in `~/.nexus/models/voice`, which nexus10 must
not read).

**New:** `nexus-core/nexus/voice/`.

- `voice/hf_cache.py`: a small reader for the layout above with no
  `huggingface_hub` import, so status checks stay cheap. `find(repo, revision,
  files) -> Path | None` resolves `refs` → snapshot, checks that every needed
  file exists, and treats any `.incomplete` blob for the repo as not ready.
  `scan()` lists cached repos with sizes for Settings.
- Status before anything else. `GET /api/voice` returns, for STT and TTS:
  `{state: "unsupported" | "runtime_missing" | "absent" | "downloading" |
  "ready" | "loaded" | "error", repo, revision, cache_path, size, error}`. It
  never loads a model or downloads.
- Download only after consent: `POST /api/voice/{stt|tts}/download` runs
  `huggingface_hub.snapshot_download(repo, revision=pinned,
  allow_patterns=[...])` in a worker into the **standard** cache, publishes
  `voice.progress` events (cache blob growth against the expected size, as old
  `speech.py` did) and verifies sizes and hashes from the pinned manifest
  (old `store.py`) before the model becomes `ready`. Loading always uses
  `local_files_only=True` / `HF_HUB_OFFLINE=1`.
- **STT engine (decide in step I1).** Port the old `Engine` protocol (`load`,
  `transcribe(bytes)`, `close`) and the manager unchanged. Two candidates:
  - Port `BarkEngine` as it is. It reads `model.bark` and the five Parakeet
    processor files from the HF cache (`ConwayResearch/Underdog-Bark-3.0-Clean`
    at `6d34ed86…`, `nvidia/parakeet-tdt-0.6b-v3` at `541d1f99…`). Needs torch,
    transformers, constriction and librosa. It was verified loading on `mps`;
    accuracy was never measured. It needs a download on this machine.
  - An MLX Whisper engine on `mlx-community/whisper-large-v3-turbo`, already
    in the cache here. macOS arm64 only, lighter than torch.

  Recommendation: port `BarkEngine` first (copy-first, tests exist), with the
  engine chosen by `[voice] stt_model`, and add MLX Whisper as the second engine
  only if Bark's memory (old estimate about 2.4 GB while loading) is too much.
  Either way the runtime is an optional dependency group (`uv sync --extra
  voice`), not a base dependency (old Nexus made it base, which broke installs
  on unsupported platforms), and status says `runtime_missing` with the
  command to run.
- STT API: `POST /api/voice/transcribe` takes a WAV body (checked by
  `audio.py`), `?partial=1` for live previews (the old pacing: at most one in
  flight, at least 0.4 s of new audio, answered with `voice_busy` while another
  inference runs), and returns `{text}`. Text goes into the composer as
  editable text and is never sent by itself.
- **TTS:** port `speech.py`'s worker with `ptts` (`uv add ptts sounddevice
  numpy`; light, no torch) and `gradium/phonon-31m-en` from the HF cache (it is
  cached here, so this works with no download). `/speak` reads the final answer
  of the latest completed turn, exactly as old Nexus restricted it.
  `POST /api/voice/speak {session_id}` streams `audio/wav` chunks back to the
  client, which plays them (TUI: the `rodio` crate; NSH: Web Audio).
  `POST /api/voice/speak/stop` stops. Old Nexus played on the daemon's speakers,
  which does not work for a remote NSH.
- **TUI capture:** the TUI is Rust only now, so capture moves from the old
  Python `voice_capture.py` to `cpal` in `nexus-tui`: push-to-talk key (old
  `Ctrl+X V` and `/voice`), start and stop cues (port the tones from
  `voice_capture.py:play_cue`), the waveform strip (port
  `rust/tui/src/live_wave.rs`), and live previews. Enter while recording stops
  and sends after the final transcript; Esc keeps the text without sending.
- **NSH capture:** `getUserMedia` → 16 kHz mono PCM16 WAV in the page → the
  same endpoint. A mic button in the composer.
- Settings → Voice and Speech: model state, cache path, size, Download, Remove
  (removes only that repo's snapshot, after confirmation), device for STT.

**Changes:** models come from the HF cache instead of `~/.nexus/models`;
speech plays on the client instead of the daemon; the STT runtime is an
optional extra; capture is in Rust and the browser instead of Python.

**Tests:** `hf_cache.find` against a fake cache tree (env-var precedence,
missing ref, `.incomplete` blob, missing file); status never imports torch or
ptts; download writes into the HF cache and verifies hashes (fake hub); copy the
old engine, manager, audio and bark tests; a fake engine for the API tests.

**Done when:** on this machine `/speak` reads the last answer aloud with the
already-cached phonon model and no download prompt, and dictation in the TUI
puts a transcript into the composer after a consented download (or right away,
if the model is already cached).

### J. Providers

**Old:** `nexus/model/providers/anthropic.py`, `gemini.py`, `ollama.py`,
`nvidia.py`, `openai.py` (API key), `discovery.py`; `nexus/auth/api_key.py`;
`claude_agent.py`, `claude_agent_auth.py`, `_claude_agent_worker.py`,
`antigravity_cli.py`, `nexus/agents/claude_code_worker.py`,
`antigravity_worker.py`. Old docs: `docs/adding-a-provider.md`,
`docs/provider-onboarding.md`, `docs/models.md`,
`docs/antigravity-troubleshooting.md`.

| Item | Dialect in nexus10 | Notes |
| --- | --- | --- |
| J1 API-key sign-in | `llmhub/auth/api_key.py` | `nexus login <provider> --api-key`, stored in `auth.json` |
| J2 openai (API key) | Responses (`responses.py`) | Same quirks table as codex |
| J3 ollama, nvidia | Chat (`chat.py`) | Ollama is local, so no sign-in; models from its `/api/tags` |
| J4 anthropic | New `llmhub/messages.py` | Thinking blocks with signatures kept in `raw` for lossless replay (Principle 2); cache-control breakpoints |
| J5 gemini | New dialect or Chat-compatible endpoint | Decide by checking which keeps reasoning replayable |
| J6 claude-agent, antigravity | External CLI workers | Last. They run their own loop, so their timeline is a child-like stream; check against the one-timeline rule before porting |

Each provider lands with `docs/llm-providers.md` and `docs/auth.md`, a
recorded wire fixture test, and a verified real call.

for antigravity lets do it like : https://github.com/NoeFabris/opencode-antigravity-auth instead of ZMX 
for claude: lets used AGENT SDK

for things that support openai format, like ollama and nvidia and groq, we can group them 

### K. Context and sessions

The old Step 12a items, with sources.

| Item | Old | New |
| --- | --- | --- |
| K1 handoff instead of compaction | `nexus/context/compact.py`, `handoff.py`, `handoff_model.py`, `nexus/session/handoff.py`, `host_support/session_handoff.py`, old `docs/context.md` "Same-session handoff" | At `[context] handoff_percent` (default 80) or on `/handoff`, the agent writes a visible summary turn, and a new root session of the same agent starts from it with `continued_from` and a link back. Never edits a sent context |
| K2 auto titles | `host_support/auto_title.py`, `session/title.py` | A one-turn session on a cheap model after the first prompt, hidden from the list, inspectable. Replaces the first-60-characters title (`runtime.py` ~1385) |
| K3 `/btw` | none (new from the archived INTERFACE_PLAN) | One call with the session's context up to its last completed tool call plus the question, no tools, answer in an overlay; the parent's context does not change |
| K4 session drafts | `host_support/context_preview.py`, `session_preview.py` | Core builds the real starting context before the first prompt (replaces the TUI's client-side estimate) |
| K5 search | `ui_support/fuzzy.py` (titles only) | FTS5 over titles and user messages; also serves C |
| K6 paging | — | Message paging and load-older-turns; the snapshot caps at 200 messages today. Pairs with TURN_SUMMARY `before=` |
| K7 approvals | `host_support/approval.py`, `ui/ratatui/permission_prompt.py` | "Allow for session" and "deny with a note" |
| K8 artifacts | `host_support/attachments.py` | Durable artifact storage, needed for MCP and web images |
| K9 cache warning | old `docs/context.md` "Prompt-cache boundaries" | Warn before an agent or model switch (Principle 8) |
| K10 file writes | — | `PUT` file with `expected_revision`, `files.changed`, then the built-in editor |
| K11 usage totals | `host_support/provider_usage.py`, `usage_snapshot.py`, `ui_support/usage.py` | Per-session totals come from `session_usage` (turn summaries). Provider quota windows (codex and copilot limits) where the provider reports them |

K1 "/handoff" or "/compact" the same things. session that start from previous session's compaction / handoff, should show that in the beginning - there should be a user message like box (up to 3 lines) (click to show to more in a modal) that shows the full compaction. 
there should be good instruction for the compaction. 
compaction is done by two methods - either automatic after reaching the percent (80) but only after the turn is over OR by user by "/handoff" or "/compact" 

K2 and K3 - technically these are grouped in one - where we only make one request to the model. for K2 is with custom instruction that send the first user message, asking for a Title - Camel Case - only few words. K3 we instruct not to call tools - we don't run the tools even if model call by mistake, we ask to only reply given all the context it has. 

### L. Worktree review and integration

**Old:** `nexus/agents/worktree_review.py`, `worktree_integrate.py`,
`nexus/worktree_cleanup.py`, `ui/ratatui/worktree_pages.py`,
`worktree_bar.py`, test `test_worktree_merge_permission.py`.

**New:** control commands (Principle 3) in `worktrees.py`: review a worktree's
diff against its base, merge it (fast-forward or merge commit, with
confirmation), and remove the worktree and branch after a merge. User commands
in both clients; the agent gets them only when granted. Closes capability row A9.

### M. Interface

**M1 TUI open items** (from the archived PLAN.md Step 12 and DESIGN_PLAN):
- `path:line` and OSC 8 links (needs hit regions; ratatui cells carry no
  escapes)
- syntax highlighting in the file view (library undecided)
- split-diff wrap bug: continuation rows start at the `+`/`-` column instead of
  the text column (`nexus-tui/src/diff.rs`)
- edit and patch diffs have no line numbers when the tool input has none
- PTY tests at 80×24 and 160×48 for anchor, follow and jump-to-latest
- a review gallery from fixed fixtures (`interface/review.md`)
- Session info: failed child count, enabled and disabled tools, kernels
- terminal notifications: check OSC 777 in kitty (it may need OSC 99) and
  track focus
- debounce completion requests if large workspaces make search slow
- compare with the old TUI in a real terminal; the user reviews screenshots

**M2 NSH parity:** the NSH side of DESIGN_PLAN I9 (new-session view) and I10
(files, narrow layouts, states); search row in the sidebar (needs a
`session.search` handler); per-row branch (needs branch data on the session
row); thought durations, message tags and the live shimmer (thought durations
come from turn summaries); pin NSH; build the macOS desktop bundle; files,
Git and worktree UI; then delete `nexus-design-mockup/`.

**M3 parity, performance, accessibility (old 12.6):** side-by-side TUI and NSH
review in light and dark; budgets for large sessions (with turn summaries step
5's numbers); keyboard-only paths.

**M4 Mermaid:** the TUI already renders diagrams as text
(`nexus-tui/src/mermaid.rs`, `mermaid_text.rs`, ported from old
`rust/tui/src/mermaid*.rs`; see the ledger). Still missing: image rendering in
kitty and iTerm (old `mermaid_image.rs`), and Mermaid in NSH with a pinned
`mermaid` bundle served by the host (no CDN, per its CSP).

M5: Subagents should not open in a new tab beside the parent agent. instead add a row below the tabs and show the name of the subagent session (child session title). 
also in the parent/root agent/session we should make the subagent render better. (remove the word "child") show icon (spinner animation for subagent working - spinner all dots on for completed) then name of the agent ("Build", "Scout" etc) - then name of that session. and then "Background" (highlighed in darkgray) if it was done in background. 
in the next line show provider/model/effort - time taken ( changing when subagent is working and time taken when done) - tokens (only after its done)

When the background subagent gets back, it should rendered well. 

### N. Operations

**Old:** `nexus/host_support/doctor.py`, `nexus/host/doctor.py`,
`host_support/install.py`, `setup.py`, `uninstall.py`, `update_check.py`,
`install.sh`, `install.ps1`, old `docs/release.md`.

- N1 `nexus doctor`: daemon reachable and running the current code (closes the
  "nothing checks that a running daemon runs the current code" gap), sign-ins,
  catalog age, config errors, search container, MCP health, voice runtime and
  HF cache state, `rg` present.
- N2 install and uninstall: `install.sh` that installs the uv tool, the TUI
  binary and the NSH bundle.
- N3 update check: compare with the latest release, never auto-update.

### O. Release gate (before 0.10.0)

- O1 run kernels in a scrubbed helper process instead of inside the daemon
- O2 back up before migrations, and recover from a failed migration
- O3 move the home: on first start of 0.10.0, `$NEXUS10_HOME` defaults to
  `~/.nexus`, with a one-time, confirmed migration of `~/.nexus10` and the old
  `~/.nexus` moved aside, never deleted
- O4 verify `nexus login copilot|grok|opencode` end to end, and add login tests
- O5 define the platform matrix (macOS and Linux) and run it
- O6 pin `nexus-search` and browser-use images and versions

## 7. Order

The user's three items lead. Each line is one reviewed increment that ends
green and visible in the real app.

1. **Turn summaries 3–6** (TUI and NSH done in the working tree,
   TURN_SUMMARY_PLAN.md): clients read `/turn`. Land this before B's TUI work,
   since both change `reduce.rs`.
2. **A1 — done** config file and Settings over the API (2026-10-29).
3. **A2 — done** mock provider (2026-10-29).
4. **B** steer and queue (Core, then TUI, then NSH).
5. **C** prompt history (Core endpoint without FTS first; K5 speeds it up later).
6. **E** agent hot-reload checks, then **F** skills.
7. **G** MCP, then browser-use as the default server (O6 pins it).
8. **I** voice: I1 HF cache reader and status; I2 TTS (model already cached);
   I3 STT engine and Core endpoint; I4 TUI capture; I5 NSH capture.
9. **D** tools: D1–D4, then D7 permissions, then D5–D6 with `nexus-search`,
   then D8.
10. **E** user tools with quarantine; **H** hooks.
11. **A3 — done** counting (2026-10-29), then **K1** handoff, **K2** titles, **K4** drafts, **K5**
    search, **K6** paging, then the rest of K.
12. Turn summaries 7 and 9 (live partial, ephemeral deltas), after B, so steer
    delivery is tested against the new delta path.
13. **J** providers, **L** worktrees, **M** interface, **N** operations.
14. **O** release gate.

## 8. Interactions with turn summaries

- **Steered input adds a step to the running turn.** The turn's `steps`,
  requests and usage include it, and `turn.summary` lists it like any other
  step. A queued input starts a new turn with its own summary.
- **`input.*` events are durable** and are not deltas, so TURN_SUMMARY step 9
  (ephemeral deltas) does not touch them. Pending inputs are a state table, as
  that plan's rule requires: clients read `/input`, not the event log.
- **Message anchors:** a delivered steer is a user message inside a turn
  (after `start_seq`), so clients must not treat every user message as a turn
  start. Update the TUI's turn grouping when B lands.
- **Recovery:** B's "undelivered steer becomes queued" runs in `recovery.py`
  next to TURN_SUMMARY step 8's `turn.ended` logic.
- **Context meter (A3)** reads `turn_request.input_tokens` from summaries
  instead of scanning `usage` events.
- **Usage totals (K11)** are the `session_usage` ledger.

### Turn summaries: TUI load measurement (steps 0 and 5)

One seeded session in a throwaway home (never `~/.nexus10`): 40 turns, 160
messages, 30 616 events (11 600 `text_delta`, 3 200 `reasoning_delta`,
14 800 `wire_event`, 84 `wire_request` carrying the conversation, 80 `usage`,
tool, retry and lifecycle events; every tenth turn retried, every seventh tool
failed), with the same turns in the state tables. Event `data` is 4.56 MB;
`nexus.db` is 8.1 MB. Commands, from `nexus-tui/`:

```sh
cargo build --release
tools/bench.sh <scratch>/bench 40 3     # seeds <scratch>/bench/home once (tools/bench_seed.py),
                                        # starts its Core, runs nexus-tui --bench 160x48 three times
```

`--bench` opens the most recent session and reports the time until its stored
history is applied, the time to the first full frame (history plus one
`render_buffer`), RSS from `ps` after that frame, and `render_buffer` time over
400 frames while scrolling a half page per frame (200 up, 200 down). Frame time
is building and painting the frame into a buffer, not terminal output. Warm
runs (the first run after seeding is cold: 690 ms before):

| | Before (event history, 20 000-event cap) | After (turn summaries) |
| --- | --- | --- |
| Open to history applied | 230–250 ms | 68–83 ms |
| Open to first full frame | 237–258 ms | 73–88 ms |
| RSS after the first frame | 133 MB (7.7 MB before open) | 19 MB (7.8 MB before open) |
| Frame time while scrolling, mean / p95 | 4.4–4.7 / 4.6–5.4 ms | 3.6–3.7 / 3.8 ms |
| Bytes read on open (event pages / `/turn`) | 7.97 MB of events + 139 KB messages | 59 KB of summaries + 139 KB messages |

Before, the session also lost 10 617 events to the cap (`… older diagnostics
are not loaded`), so its sidebar counted 5.2M input tokens; the ledger counts
all 5.9M.

## 9. Not in this plan

- GPT Live voice calls (`nexus/live/`, `rust/tui/src/live_*.rs`)
- the device gateway and the iOS app (`nexus/gateway/`, `ios/`)
- the workspace manager session (`nexus/tools/manager_*.py`)
- model tiers and routing (`nexus/model/tiers.py`, `router.py`, `usage_gate.py`)
- per-project config files (P1)
- Python hooks (H; revisit after E)
- the benchmark harness (`benchmark/`), until A2 is in use

## 10. Ledger: changes from old Nexus

Rows cite archived plan tags (`DESIGN_PLAN D06`, `INTERFACE_PLAN`); read them
with `git show fdb1860:<file>`. Rows for the decisions in section 5 are added
when each item lands.

| Old | New | Why |
| --- | --- | --- |
| Old host folded the event log into turn views; nexus10's TUI paged up to 20 000 events on open and scanned them every frame | The TUI reads `/api/session/{id}/turn` summaries and the session ledger, never the event log; live events of the running turn are scratch until its `turn.summary` (2026-10-29) | Bounded load time and memory; details survive event retention; one source of truth (TURN_SUMMARY_PLAN.md) |
| Clipboard images uploaded through the old host's attachment store | Local TUI clipboard reads and NSH paste events feed inline `Image` parts, with draft/timeline previews; bytes are durable in SQLite and replay in both dialects (2026-10-28) | Deliver image paste on the current API while keeping all model-visible image context available after reload |
| Background bash completions began `[Notification: background bash …]` | Completion context begins `→ Background Shell Done: …` and renders as a plain notice in both clients (2026-10-28) | Distinguish shell completion context from user-message cards |
| New sessions defaulted to asking for tool approval | Sessions default to `allow`; existing `ask` sessions migrate once, and later explicit `ask` choices are retained (2026-10-28) | User preference: always allow tools by default |
| Selected items used a brown band; the rail included three tab buttons and widely spaced initials | Selection uses the normal surface with a marker and stronger text in both clients; compact session-only rails, roomier composer/context/timeline spacing, no command hint, and Project/Global title search (2026-10-28) | User screenshot feedback: remove patchy fills and give the conversation room while keeping session navigation compact |
| Old Markdown replaces supported Mermaid source with a rendered diagram | Adapted diagram renderers show the diagram followed by its original fenced source; each fence has its own copy action | Preserve every model-visible item while retaining the old diagram presentation |
| Old Settings builds a modal from its host snapshot and widget kit | Settings is the old centred modal over the dimmed session: one searchable list with section headings, values right-aligned, ←/→ to change, rows from Nexus 0.10's manifest (2026-10-28) | Familiar layout on the new Core and client contracts |
| Old vertical tabs: a session rail with initials and hover details | The left sidebar resizes by its edge and collapses to that rail; Sessions/Files/Projects are icons inside the sidebar (a column on the rail) with labels on hover (2026-10-28) | Old look, without a second tab model beside the sidebar tabs |
| Old TUI composer owns the grapheme editor | The copied editor backs the composer, while its public interface and Core send path remain in Nexus 0.10 | Reuse selection, undo and paste behavior without changing the Core-facing input contract |
| Old TUI shimmer uses RGB theme colours and a host-side activity clock | The adapted TUI shimmer resolves the shared theme tiers and uses the client frame clock, with reduced-motion and monochrome fallbacks | Preserve the old motion while keeping Core-independent rendering and terminal accessibility |
| Worktree is a tool, effectively mandatory | Worktree is a session property set from the control plane | Where work happens is not a task step |
| Task tools and Nexus-state tools mixed in one list | Separate task tools and `nexus` control commands | Different permissions, events and UI |
| Normalized IR only; provider details reconstructed | Each part keeps its `raw` wire item and `origin` | Lossless replay; nothing hidden |
| `~/.nexus` | `~/.nexus10` (until 0.10.0) | Old Nexus keeps working from its checkout |
| `nexus auth codex login` | `nexus login codex` | Sign-in is per provider, one verb |
| Provider ids `github-copilot`, `opencode-go`, `xai` | `copilot`, `opencode`, `grok` | Short names users type |
| Provider subclasses and host-detection branches | Two dialect modules + a quirks table | One place per difference |
| Many vendors (Anthropic, Gemini, NVIDIA, Ollama, agy, Claude agent…) | Only codex, grok, copilot, opencode | Subscription sign-ins only, for now |
| Custom host protocol (commands over a socket) | OpenCode-style HTTP `/api/*` + one SSE stream | Any language can be a client; curl works |
| Workspace is the unit | Project (a path) owns sessions; a session has its own `directory` | Room for worktrees per session |
| `auth/` beside the model layer | `llmhub/auth/` | Sign-in exists only to serve providers |
| Agent definitions mixed into config and code | One Markdown file per agent (frontmatter + prompt) | Readable, editable, per project |
| One general agent that also spawns helpers | `orchestrate` (delegates) vs `build` (does the work); children are `build`, `quick`, `scout`, `plan` | Clear roles; the orchestrator's context stays clean |
| Subagents run inside a tool call | Child sessions with `launched_by`, `parent_id`, `parent_call_id` | A child's work is a full, visible, resumable session |
| A parent could only launch a child and wait for it | The first `session` call exposes `message_session` (once, persisted): a steer for a working child, a follow-up turn for an idle one; the answer returns as a notification (2026-10-28) | Delegation needs steering and follow-ups, not only fire-and-collect |
| Four kernel tools (`code_start`, `code_attach`, `code_run`, `code_reset`), one kernel per agent | One `code` tool; kernels owned by the daemon, shared by id across sessions | Simpler to use; a session tree works in one kernel |
| Cancelling a cell could restore a signed checkpoint | Forced cancellation discards the kernel in v1 | Monty closes the checkout; checkpoint recovery is deferred |
| bash was synchronous with a fixed timeout | No default time limit (optional `timeout`), plus daemon-owned background jobs read with `bash_output`/`bash_kill` | A long command need not block a turn; its result arrives as a notice |
| Live events only; messages in the log | Every session event stored (`event` table) | The inspector can show any past session down to the wire |
| Agent prompts name their tools in prose | `{{tools}}` placeholder filled from the session's actual tools (`Tool.guidance`) | A switched-off tool is never described to the model |
| Fixed list of child agents in the orchestrator's prompt | The `session` guidance and enum are snapshotted from freshly checked agent files at each new session | New sessions pick up agent changes without rewriting existing prompts |
| Agent files read again on every turn | Resolved once into a stored `session_snapshot`; later turns replay it | A cached prefix stays stable; edits cannot silently change an existing session |
| No session instruction file | Exactly `<session.directory>/AGENTS.md`, labelled in the starting context with explicit absence/error/size status | Project instructions are visible to the model and inspector, without ancestor discovery |
| Old sessions have no original prompt record | Lazy reconstruction marked `reconstructed`, with a visible note and a one-time `session.snapshot` event | Honest migration; stable afterwards |
| Worktree tool, effectively mandatory | On-demand workspace controls: activate change-session-directory and worktree list/create/delete when needed (done 2026-10-27) | Keep ordinary sessions small and separate task work from Nexus state |
| A session always ran in its project path | A session can move to any existing directory; the project identity is unchanged and children inherit the move (done 2026-10-27) | Worktrees and sibling checkouts without pretending they are new projects |
| A directory change rewrote context | The move is announced forward, the destination `AGENTS.md` is loaded then, and earlier prompts/messages stay byte-for-byte (done 2026-10-27) | A cached prefix stays stable |
| Worktrees were created and removed by hand or without checks | Git argument arrays, real worktree metadata, dirty/main/in-use refusals and no branch deletion (done 2026-10-27) | Never destroy a checkout, a session's directory or an unrelated directory |
| Sessions had no archive state | `archived` on every session of a root tree; one atomic idempotent transition, default/only/include list filters, and notices that do not wake an archived tree (done 2026-10-27) | Hide finished work without deleting it, stopping jobs or losing context |
| Session history was inspectable but not portable | `GET /api/session/{id}/export` (Markdown transcript or versioned JSON with snapshots, messages and the retained event log), scoped to one session or its descendants, read from one SQLite read boundary (done 2026-10-27) | Take the context out without a provider call, a mutation or a silent truncation |
| The SSE `id:` was the per-daemon `seq`, so every restart invalidated clients | Durable event row ids with a `log` generation and a `<generation>:<id>` cursor; `seq` is only a live counter (done 2026-10-27) | A resume cursor must survive restart and reject a replaced database |
| Only session events were stored; project changes and deletions were live-only | Global lifecycle events and minimal `session.deleted` tombstones are stored and replayed; a session's own history still cascades (done 2026-10-27) | A reconnect must be able to rebuild project and session state |
| A reconnecting client started a fresh live stream and had to catch up out of band | Atomic replay-to-live handoff: subscription and head boundary first, paged replay, overlap skipped, explicit resync codes and overflow (done 2026-10-27) | No lost or duplicated events across the connection boundary |
| A crash left the last in-memory turn permanently busy, with no durable record of what ran | `turn`/`turn_request`/`turn_tool` records; startup recovery marks the turn interrupted, leaves the session idle and reconciles only durable evidence (done 2026-10-27) | Busy state was never durable, so recovery needs its own evidence |
| An interrupted provider stream could be mistaken for a finished reply | Partial output stays diagnostic in the event log; only a response that durably reached `Done` is restored, and a recovery notice labels the rest (done 2026-10-27) | Never present partial text, arguments or reasoning as complete |
| A crashed daemon silently dropped pending approvals and questions | Recovery publishes explicit `permission.replied`/`question.replied` cancellations; late ids cannot resume work (done 2026-10-27) | A vanished wait is not an answer |
| Continuing meant sending a new message ("continue"), which appended model-visible text | An explicit `POST …/continue`, `/continue` and `nexus session continue ID` send the stored context unchanged, reconcile dangling calls from durable evidence, and record continuation provenance (done 2026-10-27) | The model receives exactly the context the user sees, with no invented user turn |
| Compaction rewrites the session's history | A handoff to a new root session linked by `continued_from` (planned, Step 12a) | What the user sees stays what the model saw |
| System prompt rendered per turn | Rendered once at session creation and replayed unchanged | A stable cached prefix |
| Workspace edits require approval unless allowed | In-directory `edit`/`write` (patches and nested calls too) are auto-approved with no setting; bash approval is unchanged (done 2026-10-27) | Editing authorization and command execution are separate |
| Read before edit was prompt advice | Per-session observations persist hash, ranges and clipping; local changes need affected-line coverage and whole-file operations need complete same-revision coverage (done 2026-10-27) | Stale or unseen content cannot be changed through the file tools |
| No mutation serialization for files | One per-daemon lock, revision recheck before write, whole-patch validation before any write (done 2026-10-27) | Nexus writers cannot interleave a check and a write |
| Bash can write files with no tool checks | Documented enforcement boundary; full sandbox/restricted execution remains open (PLAN.md, Known gaps) | Do not claim a filesystem invariant the tool contract cannot guarantee |
| Web tools named `websearch` / `webfetch`, deployment in `websearch/` | `web_search` / `web_fetch`, deployment in `nexus-search/` (planned, Step 11a) | Explicit names and self-contained service setup |
| Extension system and MCP integrations in old Core | Focused skills/MCP support and upstream browser-use default in `nexus-browser/` (planned, Step 11a) | Carry over needed capabilities deliberately |
| A lost mutation response was ambiguous and a retry could create a second turn | `Idempotency-Key` mutation journal; prompt/continue acceptance and turn are one transaction; a replay returns the original result and schedules nothing (done 2026-10-27) | Clients can resolve a lost response without duplicating work |
| Competing updates silently overwrote each other | Monotonic project/session `revision`, `If-Match`/`expected_revision`, and 409 `revision_conflict` before any change (done 2026-10-27) | Conflicts are deterministic and visible |
| A transient provider failure ended the turn immediately | Bounded, visible pre-stream retries with attempt rows and events; no tool/partial replay (done 2026-10-27) | Survive transient faults without hiding attempts |
| A reconnecting client had to catch up out of band | `/api/snapshot` watermark plus `Client.watch` reconnect/deduplicate/resync; CLI cursor advances only after applying (done 2026-10-27) | Rebuilding state is consistent and cannot resubmit a prompt |
| The event log grew forever and the floor stayed 0 | Prefix retention with a durable head high-water mark, recovery/replay pins and an explicit `history_unavailable` boundary (done 2026-10-27) | Storage is bounded without losing recovery evidence |
| Separate TUI and NSH designs | One interface spec for both (INTERFACE_PLAN.md) | Same features, same names |
| Old host `search_files` over the custom protocol, with read-deny roots from host policy | Core `GET /api/project/{id}/files/search` (`q`, `limit`, `directories`, `session`): the same bounded, Git-aware, ranked walk, with no read-deny roots (done 2026-10-28) | Clients complete `@` paths over HTTP. nexus10 has no read-deny policy, so the search takes no deny roots and reads only the selected root |
| Old composer strip `name branch path worktree`, with `main checkout` for a main checkout | Same order, but a main checkout says nothing; `worktree` (violet) appears only in a linked worktree. The panel ends half a row below the control row (2026-10-28) | Show only what is unusual; match the old look |
| Old counts `1.2K` | Counts are `1.2K`/`3.4M` everywhere in both clients (2026-10-28) | One format, as the old composer showed it |
| Old context header: headings with token figures, previews and a total | Same, as a rail list with a right-aligned token column, detail rows and a window share; estimates are characters ÷ 4 and marked `~` (2026-10-28) | The context is visible and costed before the first request |
| Turn footer without price | Footer adds cost from the catalog's prices when the model has them (2026-10-28) | Spec: every turn shows what it cost |
| Output clipped to the first 200 lines, "show all" did nothing | A tool's detail is the old box (`┌ │ ╰`): the command's first 2 rows and the last 8 output rows with `↑ N earlier lines · enter show all` between (diffs and the raw view keep a head and a tail); `o` or Enter on the open tool toggles the whole output; `r`, `o`, `i` work on a focused tool (2026-10-28, restyled by DESIGN_PLAN D06) | Clipping is announced and reversible; the model's own clip stays separate |
| Projects were added with the CLI only | `+ Add project` and a confirmed remove in the Projects tab; right-click or `m` opens a row menu of the manifest's `row_menu` commands (2026-10-28) | Core already serves `POST`/`DELETE /api/project` |
| Turn footer was a rule plus a left-aligned row; context items were a rail list | Footer is right-aligned `▪ Build · model · …` with no rule; context items are chip headings with indented details, as in old Nexus (2026-10-28). The footer carries no end-status word or glyph: an interrupted turn adds `/continue` (`enter continue` with an empty composer), a failed turn puts its first error line on the row above (DESIGN_PLAN D05) | The user asked for the old look, then for no end status |
| Old user card: padding rows and a left gutter; old sticky user card | The user message is a full-page-width band on the raised surface (rail `▌` at the edge, fold marker inside, `#n · time` at the right); the sticky header from old Nexus `sticky.rs` is kept: at most 3 rows, a `░` shade row with `↑ N lines of this turn above`, pushed up by the next band, click to scroll back (DESIGN_PLAN D04) | The user chose the band; the pin keeps the question in view |
| Activity groups: `<1 s`, `Ran 1 command, Read 1 file`, groups without verb glyphs | Summaries in first-seen order (`Read 2 files, ran 1 command`) with `+N −M` and `✕ N failed`; live `Working` with the newest 5 members and `… N earlier`; verb glyphs `→ ← $ * λ ? ↳ ↗` (`[glyphs.verb]` in theme.toml); only shell tool rows show individual durations, while group and Thought rows omit them (DESIGN_PLAN D06, D07) | Old Nexus's groups, polished |
| Header showed the title and breadcrumbs | Session tabs as in old Nexus: opened sessions with status glyph, `×`, `+`, `alt+x`/`alt+[`/`alt+]`; clicking the viewed tab opens a rename modal, with Enter to save and Esc to cancel (2026-10-28) | The user asked for tabs |
| Composer had a half-row edge and `used · tier% · window%` on the control row | Soft card with half-row edges, no rail or status band, the old `╱` meter and `82.0K · 41%` on the control row, `ctrl+p commands` on the project strip, completion as wide as the card (2026-10-28, DESIGN_PLAN D11, user choice; replaces the rail and status band of `5409281`) | The user chose the soft card over the rail |
| Activity groups were collapsed or live-open only | Three detail levels (low/medium/high, `alt+d`, Settings, persisted), medium by default (2026-10-28) | Spec: scan, then drill down |
| Background shell completion context appeared as full multiline notices; Working used an agent-colour shimmer and showed output tokens | TUI groups consecutive completions into a compact expandable notice, retains the full context on expansion, and uses a grey Working shimmer with an aligned elapsed-time row and no output-token count | Keep the timeline scannable without losing model context |
| Agent/model/effort pickers did nothing before a session existed | They set a draft sent with the session's creation request (2026-10-28) | `/model` and `/agent` must work on the new-session screen |
| Composer completion was host-driven (the host ranked `/` commands and searched `@` files) | The TUI ranks the manifest's slash commands locally (prefix, subsequence, typo, then the rest; always ten rows) and asks Core's `GET /api/project/{id}/files/search` for `@` paths, padded from an empty-query listing; replies are cached per token and a reply for an old token is dropped | Completion needs only the Core HTTP API, and the popup keeps its height while typing |
| Old modal backdrop: cells under a modal repainted in muted text; boxed panels with titles in the border | The TUI blends every cell of the frame 50% toward `surface.canvas` (`paint::scrim`; `DIM` where colours cannot be blended) and draws one borderless `surface.raised` block (2 cells of padding left and right, 1 row top and bottom, title and `esc` on the first row, key hints on the last, within 2 cells of the frame edge and at most 70% of its height) for the palette, pickers, Settings, confirms, help and notifications; Settings has UPPERCASE muted headings and the selection band instead of the accent fill, and `[ Cancel ]` is the selected default (DESIGN_PLAN D13, I7) | One modal recipe in both clients; the surface step and the scrim separate the modal from the session |
| Waiting card was a bracketed button row (`[ allow once ] [ deny ]`) with a 6-row card; toasts were bottom-right, one line, the oldest dropped past three; a toast kept only its text and a click dismissed it; the jump pill drew on the timeline's last row | The card has a heavy accent rail, `Allow bash?`, `waiting on you · 0:42`, the parameter table, the diff capped at 12 rows, and a key-chip choice list (`▸` on the selection band, Up/Down and Enter act on the offered choices; unoffered choices show their reason) or the question rows; it sheds diff, blanks, pads and table rows when the screen is short. Toasts are top-right under the header (36-48 cells, level `▌`, glyph, bold title, `×`, an optional body line, no hairline), three drawn with `+N more` for a queue of up to nine; a click or Enter opens the full text in the modal (`c` copies; the notifications drawer opens it too, Enter there no longer jumps to the session, the detail's Enter does). The jump pill is on the composer's live row, right-aligned with the card, `↓ N new · end` where N counts the blocks below the viewport; banner leads in the level colour (DESIGN_PLAN D14, D15, I8) | The prompt keeps the context visible and the card shows what will run; alerts stop covering the composer area and the timeline's last row |
| The starting context was a list of name chips (`System prompt`, `Tools [n]`, `Instructions`, `Agent`) with detail rows and a `total` row; the new-session view was centred text over a `Context · preview` table | `◆` sections with an estimated `~N tok` per header, one preview line or a tools grid (off tools struck through), a text `Context total` and no bar, rendered by one function (`paint::context_rows`) for the timeline and the new-session view, which has a header block and no centred text; in the TUI the context stays expanded after the first turn; the Agent item is gone (its name is in the System prompt meta) and the new-session preview is client-side and estimated until Core session drafts land; every preview line is text the model receives (the first line of the agent's prompt body, fetched with `GET /api/agent/{name}`; never the description), and the timeline's comes from Core's `system_prompt` (DESIGN_PLAN D10, I9) | One timeline that shows what the model gets, with the same look before and after the first turn |
| Files tree: an optional `▾ name ⌥ branch` row only with several checkouts, plain indent, no guides; file view: a bold `~` path, `NNNN  text` rows, a notice `lines 1-40 of 300 · scroll` at the bottom, search as `/query  2 matches`, all on the sidebar surface; narrow layouts (< 100 columns) kept the tab strip with 28 / 30-cell drawers and no scrim; the right sidebar was open from 100 columns; the empty and connecting screens were centred text; offline read `reconnecting in 4 s` and `Disconnected · read-only` | Always a root header (`SIDE-PROJECT` bold, `⌥ main` muted at the right; it switches checkouts when there are several), `│` guides in `border.default` per level, Git letters coloured by status on the row's own surface; the file view is on the page surface with the path in secondary and the basename bold, `lines 1-38 of 38` right-aligned in the header, a blank row, right-aligned numbers and a `│` gutter, and a reserved bottom row for search (`/ query  3 of 12   n next · N prev · esc end`, matches on the selection band, the current one reversed); `layout::Breakpoint` gives the D17 table (160+ 34 / 36, 120-159 28 / 30, 100-119 the left docked and the right closed until opened, < 100 drawers of 34 / 36 cells over `paint::scrim`), where the header keeps the toggles, the status and a crumb cut from the left; `paint::LOGO` (the `_/` N, accent, bold in mono) over the title on the empty workspace (`No projects yet`, `alt+3 add a project · ctrl+p commands`; `ctrl+o` has no command yet) and the connecting screen; a lost stream shows `✕ offline` in the header, the banner `Disconnected from Core · retrying in 4s · retry` and the composer placeholder `Reconnecting to Core… your draft is kept`; with nothing loaded the page says `Not connected to Core` instead of `No projects yet` (DESIGN_PLAN D16, D17, D18, I10) | Files read as a document with its place and range; narrow terminals keep the page readable; a missing Core is said in words and never looks like an empty workspace |
| Turn details (usage, model, timings, retries) lived only in the event log and vanished with it after 14 days | Stored on `turn`, `turn_request` and `turn_attempt` rows and a per-session `session_usage` ledger, served by `GET /api/session/{id}/turn` and a `turn.summary` event after every close; requests from before the upgrade backfilled once where unambiguous, unknown otherwise (2026-10-29, TURN_SUMMARY_PLAN.md steps 1–2) | Details survive event retention; session totals survive turn pruning |
| A retried attempt's usage was only in the event log | Every attempt's usage is stored and counted in the request and the ledger (2026-10-29) | A failed attempt is still billed |
| Recovery searched the event log for `turn_end` | Recovery reads `turn.ended`, falling back to the log only for a turn left open by an older daemon (2026-10-29, TURN_SUMMARY_PLAN.md step 8) | State tables are the source of truth |

| Layered old config and read-only defaults | One `config.toml`, editable HTTP-backed Settings, revision conflicts and forward defaults for new sessions (A1, 2026-10-29) | Shared settings without rewriting existing context |
| Old scripted provider coupled to the old model protocol | Dev-gated `mock` llmhub provider and eleven offline scenarios (A2, 2026-10-29) | Exercise the real loop without network |
| Client-only context estimate | Core context budget uses newest matching-model request usage, with explicit estimated fallback and nullable window (A3, 2026-10-29) | Consistent meters across clients |
| Old grep/glob/ls/multiedit ran through the old `ToolSpec` with read-deny roots and a mandatory grep subprocess | `tools/grep.py`, `glob.py`, `ls.py`, `multiedit.py` on the nexus10 `Tool`: `rg` when present (killable Python scan otherwise), Git-aware fresh walk, workspace-relative paths, no read observations from searches, whole-file observation for `multiedit` (D1–D4, 2026-10-29) | Same bounds and announced clipping without read-deny roots; a search is not a read |
| Old websearch/webfetch ran on a 1200-line outbound service with allowlisted origins, an HTML fallback for search and per-origin approval keys | `web_search`/`web_fetch` on `nexus/net.py`: public-address-only, pinned connection, same-origin redirects; search is JSON from `[web] search_url`; content fenced as untrusted; no origin allowlist (D5–D6, 2026-10-29) | The same SSRF and prompt-injection protections in one small module; per-pattern rules come with D7 |
| Old `permissions.py` (2400 lines: bundles, session grants, path guards, unattended modes, `deny → grants → allow → ask → mode`) | `nexus/permissions.py`: `tool` or `tool(glob)` rules with allow/ask/deny; any deny wins, else the last config rule decides, agent files can only add ask/deny; keys per tool (command, resolved path, URL, query); bash compounds never match an allow; one table in `config.toml`, no per-tool sub-tables; a bad rule is skipped and reported, never dropping the good ones (D7, 2026-10-29) | Same safety properties in a page; session grants are K7; path guards already live in the tools |
| Old skills: a strict six-key frontmatter grammar, generations, tiers, bundles, bundled-tool candidates and a body snapshot taken at refresh; `skill` always present | `nexus/skills/`: lenient frontmatter (unknown keys are warnings), `<project>/.agents/skills` over `~/.agents/skills`, the index frozen in the session's prompt and the body read at call time; `skill` only in sessions that started with a skill; a skill added or changed later is not injected (new sessions only, clients notify); header switches inject a JSON notice; `allowed-tools` parsed, not enforced (F, 2026-10-29) | Public skills load; the prompt stays append-only (P6); no tool is added to sessions that cannot use it |
| Old `npx skills` import (`skills_install.py`, runs third-party code behind a confirmation) | `/learn`: a shallow `git clone` or local folder, every `SKILL.md` found, a bounded plain copy to the project or global folder; nothing run, so the only question is the scope (F, 2026-10-29) | No npx or Node needed; installing is copying files |
| Old MCP: a 2500-line hand-written client, `mcp.json` in two scopes, per-server `tool_loading` (direct `mcp__server__tool` tools or `McpSearch`/`McpCall`) | `nexus/mcp/` on the `mcp` SDK, servers in `config.toml`, two global modes (`[mcp] full_definitions`, `max_listed_tools`) applied when a session starts, `mcp_tool_search` and `mcp_call` only (a search returns the definition JSON), a session waits for its servers to be online, results fenced as untrusted, `mcp:<server>` switches with JSON notices, pinned browser-use offered disabled; resources and prompts not yet (G, 2026-10-29) | One config file (P1); no cache-breaking tool changes (Principle 8); a small client we do not have to maintain |
| Old `escalate` was a registered task tool whose escalations the old runtime folded into a `SubagentOutcome.attention` | `escalate` is a control tool added to every child snapshot (`escalation: true`), recorded in memory, announced by `session.escalated`, and appended as an `Attention:` block to the parent's `session` result or notification; it grants nothing and does not wait (D8, 2026-10-29) | Principle 3: it acts on Nexus state, so it is control; the parent's timeline shows it |
| Old `ext/manager.py` (3000 lines): loaded tool modules in the host, a `[[ext]]` config table, trash and templates | `nexus/extensions.py`: tools are `*.py` files in `tools/` folders, read as literals and run in a child process per call; the session snapshot stores their source; no config table (E, 2026-10-29) | The daemon never executes user code; a running session keeps its code (P6, Principle 8) |
| Old hooks: Python and command hooks from layered `hooks.toml`, a 1600-line manager, `ContextAssembled`/`PreCompact` events | `nexus/hooks.py`: command hooks only, `[[hooks.<Event>]]` in `config.toml`, six events, the old limits and scrubbed environment, `hook_fired` events; only what a hook adds to context is a message, labelled `[hook name]` (H, 2026-10-29) | One config file (P1); no user Python in the daemon; the timeline shows what the model got |
| Client composer history in memory (TUI only) or one global file for all projects (old) | Core stores what the user sent and serves it per project; both clients recall from it; Up recalls on the first line only (2026-10-29, C) | History survives restarts and is shared across clients |

## 11. Known gaps

- **NSH browser tests: 11 of 64 fail; fixing them is deferred (decision 2026-10-29).**
  `nsh test --target web` finds no browser by itself; pass Playwright's Chromium:
  `nsh test --target web --browser "$HOME/Library/Caches/ms-playwright/chromium-1200/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"`.
  Every failure shows "Disconnected from Core · reconnecting" on screen: the mock
  server's event stream ends, so the app drops into its reconnecting state.
  Failing: `App.test.tsx` (session tabs, pre-session picker, daemon restart,
  daemon start once per outage, footer pricing, composer meter, detail cap,
  session info totals), `i6.test.tsx` (sessions sidebar), `i8.test.tsx` (two
  toast and notification tests). No clean-checkout baseline was run, so none is
  proven to be unrelated to the 2026-10-29 NSH edits. Until fixed, NSH is checked
  by `nsh check` and by driving the real app. Later work: keep the mock stream
  open, then add tests for steer numbering and the new-session skills/MCP rows.
- Kernels have no checkpoint recovery; a timeout, interrupt, crash or resource
  limit discards the kernel. Startup recovery reports the loss for a reconciled
  turn and preserves nested evidence, but restores no variables or cells. Monty
  workers run inside the daemon with its environment (a scrubbed helper process
  is planned before 0.10.0). Linux, a forced worker crash and cold-start latency
  are unverified.
- Background bash jobs live in the daemon's memory. A normal shutdown kills
  them; a daemon crash can leave the command running with no job record, and
  retained output is lost. Recovery reports an unfinished `bash.started` when it
  reconciles that session's turn, but a job that outlives an orderly turn in a
  session with no interrupted turn is not reported individually. Recovery never
  kills a process from a stored pid (it may have been reused); there is no job
  supervisor.
- Approval cancellation now publishes `permission.replied` with an explicit
  outcome during normal teardown, cell cancellation and startup recovery.
  Questions and approvals remain distinct waits; none survives a restart.
- Startup recovery closes an interrupted outer `code` call with an unknown
  outcome, preserving the durable `code.call` and `code.cell` events and saying
  the kernel was lost. It does not invent a successful call or recover kernel
  state.
- A provider retry can duplicate provider-side computation/billing; it is
  bounded and visible, not provider-side exactly-once. An ambiguous external Git
  effect (worktree create/delete) is persisted only as intent/evidence and must
  be inspected, never blindly repeated. Manual continuation is an explicit
  client action and never repeats an ambiguous POST. There is no context limit
  or compaction (planned as handoff, Step 12a below).
- Explicit updates apply to subsequent model/tool calls. An active `code` cell
  retains the task-tool bindings it started with until that outer call finishes.
  Agent switches retain the initially loaded `AGENTS.md` section; a directory
  change loads the destination's instructions as forward context instead of
  rewriting the frozen prompt.
- A client-requested directory move that is waiting for a running call lives in
  memory: a daemon crash before the next boundary loses it. Worktree deletion
  checks session directories and pending moves, not arbitrary `bash` or external
  processes using the worktree. There is no merge/integrate or branch-cleanup
  operation and no worktree permission setting.
- Archive transitions are stored in the root session's event log only; a client
  subscribed solely to a descendant stream does not see the root's transition.
  Archive has no bulk/project operation, timestamp or worktree cleanup (by
  design archiving never deletes one), and background jobs keep running.
- A session export holds one SQLite read snapshot (WAL) while streaming, so a
  very large export delays checkpointing until it finishes; writers are not
  blocked. Markdown omits wire events, and the JSON document (with every event)
  can be much larger than the messages alone. Export includes inline user images,
  but has no general artifacts and does not re-clip output.
- Sessions from before snapshots existed are reconstructed once and marked
  `reconstructed`; the note is visible, but the original prompt is unknowable.
- Reconnect resumes the event stream; startup recovery reconciles unfinished
  turns before the daemon serves requests and publishes their idle status,
  cancellations and `turn.recovered`. Manual continuation after recovery is an
  explicit request. Keyed mutations replay safely and clients resync from
  `/api/snapshot` on a cursor failure or overflow. Archive transitions are still
  stored only in the root's log.
- Event, turn lifecycle and mutation retention is bounded: a contiguous event
  prefix expires by age/payload, while unfinished turns, open waits, live jobs
  and in-flight replays pin their evidence. Messages, snapshots and model context
  are independent of diagnostic expiry. A cursor below the floor gets
  `history_unavailable` with a resync head. Expired keys are no longer safe to
  retry automatically. A very large export still holds one WAL read snapshot
  while streaming.
- Provider sign-in/out is available through the API and both clients; credentials remain in the auth store.
- Nothing checks that a running daemon runs the current code: `nexus daemon stop` after changes.
- No cross-provider message conversion beyond text and tool calls; switching
  provider mid-session works (`/model`), but foreign reasoning is dropped.
- Copilot GPT models (catalog `@ai-sdk/openai`) are sent over Responses and the rest over Chat Completions; verified live with `gpt-6-luna` (2026-10-29). Responses replay for Copilot has no encrypted reasoning yet.
- User images replay in both dialects; tool/MCP image results and general artifact storage are not implemented.
- Tool paths are confined to the workspace, but `bash` is not sandboxed, so
  "read-only" (`scout`) and "Markdown only" (`plan`) rest on their prompts.
  Read-before-edit is enforced only for the `edit`/`write` tools: unrestricted
  bash can write any file without an observation. There is also no in-process
  control over external writers, so a file can change between the final hash
  check and the write. Full enforcement needs a sandbox or restricted
  write-capable command execution before the invariant can be guaranteed.
- File observations persist per session and have no API view or pruning yet;
  they are removed with their session and replaced when a newer revision is
  read. An interrupted read that never produced a tool result is dropped.
- Child sessions launched with `wait: false` are tested offline only.
- `instructions` uses the Responses field. The current Codex CLI sends a
  developer message instead. Both work today; revisit if the backend changes.

## 12. Open questions

- `allowed-tools` in `SKILL.md` (decided 2026-10-29: ignore it for now; it is parsed
  and shown, never enforced, and the `skill` result says so). Come back to it: old
  Nexus narrowed the tool set while a skill was active; Claude Code treats it as
  pre-approval. Neither is built, because "active" has no clear end and a changing
  tool list breaks the prompt cache.
- STT engine: port Bark (download needed) or use the cached MLX Whisper (I).
- Gemini dialect (J5).
- Whether claude-agent and antigravity workers fit the one-timeline rule (J6).
- Syntax highlighting library for the TUI file view.
- Focus ring colour: shared with the accent or a separate cool hue.
- Worktree cleanup on archive/delete, and leaving a worktree.
- Whether a user can steer a child session from the parent's card (B makes it
  possible; `message_session` already steers a child).
- Output caps (lines vs bytes) and whether they are a setting.
