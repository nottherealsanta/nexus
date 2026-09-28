# TAUI_PORT_PLAN.md: bring taui's terminal UX to `nexus chat`

Implementation plan for porting the look and terminal features of
`../taui` (`taui/tui/**`) into the Nexus Textual shell. The session sidebar
changes also apply to the browser client.

Implementation status: Phases 0–4 and the Phase 5 composer and command items
are in the current worktree. The Phase 5.4 items are explicitly later work:
session tabs are unnecessary while the left sidebar provides session switching;
manual context breakdown, inline questions, blur notifications, and an MCP
Connect action need further host and view contracts. `/compact` is deferred
until the runtime exposes a manual compaction action, as recorded in
`IMPROVEMENT_PLAN.md`.

Reference screenshots (from the request):

- **S1**: **how a new session looks after the first user message.** At the
  top, the context header with the labeled blocks `System prompt`, `Tools`,
  `Skills` and `MCP`. Then the collapsible `▼ hi` turn block, a red error line,
  the reply footer `DEF · gpt-5.6-luna`, the composer
  `DEF  gpt-5.6-luna  copilot  high` with `0 (0%)` at the bottom right, and the
  activity bar. Specified in **D1**.
- **S2**: **how every list looks when it opens** (slash menu shown). Bold
  primary text, dim secondary text, subtle grey highlight, orange scrollbar
  thumb. Specified in **D2**.
- **S3**: `Resume Session` modal. Search box, `◇ search content` toggle, a
  session list with `N msgs  Xd ago current`, a detail pane, and the hint
  `Enter resume · p preview · Esc cancel`. In Nexus this becomes the browser
  for **archived** sessions. The left sidebar stays the main session list.

Decisions already made:

- taui's "self-edit" is called **Settings**.
- No new keyboard shortcuts.
- The left sidebar stays.
- Old sessions auto-archive after two days. Explicit Delete moves a session to
  restorable trash; automatic archiving never deletes one.
- The left sidebar shows current and archived sessions in both Textual and web,
  with a visible Delete control on every row and a spinner for running sessions.
- Settings scopes are global `~/.nexus` and project `<project>/.nexus`.
- No `!cmd` shell prefix.
- **The main thing to take from taui is the terminal design**: D1 and D2 below.

---

## 0. Ground rules for every phase

Read `AGENTS.md` and `docs/textual.md` first. These constraints shape every
design choice below.

| Constraint | Consequence for this plan |
| --- | --- |
| `ui/` budget < 5,000 lines. It is at **4,581** today, so about 420 lines are left. | New widgets and screens go in **`nexus/ui_support/tui_*.py`**, which has no budget. `ui/tui/*` gets only thin wiring. |
| `ui/cli/` + `ui/jsonl.py` + `ui/tui/app.py` < 2,400 | `app.py` gets at most a few lines per feature. New behavior goes in a mixin module next to `panels.py`, for example `ui/tui/context_header.py`, or put it in `ui/tui/panels.py`. |
| `host/` < 7,000 and close to the cap | New host logic goes in `nexus/host_support/`. `facade.py` only dispatches. |
| `core/`+`model/`+`tools/spec.py` < 14,000 | One optional `group` field on `ToolSpec` is fine. Nothing else in core. |
| UI encapsulation (`tests/test_ui_layering.py`) | The UI never reads `.nexus/` files or session logs. Every new data need becomes a host command in `host/protocol.py`, handled in `facade.py`. |
| Durable log first | Anything that must survive reconnect goes in the session log, such as fork provenance. The archive marker is durable too (§3.2). The UI never keeps it only in memory. |
| Theme | Colors only through `$nx-*` variables (`ui/tui/theme.py`). taui hex values map onto them (see §0.1). |
| Security | The Settings write scope and blocked paths are enforced **daemon-side** (`tools/permissions.py`), never in the widget. |

Textual in `ui_support/`: `tests/test_ui_layering.py` scans only `nexus/ui/**`.
New `ui_support/tui_*.py` files may therefore import Textual. Add each new
file to the list in `AGENTS.md` rule 2 and to the file map in
`docs/textual.md` when you create it.

### 0.1 Palette mapping (taui → nexus)

| taui | Use in nexus |
| --- | --- |
| `#a0a0a0` banner body at rest | `$nx-muted` |
| `$foreground` on hover | `$nx-text` |
| `$taui-option-active` `#2a2a2a` hover and highlight row | `$nx-element-hi` |
| `$surface-lighten-1` selected completion row | `$nx-element-hi` |
| `bold #070707 on <agent color>` agent-scoped label | `bold $nx-bg on <agent_color(name)>` |
| `bold #070707 on #8a8a8a` neutral label | new var `nx-label-neutral` (`#8a8a8a` dark, `#6f6f6f` light) with fg `$nx-bg` |
| `$primary` orange title, `Resume Session` | `$nx-accent` |
| `$taui-scrim`, `$taui-dialog-bg`, `$taui-field-bg`, `$taui-border(-focus)` | Add `nx-scrim`, `nx-dialog`, `nx-field`, `nx-border-focus` to `_DARK`/`_LIGHT` in `theme.py`. Take values from taui `theme.py`. |
| `$taui-cyan` group headers in modals | new var `nx-cyan` (`#56d4dd` / `#0e7490`) |
| scrollbar thumb (orange in S2) | `scrollbar-color: $nx-accent 50%` on `ListPanel` only |
| list panel background, `$surface` (S2) | new var `nx-list` (`#0f0f0f` / `#fafafa`) |

Do this palette step first, as **Phase 0**. It adds about 10 lines to `theme.py`,
and every later phase uses the new variables.

---

## Design target: the taui look (read this first)

**The main thing to take from taui is its terminal design.** Everything in this
section is the visual contract for `nexus chat`. The later phases add the data
and features behind it. Where they overlap, this section wins.

- **D1** (screenshot S1): how a session looks after its first user message.
- **D2** (screenshot S2): how **every** list that opens in the shell looks.

Taui sources for the measurements below:

- `taui/tui/app.tcss`
- `taui/tui/widgets/turn_container.py`
- `taui/tui/widgets/chat_input.py`
- `taui/tui/widgets/info_bar.py`
- `taui/tui/widgets/reply_footer.py`
- `taui/tui/widgets/spinner.py`
- `taui/tui/widgets/info2.py`

Colors use the `$nx-*` variables from §0.1.

### D1: A session after the first message (S1)

The main column, top to bottom. The left `SessionSidebar` and the right
`DetailsSidebar` are unchanged and keep their current toggles.

