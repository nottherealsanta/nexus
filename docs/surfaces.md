# Surfaces: what every client does the same way

Four surfaces sit on the host contract: the native Ratatui terminal app (`nexus
chat`), the GPUI desktop app (`nexus desktop`,
[desktop.md](desktop.md)), the browser app (`nexus web`, [web.md](web.md)) and
the one-shot CLI/JSONL ([cli.md](cli.md)). This page holds shared behavior and
context contracts. The browser is being deprecated: preserve existing behavior,
but new terminal/desktop features need not be ported unless requested.

## Principles

The additional [GPUI desktop client](desktop.md) follows these host and context
contracts while using desktop typography, native text input and window controls.
It reuses the native terminal workflows; adding it does not require new web work.

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

Pure presentation helpers, importable by all surfaces. Rich is used only by
`context.py` to render context Markdown.

| Module | Shared logic | Web port |
| --- | --- | --- |
| `tool_details.py` | labelled tool-call sections (Overview, Parameters flattened to `a.b[0]` rows, Code, Progress, Summary, Result blocks, Error, Context, Metrics, Diff) | `ui/web/js/tool-details.js` |
| `context.py` | `context_groups`, `tool_groups`, `context_summary`, `context_measure` (the one source for the context meter), `context_usage`, `thinking_status` | `ui/web/js/context-view.js` |
| `timeline.py` | tool headings/summaries, batch glyphs, diff splitting (`split_diff_files`, `diff_sections`), thought titles | `renderTool`, `diffFiles`, `splitDiff` |
| `prompts.py` | `approval_choices` (four decisions, unavailable ones disabled), pending questions | `approvalChoices` |
| `text.py` | control-safe, redacted text | `el(tag, cls, text)` |
| `agent_frontmatter.py` | read/rewrite agent frontmatter lines for the Settings form | `settings-files.js` |
| `usage.py` | provider usage wording: `bar`, `tone` (ok < 70 % ≤ warn < 90 % ≤ critical), `summary` (`41% used · 59% left · resets in 6d 5h (Wed Oct 7 17:05)`), `heading` | `ui/web/js/usage.js` |
| `ui/cli/commands.py` | slash commands | `SLASH_COMMANDS` |

## Layout (both surfaces)

