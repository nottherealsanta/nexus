# Design decisions

What was chosen, what was refused, and why. Each entry points to the code or doc
that carries it. Add new decisions here, newest within a section last. Historical
detail lives in [`plans/`](../plans/); where a plan disagrees with current code,
this page and the code win.

## Product

| Decision | Why | Where |
| --- | --- | --- |
| **Context is presented clearly to the user and the agent.** Every parameter and output is shown, labelled and readable; never a raw JSON dump; nothing the agent sees is hidden from the user; clipping is announced. | This is the reason Nexus exists: trust comes from seeing the real request. | `ui_support/tool_details.py`, `context.py`, [surfaces.md](surfaces.md) |
| **The web mirrors the TUI.** Same regions, commands, shortcuts and wording; a change in one lands in the other. It may look more modern. | Users switch surfaces mid-task; muscle memory must transfer. | [surfaces.md](surfaces.md), [web.md](web.md) |
| **Browser-native finish, not "Signal".** Sans-serif text, Monaspace Argon for code, three radius tokens, soft shadows. The older 0-radius, hard-offset-shadow look is history (`design.md`). | The CSS mixed both looks; one consistent system was asked for. | `ui/web/styles/tokens.css` |
| **First run only asks to connect a provider**, then picks that provider's newest tool-calling model. | Minimum steps to a working chat; credentials never enter setup commands. | `host_support/setup.py` |
| **Skill, MCP and root-agent choices lock after a session's first turn.** | Changing the prompt prefix would invalidate the prompt cache and confuse the record. | `session/session.py:context_locked` |
| **Native tool/task rows omit failure badges and result gutters.** | The loop handles tool failures; expanded details and the agent page retain errors without adding noisy duplicate status marks to the transcript. | [surfaces.md](surfaces.md), `rust/tui/src/transcript.rs` |

The desktop copies the Ratatui design language: `theme.rs` derives its palette
from the TUI `Palette` (neutral greys, warm `#FAB283`/`#C8672F` brand accent,
semantic blue/purple/cyan/success/warning/error) and resolves host colour tokens
(`$nx-*`, agent hex) the way `transcript::color` does, so agent identity, thoughts,
tasks and context sources carry the same colours on both native surfaces. Native
type, curved controls and window chrome remain desktop-specific. Its composer
retains Ratatui's editor/controls/context ordering. Image previews are bounded
presentation data from the existing host; custom clients never read local image
paths themselves. See [desktop.md](desktop.md).

## Architecture