```
┌ conversation scroll (#conversation) ───────────────────────────────┐
│   System prompt                    ← agent-colored label chip       │
│     You are a pragmatic …          ← dim body, 5 lines max          │
│                                                                     │
│   Tools                            ← agent-colored label chip       │
│     apply_patch   bash(3)   edit   ← 3-column grid, dim             │
│     …                                                               │
│   Skills                           ← grey label chip                │
│     prompt-toolkit  rich  textual                                   │
│   MCP                              ← grey label chip                │
│     cvc(4)                                                          │
│ ███████████████████████████████████████████████████████████████████ │ ← user turn block ($nx-panel)
│ ▼ hi                                                                │   3 rows: blank / text / blank
│ ███████████████████████████████████████████████████████████████████ │
│                                                                     │
│ Error: HTTP 400: {"error": …}      ← $nx-error, plain, wraps        │
│                                                                     │
│   DEF · gpt-5.6-luna               ← reply footer                   │
└─────────────────────────────────────────────────────────────────────┘
┌ composer box ($nx-panel, no border, no left bar) ───────────────────┐
│                                                                     │
│   █                                ← editor, 1–8 lines              │
│                                                                     │
│   DEF  gpt-5.6-luna  copilot  high ← info row 1                     │
│                              0 (0%)← info row 2, right-aligned      │
└─────────────────────────────────────────────────────────────────────┘
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ← activity bar
```

**Conversation scroll** (`ConversationTimeline`, `ui/tui/timeline.py`):
- `padding: 1 0 1 2`. The scrollbar is hidden (`scrollbar-size: 0 0`), as in
  taui. Mouse wheel and keys still scroll it.
- The first child is the `ContextHeader` (Phase 1). It keeps the label-chip
  and grid styling from §1.1, with one blank line between blocks and
  `margin: 0 1 1 1`.

**User turn block** (port of `TurnContainer`). Changes in `UserMessage` and
`TurnWidget`:
- The turn has `margin-bottom: 1`. Its header row spans the full width with
  `background: $nx-panel` and a horizontal layout.
- The chevron `▼` (`▶` when collapsed) is `width: 3; padding: 1 0 1 1`. It is
  near-invisible at rest (`$nx-border-strong`), and becomes `$nx-text` on
  `$nx-element-hi` on hover. Only the chevron toggles.
- The user text has `padding: 1 2 1 0` and is `$nx-text`. The block is always
  3+ rows: a blank row, the text, a blank row.
- **Removed from today's nexus look:** the agent-colored left bar
  (`outline-left`) and the send time inside the block.
- Collapsed: the body is hidden and a 1-row summary appears under the header
  with `padding: 0 2 0 4`. The left side is `N tools · first line of reply` in
  `$nx-muted`. The right side is `tokens · duration` in `$nx-quiet`. Collapsing
  is a no-op while the turn is running. A collapsed body unmounts its children
  and remounts them from the view on expand.

**Turn body** (`margin-top: 1`):
- **Thinking** (`ThoughtLine`): `$nx-muted`, `padding: 0 2`,
  `margin-bottom: 1`. Keep the click-to-expand behavior.
- **Assistant text** (`AssistantMessage` Markdown): `padding: 0 2`,
  `margin-bottom: 1`, `$nx-text`. Markdown colors follow taui `app.tcss`:
  - H1 `$nx-accent`, H2 `$nx-purple`, H3 `$nx-success`, H4 `$nx-warning`
  - bullets `$nx-accent`
  - inline code `$nx-warning` on `$nx-element-hi`
  - bold `$nx-text`, italic `$nx-purple`
  - block quote with an outer `$nx-accent` left edge on `$nx-panel`
  - fenced code on `$nx-panel`
  - table headers bold `$nx-accent`
- **Tool blocks** keep the nexus tool rendering (`tool_heading`, 10-line
  preview, diff view), restyled to `padding: 0 2; margin-bottom: 1` so they line
  up with the text.
- **Turn error:** a single `Static` reading `Error: <redacted message>` in
  `$nx-error`, with `markup=False`. No box, no icon, no padding, so it sits
  flush with the turn block's left edge, as in S1. It wraps.
- **Reply footer** (replaces `_turn_footer` `▣ Agent · model · effort ·
  duration`): `margin-top: 1; padding: 0 2`. It reads
  `<AGENT> · <model>[ · <N>s]`. The agent name is in its agent color and
  everything else is `$nx-quiet`, with a dim ` · ` separator. Seconds appear
  once the turn has finished. Drop effort from the footer; it lives in the
  composer.

**Composer box** (`ChatInput`, `ui_support/tui_widgets.py`):
- The container has `background: $nx-panel; margin: 0 2; padding: 0`, **no
  border and no left bar**.
- **Editor:** `padding: 1 2`, a transparent background, no cursor-line
  highlight, `height: auto; max-height: 8`. A 1-column vertical scrollbar
  appears only past 8 lines. There is no placeholder text.
- **Info row 1:** `padding: 0 2`. Each item has `margin-right: 2`:
  - agent name, bold, in the agent color. It opens the agent list (D2).
  - model, `$nx-text`. It opens the model list.
  - provider, italic `$nx-muted`. It opens the model list.
  - effort, `$nx-muted`. It opens the effort list. Hide it when the model has
    no effort levels.

  This replaces today's `Agent · model provider · effort` line in
  `RootAgentBar`. Reuse its `PickerLink`s.
- **Info row 2:** right-aligned, `padding: 0 2`. It shows context usage as
  `<n> (<pct>%)` in `$nx-quiet`, where `n` is the token count as a plain
  integer below 1,000 and `round(n/1000)k` above. Clicking it opens
  `ContextDetailsScreen`. Taui's format differs from today's `30.1K (3%)`, so
  update `context_usage`.
- **Removed from under the composer:** the path line and the
  `ctrl+p commands` hint. The sidebar already shows the workspace.
- `ConnectionStatus` shows only when the state is not OK, as one line between
  the conversation and the composer (`$nx-warning` or `$nx-error`). It is
  hidden when connected.

**Activity bar** (port of `ActivityProgress`, a new widget in
`tui_widgets.py`). It is one row, docked at the bottom of the main column,
`margin: 0 2`, drawn with `━`:
- **Idle:** the track is `$nx-border`. The left part, proportional to context
  usage, is `$nx-success` below 50%, `$nx-warning` from 50% to 75%, and
  `$nx-error` at 75% and above.
- **Turn running:** a segment 1/5 of the width, in the agent color, bounces
  across the bar on a 0.05s timer.
- **Session loading or reconnecting:** the whole bar pulses slowly between the
  track color and the agent color (taui `start_breathing`).
- The timer is stopped whenever the state is idle. Drive it from
  `_sync_timeline`/`_sync_status`.

**Lists** (D2) open in the space directly above the composer box, at the same
width and margins.

### D2: Every list looks like S2