- Two-row **top bar**. Row 1, **session tabs**: `▌` toggles the sessions
  sidebar (`Ctrl+B`), then one tab per active session (status glyph, the
  title's first 36 characters, `×`), `+` new session, `▐` toggles the details
  sidebar (`Ctrl+L`). Tabs hold the current session, any opened in this window,
  and any session that is working, needs input or finished unseen; `×` only
  hides a tab (the session keeps running) and a closed tab returns when its
  status changes. Closing the current tab opens its neighbour. Row 2,
  **breadcrumb**: the workspace directory (`~/…`) `›` linked worktree `›` `⎇`
  branch (from the Doctor report's `git`), and the status in words on the right
  (`Idle`, `Working`, `Needs input`); the glyph is on the tab. Toggles are
  accent-colored while open. The web keeps its Context/Logs/Export buttons on
  row 2.
- **Sessions sidebar** (34 cells / 272px): `+ New session`, a filter, then a
  two-line card per session (glyph and title; status in words or the message
  count, then age; `×`), grouped by project folder, then local activity date,
  newest project and sessions first; archived sessions for the active project
  appear last. Equal folder names show their full paths. Filtering matches
  titles, ids and project paths. The shared index shows up to 1,000 sessions
  and announces truncation. Other projects show saved activity and message
  counts; their live status is available after opening them. Opening one
  connects to its owning workspace; delete/archive apply to the active project. The current session has
  a left accent bar. Glyphs: braille spinner while working, `●` needs input,
  `✓` done, `·` idle, `◇` archived.
- **Conversation:** the **context header** opens every conversation (System
  prompt, Tools, AGENTS.md, Skills, MCP), then the timeline.
- **Composer:** editor (no border, no key-hint row), then
  `Agent  model provider  effort`, then context `3k (2%)` (plus `price ↑ at
  272K` for a tiered model); the activity bar under it marks the price-tier
  threshold (`┃`).
- **Empty session:** a few grey tips (keys and what they do, `hints.py`, picked
  per session) sit in the middle of the timeline and hide while you type.
- **Details sidebar** (42 cells / 336px): `SESSION` rows (Status, Agent, Model,
  Effort, Turns, Tool calls, Tokens, Context), `MODIFIED FILES`, `MCP SERVERS`.
- Panels dock while there is room and otherwise open over the conversation.
- Every dialog closes on Escape and on a click outside it; dialogs have no Close
  button.

## Conversation rendering

Tool headers, subagent metrics, thought headings, assistant prose and turn
footers share the same left inset in both surfaces.

- User blocks: `▼`/chevron folds a completed turn; the turn number (`#3`) sits
  right-aligned and highlighted on the first row; attachments are highlighted
  chips (`image 1 · shot.png`, `document 1 · spec.pdf · 12.4 KB`: images drop
  their byte size, documents keep the size of their text).
- Thinking is one headline row, `◇ first sentence ▸` (expand for the full
  text); a provider that reasons without sharing it shows `◇ Thought · not
  shared by the provider`. Provider thinking summaries are never invented.
- The first reply of a turn sits under its agent label (`◆ Build`, agent
  color), indented two cells.
- Ordinary tool calls are one muted, clipped summary line, consecutive calls
  tightly stacked; calls in one model iteration share batch glyphs (`⎾ │ ⎿`).
  Read and Grep show arguments without appending a repeated result summary.
  Todo shows up to five item rows; longer lists show the first four and `X more`.
  Activating a call opens its full parameters/result in a modal, never inline.
  A **running shell** (Bash) additionally shows its latest four output lines
  under the call (`⎿`) with the earlier lines counted, until it completes.
- **Subagent calls** use two lines: type and description, then recent tool calls
  while running (no elapsed time), or tool count and elapsed time when finished.
  Subagent metrics elsewhere also omit duration while the child is working. Clicking opens the
  child's page, laid out like the root (its own context header showing the request
  it actually sent, a grey Task block, a read-only composer, details panel); Esc
  returns. All subagent activity stays inline at the initiating call.
- Completed Edit/Patch rows show a diff per file (original on the left, updated on the right at every width), from the durable `ToolCallView.diff` only.
- Errors are plain red lines; each finished turn has a right-aligned footer
  `model · 1.2s · ↑64K ↓3.1K · 81% cached · 800 r` (parts with no data are left
  out; `r` is the turn's reasoning token count, whether or not the provider
  shared the reasoning text). Agent color: the host's `color`, else a name hash.

## Context presentation

- Context header blocks are left-aligned label chips with their token estimate
  in grey (~4 chars/token, `estimate_tokens`). The System prompt and AGENTS.md
  preview one line (the rest counted). The System prompt dialog is the literal
  prompt without AGENTS.md (it has its own block) and shows its token count.
  Incomplete or clipped snapshots retain the original text to avoid losing
  context.
- The **Tools** dialog is a table: `BUILT-IN TOOLS`, then `MCP`, one row per
  family (swatch, name, every tool name, tokens). A row expands to its tools
  (name, parameter count and summary, tokens); a tool opens its description,
  parameters and schema.
- The **Skills** dialog has a sidebar of skills (`●` on / `○` off, scope) and
  the selected skill's SKILL.md rendered as Markdown (read with
  `SettingsRead`; the index description stands in, labelled, when it cannot be
  read), with its On/Off switch. MCP keeps the On/Off list.
- The **Context** dialog (Ctrl+I) opens with a recorded **usage by turn** table
  (context size, input, cache read/write, output, reasoning, time, total), then
  the estimated next request. For a tiered model it notes the price per tier.
- **Skill and MCP controls.** Those two blocks show `Project N | Global N`
  counts of discovered entries (including ones switched off). Clicking opens
  individual On/Off controls with their scope (tools too; switched-off tools stay listed so they can be switched back on). Choices are saved for the session
  (`ContextExtensionSelect`), survive reconnect, and can change until the first
  turn; afterwards controls are disabled with a prompt-cache explanation. Root
  agent picking/cycling follows the same rule. Subagent headers are read-only.
- The meter shows the provider's measured prompt size whenever one exists
  (`context_measure`), else the estimate, never inventing a number. The latest
  thinking heading appears beside it while a model thinks. When the model's
  price rises with prompt size (models.dev `cost.tiers`, carried in the request
  context as `pricing`), the meter marks each threshold.

## Messages during a turn

