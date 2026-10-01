# Surfaces: what every client does the same way

Three clients sit on the host contract: the Textual app (`nexus chat`,
[textual.md](textual.md)), the browser app (`nexus web`, [web.md](web.md)) and
the one-shot CLI/JSONL ([cli.md](cli.md)). This page holds the behavior that must
be identical across them. **A feature or wording change in one surface goes into
the other.** The web app mirrors the TUI: same places, same commands, same
Control-key shortcuts; it may look more modern but must behave the same.

## Principles

1. **Present context clearly.** Show everything that matters (every parameter,
   every output), labelled and readable, never a raw JSON dump, and never hide
   from the user what the agent can see. Clipping is always announced.
2. **Render from the reduced view.** `ConversationView` plus host commands, never
   files, managers or ad-hoc state ([events-and-view.md](events-and-view.md)).
3. **Host contract only.** New data or actions are host commands
   ([host.md](host.md#adding-a-capability-a-ui-can-use)).
4. **Durable state survives reconnect.** A turn started in the terminal is live
   in the browser and vice versa; closing a view never stops work.
5. **Everything from the host is untrusted text.** Control-safe and redacted in
   the terminal; `textContent`/escaped Markdown in the browser.
6. **Tolerate stale async results.** Compare session and request ids before
   applying a late response.

## Shared code (`ui_support/`)

Pure presentation helpers, importable by all surfaces. The TUI-only widgets
(`tui_*.py`) are the only `ui_support` files that may import Textual.

| Module | Shared logic | Web port |
| --- | --- | --- |
| `tool_details.py` | labelled tool-call sections (Overview, Parameters flattened to `a.b[0]` rows, Code, Progress, Summary, Result blocks, Error, Context, Metrics, Diff) | `ui/web/js/tool-details.js` |
| `context.py` | `context_groups`, `tool_groups`, `context_summary`, `context_measure` (the one source for the context meter), `context_usage`, `thinking_status` | `ui/web/js/context-view.js` |
| `timeline.py` | tool headings/summaries, batch glyphs, diff splitting (`split_diff_files`, `diff_sections`), thought titles | `renderTool`, `diffFiles`, `splitDiff` |
| `prompts.py` | `approval_choices` (four decisions, unavailable ones disabled), pending questions | `approvalChoices` |
| `text.py` | control-safe, redacted text | `el(tag, cls, text)` |
| `agent_frontmatter.py` | read/rewrite agent frontmatter lines for the Settings form | `settings-files.js` |
| `ui/cli/commands.py` | slash commands | `SLASH_COMMANDS` |

## Layout (both surfaces)

- One-row **top bar**: `▌` toggles the sessions sidebar (`Ctrl+B`), the session
  title (`New session` before the first message), status, `+` new session, `▐`
  toggles the details sidebar (`Ctrl+L`). Toggles are accent-colored while open.
- **Sessions sidebar** (34 cells / 272px): one line per session (glyph, title,
  status or age, `×`), grouped by day like `/sessions`, archived last. Glyphs:
  `●` current, braille spinner while working, `✓` done, `·` idle, `◇` archived.
- **Conversation:** the **context header** opens every conversation (System
  prompt, Tools, AGENTS.md, Skills, MCP), then the timeline.
- **Composer:** editor, then `Agent  model provider  effort`, then context
  `3k (2%)`; the activity bar under it.
- **Details sidebar** (42 cells / 336px): `SESSION` rows (Status, Agent, Model,
  Effort, Turns, Tool calls, Tokens, Context), `MODIFIED FILES`, `MCP SERVERS`.
- Panels dock while there is room and otherwise open over the conversation.
- Every dialog closes on Escape and on a click outside it; dialogs have no Close
  button.

## Conversation rendering

- `▼`/chevron user blocks fold a completed turn; thinking shows as `Thought:`
  lines (expandable); provider thinking summaries are never invented.
- Ordinary tool calls are one muted, clipped summary line, consecutive calls
  tightly stacked; calls in one model iteration share batch glyphs (`⎾ │ ⎿`).
  Activating a call opens its full parameters/result in a modal, never inline.
- **Subagent calls** use two lines: type and description, then recent tool calls
  while running, or tool count and elapsed time when finished. Clicking opens the
  child's page, laid out like the root (its own context header showing the request
  it actually sent, a grey Task block, a read-only composer, details panel); Esc
  returns. All subagent activity stays inline at the initiating call.
- Completed Edit/Patch rows show a diff per file (side by side when wide, unified
  when narrow), from the durable `ToolCallView.diff` only.
- Errors are plain red lines; each finished turn has a footer
  `AGENT · model · 1.2s`. Agent color: the host's `color`, else a name hash.

## Context presentation

- The context header blocks open grouped, collapsed dialogs with token estimates
  (~4 chars/token, `estimate_tokens`). The System prompt opens as Markdown; the
  Tools block lists one collapsed row per tool (`tool_groups`: by `group`, then a
  group per MCP server).
- **Skill and MCP controls.** Those two blocks show `Project N | Global N`
  counts of discovered entries (including ones switched off). Clicking opens
  individual On/Off controls with their scope. Choices are saved for the session
  (`ContextExtensionSelect`), survive reconnect, and can change until the first
  turn; afterwards controls are disabled with a prompt-cache explanation. Root
  agent picking/cycling follows the same rule. Subagent headers are read-only.
- The meter shows the provider's measured prompt size whenever one exists
  (`context_measure`), else the estimate, never inventing a number. The latest
  thinking heading appears beside it while a model thinks.

## Messages during a turn

Enter **queues** a message as a new turn. `Ctrl+Enter` **steers** the active turn
at the next model step, after the current operation. `Alt+Enter` **interrupts**
and sends first, keeping other queued messages. `Shift+Enter` and `Ctrl+J`
insert a newline in the terminal (`Shift+Enter` only in the browser). Pending
input is durable and visible after reconnecting ([loop.md](loop.md#steering-queue-interrupt)).

## Keyboard

The source is `SHORTCUTS` in `ui/tui/app.py` / `tui_command_palette.py`:
`Ctrl+P/N/O/F/G/B/L/S/I/T/E/C/R`, `Shift+Tab`, `a`, `Esc`; `Ctrl+Space` toggles
dictation. **`Ctrl+X` is a leader**: then `M` (model picker), `V` (dictation), or
an alias from `LEADER_SHORTCUTS` (`?` lists them). The model picker is one
searchable modal with provider groups, favorites (`Ctrl+F`), recents, sort
(`Ctrl+S`), a `↻`/`Ctrl+R` catalogue refresh (`ModelsRefresh`) and optional effort
(`←/→`); dated models older than six calendar months are omitted (undated
fallbacks stay).

## Approvals and questions

Shown as list prompts docked above the composer (TUI `PermissionScreen` /
`QuestionScreen`; web `renderApprovals`). Choices come from `approval_choices`
(`y` once, `a` always, `n` deny once, `d` deny always, or `1–3` for questions).
While an approval is pending the background is inert. Arbitration is daemon-side
(first responder wins); with no viewer attached `on_unattended` applies.

## Settings

Settings is a full-screen page (TUI) or dialog (web) with the same sections:
GENERAL (Appearance, Layout, Conversation detail, Keyboard, Workspace, Providers,
Voice) and CONFIGURE (Agents, Tools, MCP, Skills, Hooks, Config, Soul). File
categories share a global/project scope, a list and an editor over the `Settings*`
commands ([extensions.md](extensions.md#settings-files-host)).

- **Autosave:** edits save 700 ms after typing stops and flush before changing
  item, page or scope, or closing. Validation and disk conflicts leave the edit
  unsaved with an error; leaving asks whether to discard invalid changes. New
  items save their template on naming; editing a built-in agent creates a scoped
  override.
- **Reset:** headings offer a confirmed *Reset to default* where settings exist
  (appearance, layout, voice, agents, file categories). Agents reset only
  overrides and the scoped default-agent choice; Tools and Skills list every
  custom item before removing it. Removed files move to settings trash.
  Keyboard, Workspace and Providers have no reset. Browser conversation detail
  resets session, workspace and browser preferences together; appearance resets
  to System.
- **Agents page:** a "New sessions start with" row (`AgentDefaultSet`), one Model
  row and an ordered Fallbacks list above the prompt editor, each opening the
  shared model picker (provider, model and effort together).
- **Providers:** cards for Codex (browser or device code), GitHub Copilot (device
  code) and OpenCode Go (password field); polls `ProviderLoginPoll`.
- **First-run setup:** only asks to connect a provider; the first connected one
  is saved with its newest model via `SetupSave` and the screen closes into chat
  (a restart is asked for only while turns run). Never required in dev mode.

## Dictation

`Ctrl+Space` toggles bounded dictation, `/voice status|download|on|off` and
Settings → Voice manage it. First use asks before downloading the model;
preparation is silent. An orange dot at the far left of the context-size row shows
only while recording, without shifting layout. Any key stops and transcribes (the
key is swallowed); `Esc` discards. The transcript is inserted as editable composer
text. Details: [voice.md](voice.md).

## Dev mode

`nexus --dev chat|web` (or `NEXUS_DEV=1`) uses an isolated home and sandbox
workspace and exposes `/mock` (list, run `NAME [--speed N] [--seed N]`, `clean`);
the web shows a `DEV` badge (from `Health.dev`). See [devtools.md](devtools.md).

## Adding a user-visible feature

1. Host command or reducer field first ([host.md](host.md), [events-and-view.md](events-and-view.md)).
2. Shared logic in `ui_support/` if it is pure; then the TUI (`ui/tui/`) and the
   web (`ui/web/js/`) in the same place.
3. A check in each: a Textual pilot test and `tests/playwright_web_check.py`
   ([testing.md](testing.md)).