S2 shows the slash menu. The **same widget and style** are used for every list
that opens in the shell:
- slash commands
- `@` file completions
- command-argument completions (for example `/model <id>`, `/agent <name>`)
- agent, model and effort pickers (today's `AgentPickerPanel`)
- any future inline list (skills, tasks)

Modal screens (Sessions dialog, archived browser, Settings) keep their own
layouts, but use the same highlighted-row colors.

**One widget:** add `ListPanel` in a new file `nexus/ui_support/tui_list.py`.
It replaces both `CompletionPopup` (`tui_widgets.py:158`) and the rendering in
`AgentPickerPanel` (`ui/tui/agent_picker.py`). `AgentPickerPanel` keeps its
`Selected`/`Cancelled` messages and delegates drawing to `ListPanel`.

Style:
- **Placement:** in the flow directly above the composer box, with
  `margin: 0 2` so it is exactly as wide as the composer. It is not a floating
  60-column dropdown.
- **Panel:** `background: $nx-list` (new variable, slightly darker than
  `$nx-panel`: `#0f0f0f` dark, `#fafafa` light). `border-top` and
  `border-bottom` are `tall $nx-element-hi`, with no side borders.
  `padding: 0 0 0 1`, `height: auto; max-height: 8`.
- **Rows:** `height: 1; padding: 0 0 0 1`, default color `$nx-quiet`.
  - **Primary text** in **bold** `$nx-text` (for example `/agents`), then two
    spaces, then **secondary text** in dim `$nx-quiet`. There are no padded
    columns in the slash list; the description follows the name directly, as
    in S2.
  - **Selected row:** `background: $nx-element-hi; color: $nx-text` across the
    full row width. Never an accent fill.
  - Hover highlights the row. A click selects it and accepts.
- **Scrollbar:** 1 column. The thumb is `$nx-accent` at about 50% (the dim
  orange in S2), and the track is `$nx-list`. It is shown only when there are
  more rows than fit.
- **Filter header:** while filtering a picker, the first row is `> query`,
  where `> ` is dim and the query is bold `$nx-accent`.
- **Current-item marker:** a dim ` ◀` after the current model, agent or
  effort. That item's primary text is bold `$nx-accent`, as in taui.

Row content for each list:

| List | Primary (bold) | Secondary (dim) | Order |
| --- | --- | --- | --- |
| Slash | `/name` | `Summary (/name args)` | alphabetical, aliases hidden |
| `@` files | relative path | — | nexus's current ranking |
| Agents | name, in agent color | `provider/model` + ` ◀` | nexus's current order |
| Models | model id, padded to 45 | `<N>k ctx` + ` reasoning` + ` ◀` | nexus's current order, current preselected |
| Effort | label (`Low`, `High`, …) | ` ◀` | model's levels, `(default)` first |
| Command args | value | its description, if any | as produced |

Keys, the same for every list:

| Key | Action |
| --- | --- |
| ↑ / ↓ | Move the selection, wrapping around |
| PgUp / PgDn | Page |
| Enter / Tab | Accept |
| Esc | Close; focus returns to the editor |

The selection stays scrolled into view (`scroll_to_widget`).

### D3: How to verify the look

- Add a deterministic state `first_message` to `tests/visual_tui_demo.py`. It
  has the context header, one user turn `hi`, a turn error, the reply footer,
  and an empty composer. Add a state `slash_menu` with `/` typed.
- Render both at `size=(200, 55)` with `export_screenshot()` and Playwright,
  and save them to `artifacts/visual-tui/first_message.png` and
  `slash_menu.png`. Compare them side by side with S1 and S2 before merging.
- Pilot assertions in `tests/test_tui_layout.py` and
  `tests/test_tui_spacing.py`:
  - The turn block has no `outline-left`, and its height is 3 for one line.
  - The error has no border.
  - The reply footer text matches `^\S+ · \S+`.
  - The composer has no border.
  - Info row 2 reads `0 (0%)` on a new session.
  - The `ListPanel` width equals the composer width, and the selected row has
    no accent background.
  - The activity bar exists, and its timer is stopped when idle.

---

## Phase 1: Context header (S1 top half)

### 1.1 Target look and behavior (from taui)

Source files to copy from: `taui/tui/widgets/system_prompt.py`,
`tool_groups_banner.py`, `skills_banner.py`, `mcp_banner.py`,
`taui/tools/schema_format.py`, and `taui/tui/app.py:2305-2495`
(`_build_context_banner_parts`, `_maybe_show_context_banner`,
`_refresh_context_banner`).

- Four blocks at the **top of the conversation scroll**, before the first
  user turn. They scroll away with the transcript. They are not a docked pane.
- Each block has a **label chip** such as ` System prompt ` or ` Tools `, drawn
  as bold text with a solid background, followed by a **body** indented two
  columns.
  - `System prompt` and `Tools` use the **agent color** as the label background.
    They are agent-scoped.
  - `Skills` and `MCP` use the **neutral grey** label. They are session-global.
- Body at rest is `$nx-muted`. On hover the whole block gets
  `background: $nx-element-hi` and `color: $nx-text`. The whole block is
  clickable and opens a modal.
- **System prompt body**: the first 5 lines of the prompt, with markup escaped.
  If there is more, add a dim italic line `… +N more lines`. Show `(empty)` if
  the prompt is empty.
- **Tools body**: tool **groups** in a fixed **3-column** grid. Column width is
  the longest label + 2. A cell reads `group(N)` when the group has more than
  one tool, and `name` otherwise. Groups are sorted by name.
- **Skills body**: skill names in the same 3-column grid. Show dim `(none)` if
  there are no skills.
- **MCP body**: `server(N)` cells, where N is the tool count. Configured but
  disconnected servers use a dimmer color (taui `#5a5a5a` maps to `$nx-quiet`).
- Spacing: block `margin: 0 1 1 1; padding: 0 1`. Body `padding: 0 1 0 2`.
  Blocks are separated by one blank line, as in S1.

### 1.2 Modals (click targets)

All share one frame: centered, 80–90% of the screen, `$nx-dialog` background,
bold `$nx-accent` title, a scroll area with a top rule, and `Close` plus Esc.

- **SystemPromptModal**: the full prompt as plain text (`markup=False`) with a
  scroll. Title is the label text.
- **ToolsModal**: title `Tools · N groups · M tools`. A group with more than one
  tool gets a `$nx-cyan` header `▾ bash  (3)`, then `· name` rows. A group with
  one tool is a flat bold `$nx-accent` name. Each tool shows its description and
  one parameter row per property:
  `* name  type  — description  (default: …)`. A red `*` marks required
  parameters. Names are green, types blue, descriptions muted. Port
  `format_schema_type` and `schema_param_rows` from taui
  `tools/schema_format.py` as pure functions. Add an `Edit tools…` button that
  opens Settings on the `TOOLS` category (Phase 4). `SkillsModal` and
  `McpModal` get the same button for their category. Hide the buttons until
  Phase 4 lands.
- **SkillsModal**: per skill, show the bold name, then scope
  (workspace/user/builtin) and path in dim text, then the description.
- **McpModal**: per server, show a `$nx-cyan` header with its tool names.
  Disconnected servers are italic and dim with their status. A `Connect` button
  is out of scope until a host `McpConnect` command exists. List it under
  follow-ups.

### 1.3 Data: what the host already gives and what to add

`ContextInspect` → `ContextInspectResult` (`host/protocol.py:650`, built in
`runtime.py:~2540`) already carries:

- `system_text`: the system prompt, already redacted.
- `tools[]`: `{name, description, input_schema}`.
- `skills_index[]`: `{name, description, included}`.
- `mcp_index`: a string.
- `agent`: includes the name. Use it for the label color through `agent_color`.

Add the following. Each addition is optional with a default, so older clients
and the web app are unaffected.

1. **Tool groups.** Add `group: str = ""` to `ToolSpec` (`tools/spec.py:369`).
   Empty means the group is the tool's own name. Set it on the families:
   `bash`, `BashOutput` and `KillShell` → `"bash"`;
   `ReloadExtensions`, `ListExtensions` and `WriteTool` → `"extensions"`. Also
   `edit`/`multiedit` if you want them together, but taui keeps `edit` solo.
   Extension tools may set it. In `runtime.py` inspect, emit
   `"group": spec.group or spec.name` and `"bundle": spec.bundle` per tool.
   MCP tools (`mcp__<server>__<tool>`) get `group = "mcp:<server>"`. The UI moves
   them from the Tools block into the MCP block.
2. **Skill scope and path.** Add `"scope"` and a display-safe `"origin"` to each
   `skills_index` row. Use the same provenance string `ListExtensions` already
   exposes. Never send an absolute home path. Use `~/…` or a workspace-relative
   path.
3. **MCP servers.** Add `mcp_servers: list[dict]` to `ContextInspectResult`,
   one entry per server: `{name, status: "connected"|"failed"|"disabled",
   tool_count, tools: [names…]}`. Build it in a `host_support/` helper from the
   MCP manager state the runtime already has. Bound it to 64 servers and 256
   names per server. Keep `mcp_index` for the modal body.

Test: extend the host inspect tests (`tests/test_host_facade.py` or the existing
context-inspect test) for `group`, `bundle`, `mcp_servers`, and skill `scope`.
Add a `ToolSpec` test that `group` defaults to empty and passes validation.

### 1.4 UI implementation

- **New file `nexus/ui_support/tui_context_header.py`** (Textual allowed), with:
  - `ContextHeader(Vertical)` holding four children: `PromptBlock`,
    `ToolGroupsBlock`, `SkillsBlock`, and `McpBlock`. Each is a
    `Container` subclass with `set_data(...)`. Each owns its label and body
    `Static`, `:hover` CSS, and `on_click` → `app.push_screen(<Modal>)`.
  - The four modals, `SystemPromptModal`, `ToolsModal`, `SkillsModal` and
    `McpModal`, share a `_ContextModal` base (title, scroll, Close, Esc).
  - Pure helpers, each unit-testable without Textual:
    `render_columns(labels, columns=3)`, `group_tools(tools) -> (groups, mcp)`,
    `prompt_preview(text, lines=5)`, `schema_param_rows(schema)`, and
    `format_schema_type(prop)`. Put the pure helpers in
    **`nexus/ui_support/context.py`** or a new `ui_support/schema_text.py` so the
    web app could reuse them.
- **Mounting:** `ConversationTimeline` (`ui/tui/timeline.py`) gets one
  `ContextHeader` as its **first child**, created once in `compose`/`on_mount`.
  It is never reconciled away by `set_view`. `set_view` must skip it when it
  reconciles `TurnWidget`s by durable ID. This takes only a few lines in
  `timeline.py`.
- **Retire `ContextPreview`** (the `CURRENT REQUEST CONTEXT` pane in
  `tui_widgets.py:648` and `app.py` `compose`). The header replaces it. Keep
  `ContextDetailsScreen` (ctrl+i and `/context`) as the full raw view. Removing
  the preview pane's app state (`_context_preview_*`) frees lines in `app.py`.
  Move the inspect-fetch logic into a small mixin `ui/tui/context_header.py`
  (`ContextHeaderMixin`, the same pattern as `PanelsMixin`) with:
  - `_refresh_context_header()`, which calls `client.inspect_context(session)`,
    guards on `self.controller.session` and a generation counter (race
    hygiene; see `tests/test_tui_session_switch_race.py`), then calls
    `ContextHeader.set_data(...)`.
  - Triggers: session open or switch, agent select, reset or cycle, model or
    effort select, extensions reload, and reconnect. **Do not blank the header
    while a turn is active.** Today the code sets an error while active. Keep
    the last snapshot instead.
  - Failure shows `Context unavailable` in dim text inside the header. It never
    breaks the shell.
- **Preferences:** the existing context-preview toggle in Settings
  (`TuiPreferences`) becomes "Show context header". Keep the same pref key, or
  migrate it.

### 1.5 Tests and checks

- `tests/test_tui_context_header.py` (new):
  - Pure: 3-column layout widths, `bash(3)` versus solo labels, MCP split,
    5-line preview plus `+N more lines`, markup escaping (`[` in the prompt),
    and required-marker rendering.
  - Pilot: the header is the first child of `#conversation`, and it survives
    `set_view` with 3 turns. Click on each block pushes the right modal. Esc
    closes it. The header refreshes after `AgentSelect`, and the label color
    changes. Failures render `Context unavailable`.
  - Add fake `ContextInspect` responses to the test transports
    (`tests/test_tui_panels.py:PanelTransport` and `tui_acceptance_fixture.py`).
- Update `tests/visual_tui_demo.py` states `empty` and `transcript` so they
  show the header. Regenerate `artifacts/visual-tui/` and compare with S1.
- Update the `tests/test_tui_layout.py` and `tests/test_tui_spacing.py`
  expectations that mention `#context-preview`.

---

## Phase 2: Implement D2 (every list), starting with the slash menu

Source: `taui/tui/widgets/info2.py` (`Info2`, `Info2Item`,
`_rebuild_completions`, `_model_label`, `_agent_label`, `_variant_label`) and
`chat_input.py:_show_completion`. **D2 is the visual contract.** This phase
builds `ListPanel` and moves every list onto it: slash first, then `@` files,
command arguments, and the agent, model and effort pickers.

### 2.1 Target

Look and keys: **exactly D2.** Slash-menu specifics:
- Items are sorted **alphabetically**, and aliases are hidden. Hide `/session`
  and `/sesssion`, but keep them in `BY_NAME` for dispatch. Add
  `CommandSpec.hidden: bool = False`.
- Descriptions are sentence case, with the argument hint appended when there is
  one, the way taui does it: `List or activate agents (/agents [ID])`.
  Build it as `f"{summary} (/{name} {args})"` when `args` is set.

### 2.2 Changes

- `ui/cli/commands.py`: sentence-case every `summary` and add `hidden`. Check
  `help_text` and any test that asserts literal summaries
  (`grep -rn "start a new session" tests/`).
- **New `ui_support/tui_list.py`: `ListPanel`.**
  - It is a `VerticalScroll` of one-line rows. Each row is a
    `(primary, secondary, marker)` item.
  - API: `show(items, selected, *, filter_text="")`, `move(delta)`,
    `page(delta)`, `selected_value`, `hide()`.
  - Messages: `Accepted(value)` and `Dismissed`.
  - All styling from D2 lives in its `DEFAULT_CSS`, using `$nx-*` variables
    only.
- `ui_support/tui_widgets.py`: `ChatInput` uses `ListPanel` in place of
  `CompletionPopup`, which is deleted. Keep the `ChatInput` method names
  (`completion_visible`, `move_completion`, `accept_completion`,
  `close_completion`) so `app.py` is unchanged. The slash, `@` file and
  command-argument completions all feed the same panel.
- `ui/tui/agent_picker.py`: `AgentPickerPanel` draws through `ListPanel`,
  using the agent, model and effort row formats from the D2 table. This should
  **shrink** `ui/`.
- `app.tcss`: remove the old `#completion-popup` and inline-picker rules.

### 2.3 Tests

`tests/test_tui_completion.py`:

- alphabetical order, hidden aliases absent
- selected-row style has no accent background
- more than 8 matches scrolls and the selection stays visible
- the args hint is present

Update `tests/playwright_tui_check.py` if it matches old row text.

---

## Phase 3: Auto-archive and the archived-sessions browser (S3)

**Unchanged:** the left `SessionSidebar` stays the main way to see and switch
sessions, and the `SessionsScreen` dialog (ctrl+o, `/sessions`) stays as it is.
This phase adds three things:

1. old sessions archive **automatically**
2. an archived session is **never deleted**
3. a taui-style browser (S3) finds any archived session, and resuming it brings
   it back

### 3.1 How it works today, and what changes

Before this port, "Archive" in the sidebar called `SessionDelete`
(`ui/tui/panels.py:_archive_session` → `client.delete(...)`). That moves the
session into **trash** with a `TrashRecord.delete_after` of 7 days
(`session/manager.py:82`, `DEFAULT_RETENTION_SECONDS`), and
`SessionManager.purge_expired` (`manager.py:947`) can remove it afterwards.
Nothing calls `purge_expired` yet, but the design treats archive as a
delayed delete. **Archive must become its own state, separate from trash.**

| | Trash (unchanged) | Archive (new) |
| --- | --- | --- |
| Meaning | The user deleted it | Inactive, kept forever |
| Expiry | `delete_after` + `purge_expired` | **None.** `purge_expired` must never touch archived sessions. |
| Where it lives | `<sessions>/../trash/` | Stays in `.nexus/sessions/`, with an archived marker |
| Shown in sidebar | No | Yes, with a Delete control |
| Can be continued | After restore | Yes. Opening or sending to it unarchives it automatically. |

### 3.2 Session layer (`nexus/session/manager.py`)

- **The archive marker is durable but outside the conversation.** Recommended
  option: a small sidecar index `.nexus/sessions/archive.json`
  (`{session_id: {archived_at, reason: "auto"|"user"}}`), written atomically
  (temp, fsync, replace) under the manager's existing lock. That keeps the
  append-only log for conversation events only, and archiving never touches a
  log that might be mid-turn. Alternative: append a `session.archived` event.
  That is simpler to replay, but it writes to logs of idle sessions. Pick the
  sidecar unless you disagree.
- New methods: `archive(session_id, reason)`, `unarchive(session_id)`,
  `archived() -> list[ArchiveRecord]`. `list()` gains
  `include_archived: bool = False`. The sidebar combines the active list with
  `SessionListArchived` so archived sessions stay visible on the left.
- **Auto-archive sweep:** `archive_stale(now, older_than)` archives sessions
  that match all of these:
  - `last_activity` is older than the threshold
  - state is `idle`
  - `viewers == 0`
  - not the session currently open in any client

  It never archives a running session or one awaiting input or permission.
  Run it from the daemon on startup and then hourly. The host already has
  periodic work; put the scheduling in `host_support/`. It must be bounded:
  at most 500 sessions per sweep.
- **Config:** `[sessions] auto_archive_days = 2` in `nexus.toml`. `0`
  disables it. Validate it in `config/` (an integer from 0 to 3650).
- **Resuming unarchives.** `open()`, `enqueue()` or `fork()` on an archived
  session removes the marker first, so continuing a session is one step.
- **Separate archive from trash.** The Sessions dialog ctrl+d calls
  `SessionArchive`; ctrl+z calls `SessionUnarchive`. The sidebar Delete button
  calls `SessionDelete` to restorable trash.

### 3.3 Host commands (`host/protocol.py`, logic in `host_support/session_archive.py`)

- `SessionArchive(session)` / `SessionUnarchive(session)` → `SessionSummary`.
- `SessionListArchived(query: str = "", limit: int = 200, cursor: int = 0)` →
  `{sessions: [ArchivedSummary], has_more}`. `ArchivedSummary` =
  `SessionSummary` plus `archived_at`, `reason`, `message_count`, `created_at`
  and `parent_id` (see §3.4).
- `SessionPreview(session, max_chars=6000)` →
  `{text, truncated}`. The text comes from the **reduced view** (user and
  assistant text turns only, with `role:` prefixes), with secrets redacted and
  the length bounded.
- `SessionSearch(query, archived_only=True, limit=50)` → `{ids}`. The
  "search content" toggle calls it. The daemon scans at most 50 sessions,
  reading at most 512 KiB of log tail from each (the same bound `Doctor`
  uses). The client never loads every session.
- `SessionListResult` gets an `archived_count: int` so the sidebar can show its
  footer entry (§3.5).

Add `Client` methods in `nexus/client/protocol.py`, and add fake responses to
the test transports.

### 3.4 Summary fields the browser needs

Add these optional, primitive fields to `SessionSummary`
(`session/manager.py:105`):

| Field | Source |
| --- | --- |
| `message_count: int = 0` | The number of user messages in the `state.messages` that `_summary()` (`manager.py:~689`) already loads. |
| `created_at: float = 0.0` | The timestamp of the first record. |
| `parent_id: str = ""`, `fork_seq: int = 0` | `fork()` (`manager.py:1228`) records no parent today. Append a durable `session.forked` event (`{parent, at_seq}`) as the child's first event. Declare it in `nexus/events.py`, and make `view/reduce.py` tolerate it. |

### 3.5 UI

- **Sidebar:** active and archived sessions appear on the left, each with a
  Delete control. An extra `Archived · 37` footer row in `SessionSidebar`
  (`ui_support/tui_panels.py`) opens the archived browser when
  `archived_count > 0`.
- **Archived browser:** a port of taui `screens/session_picker.py`, as
  `ArchivedSessionsScreen` in `ui_support/tui_panels.py`, with wiring in
  `ui/tui/panels.py`. Also open it with `/archived`, with `/resume` as an
  alias, and from the palette. There is **no new key binding.**
  - Modal on the `$nx-scrim` backdrop, dialog `width: 120; max-width: 95%;
    max-height: 86%`, `$nx-dialog` background, no border.
  - Title `Archived Sessions`, centered, bold `$nx-accent`.
  - Row 1: `Input` with placeholder `Search archived sessions...`, a
    `solid $nx-border` border that becomes `$nx-border-focus` on focus. Next
    to it, the `◇ search content` toggle, which shows `◆` when on and `⟳`
    while loading.
  - Body, height 20:
    - Left `OptionList`, `2fr`. Row:
      `<title ≤40, padded to 40>  <N> msgs  <ago>`, where ago is the last
      activity. The title is white and the rest dim. Fork children nest under
      their parent with a dim `├─ ` prefix. Highlight is `$nx-element-hi`
      and bold. Scrollbar thumb `$nx-accent`.
    - Right detail pane, `1fr`. On highlight it shows the title, a blank line,
      `ID: <12-char id>`, `<N> msgs · <ago>`, `Archived <ago> (auto|manual)`,
      a blank line, and `Press p to preview content`. Pressing `p` calls
      `SessionPreview` and shows the body, `preview truncated` if needed, and a
      `---` footer.
  - Hint: `Enter resume · p preview · Esc cancel`.
  - Enter calls `SessionUnarchive`, then switches to the session through the
    normal session-switch path. It then shows in the sidebar again.
  - Keys: ↑/↓ move, ↑ from the first row returns to search, Enter resumes, `p`
    previews (ignored while typing in search), Esc closes. Pagination uses
    `cursor` when there are more than 200 archived sessions.
  - Filter: substring matches first, then subsequence matches, on id, title and
    first prompt. With the content toggle on, match on `SessionSearch` ids.
  - **Fix the taui bug visible in S3:** fold every title to a single line
    (`sanitize` plus whitespace collapse) before padding.

### 3.6 Tests

- Session layer:
  - The sweep archives only sessions that are stale, idle and have no viewers.
    Running, awaiting and currently open sessions are skipped.
  - `purge_expired` never touches archived sessions.
  - Archived sessions survive a daemon restart.
  - `open` and `enqueue` unarchive.
  - `fork` writes `session.forked`, and replay keeps it.
  - The sidecar is written atomically and survives a corrupt file (rebuild
    from an empty map and log a warning; never lose sessions).
- Host: `SessionArchive`/`SessionUnarchive`, `SessionListArchived` paging,
  `SessionPreview` bounds and redaction, `SessionSearch` limit, and
  `archived_count`.
- TUI (`tests/test_tui_panels.py`):
  - The sidebar shows active and archived sessions and retains the `Archived · N` browser entry.
  - Sidebar Delete uses trash; the Sessions dialog archive and ctrl+z use archive commands.
  - The browser rows show `N msgs` and `ago`, multi-line titles render on one
    line, and fork children are indented.
  - `p` previews, and `p` typed into search does not.
  - The content toggle filters, and Enter unarchives and switches.

---

## Phase 4: Settings (taui's "self-edit", renamed)

This is taui's self-edit feature under the name **Settings**. It has **no new
key binding.** Nexus already has a small `SettingsScreen`
(`ui_support/tui_panels.py`, opened from `ui/tui/panels.py`) for theme, panels
and context preview. Phase 4 grows that screen into taui's config console.
It also adds an optional **`settings` agent** that makes config changes
through chat.

### 4.1 What taui does (reference only)

- **The console** (`taui/tui/screens/self_edit_modal.py`,
  `taui/self_edit/inventory.py`). A yellow modal with category tabs, each with
  a count (AGENTS, SKILLS, COMMANDS, TOOLS, PROMPTS, MCP, GENERAL), and
  `global`/`project` scope chips with the scope path shown on the right. It has
  a list pane (`✚ NEW`, `⤓ ADD FROM SOURCE` for skills, a "show built-ins"
  toggle, `OptionList`) and an inline editor. It has delete confirmation, an
  unsaved-changes prompt, and the footer
  `n new · e edit · d delete · ←→ category · tab scope · esc close`.
- **The mode** (`taui/self_edit/factory.py`, `prompts/self_edit_system.md`).
  The session swaps in a specialist prompt that documents every config file,
  and tools limited to the config roots. The option `self_edit_confirm_edits`
  gates writes behind approval.

### 4.2 Scopes

There are exactly two scopes. Both are enforced daemon-side.

| Scope | Root | Notes |
| --- | --- | --- |
| **Global** | `~/.nexus/` | Already the user layer (`config/paths.py`: `~/.nexus/config.toml`). It also holds **`credentials.json`**, which is always blocked: it can't be listed, read or written. Credentials never leave the daemon. |
| **Project** | `<project>/.nexus/` | The current workspace's `.nexus/`. |

Always blocked, in both scopes: `credentials.json`, `sessions/` (including the
archive sidecar), `cache/`, `trash*`, the daemon socket, pid and lock files,
and anything reached through a symlink that leaves the root. Keep this list in
**one** place, `host_support/settings_scope.py`, and use it from both the
host commands and the permission check for the agent (§4.5).

### 4.3 Host commands (`host/protocol.py`, logic in `host_support/settings_inventory.py`)

The UI never touches files. All commands are bounded and path-checked.

- `SettingsInventory(scope: "global"|"project")` →
  `{root_display, categories: [{key, label, count}], items: [{category, id,
  label, summary, builtin, rel_path}]}`. The root is shown as `~/.nexus` or
  `<project>/.nexus`, never as an absolute home path. At most 512 items, and
  summaries of at most 200 characters.
- `SettingsRead(scope, category, id)` → `{body, rel_path, builtin, sha256}`.
  At most 256 KiB, redacted.
- `SettingsWrite(scope, category, id, body, expected_sha256: str | None)`:
  1. **Validate first.** Agents go through the agents-manager frontmatter
     parser, tools through `ExtensionsValidate`, and skills through the
     SKILL.md parser. `hooks.toml`, `config.toml`/`nexus.toml` and
     `mcp.json` get a parse check plus a schema check.
  2. Write atomically (temp, fsync, replace). Reuse the `WriteTool` writer in
     `tools/builtin/meta.py`.
  3. Run `ExtensionsReload`, then return the diff (`loaded`, `unloaded`,
     `failed`) and any config-reload result.

  A mismatched `expected_sha256` returns `conflict`, and the file is
  unchanged.
- `SettingsDelete(scope, category, id)`. Tools use the existing
  `ExtensionsTrash`. Agents and skills go to the same trash with a restore
  record. Never do a hard delete.
- Categories:
  - `agents`: `agents/<name>.md`
  - `skills`: `skills/<name>/SKILL.md`
  - `tools`: `tools/<name>.py`
  - `hooks`: `hooks.toml`
  - `mcp`: `mcp.json`
  - `config`: `config.toml` in global scope, `nexus.toml` in project scope
  - `soul`: `SOUL.md`
  - `general`: the existing TUI preferences (theme, panels, context header).
    These are client-local (`tui.json`) and stay handled in the client as
    today.

  Built-ins are listed read-only when "show built-ins" is on.

### 4.4 UI: the Settings console

Put the widgets in a new file `nexus/ui_support/tui_settings.py`, and move the
existing `SettingsScreen` there. Wiring stays in `ui/tui/panels.py`, where the
Settings entry already lives, so `app.py` does not grow.

- **Open it with:** `/settings` (new `CommandSpec`), the palette entry, the
  existing Settings entry, and the `Edit…` buttons in the Phase 1 `ToolsModal`,
  `SkillsModal` and `McpModal`, which open straight to that category. The
  existing binding stays as is, and **no new key is added.**
- **Layout, a port of `SelfEditModal.compose`:**
  - Top row: category tabs with counts (`GENERAL`, `AGENTS 3`, `SKILLS 2`,
    `TOOLS 1`, `HOOKS`, `MCP 1`, `CONFIG`, `SOUL`). They wrap to 2 rows when
    narrow, as in taui `_layout_tabs`. Scope chips `global` / `project` sit on
    the right.
  - Scope path line, right-aligned: `~/.nexus` or `<project>/.nexus`.
  - Body: a list pane at 34% (`✚ NEW`, `show built-ins` toggle, `OptionList`)
    and an editor pane (`TextArea` with a language chosen from the category,
    plus `Save` and `Revert`).
  - `GENERAL` shows today's settings rows (theme, panels, context header)
    instead of the list and editor.
  - Footer:
    `n new · e edit · d delete · ←→ category · tab scope · ctrl+s save · esc close`.
- **Palette:** taui's yellow console (`#f0c808` accent, `#5a4500` border,
  `#0d0d0d` panel), added as `nx-settings-*` theme variables with light-theme
  counterparts.
