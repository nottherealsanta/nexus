# Textual app (`nexus chat`)

The terminal chat shell. It is a **pure host client**: every session, model,
agent, permission and turn operation is a host command over the Unix socket, and
it renders the reduced `ConversationView`. It never reads session files.
Behavior shared with the browser is in [surfaces.md](surfaces.md); host contract
in [host.md](host.md).

## Launch path

`nexus chat` → `cli.py:_chat_entry` / `_chat` → a UDS host client
(`client/protocol.py:Client`) → `ui/tui/run.py:run(client, session=…, reconnect=…)`
→ `NexusTextualApp.run_async()`. Needs stdin/stdout TTYs; non-interactive callers
use `nexus run` or JSONL. Textual is pinned (`textual==8.2.8`,
`textual-diff-view==0.1.5` in `pyproject.toml`).

## File map

| File | Owns |
| --- | --- |
| `ui/tui/app.py` | `NexusTextualApp` (mixes in `PanelsMixin`): `compose` (`SessionTabs` and `TopBar` over `MainLayout` = sessions sidebar · chat column · details sidebar · logs drawer), the `SHORTCUTS` table, slash dispatch (`_dispatch_chat_command`), command palette, session switching, inline pickers, permissions, reconnect, `_sync_status`/`_sync_agent`/`_sync_timeline` |
| `ui/tui/panels.py` | `PanelsMixin`: sidebar visibility (docked or transient overlay when narrow), top-bar sync (`_sync_tabs`: which sessions are tabs; tab open/close), `SessionList`/`Doctor` polling, archive/delete/restore, Sessions and Archived dialogs, Settings console, panel toggles; `MainLayout` relays resizes |
| `ui/tui/controller.py` | `TuiController`: event bridge and reducer seam: `bootstrap`, `start_turn`, `resume`, `ingest(event)` (applies `view.reduce.apply`), `switch_session`, model/agent/effort selection, `cancel`; holds `view: ConversationView` |
| `ui/tui/timeline.py` | `ConversationTimeline.set_view(view)` reconciles `TurnWidget`s by durable ids; `UserMessage` (chevron collapse, right-aligned turn number, attachment chips), `ThoughtLine` (`◇` headline, hidden-reasoning label), the `◆ Agent` label, `AssistantMessage` (streaming Markdown), `ToolActivityWidget` (one muted line plus an inline diff for completed Edit/Patch, a live output tail for a running shell), `EmptyHints`, `TaskActivityWidget` (child-agent details and modal link); the first child is the context header |
| `ui/tui/tool_details.py` | `ToolDetailsScreen`: bounded, scrollable modal over `ui_support/tool_details.py`; Esc or outside click returns focus to the row |
| `ui/tui/agent_picker.py` | `AgentPickerPanel` (inline agents/effort picker) and the modal `AgentPicker` |
| `ui/tui/agent_transcript.py` | `AgentTranscriptScreen`: a live child page laid out like the root |
| `ui/tui/agent_row.py` | selectable row for one `AgentView` |
| `ui/tui/permission.py` | `PermissionScreen`, `QuestionScreen` (list prompts docked above the composer), `ask_pending_question` |
| `ui/tui/new_session.py` | `/new`: pick the root agent a session starts with |
| `ui/tui/extras.py` | host-backed informational and Git chat commands kept out of the controller |
| `ui/tui/mock.py` | `/mock` (dev mode) |
| `ui/tui/messages.py` | typed Textual messages (`EventReceived`, `TurnFinished`, `PermissionRequested`, `AgentPickerRequested`, …) |
| `ui/tui/keys.py` | `NexusDriver`: xterm `modifyOtherKeys` / Kitty CSI-u decoding so Shift+Enter/Ctrl+J insert newlines and Ctrl/Alt+Enter steer/interrupt |
| `ui/tui/keychord.py` | `LeaderKeys` (Ctrl+X leader) and the "any key stops dictation" rule; runs from `on_event` |
| `ui/tui/theme.py` | `NEXUS_DARK` / `NEXUS_LIGHT` (opencode palette), defining the `nx-*` variables (`$nx-bg`, `$nx-panel`, `$nx-element`, `$nx-accent`, `$nx-blue`, …) |
| `ui/tui/app.tcss` | all CSS; colors only through `$nx-*` so both themes work |
| `ui/tui/run.py` | entry; preferences at `$XDG_CONFIG_HOME/nexus/tui.json` (theme, panels, context preview) |
| `ui/tui/widgets.py` | re-exports `ui_support/tui_widgets.py` |

