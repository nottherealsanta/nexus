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

The desktop's latest visual direction uses neutral greys, curved controls and
compact activity trees, following the supplied native references. Its composer
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

The 26 elements in `design-mockups/` were judged variant by variant and the
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

## Settings revamp (native client)

Workspace, Config, Soul and Hooks are hidden from the native Settings list for now;
Settings is left only by an explicit close, never by an operation kind missing from
an allow-list. A run-mode switch on an agent removes the other mode's fields so the
file never holds a model and tiers that contradict each other. Not verified: native
PTY rendering of the revamp.