- After a save, a status line shows the reload diff, or the validation error
  under the editor. Then refresh the agent colours (`_sync_agent_colors`) and
  the context header (Phase 1).
- Keep `ConfirmDelete` and `UnsavedChangesPrompt`, which are small ports.
- v1 skips taui's structured agent form (tool toggles, usage, color swatch,
  model picker). Agents are edited as raw frontmatter plus body. The
  structured form is a follow-up.

### 4.5 The `settings` agent (chat-driven edits, optional second step)

- A built-in agent `settings`, defined in `nexus/agents/data/`:
  - colour `#F0C808`
  - tools: `read`, `ls`, `glob`, `grep`, `edit`, `multiedit`, `write`,
    `ReloadExtensions`, `ListExtensions`, `WriteTool`, `skill`, with **no
    `bash`**
  - `contexts` includes root
- The prompt file `nexus/agents/data/settings.md` ports taui
  `self_edit_system.md` to Nexus layouts. Take every format from
  `EXTENDING.md`, the source of truth:
  - agent frontmatter keys
  - skill layout
  - `WriteTool` → `ReloadExtensions`
  - `hooks.toml`, `mcp.json`, config sections, `SOUL.md`
  - "read before edit"
  - "show the reload diff"
  - "global = `~/.nexus`, project = `<project>/.nexus`; ask which one if the
    user doesn't say"
