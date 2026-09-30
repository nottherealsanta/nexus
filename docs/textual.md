# Textual app (`nexus chat`)

The terminal chat shell. It is a **pure host client**: every session, model,
agent, permission and turn operation is a host command over the Unix socket.
It renders the reduced `ConversationView` and never reads session files.
Background on the host is in [core.md](core.md).

## Launch path

`nexus chat` → `cli.py:_chat_entry` / `_chat` → creates a UDS host client
(`client/protocol.py:Client`) → `ui/tui/run.py:run(client, session=…, reconnect=…)`
→ `NexusTextualApp.run_async()`. It requires stdin/stdout TTYs. Non-interactive
callers use `nexus run` (`ui/cli/run.py`) or JSONL (`ui/jsonl.py`).

Textual is pinned (`textual==8.2.8`, `textual-diff-view==0.1.5` in `pyproject.toml`).

## File map

| File | What it owns |
| --- | --- |
| `ui/tui/app.py` | `NexusTextualApp` (mixes in `PanelsMixin`): layout (`compose`: `TopBar` over `MainLayout` = sessions sidebar · chat column · details sidebar · logs drawer), the `SHORTCUTS` table, slash-command dispatch, command palette, session switching, inline pickers, permissions, logs, reconnect, and `_sync_status`/`_sync_agent`/`_sync_timeline`. |
| `ui/tui/panels.py` | `PanelsMixin`: sidebar visibility (docked, or a transient overlay when too narrow), top bar sync and actions, `SessionList`/`Doctor` polling, archive/unarchive and delete/restore, the Sessions and Archived dialogs, Settings console, panel toggles. `MainLayout` relays resizes (see gotchas). |
| `ui/tui/extras.py` | Host-backed informational and Git workflow chat commands kept out of the shell controller. |
| `ui/tui/controller.py` | `TuiController`: the event bridge and reducer seam. `bootstrap`, `start_turn`, `resume`, `ingest(event)` (applies `view.reduce.apply`), `switch_session`, model/agent/effort selection with metadata refresh, `find_agent`, `cancel`. Holds `view: ConversationView`. |
| `ui/tui/timeline.py` | `ConversationTimeline.set_view(view)` reconciles `TurnWidget`s by durable IDs. `UserMessage` has a chevron that collapses the completed turn; `ThoughtLine` expands thinking; `AssistantMessage` streams Markdown; `ToolActivityWidget` renders one muted line (plus an inline diff for completed Edit/Patch calls, via `ui_support/tui_diff.py`) and opens `ToolDetailsScreen` for call/result inspection; `TaskActivityWidget` includes child-agent details and a modal link. The first timeline child is the context header. |
| `ui/tui/tool_details.py` | `ToolDetailsScreen`: bounded, scrollable modal for full tool call parameters, results, errors and diffs; Escape or a click outside returns focus to the transcript row. |
| `ui/tui/agent_picker.py` | `AgentPickerPanel` (inline picker for agents and effort; posts `Selected`/`Cancelled`) and the modal `AgentPicker`. |
| `ui_support/tui_model_picker.py` | Dedicated searchable `/model` modal, newest update/release by default (`Ctrl+S` toggles natural name order), with user-level favorites and recent selections. Dated models older than six calendar months are omitted; undated fallback entries remain available. |
| `ui_support/tui_setup.py` | First-run setup only asks to connect a provider (the Settings → Providers cards plus the env-key providers). The first connected provider (already connected ones count, Ollama never) is saved with its newest models.dev model via `SetupSave`, the daemon reloads its routes, and the screen closes into chat. It asks for a restart only while turns run. |
| `ui/tui/agent_transcript.py` | `AgentTranscriptScreen`: a full live page laid out like the root (`TopBar`, `ConversationTimeline` with its `ContextHeader`, a read-only composer row, `DetailsSidebar`). Clicking a Task card opens it directly (no details modal). The header shows the request the child actually sent (`AgentTranscript` → `context`): System prompt, then a grey Task block (`ContextHeader.set_task`, hidden at the root; the recorded prompt, else the Task call's input), then every tool definition; the timeline renders the child's `body`, so tools and messages look and expand exactly as at the root. Esc returns to the parent. |
| `ui/tui/agent_row.py` | Selectable row for one `AgentView`. |
| `ui/tui/permission.py` | `PermissionScreen` and `QuestionScreen`: list prompts (`ListPrompt` in `ui_support/tui_list.py`) docked above the composer like the pickers; `ask_pending_question` opens pending `question` calls. Arbitration stays daemon-side. |
| `ui/tui/messages.py` | Typed Textual messages (`EventReceived`, `TurnFinished`, `PermissionRequested`, `AgentPickerRequested`, …). |
| `ui/tui/keys.py` | `NexusDriver`: xterm `modifyOtherKeys` / Kitty CSI-u decoding, so Shift+Enter, Ctrl+Enter and Ctrl+J insert newlines. |
| `ui/tui/theme.py` | `NEXUS_DARK` / `NEXUS_LIGHT` (opencode palette). Both define the `nx-*` variables (`$nx-bg`, `$nx-panel`, `$nx-element`, `$nx-accent`, `$nx-blue`, …) that all CSS and markup use. |
| `ui/tui/app.tcss` | All Textual CSS for the shell. Colors only through `$nx-*` variables, so both themes work. |
| `ui/tui/run.py` | Entry point; passes `preferences_path()` = `$XDG_CONFIG_HOME/nexus/tui.json` (theme, panels, context preview). |
| `ui/tui/widgets.py` | Re-exports widgets from `ui_support/tui_widgets.py`. |
| `ui_support/tui_panels.py` | `TopBar` (sidebar toggle, session title, status, new, details toggle), `SessionSidebar`/`SessionRow` (one-line rows grouped by day like `/sessions`, an `Archived` group, spinner, visible Delete control), `DetailsSidebar`, `SessionsScreen`, base `SettingsScreen`, `TuiPreferences`, and pure panel helpers. |
| `ui_support/tui_widgets.py` | `ChatInput`/`ChatEditor` (composer, slash, argument and file completion, prompt history), `RootAgentBar`, `ActivityProgress`, `ConnectionStatus`, `ContextDetailsScreen` (Ctrl+I: summary, then a collapsible per group from `context.py:context_groups`), `context_group_widgets`/`ContextEntryWidget` (bodies mount as Markdown on first expand), `LogsDrawer`, and worktree screens. |
| `ui_support/tui_diff.py` | `ToolDiff`: one `textual_diff_view.DiffView` per file of an Edit/MultiEdit/Patch `diff` artifact (unified, auto-split when it fits). `ui_support/timeline.py:diff_sections` splits multi-file hunks on `---`/`+++` headers and pads to real line numbers. |
| `ui_support/tui_list.py` | Shared `ListPanel` and `ListItem` presentation for inline completions and pickers. |
| `ui_support/tui_context_header.py` | Scrollable context header, its detail modals (`ToolsModal` for the Tools block: `context.py:tool_groups`, one collapsed row per tool; `ContextModal` for the others), and pure grouping/schema text helpers. |
| `ui_support/tui_archived.py` | Archived-session search, preview, and resume dialog driven by host callbacks. |
| `ui_support/tui_providers.py` | `ProvidersPane`: Settings → Providers cards for Codex (browser or device code), GitHub Copilot (no sign-in; Nexus has no GitHub OAuth app) and OpenCode Go (password field for the API key); polls `ProviderLoginPoll` and opens sign-in URLs with `App.open_url`. |
| `ui_support/tui_settings.py` | Full-screen Settings page: a left sidebar (Appearance, Layout, Keyboard, Workspace, Agents, Tools, MCP, Skills, Hooks, Config, Soul) over the host `Settings*` commands. Agents get a "New sessions start with" row (`AgentDefaultSet` in the selected scope), one Model row and an ordered Fallbacks list above the prompt editor, each opening the shared `/model` picker (provider, model and effort together); built-ins save as `~/.nexus` overrides by default. |
| `ui_support/voice_capture.py`, `ui_support/tui_voice.py` | Bounded `sounddevice` PCM capture and `VoiceController`: Ctrl+Space toggles dictation; first use asks before downloading; transcript insertion uses the host client. An orange dot at the far left of the context-size row appears only while recording, without layout shifts or automatic loading or transcription labels. `/voice status|download|on|off` and Settings → Voice manage state. Install the optional dependency with `uv sync --extra voice`; weights download separately after confirmation. |
| `ui_support/agent_frontmatter.py` | Pure text helpers that read and rewrite an agent file's `model`/`provider`/`reasoning_effort`/`fallback` lines for the Settings form. |
| `ui_support/tui_history.py` | Bounded prompt history shared with the durable session view. |
| `ui_support/timeline.py`, `context.py`, `text.py` | Pure formatting: timeline filtering, context and usage projections (`context_measure` is the one source for the context meter; `context_groups`/`tool_groups` feed both dialogs and are ported to `ui/web/js/context-view.js`), control-safe and redacted text. |
| `ui/cli/commands.py` | Slash commands as data (`SPECS`, `BY_NAME`, `parse`, `help_text`). The TUI palette and the CLI share it. |
| `ui/cli/details.py` | `detail_lines(session, view)`, used by `/details` and the status line. |

Project-scoped settings and extensions are written under `<workspace>/.agents/`;
existing `.nexus/` project extensions and settings remain readable as a
lower-precedence fallback. Session records for all workspaces are stored in the
shared `~/.nexus/nexus.db` database, while caches and logs are machine state
under `~/.nexus/`.

## Look

- A one-row top bar spans the shell: `▌` toggles the sessions sidebar (`ctrl+b`), then the session title (`New session` before the first message), the session status, `+` for a new session, and `▐` for the details sidebar (`ctrl+l`). Toggles are accent-colored while their panel is visible.
- Every dialog closes on Escape and on a click outside it. `PanelsMixin.on_click` turns a click on a pushed screen's backdrop into an Escape key, so a new dialog only needs an Escape binding and gets no Close button.
- The sessions sidebar groups sessions by day exactly as the `/sessions` dialog does, one line each (glyph, title, status or age, `×`), with archived sessions last. When the terminal is too narrow to dock it (under 110 columns, or 170 with the details sidebar), the toggle opens it as an overlay that closes on Escape or on picking a session, without changing the saved preference.
- The timeline begins with the five-block context header (system prompt, tools, AGENTS.md, skills, MCP). Chips use the agent's color; an empty part shows only a grey chip. Body rows share one indent. A user message occupies a panel block with a chevron. Completed turns collapse and remount their bodies from the reduced view. Thinking expands inline; ordinary tool calls stay one muted, clipped summary line with consecutive calls tightly stacked. Subagent calls use two lines: type and description, then recent tool calls while running or tool-call count and elapsed time when finished. Selecting a tool opens its full bounded parameters/result in a modal, never inline. Errors are plain red lines; the reply footer shows agent, model, and duration.
- The composer has a transparent editor, agent/model/provider/effort controls, and context usage on the second row. Pasted text longer than 20 lines or 2,000 characters is kept in an editable attachment pill and expanded inline on submit. The activity bar shows context fill when idle and motion during a turn or reconnect.
- Ctrl+X is a leader key: press it, then `M` (model picker), `V` (start dictation), or one of the aliases in `LEADER_SHORTCUTS` (`?` lists them). While recording, any key stops and transcribes (the key is swallowed) and Esc discards. Ctrl+X no longer cuts in the composer on the main screen. `ui/tui/keychord.py:LeaderKeys` runs from `NexusTextualApp.on_event`.
- Ctrl+Space toggles bounded dictation; `/voice` is also available. On first use, explicitly confirm the model download; preparation is silent, and the modal offers a start action when ready. Only an orange dot at the far left of the context-size row indicates active recording, without shifting the line. Audio is sent to the daemon as validated WAV and the transcript is inserted as editable composer text. The optional `voice` extra and approximately 179 MB model download are separate steps.
- Slash, file, argument, agent, and effort lists share `ListPanel`: dim rows, grey selection, orange scrollbar, and a filter row when applicable. Model selection from `/model` or the composer opens one searchable modal with provider groups, favorites (`Ctrl+F`), recent models, sort order (`Ctrl+S`), and optional effort (`←/→`). The archived browser is a modal screen; Settings is a full-screen page.

## Common changes

- **New slash command:** add a spec to `ui/cli/commands.py`, then a branch in `NexusTextualApp._dispatch_chat_command`. The palette picks it up from `SPECS`.
- **New key binding:** add a row to `SHORTCUTS` in `tui_command_palette.py` (leader keys go in `LEADER_SHORTCUTS`). Keys the editor consumes are handled in `on_key` instead.
- **Show new data:** it must come from `ConversationView` (extend `view/model.py` and `view/reduce.py`) or from a host command. See "Adding a capability" in [core.md](core.md).
- **Tool card rendering:** `ToolActivityWidget` in `timeline.py`. Keep all subagent activity inline at the initiating Task call.
- **Styling:** `app.tcss` and `theme.py`. Spacing is covered by `tests/test_tui_spacing.py` and `tests/test_tui_layout.py`.
- **Gotcha: left bars use `outline-left` where retained.** In Textual 8.2.8 a left border breaks wrapping width on auto-height text blocks and clips characters.
- **Gotcha: `App.on_resize` never fires** (`App._on_resize` stops the event). Layout that depends on width goes through `MainLayout.on_resize` → `_sync_layout`.
- Side-panel polling must never break the shell: failures render as "unavailable". Test fakes reject unknown commands, so new host calls need a fake response (see `tests/test_tui_panels.py:PanelTransport`).
- Race hygiene: async work re-checks `self.controller.session` and `_agent_metadata_revision` before it applies results. Keep that pattern (`tests/test_tui_session_switch_race.py`).

## Testing

- Pilot tests: `tests/test_ui_tui.py`, `tests/test_tui_panels.py` (sidebars, Sessions dialog, Settings), `test_tui_*` (activity, completion, composer_agent, functional_journeys, integration_render, keys, layout, markdown, metadata, model picker, session switch race, spacing).
- Deterministic fixtures without a daemon: `tests/tui_acceptance_fixture.py`, `tests/visual_tui_demo.py` (`VisualDemoApp`, states such as `empty`, `transcript`, `permission`, `picker`, `functional`, `reference`).
- In a browser: `tests/browser_serve.py` serves the real shell through textual-serve and adds a Shift/Ctrl+Enter bridge. `tests/playwright_tui_check.py` drives it. `tests/visual_tui_check.py` captures screenshots to `artifacts/visual-tui/`.
- Against a scripted model through the real stack: `tests/mock_llm_serve.py` and `tests/playwright_mock_llm_check.py`.
- Screenshots against a live scripted daemon: run the app with `app.run_test(size=(200, 55))`, call `app.export_screenshot()` (SVG), and render it to PNG with Playwright. This is more reliable than textual-serve for layout checks.
- The layering rule (`tests/test_ui_layering.py`) keeps Textual imports inside `ui/tui/` and `ui_support/`, and caps the pure-client files (`ui/cli/`, `jsonl.py`, `tui/app.py`) at 2,400 lines. Put new shell behavior in its own `ui/tui/` module, as `panels.py` does. The whole `ui/` budget is 5,000 physical lines (`tests/test_phase3_exit.py`).

## Dev mode and `/mock`

`nexus --dev chat` (or `NEXUS_DEV=1`) runs the shell against an isolated home
(`~/.nexus/dev`) and a seeded sandbox workspace, never your real one. `/mock`
exists only in dev mode (`commands.DEV_SPECS`, added to `SPECS` at import when
`NEXUS_DEV` is set):

- `/mock` lists scenarios; `/mock NAME [--speed N] [--seed N]` starts one in a
  new `mock-NAME-N` session, switches to it and follows the live run
  (`ui/tui/mock.py`, routed through `ExtraCommandsMixin`); `/mock clean`
  restores the sandbox.
- Every scenario ends with an in-band `✓ mock verdict — …` message.
- Setup is never required in dev mode (mock models need no provider).

See `plans/MOCK_PLAN.md` and `nexus/devtools/mock/`.

## Settings saving and reset

Settings changes save automatically. In the terminal file editor, edits save
700 ms after typing stops and flush before changing item, page or scope or
closing Settings. Validation and disk conflicts leave the edit unsaved with
an error; leaving then asks whether to discard invalid changes. Built-in agent
edits create scoped overrides, and new items save their template on naming.

GENERAL and CONFIGURE separate the sidebar groups. Page headings offer a
confirmed Reset to default where settings exist: appearance, terminal layout,
voice, agents, and terminal file categories. Agents reset only built-in overrides
and the scoped default-agent choice; custom agents stay. Tools and Skills reset
all custom items in the selected scope and list them before confirmation.
Removed files and previous config values move to settings trash. Keyboard,
Workspace and Providers have no reset. Browser conversation detail resets the
current session, workspace and browser preferences together; appearance resets
to System. Full-size controls share a common height.