| Decision | Why | Where |
| --- | --- | --- |
| **One daemon per workspace owns the runtime; every surface is a pure client** with no in-process fallback. | One place for locks, turns and approvals; surfaces can crash or disconnect without losing work. | [host.md](host.md) |
| **`core/loop.py` knows only protocols.** | Testable with fakes; every manager replaceable; layering enforced by tests. | [loop.md](loop.md), `tests/test_layering.py` |
| **Events are the spine; views are pure reductions.** Every drawable state is an event, persisted before fan-out, with a monotonic per-session `seq`. | Replay reproduces live state; reconnect is "fold to `seq`, follow from `seq+1`"; SSE `Last-Event-ID` maps onto `seq`. | [events-and-view.md](events-and-view.md) |
| **`view/` sits below `model/`** and imports only `events`. | Daemon, CLI, browser and replay share one reducer. | `view/` |
| **A turn outlives its viewers**; scheduling never consults a subscriber count. | Closing a tab must never cancel work. | `host/supervisor.py` |
| **Global turn cap (4) with per-session FIFO and round-robin.** | "Five sessions × parallel tools × subagents is a fork bomb." | `host/supervisor.py` |
| **Per-turn freeze** of config, environment, `SOUL/MEMORY/AGENTS`, tool catalogue; one manifest generation per loop iteration. | Determinism and prompt-cache stability; a reload is visible next iteration/turn. | `runtime.py`, `context/manager.py` |
| **Tool-level failures are model-visible results; harness-level failures end the turn.** | The model can self-correct; the user sees real failures. | [loop.md](loop.md#failure-handling) |
| **Assistant message is persisted before any tool runs.** | No provider accepts a result without its call; crash recovery closes dangling calls without executing. | `core/loop.py` |
| **Recovery closes turns and `!` runs a stopped process left open** (`turn.failed`/`shell.completed`, reason `interrupted`). Turns: proven dead by the session lock. `!` runs hold no lock: closed only when not live in this process and the recorded `pid` is gone. | A replay must never show work as running that nothing runs. A `!` run gets no lock because it must not block turns. | `session/session.py` |
| **Whole-batch permission check before any tool runs.** | One approval covers several calls; a denial never leaves half a batch executed. | `core/loop.py` |
| **Fallback models only on a provider error that produced no output.** A refusal or partial stream never falls back; no mid-stream rerouting. | Avoid duplicated or inconsistent output. | `model/router.py`, `core/loop.py` |
| **Switching provider mid-session is allowed, lossy and visible** (`context.degraded`). | Flexibility without silent corruption. | `model/capabilities.py` |
| **Registry is authoritative for capabilities; adapters own transport-only fields.** A provider rejection contradicting the registry degrades once and records `registry.mismatch`. | One source of truth that can be fixed upstream; turns still complete. | `model/registry.py` |
| **Everything is bounded:** sizes, counts, pages, queues, timeouts. | Hostile or runaway input must not grow memory or stall the daemon. | throughout |

## Storage

| Decision | Why | Where |
| --- | --- | --- |
| **Sessions are append-only records in one shared SQLite DB** (`~/.nexus/nexus.db`, WAL, `synchronous=FULL`, `BEGIN IMMEDIATE`); JSONL is an export format only. Replaced per-project JSONL directories. | Indexed listing, cross-project state, race-free `seq` across daemons, one transaction for delete/archive. Legacy directories are not imported. | [sessions.md](sessions.md) (`plans/STATE_PLAN.md`) |
| **Record bytes are unchanged msgspec JSON.** | Reducer, export, fork, replay untouched by the storage move. | `session/db.py` |
| **Snapshots are derived caches validated against the log; compaction never deletes history.** | Fast resume without risking loss; "omission from the prompt does not delete history". | `session/snapshot.py`, [context.md](context.md) |
| **Project extensions and settings live in `<workspace>/.agents/`; legacy `.nexus/` is a read-only lower-precedence fallback.** Machine state is under `~/.nexus/`. | A conventional, tool-neutral project dir; writes have one home. | [config.md](config.md) |
| **Config: layered, `msgspec`, unknown keys are errors; lists append; flat v1 bridges into v2.** | Typos must fail loudly; old configs keep working. | `config/` |
| **Secrets are references (`${env:…}`/credential store)**, resolved at use. | Nothing secret in config, logs or events; private credential storage is separate. | [security.md](security.md) |
| **Python ≥ 3.13.** | `object.__setattr__` on msgspec Structs, used across the codebase, fails on 3.12 and older. | `pyproject.toml` |

## Tools, permissions, extensions

| Decision | Why | Where |
| --- | --- | --- |
| **`deny` is absolute and daemon-side.** Order: deny → session grants → allow → ask → mode. | No grant, path trick or model argument may override it. | `tools/permissions.py` |
| **`*_always` persists an exact-action rule** (JSON-encoded key) or degrades to `*_once`; it never broadens. | A click must not silently authorise more than was shown. | `tools/permissions.py` |
| **Paths are canonicalised before any allow;** write roots and read-deny roots are hard boundaries; the state DB is never tool-accessible. | `../` and symlink escapes fail closed. | `PathGuard` |
| **No OS sandbox. Extensions are trusted code.** Quarantine validates; it does not sandbox. | A false sense of isolation is worse than a stated boundary. | [security.md](security.md) |
| **Two extension tiers:** data is parsed; code is imported under version-stamped module names, never `importlib.reload`. | An in-flight call keeps its generation; generations are immutable. | `tools/loader.py`, `ext/` |
| **Reload is all-or-nothing with one compare-and-swap.** | No partial manifests; a failed edit leaves the running world untouched. | `ext/manager.py` |
| **Not hot:** core modules, pip installs, manifest shape. | Widen an interface instead of making core reloadable. | `nexus doctor --explain-reload` |
| **`ToolContext` has no `Runtime`.** | A hot-loaded tool can be reviewed rather than trusted with the stack. | `tools/spec.py` |
| **Declarations are never grants** (skills, agents, hooks narrow only); read-only roles are structural. | A markdown file must not expand authority. | `skills/activation.py`, `agents/model.py` |
| **Restricted frontmatter, no YAML dependency.** | Small attack surface; one bad line rejects the file. | `agents/model.py`, `skills/frontmatter.py` |
| **Tool names are lowercase; legacy names map one-way** and never widen rules. | Stable public vocabulary with compatibility for old rules. | `tools/names.py` |
| **Agent questions are data, not grants** (separate event group). | An answer must never be mistaken for an approval. | `events.py` |
| **A hook is policy, not a permission;** a `modify` is re-validated and re-gated. | Hooks cannot launder a denied call. | `hooks/` |
| **Blocked subagents report to the root, never to the user; the root escalates.** `escalate` exists only in a child's catalog, grants nothing and is allowed in every mode; the child then ends (no pause/resume). | The root owns the conversation and the user; a human answer is the only thing that widens authority. | [agents.md](agents.md), `tools/builtin/escalate.py` |
| **Children never exceed parents:** tools intersect, permissions inherit, tier is clamped, depth/fan-out/spend bounded. | Delegation must not be an escalation path. | [agents.md](agents.md) |
| **Worktree integration applies frozen bytes to a clean parent with a rollback journal; no Git commit/reset/stash/checkout; the user acknowledges an exact review.** | Reviewable, recoverable, no surprise history changes. | `agents/worktree_*.py` |
| **MCP is wrapped once (`mcp/client.py`); its output is untrusted;** a tool is mutating unless `readOnlyHint` is explicitly true; a dead server never fails a turn. | Upstream churn stays in one file; fail-safe defaults. | `mcp/` |
| **Web tools are public-address-only with address pinning;** search results' links are never fetched. | SSRF and rebinding defence; injection surface reduction. | `net/` |
| **`bash` yields long commands to background jobs** instead of killing them; `wait` returns on exit. | Long builds finish; the model stops polling. | [tools.md](tools.md#shell-jobs) |

## Providers and models

| Decision | Why | Where |
| --- | --- | --- |
| **Any OpenAI-compatible vendor is a config block, not code** (and must state a `base_url`). | No silent default to `api.openai.com`. | `runtime.py:_adapter_kind` |
| **The models.dev catalogue supplies descriptions only,** never endpoints or credentials; vendored snapshot with MIT attribution; offline mode. | A third-party catalogue must not redirect requests. | `model/registry.py` |
| **Tiers resolve deterministically** (user pin → curated → cost → `low`) and only clamp downward. | Predictable routing and spend. | `model/tiers.py` |
| **Codex models go through the OpenAI adapter's Responses dialect** (ChatGPT OAuth, experimental, private endpoint), not a Codex CLI subprocess. | One adapter, refresh tokens in the private credential file only. | `auth/codex.py` |
| **GitHub Copilot uses GitHub.com device sign-in with OpenCode's OAuth app id;** the GitHub token is the Copilot bearer (no `copilot_internal` exchange). | Simple, verified against `/models`. The device flow itself was not live-tested. The root `README.md` still says Copilot is omitted; it predates this. | `auth/copilot.py` |
| **OpenCode is integrated over ACP only;** Nexus never reads its credential store; ACP tool calls stay inside that agent. | Clear trust boundary. | `model/providers/opencode.py` |
| **Claude subscription via the official Agent SDK in an isolated worker,** SDK tools/hooks/settings/persistence disabled; text-only, buffered. | Nexus keeps logging, permissions and execution. | `model/providers/claude_agent.py` |
| **Explicit `claude-code/<model>` subagents run Claude Code's own agent loop and tools in a PTY, outside Nexus's dispatcher; the earlier text-only PTY model bridge (`model/providers/claude_code.py`) was dropped and deleted.** | A bridge that disables Claude's tools and feeds JSON tool intentions into a Nexus child loop wastes what Claude Code is for and implies Nexus enforcement it cannot give an interactive CLI. Instead the policy is explicit and refuse-don't-widen (read-only roles only get `Read,Grep,Glob`; budgets refused; bypass/auto never passed; no prompt is ever answered), usage is reported as unknown, and file changes are measured by Nexus. Main-agent use stays on the SDK. Live behavior not verified; remaining gaps are listed in [agents.md](agents.md#external-claude-code-workers). | `agents/claude_code_worker.py`, `runtime.py:_ChildRuntime` |
| **`claude-code` subagents have a backend switch (`[models] claude_code_subagent_backend`: `worker` default, or `sdk`).** | Some users want Claude as a subagent under Nexus's tools, permissions and measured usage rather than Claude's own tools. `sdk` reuses the Agent SDK provider in the normal child loop, built per child so it never makes Claude a selectable main model. No silent default model on either backend. | `runtime.py:_ChildRuntime`, `host_support/model_settings.py` |
| **Antigravity (`kind = "antigravity"`, alias `agy`) is a subagent-only external worker, run headless over stream-json with agy's own tools; it defaults to `worker_inherits_agy_settings = true` (explicit `false` refuses execution) and refuses what Nexus cannot enforce.** | Same refuse-don't-widen rule as `claude-code`, but agy is driven over pipes (the Claude worker's earlier "ZMX" design used a PTY) so no terminal is emulated. A separate provider kind rather than extending `claude_code_subagent_backend`, which keeps Claude behaviour untouched and lets agy have no Nexus-tools backend. agy applies its own settings, MCP servers and plugins and PathGuard cannot reach it, so read-only roles, tool/MCP/restriction fields, worktrees and token/cost budgets are refused, and only `auto`/`accept-edits`/`plan` are passed. Models are the exact IDs `agy models` reports (no hard-coded catalogue); tiers may name `antigravity/<id>` explicitly, with no price ranking; never the root, title or fallback model. Usage is the terminal `result.usage` or `unknown`; cost is always `unknown`. Implemented offline only (no real model turn, no credentials). | `agents/antigravity_worker.py`, `model/providers/antigravity_cli.py`, `runtime.py:_plan_antigravity` |
| **Antigravity sign-in and quota are manual and honest.** | `agy` has no `auth` subcommand, so Nexus can prove only that the CLI exists, never that it is signed in; Settings says to run `agy`. Usage is shown as unavailable (no windows) with a note to run `/usage` in `agy`: its output format is unverified, so `agy -p /usage` is not run and no numbers are invented. | `host_support/provider_auth.py`, `host_support/provider_usage.py` |
| **Provider usage reuses the credentials Nexus already holds;** Claude's comes from `claude -p /usage`, not the `api/oauth/usage` endpoint CodexBar calls. | Reading Claude Code's OAuth token from its keychain item would break "the CLI owns the credential store"; `/usage` is a local command that costs no tokens. Its text format is unversioned, so a CLI change shows as a row error rather than wrong numbers. | `host_support/provider_usage.py` |
| **Claude sign-in runs `claude auth login` headless and takes a pasted code;** Settings never signs Claude out. | The daemon never opens a browser, and the login is shared with Claude Code, so a Nexus "Disconnect" would surprise the user. | `host_support/provider_auth.py` |
| **`Ctrl+U` opens usage even in the composer.** | One direct key in both surfaces; the TUI composer loses readline's delete-to-line-start (`Cmd+Backspace` still works). | `ui/ratatui/actions.py` |
| **Thinking summaries are provider-supplied and never invented;** no duration is fabricated. | Honest presentation. | [loop.md](loop.md#thinking) |
| **Voice uses Kestrel's internal Parakeet runtime, not Photon** (telemetry, no opt-out). Model download is consent-gated and never implicit. | Privacy. Real inference is unverified. | [voice.md](voice.md) |
| **Live dictation re-transcribes the growing recording as non-queueing `partial` previews; the final transcript still comes from one pass over the whole recording.** | Parakeet TDT here is offline, not streaming; whole-recording passes avoid word-boundary seams between chunks, and previews can never delay or replace the final text. Cost grows with length, bounded by `max_seconds`. | [voice.md](voice.md#flow) |

## Web

| Decision | Why | Where |
| --- | --- | --- |
| **Plain HTML/CSS/ES modules, no build step, no framework.** | Served from the wheel; edits visible on reload. | `ui/web/` |
| **Strict CSP:** no inline script or `style=""`, no external hosts (font is vendored). | Untrusted text must not execute. | `host/web.py` |
| **One-use ticket → `HttpOnly; SameSite=Strict` cookie + CSRF; exact `Origin`.** | A local listener still must defend against other pages. | `host/web.py` |
| **Browser state comes from a versioned snapshot plus JSON-Pointer patches with `resync`.** | Cheap live updates, safe reconnects. | `host_support/browser_view.py` |

## Process

| Decision | Why | Where |
| --- | --- | --- |
| **No line caps;** new terminal behavior still gets its own module. | Caps produced contortions; layering is what matters. | `tests/test_phase3_exit.py` |
| **Conventional Commits drive versions; never edit `version` or `CHANGELOG.md` by hand.** A version-bump request defaults to the next patch and authorises the full release; a minor bump needs explicit approval. | release-please owns the files; patch releases should be cheap. | [release.md](release.md) |
| **Docs are the source of truth; code wins over plans.** | Agents need one place to look. | [README.md](README.md) |

## mcp.json reads other clients' dialects (2026-10)

People paste MCP config from VS Code, Claude Code, Cursor or OpenCode; refusing
`type`, an argv `command` or `${VAR}` made those servers vanish silently. Each
entry is now folded into Nexus keys first, foreign approval/OAuth keys are
accepted but shown as ignored (never acted on), any other unknown key still fails
that one server, and failures are rows in Settings and the context header. `${VAR}`
now expands like Claude Code (it used to be literal); bare `$VAR` stays literal.
Each file is isolated, so a broken project file no longer freezes the global set.

## Known gaps and drift (as of this writing)

Stated so nobody builds on a false assumption.

- **No summarizer is wired in `Runtime`:** `hybrid` compaction is evict-then-drop,
  and `strategy = "summarize"` fails actionably ([context.md](context.md#compaction)).
- **`[permissions] mode` defaults to `allow` in the schema** but `ask` in
  `PermissionEngine`'s constructor and in examples; set it explicitly.
- **Root `README.md`** still says raw GitHub Copilot is omitted, that the web
  frontend is deferred, and suggests `summarize` for context overflow; the code
  differs (see above and [web.md](web.md)).
- **`examples/nexus.toml`** predates `.agents/`, `default_type = "task"` and
  lowercase tool names. [config.md](config.md) is authoritative.
- **Docstrings** in `hooks/__init__.py` and `examples/hooks.toml` mention
  `.nexus/hooks.toml`; the canonical path is `.agents/hooks.toml`.
- **Voice** real-model inference, platform support and network behavior are
  unverified. **Copilot** device sign-in is not live-tested.
- **Not done:** completion notifications for background shell jobs still running
  at turn end; `nexus ext restore`.

## Standard installation includes Claude Agent; the installer adds voice

Claude Agent SDK and `sounddevice` are required package dependencies; the old
`claude-agent` extra remains a compatibility alias. The voice runtime
(`moondream`, which needs `kestrel-native`) stays in the `voice` extra because
`kestrel-native` publishes no musl wheels: as a hard dependency it made
`install.sh` fail outright on Alpine. PEP 508 markers cannot tell musl from glibc,
so `install.sh` adds `voice` itself except on musl (or with `--no-voice`), and
`nexus update` keeps installed extras. Plain `pip`/`uv tool install nexus-harness`
does not get it, so `nexus voice init` installs the runtime into the current
install (keeping version, source and extras) before downloading the model. Claude login and the
consent-gated voice model download remain separate setup steps. Voice runtime
platform support beyond macOS and glibc Linux is not verified (see [voice.md](voice.md)).

## The 2026-10 UI redesign: picks from the design mock-ups

The 26 elements in the (since removed) Textual `design-mockups/` were judged variant by variant and the
picks applied to both surfaces ([surfaces.md](surfaces.md) has the result).
Notable choices and why:

- **Two-row top bar (tabs, then breadcrumb + status).** Several sessions run at
  once; tabs make every active one one click away with its status glyph, and the
  breadcrumb answers "where does this session work" (directory, worktree,
  branch). Closing a tab only hides it: sessions are daemon state, not window
  state, so tabs are in-memory per window (not persisted, not shared).
- **Branch from `.git/HEAD`, no subprocess** (`host_support/git_head.py`, in the
  Doctor report). Bounded file reads keep the health poll cheap and safe.
- **Problems-first logs.** Warnings and errors are what a reader opens logs for;
  the info/debug lines stay one click away, counted, never dropped.
- **Context header keeps today's chips**, adds grey token estimates and one-line
  previews; full text stays in the dialogs. **Tools dialog as a family table**
  and **Skills with a Markdown body** (SKILL.md through `SettingsRead`, so the UI
  still reads no files). **Usage by turn** in the Context dialog uses recorded
  provider usage, next to the estimated next request.
- **Tiered pricing** (models.dev `cost.tiers`) is parsed into `Cost.tiers`, used
  for cost accounting, and surfaced as `pricing` in the request context so the
  meter can mark where the price rises; it is advisory display, not a limit.
- **Thought headline** labels reasoning the provider did not share instead of
  showing nothing, so the user sees the model did reason.
- **Fuzzy search** (`ui_support/fuzzy.py` + `js/fuzzy.js`, same constants) for the
  model picker and web palette; the native palette uses the shared fuzzy matcher.

Provider credentials use `~/.nexus/credentials.json` instead of the OS keychain to
avoid backend errors and access prompts. The file is plaintext with owner-only
permissions, atomic replacement and bounded cross-process locking. Keychain
records are not read or migrated automatically; existing users sign in again.
The legacy `auth = "keychain"` configuration spelling remains compatible and
now selects the file-backed pasted-key flow. Claude CLI credentials remain
owned by Claude.

## Cross-project sessions without shared hosting

The sidebar uses the shared SQLite metadata index to group sessions by recorded
project root, ordered by each project’s newest activity, then local date. Folder
names label projects; equal names show full paths. Opening a foreign session
connects to its owning daemon instead of running its tools in the current
workspace. Foreign rows show saved activity until opened. The shared-daemon
implementation remains separate from this navigation feature.

## Ratatui prototype boundary

The native subprocess ships through `setuptools-rust` alongside the Python
console script; the existing setuptools package-data declarations remain intact.
This keeps crash isolation and avoids an unused PyO3 boundary. The alternative
maturin/PyO3 design in the earlier feasibility work remains an option if measured IPC
cost warrants it. Rust sources and Cargo.lock are included in the sdist; release
wheels require native platform builds. The first macOS arm64 wheel was built and
installed locally; other targets are not verified.

## Native TUI overlays and black theme (2026-10-02)

Native conversation/dialog backgrounds are explicitly black rather than terminal
transparent. Simple inspection/picker views are bounded overlays, root-agent
selection is anchored above the composer, and Settings editing keeps a full page.
An explicit presentation field separates placement from title wording. Provider
usage opens from cached data before fetching to keep the interface responsive;
refresh failures preserve useful data and show the error.

Subagent failures hand their context back instead of losing it. A failed child
returns a deterministic digest (no model call) and a saved markdown report, and
the root reads it and writes the next `task` prompt itself; a `resume_from` tool
field was removed because models filled the optional string with `""`, `" "` or
`"none"` and failed three spawns in a row. A model-written wrap-up was not chosen
because it would need a call exactly when the limit, budget or provider has already
failed. The iteration cap now defaults to unlimited (`max_iterations = 0`); the
wall-clock and token limits remain the automatic stops.

The wall clock is now unlimited by default too (`max_turn_seconds = 0`,
2026-10-08). The 30-minute default silently ended long turns right after a
tool result, without sending that result to the model, and a parent's clock
includes time spent waiting on subagents. Users saw the agent stop mid-task.
No limit is applied automatically now; any positive limit is opt-in, and a limit
stop is labelled in the turn footer.

## Native redesign: layout, context and incremental rendering (2026-10-02)

Tool grouping is a pure helper consumed by native projection. Full-height sidebars replace the lower
Logs pane. At constrained widths the most recently opened sidebar takes priority,
with a narrow right overlay; saved visibility remains intact across resizing.
Open sessions are marked in the Sessions list when its presence removes center tabs.

Context inspections can be refused during a turn. Keep the last successful preview
with its timestamp and label it as cached, while taking accent identity from the
current selected agent. Never infer context tiers or model windows from defaults.

Schema 2 sends changed block suffixes to avoid encoding and parsing the entire
history for each token. These dependent patches must be applied in sequence;
only schema 1 full snapshots can be coalesced. Stable revisions let the renderer
reuse wrapped history and indexed row ranges. Terminal output is buffered to
reduce per-cell system calls. The synthetic PTY benchmark meets latency budgets,
but streaming CPU remains above its target; it is not evidence of live-provider
end-to-end latency.

## Tool failures are not marked in the timeline

A failed tool call does not turn its group red, show `✗`, or add `· N failed`:
the loop recovers on its own, so the mark is noise. The error text stays in the
expanded call detail (nothing the agent saw is hidden). Header chips for skills
and MCP show unlabelled `project global` counts for the same reason: the order is
fixed and the section dialog names the scopes.

## Native transcript copies the OpenCode row layout

Thought duration comes from event timestamps in the reducer rather than from the
UI, so every surface and a replay agree. Subagent pages drop the context header
for a task prompt card to match the reference design; inspecting a child's
context chips from its page is the cost of that choice.

## Session titles are a side call, not a tool or a tag

A new session is named by one small request to a cheap model (the `low` tier by
default), started in the background after the first message. A title tool in the
session would run on the expensive model, pollute the agent's context and tools,
and break prompt caching; a title tag in the main reply leaks into the stream and
varies by provider. The side call is a plain request: no loop, no tools, bounded
input and output, a timeout, and any failure keeps the first-message title.

The title is metadata on the session row (`title_source`), not an event in the
log: it is not part of the conversation, and replaying it would change nothing the
agent sees. It is on by default, visible and switchable in Settings → Session
titles, which names the model the first message goes to (it can be another
provider than the session's). Cost: schema 2 means an older Nexus refuses the
shared database. Not done: re-titling later, manual rename (`user` is reserved).

## GPUI desktop reuses native host workflows

The desktop client is a separate Rust crate in `rust/desktop/`, using GPUI for
windowing, layout, rendering and native text input. It reuses the Ratatui
presentation bridge and includes its wire structs, so approvals, settings,
sessions, context and subagents keep one host-only implementation. UI geometry
and native input stay in Rust; providers and durable reduction stay in Python.
This avoids maintaining a second agent harness or copying the host protocol into
Rust. The cost is a Python presentation process and GPUI's pre-1.0 dependency;
GPUI is pinned and desktop packaging is separate from the terminal wheel.

## Subagent roles own their allowed tiers

Each subagent role lists the tiers it may use (`tiers:`, default first) so a quick
lookup cannot spin up a flagship and an advisor is never given the cheapest model.
The calling agent is told this in the `subagent` tool description. A request
outside the list moves to the nearest allowed tier with a note, never an error:
a model hint must not break delegation. `agents.max_tier` stays as a user ceiling
(default `high`; it was `medium`, which would have stopped the advisor ever
reaching `high`). Roles without `tiers` keep the old behaviour, so existing custom
agents change nothing until their owner opts in. Tier names are checked for shape
at parse time and resolved later, because custom tiers live in config, not here.

### One terminal client

Ratatui is the sole terminal renderer. Maintaining a second widget implementation
and its dependency stack is no longer part of the product. Missing native binaries
fail with build/install guidance. Optional binary builds still allow CLI/browser
installs on platforms without a Rust toolchain; terminal chat requires the binary.

## Desktop keys follow the terminal, with Cmd aliases

The shared `ui_support/shortcuts.py` tables are canonical for the desktop as well
as Ratatui: every `SHORTCUTS` and `LEADER_SHORTCUTS` row has a desktop route, and
`tests/test_desktop_keymap_parity.py` enforces it. Ratatui meanings win where they
collide with macOS text editing; `Cmd` bindings are aliases only. This is why
`Ctrl+E` toggles the Logs drawer rather than moving to end of line (`Cmd+→` does
that), matching the terminal. New leader rows (`c` context popover, `z` update
help) were added to the shared table because the terminal and web already handled
them; the desktop now routes them too. The transient Ctrl+X leader is expressed
with GPUI multi-stroke bindings. Scoped keys never intercept typing: `a`, the
PageUp/PageDown transcript scroll and the `[` / `]` inspector tabs carry
`!Editor`, and `Ctrl+S` saves only inside the `Form` context. See
[desktop.md](desktop.md#keys).

## Desktop overhaul starts with measured performance

The desktop source launcher prefers a release binary to a newer debug binary,
with dependency optimization in dev builds and a warning for debug fallback.
Composer synchronization is debounced while native editing stays local. Bounded
CPU traces identify bridge/render costs before the store and visual overhaul;
they do not claim display FPS. Transcript rows borrow the projected content
directly, avoiding per-frame deep copies without a duplicate Arc store. See
[desktop.md](desktop.md#overhaul-performance-foundation).


### Terminal interaction and incremental mirroring

Tool/group/output, thought and turn folding belong to Rust. Python supplies
complete labelled, redacted presentation and remains the canonical session reducer
and host-action owner. Local disclosure sends no action. Sidebar/tab/file/log
changes use optimistic state plus ordered acknowledgement numbers, allowing prompt
redraw without stale persistence echoes reverting newer choices.

Terminal schema 3 omits unchanged sections and carries ordered transcript suffixes;
this avoids retransmitting session/history/sidebar data per token. Omitted
one-shots never replay. Ordinary root deltas reuse the projected history and
wrapped row prefix. Other events and child pages retain full projection for
correctness; an open subagent page skips the unused root projection and reuses
per-turn safe blocks and the modified-files scan. Desktop keeps its encoder. This boundary avoids duplicating domain
reduction in Rust while removing presentation round trips. Large first-load and
first-wrap costs remain explicit benchmark cases.

The host's `state()` (a full reducer fold of the session log, run on the host
loop) is cached per session and extended with only the new events; a log that no
longer matches the cached last event is refolded. Opening a subagent page used to
cost one full fold per click (about 160 ms at 500 turns, stalling every client).

Development launches take the most recently built native binary, so a plain
`cargo build` or `cargo test` makes the unoptimized debug build the one `nexus chat`
runs. Build with `--release` when judging feel (release draw/key→frame measured
3–6× faster than debug).

Older turns start folded: only the newest two turns of a page are open by default
(`RECENT_TURNS` in `rust/tui/src/disclosure.rs`). Older turns are rarely what the
reader wants, and a folded turn costs the layout almost nothing. The window is
computed from the blocks Rust already holds, so it is local and needs no host
round trip; when a new turn pushes the oldest out, the layout is told to rebuild
from that turn because no patch covers it. Python still projects and sends the
folded turns; lazy loading of old turns is a separate, undecided change.

Terminal presentation no longer redacts credential-shaped text. The idea of Nexus is
that the user sees what the agent sees, and the six regex passes per field were the
largest cost of a cold projection (about 58% at 300 turns, so a session switch or
close lagged). `ui_support/text.py` still escapes terminal controls and caps length;
`util.redact_secrets` still covers logs, errors and host-boundary messages. The
browser client's `redactToolText` (`ui/web/js/app.js`) still redacts: not changed.

Switching sessions keeps each open tab's reduced view and cursor
(`SessionController._parked`, up to 8) and returns by reading only newer events; the
projected-turn cache is keyed by session and bounded at 64 MiB (`TURN_CACHE_BYTES`)
instead of being cleared. The context preview refreshes in the background rather
than before the first paint.

The terminal loop paces frames at 16 ms (`FRAME_INTERVAL`, `rust/tui/src/main.rs`):
a wheel fling used to draw hundreds of near-full-screen frames faster than a terminal
parses them (32 MB over 8,000 events, 2.6 MB paced), so a reversal waited behind the
backlog. The spinner clock advances on every loop pass, not only when input is idle.
Slow phases (over 50 ms) are logged to `~/.nexus/tui-stalls.log`.

Startup paints before it loads: the native client is spawned before the daemon connection
and shows a splash until the first snapshot. Only the session bootstrap and context preview
gate that snapshot; `doctor`, `list_models` and `setup_status` were each awaited in sequence
before the process even started (about 0.9 s) and now run in the background. Typing during the
splash is discarded, not buffered, because a draft would otherwise be submitted against a
session the host has not opened. Known gap: `doctor` still takes about 0.7 s because it waits on
`runtime.ensure_started()`, which parses the whole model catalogue; the branch appears late.

## Settings revamp (native client)

Workspace, Config, Soul and Hooks are hidden from the native Settings list for now;
Settings is left only by an explicit close, never by an operation kind missing from
an allow-list. A run-mode switch on an agent removes the other mode's fields so the
file never holds a model and tiers that contradict each other. Not verified: native
PTY rendering of the revamp.

## Toasts replace the shell notice (2026-10)

A notice was one string rendered as an `Error:` transcript line, whatever it was
("Copied 120 characters" and an exception looked alike) and it lingered until the
next event. Toasts carry a level, expire, and can be dismissed. The host owns *what*
happened (id, level, text, optional action); the client owns *when* it disappears
(timers, hover pause, dedup, dismissal), the same split as hover state, so no
round-trip is needed to expire one. Ids are time-seeded so a restarted Python process
cannot produce ids the client has already seen. Persistent conditions stay where
they were (disconnected banner, update chip, transcript projection failures);
toasts are for events. Every toast is mirrored to the Logs tab, so ephemerality never
hides information. Esc does not dismiss toasts because Esc already stops and closes.
The widget kit lives in `rust/widgets` so the design mock-ups and the TUI share one
implementation.

## Settings are typed pages the host builds and the client renders (2026-10)

Settings had grown into menus with pages inside pages, and the rule that kept it
"still Settings" (an allow-list of operation kinds) was the likely cause of Settings
closing unexpectedly. A page is now data: the host builds it from host results, the
client renders it, and every operation rebuilds it. That makes "never close
unexpectedly" structural (an operation cannot replace the page with a panel), keeps
the client free of settings logic (it only knows controls), and keeps what is shown
equal to what the host reports (no optimistic local values to drift). The cost is a
host round trip per change, which is local and fast. Scope is a property of the area
(only per-file areas have it), not of the page chrome, so always-global areas never
imply a choice that does not exist. Drill-ins are limited to editors, pickers and
confirmations, which stack on the page and return to it.

## Context header sections (identity-only System prompt, thin Tools list)

The System prompt row is identity and soul only; Environment, AGENTS.md, MEMORY.md,
Skills, Tools and MCP each own their part, so rows sum without double counting and
nothing the agent sees is hidden. Tools is a one-line-per-tool `list` panel with the
detail on a second `detail` page (≤ 88 columns, as tall as its text), because a
full-width grouped list buried the one number people look for (the token cost).
Skills are cards (one selectable item with dim rows) that open a skill page; MCP is a
thin `list` like Tools (one line per server, token column, Restart chip) that opens a
server page, all through host commands. The dev MCP server lives in `devtools`
and is wired only by the sandbox's generated `mcp.json`, so the product path never
imports it. Empty blocks are greyed, and the header has no total footer or overflow
text; the five-row inventory cap is therefore silent (a known tension with "clipping
is announced"). Toggling from a tool page works in place (the list underneath is
rebuilt); not verified on the desktop client.

## File changes stand alone; MCP restart is a host command (2026-10)

Activity groups fold reads, searches and commands into one summary line, which is
right for exploration but hid the changes a reader most wants to check. Edits,
writes, patches and questions are therefore never folded: each is its own `change`
row with the path and `+added −removed`. The row starts closed (the header already
names the file and the size of the change, and open diffs crowded out the reply);
Enter opens a unified diff in add/delete colours (a group of one repeated its header
as a member row and hid the diff behind a second click). MCP Restart is a host
command (`McpServerRestart`) rather than a client-side reload because only the
daemon owns connections; the connection is shared, so a restart is visible to every
session. Refresh reuses `ExtensionsReload`, which already reconciles `mcp.json`, so
there is one path for picking up new servers. Refresh then restarts not-connected
servers, because reload alone leaves a failed server waiting out its backoff. A leading `/` runs a command only when
the first word is a known command or a bare word; anything path-shaped is prose,
trading silent sends of `/foo.bar` typos for never refusing a pasted path. The
80-column centered conversation is a local terminal preference, like the sidebars.
Rust rendering of the Restart chip and centered layout is covered by unit tests;
not verified in a real terminal.

## `/speak` uses Paradee on ONNX, not Kokoro on torch (2026-10)

Paradee-8M-v1.0 (Apache 2.0) is an 8M-parameter model distilled from Kokoro-82M
that speaks Kokoro's af_heart voice. Its int8 ONNX file is 9 MB against about
325 MB for Kokoro's weights, so the download is about 25 MB with the spaCy English
package. It runs on onnxruntime's CPU provider with no torch, so the speak extra
is lighter and there is no MPS or device setting. The model card reports UTMOS
4.41 (Kokoro 4.52) and the same WER (5.7%); quality was not re-measured here.
One benchmark on an Apple M4 measured a 2.0 s cold load (Kokoro 2.5 s), a
paragraph at about 34x real time (Kokoro 9x) and 0.5 GB peak memory (Kokoro 2.6 GB).
Trade-offs: one voice (the language and device choices are single-valued now);
phonemes come from misaki with `fallback=None`, so words missing from its
dictionary are dropped rather than spelled by espeak. The speak extra names
`misaki` with `spacy` and `num2words` instead of `misaki[en]`, because the `en`
extra pulls `spacy-curated-transformers` and with it torch. Code:
`nexus/host_support/speech.py`, `nexus/ui_support/speech_settings.py`; the
behavior is in [voice.md](voice.md#speak-the-latest-answer-terminal-clients).

## Speech survives upgrades without asking again (2026-10)

An upgrade must not make a user who already speaks answers reinstall, re-init or
re-consent. Three things used to break that: `nexus update` could drop the
`speak` extra, spaCy's English package was pip-installed into the tool venv (which
`uv tool install --force` replaces, and which has no pip at all), and a release
that changes the model showed the consent dialog again. So `update_extras` keeps
`speak` the way it keeps `voice`; the phonemizer wheel is unpacked under
`~/.nexus/models/speech/python`; and consent is a durable fact (a consent file,
or an earlier Kokoro/Paradee cache), after which the daemon fetches a missing
model by itself and `/speak` waits for it and speaks. The consent covered a local
speech model of the same kind, a few tens of MB; a much larger model would need
asking again. Re-running `install.sh` (rather than `nexus update`) does not read
the old receipt, so it keeps `speak` only when passed in `--extras`; not changed here.

## `nexus uninstall` removes all data, after asking (2026-10)

`uv tool uninstall` leaves `~/.nexus` behind on purpose, so there was no way to remove
Nexus completely without knowing every path it writes. `nexus uninstall` deletes the
whole Nexus home rather than a list of known files, because the home gathers entries
the docs do not list (handoffs, search server state, model preferences); a list would
miss them. Deleting a whole directory is only safe when it really is the Nexus home,
so `/`, the user's home and its parents, and a custom `NEXUS_HOME` without `nexus.db`
are refused. It asks with a plain `[y/N]` after listing every path and size, and needs
`--yes` without a terminal. The Hugging Face cache is shared with other tools, so only
the two speech model folders are removed. Project files are not Nexus state and are kept.

## Composer `!` shell output is context, not a prompt

`!command` runs with bash and its bounded output is added to the model context
as a user message without starting a turn: the user is showing the agent
something, not asking it to act. During a turn the output waits for the next
safe boundary (never between a tool call and its result) and does not extend
the turn. Each run is drawn as a `shell` turn reusing the `bash` tool row, so
the user sees exactly what the agent will see. Shell output (tool and `!`)
is capped at 2,000 lines or 50 KiB; the full text goes to a private temp file
named in the result instead of being silently cut.

## GPT Live: delegation, not a second voice agent (2026-10)

The voice model only talks; it hands work to the session the user already has
open through `delegation.created`, and the bridge enqueues it as an ordinary user
message (idle: queue, running: steer; `input.queued` carries an `origin` label).
OpenCode's alternative is a hidden second LLM session with control tools. We took
oh-my-pi's shape: it costs no extra model, adds no reasoning hop, and keeps
everything the voice model "did" visible as a plain user message in the
transcript. Status, steer and stop reuse Nexus mechanics (progress appends, steer
mode, Esc). A spoken "stop" and OpenCode-style control tools are deferred.

## GPT Live: the audio helper runs on the client side

Microphone permission attaches to the terminal or app that captures (dictation
already works that way); a detached daemon may have no TCC attribution. So the
helper process is spawned by the client and exchanges only SDP and small JSON
events with the daemon. The daemon owns signaling and the sideband WebSocket, so
credentials never leave it. Not verified on macOS yet (helper is milestone M3).

## GPT Live: approvals are UI-only

Voice never answers a permission or a question. The bridge announces "the user
must approve this on screen" and nothing else; the mis-heard "yes" that runs
`bash` is the failure we refuse to allow. The bridge observes the session through
`Session.observe()`, a tap that is not a viewer, so a call cannot make an
unattended session look attended or change the unattended policy.

## GPT Live: the protocol is undocumented

Endpoints, headers and event names are re-implemented from two MIT-licensed
reference integrations at pinned commits (`nexus/live/NOTICE`), without copying
code, and none of it has been run against the real service. Choices recorded:
the `originator`/`User-Agent` are the `CodexOAuthManager` identity (`nexus`),
because the notes show no evidence the server requires another (one reference
claims the Codex desktop app, the other its own name; whether `nexus` is
rejected is not verified); attestation is omitted; a sideband 401/403/429 is not
retried; a non-fatal `error` event is forwarded and the call continues, a fatal
one (401 or an auth code) ends it; the sideband is the only event source (not the
data channel); newline-joined delegation parts; the three per-call UUID headers
are random, not the Nexus session id. Fail closed, label experimental.

## GPT Live: streaming captions are persisted, coalesced

The session event sink persists first and fans out second; it has no
non-durable fan-out, and the subscribe watermark drops any event without a fresh
`seq`. A bus-only path would mean touching `Session.subscribe` and every follower,
so instead `live.transcript.delta` (the current partial caption) and `live.phase`
are persisted at most 4 times a second, only on change, and replay folds them
harmlessly. `live.levels` is in the catalogue but never emitted by the daemon:
clients draw their meter from the helper's own output. Revisit if a bus-only
path appears.

## xAI is a preset over the Responses adapter, not a new parser (2026-10)

Grok speaks the OpenAI Responses wire with the standard reasoning-summary
events (OpenCode's `xai-responses` protocol differs only in hosted-tool
vocabulary). A config block could already reach it, but could not express
`store: false`, the `x-grok-conv-id` cache header, or xAI's PNG/JPEG/WebP-only
image rule, so `providers/xai.py` subclasses `OpenAIProvider` through small
hooks (`_prepare_request`, `_finalize_body`, `_headers`) instead of copying
its decoder. Hosted tools need new timeline semantics and the WebSocket
transport is an optimisation, so neither is implemented.

## xAI connects only through the SuperGrok subscription (2026-10)

The user asked for the subscription and nothing else, so xAI has one credential
route: `auth = "xai_oauth"`, a device-code sign-in from Settings → Providers.
API keys (config, `${env:XAI_API_KEY}`, a pasted key) are refused for xAI, and
`xai_oauth` is refused on any other section or `base_url`, so the subscription
token can only ever reach `api.x.ai`. As with Copilot, Nexus has no OAuth app of
its own and uses OpenCode's xAI client id and scope (attributed in
`auth/NOTICE`); it sends `referrer = nexus` rather than claiming to be OpenCode,
which xAI accepted in a live sign-in on 2026-10-14. Like the ChatGPT route, only
the refresh token is persisted. Not yet observed live: a token refresh after
expiry.

## Usage reserve is elapsed share minus used share (2026-10)

The usage page answers "can I spend more?" for weekly and monthly limits with
one number: reserve = share of the window elapsed − share used (half the month
gone, 20% used → 30% in reserve). It assumes an even spending pace, which is a
heuristic, not a provider forecast, so it is labelled and explained on the page.
Shorter windows (5-hour) get none: they reset before pacing matters. Window
length comes from the host (`period_seconds`), never guessed from a label in the
client. Claude's CLI prints only reset text, so the host parses it (in the named
zone, inferring the year); an unparsed reset simply shows no reserve. The bars
became thin line glyphs (`━─`) and colour only the bar, so the rows read as
data rather than a wall of colour; `N% left` was dropped as the bar's track.

## The `agy models` list is cached until the model list refreshes (2026-10)

Every tier or default-model edit rebuilt the registry, which re-ran `agy models`
(network, 3.5-7 s) and re-parsed the 5.6 MB catalogue, so Settings > Models
lagged per edit. The probe result now lives on a `RegistryCache` owned by the
runtime and on disk (`cache/agy-models.json`). There is deliberately no TTL: the
list changes only when the user updates agy, which is what the model-list
refresh is for, so the agy list has its own refresh, separate from the models.dev
refresh: the `ProviderModelsRefresh` host command, a **Refresh models** button on
the Antigravity provider page that also shows the count and last-refreshed time
(kept in the cache file). Other triggers: a changed executable/section or an
empty cache. `ModelsRefresh` and catalogue re-fetches leave it alone. A failed probe
keeps the last good list. Tier-only edits skip the provider/registry rebuild
entirely (`Runtime.reload_tier_config`). Not verified: behaviour when agy is
upgraded without a refresh (the old list stays until refreshed).
## The sessions page shows both tails and reuses the composer (2026-10)

The sessions dashboard is a full-width list grouped by status, matching the
reference layout: location and `[Choose Alt+L]`, `+ New Agent`, Open Previous,
Worktree, and a two-line row. Hovering a row (or keyboard-selecting it) shows
the last user message and the last agent message. Those snippets live on the
session row (`last_user`, `last_agent`, schema 4), not on `SessionSummary`, so
ordinary session lists do not carry message text. The open session overlays its
live view on every snapshot. The composer below the list is the existing
composer; Enter sends to the selected session. Verified in a 140-column
dark capture (`artifacts/ratatui-parity/grok-dashboard-dark-140-unicode.png`):
status groups, header actions, the working row, and the existing composer
card. The keyboard-selected row is expanded into You / Agent lines. A live
`nexus chat` still shows the previous screen until it is quit and reopened,
because it keeps the release binary it was launched with.

## The native TUI has no left sessions sidebar; the dashboard and top bar replace it (2026-10)

The Ratatui client dropped the left Sessions sidebar (drawer, docked column, project
chips, filter, `F6` focus). Two surfaces covered the same job: the sidebar was a
second, narrower list of the sessions the dashboard already shows with more
context, and it cost 40 columns plus a drawer mode, a focus state and a client-side
filter that had to stay in step with the dashboard. Now `/sessions` (also `Ctrl+B`,
`Ctrl+O`, `Ctrl+S`) opens the dashboard, and the Grok-style top bar carries
`‹ i/n ›` to cycle the open sessions and a clickable `[Dashboard]`. The right
details sidebar stays: it shows facts about the current session, not a list of
others. The `sessions_sidebar` preference and snapshot key are kept only for the
GPUI desktop client, which still has its own sidebar; the Settings → Layout row is
gone. The web client keeps its sidebar (it is being deprecated, not changed).
Modals share one backdrop dim (`render::dim_backdrop`) instead of each drawing its
own scrim. Not verified: native screenshots of every modal kind over the dim.

# In-progress Grok TUI implementation notes

- The widget-role foundation is implemented as methods, not role constants.
- Projection/tool verb signals are implemented with focused Python tests passing.
- The host preview contract is implemented. The unrelated fake `_load_config`
  failure in `test_facade_handle_dispatches_every_verb` is pre-existing.
- The Mermaid module is registered and called from `markdown.rs`. Its actual
  reference revision is `559751fdcec02d413e4c57c8832ab275e4f44980`, not the
  planned `2bdd1d6`.
- Double-click on a tool uses a 300 ms window on the same semantic block and
  operation, and toggles local disclosure. A single click selects, and `i` opens
  the full inspector (so nothing the agent saw is unreachable). Local disclosure
  matches on semantic kind and id and ignores presentation metadata.
- Settings hover uses real hit ranges. The dashboard invalidates in-flight work
  when a replacement overlay closes.
- Not claimed: screenshot acceptance, performance acceptance, final combined
  verification, or commits.

## Settings is one root list with drill rows, not an area column (2026-10)

The native TUI Settings modal drops the left list of areas. The root list (client-side,
`settings_page/root.rs`) shows section headers, the local appearance toggles and one drill
row per area from the host's `nav`; drilling sends the existing `nav_select`, and Esc/Left go
back locally. This matches Grok's settings modal and needs no new host command. Cost: the
host still loads Appearance behind the root on open. Appearance and Layout rows are inlined via a
`settings_overview` snapshot field built from those areas' builders (same handlers and
allow-list); Keyboard stays a drill row. Every framed dialog
(pickers, panels, Settings) shares `dialog_frame`: `border` outline, bold title top-left,
clickable `[×]` top-right (`render::close_rect`), and `element` selection fill. Not verified
on a light theme.

## Sessions dashboard groups by time and previews only on keyboard selection (2026-10)

Superseded in part: the preview pane (the `Response` box) was removed; see "Sessions dashboard has no preview pane" below.

The native dashboard groups by last-updated local time (Today, Yesterday, This week,
Earlier) instead of status: people look for "the thing I did recently", and status is
still carried by the row glyph and header chips. The host computes `bucket` and
`updated` per row so the client needs no time zone logic. The `Response` box now follows
only the keyboard selection: hover used to swap it under a resting pointer and made the
list jump, so hover is a background tint only and a box appears after Up/Down with an
empty composer. Up from the top row opens the search bar (also `/`, a click, `Ctrl+K`).
The `[×]` keeps the archive confirmation. Not verified on a light theme.

## Turns held by the global cap are shown, from supervisor state (2026-10)

An idle session whose submit the supervisor parks (all `max_concurrent` slots busy)
looks exactly like an idle send that is about to start, so the native client hid the
message. The supervisor now reports `slot_wait` on its `daemon.session_*` events and
on each follow subscription's first frame instead of the client guessing from the
durable log: the wait is scheduling state, not history, so it is not journaled and
a reconnect re-derives it. The head entry is kept only while `slot_wait` is set, so
the "steer" flash fix for ordinary idle sends stands. The count of running turns is
a snapshot at the last event and may lag while the session stays parked.

## Sessions dashboard has no preview pane (2026-10)

The dashboard has no preview: no `SessionDashboardPreview` host command or
`SessionDashboardPreviewResult` (with `PreviewRow` and `PendingInput`), no client
`dashboard_preview` method, no `preview` key on the dashboard snapshot, no preview pane
in the Rust terminal client, and no `session_preview` dashboard operation. The list
(`ProjectSessionsList`, about 10 ms) and its schema 4 row snippets remain. The archive
`SessionPreview` command is unchanged.

Why: `dashboard_preview` replayed the selected session's whole event log through the
reducer on the daemon event loop, and the dashboard re-polled it every 0.5 s. Measured
0.40 s for a 12k-event session and 1.83 s for a 19k-event session, so `/sessions` felt
slow and blocked other daemon requests. Caching was considered; removing the preview
was chosen. Not verified: that the concurrent removal is complete in code.

## Sessions dashboard has no composer (2026-10)

The native client does not draw the composer on the Sessions dashboard
(`render::composer_height` is 0 while a dashboard is shown), and `main.rs` no longer
routes dashboard keys into a draft: list shortcuts, search and rename own their keys and
everything else is ignored. Enter opens the selected session. Sending to a selected
session from the dashboard is gone.

Why: a hidden-or-duplicate input under the list made typing ambiguous (search versus
message) and cost rows the list can use. Not verified: visually in a real terminal.

## Failed tool calls do not open a group; the failed count is dark red (2026-10)

A folded tool group draws only its header (and the live tail while running). The native
client used to draw failed members under the folded header, which in effect expanded the
group; `transcript/groups.rs` no longer does. The `· N failed` count stays in the header,
coloured with the new `Palette::error_deep` (dark `196,66,88`, light `160,28,48`), darker
than `error`, which was too light there. The Python projection still sends `failed_members`
(operations stay allowed); the web app has no such rule.

Why: the user wants groups collapsed by default regardless of outcome and opens them
manually. Not verified: visually in a real terminal.

## Slow commands do not block the connection's read loop (2026-10)

The daemon read loop awaited every command before reading the next frame, so the
startup `Doctor` (full `PRAGMA quick_check` and session-tail scan over a ~1.7 GB
`nexus.db`) delayed a `SessionOpen` sent right behind it on the chat client's single
connection. Measured on a cloned cold database: session open 13.2 s behind Doctor,
1.8 s after the change (0.4 s warm). `Doctor`, `UpdateStatus`, `ModelsList`,
`ModelsRefresh` and `SetupStatus` now run as bounded background tasks
(`SLOW_COMMANDS` in `host/daemon.py`); all other commands keep strict arrival order.

Why: results are already matched by frame id, so out-of-order replies are safe, and
detaching only the known slow commands avoids reordering pipelined session commands.
Not addressed: Doctor itself still reads the whole database cold (it competes for disk
with the first session read; the remaining 1.8 s vs 0.4 s warm), and the TUI's own
request path was not changed.

## Tier edits apply live, without a daemon restart (2026-10)

Settings -> Models reorders, adds and removes tier models (and sets tier efforts).
`HostFacade` used to reload only when no turn was running; otherwise it wrote the
config, returned `restart_required=True`, and the new order never applied until a
restart. It now always calls `Runtime.reload_tier_config` (tier table, registry
re-index, `ModelRouter.set_tiers`), which touches no provider. Subagent models are
resolved at spawn, so the next spawn uses the new order; running children keep their
model. `restart_required` remains only for runtimes with injected routes, and for
provider-changing settings, which still need an idle runtime.
Test: `tests/test_host_tier_hot_apply.py`. Not verified: the roster's effort labels in an
already-running turn (they read the runner's construction-time config).

## Transcript keeps a 20-row bottom buffer, and following parks there (2026-10)

The last reply sat against the composer. A transcript taller than the viewport now scrolls
`BOTTOM_BUFFER` (20, clamped to viewport - 4) blank rows past the last content row. That bottom
position is the follow position, the scroll-down clamp and the scrollbar range, all from one
function (`session::live_offset`); the earlier stuck-scroll bug came from these disagreeing. The
pinned-prompt tail padding is unchanged, and `max_scroll` takes the larger of padding and buffer.
Transcripts that fit the viewport get no buffer, so they never scroll. Tests: see
`docs/ratatui-parity.md`. Not verified: feel in a real terminal (only TestBackend and unit tests).

## GPT Live: upgrade webrtc-rs instead of vendoring a patch (2026-10)

Calls dropped at about 43 s. The cause is webrtc-ice 0.13, which sends an ICE consent
check only when the path is idle, so audio never triggers one and the server ends the
media path (see [live.md](live.md#root-cause-of-the-43-s-drop)). We upgraded the
`nexus-live` helper to `webrtc = "0.17.2"` (webrtc-ice 0.17.2), which contains the upstream
fix (pion/ice PR #767, webrtc-rs/rtc PR #86). Vendoring and patching webrtc-ice 0.13 was
rejected: it would carry a fork of ICE code we do not own, and the upstream release already
fixes it. The locked version is pinned by `tests/test_live_packaging.py`. Not verified
against a real call.

## Terminal shows the open call in a pane above the composer; the card follows once it ends (2026-10)

The Live card was a timeline block, so a long call pushed the conversation and the
controls around. In the terminal the open call's rows now sit in a 5-row pane between the
timeline and the composer. It follows the newest rows, the wheel scrolls it, and a button
expands it to read the transcript; the expanded state is local and resets when the call ends.
The open call is left out of the timeline, and its card joins the timeline once the call
ends, so the record reads in call order. The desktop client keeps the card in the timeline
during a call, because its layout has no pane. Tests: `tests/test_ratatui_live.py`. Not verified
in a real terminal.

## GPT Live: delegation text is the heard utterance; no voice-authored handoff yet (2026-10)

Decision: keep `delegation: {type: client}` and record, on `live.delegated`, `source`,
`heard` and `verbatim` beside `text`, instead of declaring a `send_to_agent` function tool.
Reason: in the recorded call `session-1ce5e575` every delegation text equals the user's
transcript although the instructions demand a rewrite; the public GPT-Live docs describe no
session-level function tools for the voice model (only `delegation.responses.tools`, which
runs OpenAI's backend), and OC's `end_call` is a tool of its coding agent, not of the voice
model. Declaring tools would be a guess against an undocumented endpoint, with a risk of
double-enqueueing. Known gap: the voice model cannot yet send its own wording; a live spike
with `[live].log_raw` should test a `tools` array on the session. Details:
`plans/in-progress/GPT_LIVE_PROTOCOL_NOTES.md` "Authorship of delegation text".

## GPT Live: Codex-style handoff, transcript context and speakable replies (2026-10)

Decision: the agent receives the delegation text plus a labelled transcript of both speakers
since the previous delegation and a speech-recognition note, as one plain-text user message
(`bridge.compose_request`); the final answer goes back as plain speakable text via
`session.context.append` with no label and no delegation id. The "rewrite before you hand off"
instruction is removed.
Reason: Codex (commit 6ae8b8aa, `core/src/context/realtime_delegation.rs`, `core/src/realtime_conversation.rs`,
`codex-api .../methods.rs`) does exactly this on `gpt-live-1-codex`: no voice-authored message
exists, so the agent gets the heard input and the recent transcript (input keeps its start,
transcript keeps its end, 4 KiB each) and the TUI replies with a speakable session append. The
"Agent Final Message" label is a v1 artefact of other clients. Nexus data already showed the
rewrite rule had no effect.
Why in the message text and not hidden: Nexus shows the user everything the agent sees; the
reducer has an `origin` label but no hidden-context channel for user messages, so the transcript
is visible in the timeline and replays from `input.queued`. Progress and notices keep the
delegation id (Codex's core-managed path); only the final answer follows the TUI path.
Not verified against the real service: that the service accepts a final append without the
delegation id, and what a voice model does with it. Tests: `tests/test_live_bridge.py`,
`tests/test_live_manager.py`, `tests/test_live_protocol.py`. This supersedes the "no
voice-authored handoff yet" gap above: the gap stays, the handoff no longer depends on it.

## GPT Live: Codex-style sideband (no client pings, unlimited backoff reconnect) (2026-10)

Decision: the sideband sends no client WebSocket pings, retries an early 404 on the initial join
(100/300/800 ms), and after an unclean loss reconnects with backoff 200 ms doubling to 5 s, the
counter reset by a 30 s stable connection, until a 404/410, an auth refusal, a clean close, stop or
the call deadline; our own addition is a 5-minute cap on continuous failure. Sends that never reached
the socket wait up to 10 s for the reconnect; a write that failed or timed out is still never replayed
(`live_send_ambiguous`). Reason: this is what OpenAI's Codex does (see
`plans/in-progress/GPT_LIVE_CODEX_REFERENCE.md` section 2: no client pings, `sideband.rs` reconnect loop,
`methods.rs` early-404 retry), and the old 2-attempt, never-reset budget ended a call on a brief blip.
Costs: a half-open socket is no longer caught by a pong deadline (only reads, failed writes, server
close); Codex replays a pending message after reconnect and we deliberately do not, because a replay can
duplicate a delegation. Tests: `tests/test_live_transport.py`. Not verified against the real service.

## GPT Live: pause is bounded mute, not a held-open call (2026-10)

Decision: `Ctrl+X P` / `/live pause` silences the microphone (helper sends silence) and the voice's audio, holds
voice-bound updates (<= 8, commentary dropped) and ends the call with reason `pause_timeout` after
`[live].pause_seconds` (120); a later `Ctrl+X P` starts a new call that carries the transcript over (the
existing handoff). Stop stays `Ctrl+X D`. `Ctrl+X F` was not used: it is Fork session in both clients.
Reason: OpenAI's Realtime costs guide bills GPT Live per second of session time including silence and says
muting "does not close the session", so a pause that keeps the call open keeps billing; and Nexus cannot close
the socket and keep the call (WebRTC media dies with the peer, no resumable call id). Codex has only mute, the
same silence-over-RTP behavior. Cost: a pause longer than the window loses the live voice context and is a new
call (transcript carried over, not the model's hidden state). Not verified: real billing on a ChatGPT-account
call, and a sideband cancel for a response already being produced. Details: [live.md](live.md#pause-vs-stop).