- **Write scope enforcement:** add an agent frontmatter key
  `write_roots: [global, project]`, validated in the agents manager. In
  `tools/permissions.py` it **narrows** fs writes for turns under that agent
  to those roots, minus the blocked list in §4.2. It can never widen them.
  Reads keep the normal workspace roots. `deny` rules still win.
- **Optional approval:** `[settings] confirm_edits = false`. When true, the
  agent's mutating tools resolve to `ask`.
- **Entry:** switch agents the usual way (picker, `/agent settings`), or
  use the Settings console's `Ask agent…` button, which selects `settings` and
  focuses the composer. There is no key and no `/i`. Agent selection is
  already durable per session.
- **While it is active:** the composer gets the yellow left bar, a one-line dim
  notice above the composer reads
  `SETTINGS AGENT · writes limited to ~/.nexus and <project>/.nexus`, and the
  context header shows its prompt and tools automatically.

### 4.6 Tests

- `tests/test_host_settings_inventory.py`:
  - every category round-trips in both scopes
  - `credentials.json`, `sessions/`, `cache/`, the socket and a symlink escape
    are refused for list, read, write and delete
  - invalid agent frontmatter is refused and the file is unchanged
  - a sha conflict is detected
  - delete goes to trash and can be restored
  - no absolute home path appears in any result