The system-prompt modal renders literal, wrapping text rather than Markdown,
matching the browser client. XML-style blocks such as `<environment>` and their
workspace/platform/profile contents remain visible; prompt syntax is not interpreted
as HTML or Rich markup.

Textual-dependent support modules in `ui_support/` (the only ones allowed to
import Textual besides `ui/tui/`):

| File | Owns |
| --- | --- |
| `tui_widgets.py` | `ChatInput`/`ChatEditor` (slash, argument and file completion, history), `RootAgentBar`, `ActivityProgress`, `ConnectionStatus`, `ContextDetailsScreen` (Ctrl+I), `ContextEntryWidget` (bodies mount on first expand), `LogsDrawer`, worktree screens |
| `tui_panels.py` | `SessionTabs`/`SessionTab` (row 1), `TopBar` (breadcrumb + status, row 2), `SessionSidebar`/`SessionRow` (two-line cards), `DetailsSidebar`, `SessionsScreen`, base `SettingsScreen`, `TuiPreferences` |
| `tui_context_header.py` | scrollable context header (token estimates, one-line previews) and its modals (`ToolsModal` family table, `SkillsModal`, `ExtensionsModal` for MCP, `ContextModal`) |
| `tui_list.py` | shared `ListPanel`/`ListItem` (completions and pickers): dim rows, grey selection, orange scrollbar, filter row |
| `tui_model_picker.py` | fuzzy-searchable (`fuzzy.py`), grouped `/model` modal with favorites, recents, sort, `↻`/Ctrl+R refresh (`ModelsRefresh`) |
| `tui_command_palette.py` | palette entries and the `SHORTCUTS`/`LEADER_SHORTCUTS` reference |
| `tui_settings.py` | full-screen Settings page over `Settings*` commands |
| `tui_setup.py`, `tui_providers.py` | first-run setup; Providers pane (Claude card takes a pasted sign-in code) |
| `tui_archived.py` | archived-session search/preview/resume dialog |
| `tui_diff.py` | `ToolDiff`: one `textual_diff_view.DiffView` per file (split view: original left, updated right; plain filename titles) |
| `tui_history.py` | bounded per-user prompt history |
| `tui_voice.py`, `voice_capture.py` | `VoiceController`, the floating live `VoiceStrip` (`#voice-strip` in `#main-column`'s overlay layer); bounded `sounddevice` capture with `snapshot()` |

## Terminal-specific behavior

Context detail bodies escape control characters before rendering, including
literal system-prompt text, so terminal escape sequences cannot affect display.

- Sidebars dock at ≥ 110 columns (170 with both open); otherwise the toggle opens an
  overlay closed by Escape or picking a session, without changing the saved preference.
- `PanelsMixin.on_click` turns a click on a pushed screen's backdrop into Escape,
  so a new dialog needs only an Escape binding.
- The composer has a transparent editor. Pasted text over 20 lines or 2,000 chars
  becomes an editable attachment pill expanded inline on submit.
- The activity bar shows context fill when idle (with a `┃` at each price-tier
  threshold) and motion during a turn or reconnect. The Logs drawer lists
  problems first and folds info/debug lines behind a counted toggle. Thinking expands inline.
- Ctrl+X no longer cuts in the composer on the main screen.
- Dictation runtime comes from the `voice` extra, which the installer adds except on musl ([voice.md](voice.md)).

## Common changes

| Change | Where |
| --- | --- |
| New slash command | spec in `ui/cli/commands.py` + a branch in `_dispatch_chat_command`; the palette reads `SPECS`; mirror it in the web ([cli.md](cli.md)) |
| New key binding | row in `SHORTCUTS` (leader keys in `LEADER_SHORTCUTS`); keys the editor consumes go in `on_key` |
| Show new data | extend `view/model.py` + `view/reduce.py` or add a host command ([host.md](host.md)) |
| Tool card rendering | `ToolActivityWidget` in `timeline.py`; keep subagent activity inline at the Task call |
| Styling | `app.tcss`, `theme.py`; spacing is covered by `test_tui_spacing.py` / `test_tui_layout.py` |

## Gotchas

- **Left bars use `outline-left`.** In Textual 8.2.8 a left border breaks wrapping
  width on auto-height text and clips characters.
- **`App.on_resize` never fires** (`App._on_resize` stops the event). Width-
  dependent layout goes through `MainLayout.on_resize` → `_sync_layout`.
- **Side-panel polling must never break the shell:** failures render as
  "unavailable". Test fakes reject unknown commands, so a new host call needs a
  fake response (`tests/test_tui_panels.py:PanelTransport`).
- **Keep event handling cheap.** `presence.*` events update only the top bar,
  and `TurnWidget.set_turn` returns early for a turn the reducer shared
  unchanged (same object, agents, flags). Before this, every presence event on
  stream attach re-reconciled every turn, delaying a submitted prompt by up to
  ~0.6s in long sessions (`tests/test_tui_submit_latency.py`).
- **Race hygiene:** async work re-checks `self.controller.session` and
  `_agent_metadata_revision` before applying results
  (`tests/test_tui_session_switch_race.py`).
- New shell behavior goes in its own `ui/tui/` module (as `panels.py` does), not
  into `app.py`; there are no line caps.

## Testing

- Pilot tests: `tests/test_ui_tui.py`, `test_tui_panels.py`, and the `test_tui_*`
  family (activity, completion, composer agent, functional journeys, integration
  render, keys, layout, markdown, metadata, model picker, session switch race,
  spacing, voice, …). CI skips the timing-sensitive pilot files; run them locally
  before committing ([testing.md](testing.md)).
- No-daemon fixtures: `tests/tui_acceptance_fixture.py`, `tests/visual_tui_demo.py`
  (`VisualDemoApp`, states `empty`, `transcript`, `permission`, `picker`,
  `functional`, `reference`).
- In a browser: `tests/browser_serve.py` serves the real shell through
  textual-serve with a Shift/Ctrl+Enter bridge; `tests/playwright_tui_check.py`
  drives it; `tests/visual_tui_check.py` captures to `artifacts/visual-tui/`.
  `tests/playwright_context_controls_check.py` exercises skill/MCP controls at
  wide and narrow sizes (screenshots in `artifacts/context-controls/`).
- Against a scripted model through the real stack: `tests/mock_llm_serve.py`,
  `tests/playwright_mock_llm_check.py`.
- Reliable layout screenshots: `app.run_test(size=(200, 55))`, then
  `app.export_screenshot()` (SVG) rendered to PNG with Playwright.

## File and image input

`/attach <path>` attaches a local file (`/attach clear` removes pending
attachments). The browser also has an Attach file button and accepts image/file
paste and drag/drop in the composer. Expand an attachment to inspect it before
sending; the TUI opens converted documents in a scrollable Markdown preview.
Ctrl+V in the terminal editor reads a local clipboard image on macOS, or on
Linux with `wl-paste` (Wayland) or `xclip` (X11), and shows a pending
`clipboard.png` attachment. Text paste retains its existing behavior. This
requires the terminal to pass Ctrl+V to Nexus; terminal-managed paste shortcuts
only send text. Remote SSH clipboard images and Windows are not supported; use
`/attach <path>` there. Native macOS pasteboard access was verified; clipboard image conversion is
covered with fakes and has not been verified with a live image.

Enter submits attachments even without prompt text; queue, steer, and interrupt
use the same attachment path. Switching sessions clears pending attachments.

PNG, JPEG, GIF, and WebP stay image blocks for vision-capable models (labelled
metadata in the terminal, visible previews in the browser, also after
reconnect). AnyDoc converts PDF, Word, PowerPoint, Excel, OpenDocument, RTF, EPUB
and CSV to Markdown and is installed by default. Text/source files are included
directly, including Unicode BOM encodings. Unsupported binaries, malformed
documents and scanned PDFs needing hosted OCR (disabled) give a visible error.
Limits: 8 MiB per file, eight per message, 12 MiB encoded combined. Prepared
drafts expire after one hour; submitted content stays in the durable log.

Validation: `tests/test_attachments.py`, `tests/test_tui_attachments.py`,
`tests/playwright_attachments_check.py`.

Escape and Ctrl+C dismiss open dialogs and Settings (including nested screens)
and restore focus on the main conversation. On the main conversation, two Escape
presses within 1.5 seconds cancel the active turn and return pending queued messages to the composer via
`SessionCancel(return_queue=True)`. Messages keep queue order, separated by blank
lines, followed by any existing unsent draft; they no longer run automatically.
A single Escape shows a stop hint. Ctrl+C retains immediate
turn cancellation on the main conversation.

Attachments insert editable `image 1`, `image 2`, or `document 1`,
`document 2` references at the composer cursor (each kind is numbered separately
within a draft). Preview rows show the same reference and filename. Removing a
browser attachment does not renumber remaining references. Submitted attachment
metadata carries the same labels beside the image bytes or complete document text
in the durable user message, so references in sentences stay meaningful on replay.

Submitted messages separate the prompt sentence from numbered attachment rows.
The browser shows labelled image thumbnails and expandable full document cards,
also in request-context messages. The terminal shows compact labelled rows;
clicking the message body opens the complete attached text and image metadata.