Enter **steers** the active turn at the next model step, after the current
operation (or starts a new turn when idle). `Ctrl+Enter` explicitly **queues**
a message as a new turn. `Alt+Enter` **interrupts**
and sends first, keeping other queued messages. `Shift+Enter` and `Ctrl+J`
insert a newline in the terminal (`Shift+Enter` only in the browser). Pending
input is durable and visible after reconnecting ([loop.md](loop.md#steering-queue-interrupt)).

## Keyboard

The source is `ui_support/shortcuts.py`:
`Ctrl+P/N/O/F/G/B/L/S/I/T/E/U/C/R`, `Shift+Tab`, `a`, `Esc`; `Ctrl+Space` toggles
dictation. `Ctrl+U` (also `Ctrl+X U` and `/usage`) opens the provider usage modal:
one section per connected provider with a bar per limit window, its reset,
notes and source, plus who is not connected; `r`/Refresh re-reads it. In the
TUI it takes precedence over the composer's delete-to-line-start. **`Ctrl+X` is a leader**: then `M` (model picker), `V` (dictation), or
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
Models, Session titles, Voice) and CONFIGURE (Agents, Tools, MCP, Skills, Hooks, Config, Soul). File
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
- **Models:** one row per tier (`low`, `medium`, `high`, custom): its source
  (`your list` / `built-in` / `by price`), how many models, and the model it runs
  on now (or "no runnable model"). A tier opens an ordered list of models: move up
  or down, remove, `+ Add model` (the shared picker), *Reset to default*. Changes
  save at once (`ModelTierSet`/`ModelTierReset`, global config); the daemon
  reloads routes while no turn runs, otherwise the notice asks for a restart. The
  last model cannot be removed (use Reset). A row "Highest tier for subagents"
  sets `[agents] max_tier` (`AgentMaxTierSet`). Rows and help text come from
  `ui_support/tier_settings.py`; native:
  `ui/ratatui/tier_pages.py`.
- **Session titles:** an on/off row and a "Title model" row (a tier, recommended
  `low`, or one model), under a two-line explanation that names the resolved model
  and says that off keeps the first line of the first message. If the model cannot
  run the page says titles use the first message. `SessionTitleSettings(Set)`.
- **Agents → Tiers row** (subagents only): checkboxes per tier, the first checked
  is the default (`Make default`), at least one stays checked. Conflicts are shown
  under the row: a pinned model in an unchecked tier, `model: inherit` with tiers,
  a tier above the global limit. No `tiers` reads "not set (uses the parent's
  model)". New agents start with `tiers: [low, medium]`
  (`tier_settings.new_agent_template`, one template for both clients).
- **Providers:** cards for Codex (browser or device code), GitHub Copilot (device
  code), OpenCode Go (password field) and Claude (browser, then a password field
  for the code the sign-in page shows, sent with `ProviderLoginCode`); polls
  `ProviderLoginPoll`. Claude has no Disconnect (`can_logout: false`): its login is
  shared with Claude Code.
- **First-run setup:** only asks to connect a provider; the first connected one
  is saved with its newest model via `SetupSave` and the screen closes into chat
  (a restart is asked for only while turns run). Never required in dev mode.

## Dictation

`Ctrl+Space` toggles bounded dictation, `/voice status|download|on|off` and
Settings → Voice manage it. First use asks before downloading the model;
preparation is silent. An orange dot at the far left of the context-size row shows
only while recording, without shifting layout. A live strip floats just above the
composer (an overlay, so nothing moves): a pulsing dot, the elapsed time and an
audio-reactive waveform (newest sample on the right), then the running preview
transcript in up to three rows, newly heard words highlighted as they arrive and
the oldest words replaced by "…" when it is clipped. After stop, the strip switches
to "transcribing" (the waveform settles into a ripple and a glow sweeps the
words) until the final text lands. Any key stops and transcribes (the key is
swallowed); `Esc` discards. The final transcript is inserted as editable composer
text. Details: [voice.md](voice.md).

## Dev mode

`nexus --dev chat|web` (or `NEXUS_DEV=1`) uses an isolated home and sandbox
workspace and exposes `/mock` (list, run `NAME [--speed N] [--seed N]`, `clean`);
the web shows a `DEV` badge (from `Health.dev`). See [devtools.md](devtools.md).

## Adding a user-visible feature

1. Host command or reducer field first ([host.md](host.md), [events-and-view.md](events-and-view.md)).
2. Shared logic in `ui_support/` if it is pure; then the native TUI (`ui/ratatui/`) and the
   web (`ui/web/js/`) in the same place.
3. A check in each: a native PTY test and `tests/playwright_web_check.py`
   ([testing.md](testing.md)).

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