- `tests/test_settings_agent.py`: the agent loads and is listed, the
  `write_roots` narrowing works, and every blocked path is refused.
- `tests/test_tui_settings.py`:
  - `/settings` opens the console
  - `GENERAL` still changes the theme and panels (move today's Settings
    tests here)
  - tabs, scope switching, new, save, diff shown
  - unsaved-changes prompt
  - `Edit…` from `ToolsModal` lands on `TOOLS`

---

## Phase 5: Other taui terminal features Nexus lacks

Each item below is taui behavior that has no equivalent in `nexus/ui/**` or
`nexus/ui_support/**` today. I grepped for each one. They are ordered by value
over cost. Each gets its own small PR.

### 5.1 Look-and-feel parity with S1

This section moved to **D1**: collapsible turns, reply footer, plain red
errors, composer info rows, and the activity bar. Build it as PRs D1a–D1c (see
the PR table).

### 5.2 Composer features

6. **Prompt history** (taui `chat_input.py:load_history`): ↑/↓ on the first or
   last line of an empty editor, or one not edited since the last recall,
   cycles previous prompts. The source is the **session's user messages from
   the view**, which is already durable, merged with a small per-user file at
   `$XDG_STATE_HOME/nexus/prompt_history`, capped at 500 entries and 4 KiB
   each. It lives in `ChatInput`.
7. **Large paste collapse** (taui `screens/pasted_content.py`,
   `widgets/attachments_bar.py`). A paste over 20 lines or 2,000 characters
   becomes a pill `[Pasted #1 · 143 lines]` above the editor. Clicking it opens
   a view and edit modal, and × removes it. On submit it expands inline. This
   is client-only and needs no host change.
8. **Double-press to quit**: ctrl+c on an idle, empty composer shows `Press
   ctrl+c again to quit` for 1.5s. ctrl+c with text in the composer clears it.
   ctrl+c during a turn cancels it, as today.

### 5.3 Commands taui has and Nexus lacks

Add a `CommandSpec`, a dispatch branch, and a host command where needed. Sizes
are a rough guide.

| Command | taui behavior | Nexus plan |
| --- | --- | --- |
| `/clear` | Clear the conversation | Alias of `/new`, which starts a fresh session. Never mutate the log. |
| `/compact` | Compact history | Only if the runtime exposes compaction. Check `core/` context management. Otherwise skip it and note it in `IMPROVEMENT_PLAN.md`. |
| `/copy` | Copy the context as JSON to the clipboard | `ContextInspect` result → JSON → `App.copy_to_clipboard` (OSC 52). Client-only. |
| `/cost` | Token usage and estimated cost | Reuse usage in `ConversationView`. Add cost only if the model registry has prices. |
| `/diff`, `/review`, `/commit` | Git workflows (`commands/git_workflows.py`) | `/diff`: host command `GitDiff(staged, ref)` → bounded patch, shown with `textual-diff-view`. `/review` and `/commit` send a templated prompt to the agent (taui does the same), and commit still needs the agent's normal permission. |
| `/hotkeys` | Keyboard reference | Nexus has it in the palette. Add the command, and add the input-prefix section (`/`, `@`). |
| `/theme [dark\|light]` | Switch theme | Settings already has it. Add the command. |
| `/verbose` | Toggle tool-output verbosity | Pref: tool previews at 10 lines versus full. |
| `/mcp` | List MCP servers | Opens `McpModal` from Phase 1. |
| `/skills` | List skills | Opens `SkillsModal` from Phase 1. |
| `/tasks` | Background subagent tasks | Map it to nexus subagents/worktrees if the view exposes them. Otherwise skip it. |
| `/resume` | Session picker | Alias of `/archived` (Phase 3). |
| `/reload` | Reload extensions and MCP | Host `ExtensionsReload` exists. Show the diff. |

### 5.4 Later (bigger, lower priority)

- **Session tabs** (taui `session_tab_bar.py`, ctrl+pageup/pagedown): several
  open sessions as tabs. This overlaps with the nexus sessions sidebar, so
  evaluate it after Phase 3.
- **Context breakdown screen** (taui `screens/context_breakdown.py`): tokens
  per section (system, tools, skills, history). Needs per-part token counts in
  `ContextInspectResult.request_context`.
- **Question panel inline** (taui `widgets/questions_panel.py`): answer the
  agent's `question` tool inline above the composer instead of in a modal.
  Check what nexus does today (`ui_support/timeline.py` mentions questions).
- **Window-blur notification**: when the terminal loses focus
  (`AppBlur`), ring the bell or send a desktop notification when a turn finishes
  or needs approval.
- **McpConnect** host command, plus the Connect button in `McpModal`.

---

## Order of work and PR slicing

| # | PR | Depends on | Size |
| --- | --- | --- | --- |
| 0 | Theme vars (§0.1), including `nx-list` | — | S |
| D1a | Turn block + chevron/collapse, body spacing, Markdown colors, plain error, reply footer | 0 | M |
| D1b | Composer box: no border or left bar, info rows 1–2, `context_usage` format, remove path/hint, `ConnectionStatus` only when not OK | 0 | M |
| D1c | Activity bar | D1b | S |
| 1a | Host: `ToolSpec.group`, inspect `group`/`bundle`/`mcp_servers`/skill scope | 0 | S |
| 1b | UI: `ContextHeader` + 4 modals, retire `ContextPreview` | 1a | L |
| 2 | `ListPanel` (D2) for slash, `@`, args, agent/model/effort | 0, D1b | M |
| 3a | Session layer: archive state, sidecar, `archive_stale` sweep, `purge_expired` exclusion, `session.forked`, summary fields | — | M |
| 3b | Host: `SessionArchive`/`Unarchive`/`ListArchived`/`Preview`/`Search`, `archived_count`, config `auto_archive_days` | 3a | M |
| 3c | UI: both sidebars show archived sessions and Delete controls; Textual adds the `Archived · N` browser entry | 3b | M |
| 4a | Host: settings scope + blocked list, `Settings{Inventory,Read,Write,Delete}` | 0 | M |
| 4b | UI: Settings console (grows the existing `SettingsScreen`), `/settings`, `Edit…` buttons | 4a, 1b | L |
| 4c | `settings` agent + `write_roots` narrowing | 4a | M |
| 5.x | One PR per item in Phase 5 | varies | S–M each |

Each PR must pass:

```sh
.venv/bin/python -m pytest -q
ruff check nexus tests
.venv/bin/python tests/playwright_tui_check.py
.venv/bin/python -m pytest -q tests/test_ui_layering.py tests/test_phase3_exit.py tests/test_layering.py
```

Also:

- Check line budgets after every UI PR: `find nexus/ui -name '*.py' | xargs cat | wc -l`
  must stay under 5,000, and `app.py` + `ui/cli/` + `jsonl.py` under 2,400.
- Take a screenshot with `app.run_test(size=(200, 55))`, then
  `export_screenshot()`, then render it through Playwright. Compare against S1,
  S2 and S3 (see D3) and save to `artifacts/visual-tui/`.
- Update `docs/textual.md` (file map, "Look" section, common changes),
  `README.md` (commands and keys), `AGENTS.md` rule 2 (the new `ui_support/tui_*`
  files), and `EXTENDING.md` (`write_roots`, `ToolSpec.group`, `[sessions] auto_archive_days`, `[settings] confirm_edits`).

## Decisions made during implementation

1. **Archive marker storage (Phase 3a).** Archive markers use the sidecar
   `.nexus/sessions/archive.json`, so archiving never writes to session logs.
2. **Auto-archive threshold.** The default is 2 days of inactivity
   (`[sessions] auto_archive_days`, where 0 turns it off).
3. **Real delete.** Resolved: both sidebars show a per-session Delete control
   that moves the session to restorable trash. Archive remains a separate state.
4. **The `settings` agent (4c).** The built-in settings agent ships with the
   Settings console and scoped write roots.
