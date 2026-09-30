> Browser finish update: `docs/web.md` is authoritative for the current web UI.
> Use system sans-serif for interface text, Monaspace Argon for code, stroke
> icons, rounded controls and soft dialog shadows. Keep the TUI region order
> and action placement. The Signal specification below records the original finish.

# Nexus web app design

## Purpose

This is the visual spec for `nexus web`. The browser client is the Textual shell
(`nexus chat`) in a browser. It has the same regions in the same places, the same
commands, keys, and wording ([docs/web.md](docs/web.md), "Parity with the TUI").
This document only decides how it **looks**: a modern finish on a terminal
layout.

The visual language is called **Signal**. It comes from four reference
boards: a flat near-black canvas, square hairline frames,
five saturated signal colors, and monospace labels in capitals. Color is carried
by small solid swatches, tinted tags, outlined bars, and buttons with a hard
offset shadow. Nothing is rounded, blurred, or glossy.

It changes no session, daemon, permission, or model behavior.

## Principles

1. **Same place, new finish.** Every region, control, and label sits where the
   TUI puts it. If a design idea would move something, it is wrong for this app.
2. **Square and exact.** Corner radius is 0. Lines are 1px hairlines. Shapes
   snap to the 8px cell and 20px row, like the terminal they mirror.
3. **Color is signal, not decoration.** The canvas is monochrome. Each of the
   five signal colors has one job (see "Signal roles"). A color on screen tells
   the reader something.
4. **Depth by offset, never by blur.** Raised things (primary buttons, floating
   dialogs) cast a hard, solid offset shadow. No soft drop shadows, glows,
   gradients, or glass.
5. **Conversation first.** Frames and tags stay quiet around the
   transcript. The strongest color on screen is either the current action or a
   state that needs the user.
6. **One shared reality.** Render the host's projection. The browser never
   recreates session state or infers permissions.

## Non-goals

- No layout that differs from the TUI: no moved panels, extra toolbars, avatars,
  chat bubbles, or right-aligned user messages.
- No rounded corners, pills, or circles except where a glyph itself is round.
  Status dots are squares.
- No blurred shadows, gradients, glassmorphism, or ambient animation.
- No background pattern. The reference boards' drafting grid is **not** used;
  every surface is a flat solid color.
- No remote fonts or assets. The CSP allows only `'self'`.
- No display-size marketing type (the "Visualize" / "Grafana" boards). The app
  keeps Monaspace Argon at UI sizes. Large type appears nowhere in the shell.
- No browser-side reasoning, permission checks, tool execution, or event
  reduction.

## Reference boards and what we take from them

| Board | What we take | Where it lands |
| --- | --- | --- |
| "Your platform + core pillars = endless configurations" | Square hairline frames on a flat dark field (the grid behind them is left out). Capitalized labels joined by rule lines (`LABEL ──── + ──`). A light 1px frame around the focused region. | Section headings with rules, composer focus frame, dialog frames |
| Swatch · tag · bar legend | The row pattern **solid square swatch, tinted tag with bright text, outlined bar with tinted fill**, one color per row. | Context-usage legend, details sidebar counts, context/tools dialog summary, status tags |
| "GET STARTED →" buttons | Tinted fill, bright 1px border, bright capitals, arrow on the right, and a **solid offset shadow** in a deeper shade of the same color. | Primary actions: new session, approval choices, dialog confirm buttons |
| Pixel constellation / "Visualize" nodes | Small square nodes with a tinted halo square and a bright core, joined by thin colored lines. | Agent and tool status glyph treatment, the working indicator, and the empty-session mark |
| Solid banner strips over type | Solid bright fill with near-black text. | Context-header chips (already solid in the TUI), the Needs-approval top-bar tag, selected palette row key |

## Information architecture (unchanged from the TUI)

The regions, their order, and their sizes come from `ui/tui/app.tcss` via
[docs/web.md](docs/web.md). One terminal cell = 8px wide; one row = 20px here
(16px in the terminal, taller for readability).

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ▌  Session title                         Context  Logs  Export  [IDLE]  +  ▐ │ top bar 44px
├──────────────┬─────────────────────────────────────────────┬─────────────────┤
│ [+ NEW SESS.]│  ▚ SYSTEM PROMPT  …                         │ SESSION ─────── │
│ Filter…      │  ▚ TOOLS  …                                 │ Status   idle   │
│ SESSIONS ─ 2 │  ▚ SKILLS                                   │ Agent    Build  │
│ ● Title  now │  ▚ MCP                                      │ …               │
│ · Title  2h  │ ┌─────────────────────────────────────────┐ │ MODIFIED FILES ─│
│              │ │▼ user prompt                            │ │ M notes.txt +1-1│
│              │ └─────────────────────────────────────────┘ │ MCP SERVERS ─── │
│              │  → Read notes.txt                           │                 │
│              │  ← Edit notes.txt   split diff              │                 │
│              │  assistant markdown                         │                 │
│              │  BUILD · model · 1.2s                       │                 │
│              │ ┌─────────────────────────────────────────┐ │                 │
│ ↵ open · …   │ │ editor                                  │ │                 │
│ ■ Live sync  │ │ BUILD model provider effort             │ │                 │
│   ⟳ ⌘ ⚙      │ └─────────────────────────────────────────┘ │                 │
│              │  ▬▬▬▬▬▬▬───────────────── activity bar       │                 │
└──────────────┴─────────────────────────────────────────────┴─────────────────┘
   272px (34 cells)          flexible                           336px (42 cells)
```

| Region | Contents (same as TUI) | Web ids |
| --- | --- | --- |
| Top bar | `▌` sessions toggle, title, Context / Logs / Export, status, `+`, `▐` details toggle | `.topbar`, `#sidebar-toggle`, `#inspector-toggle` |
| Sessions sidebar | New session, filter, day-grouped rows, Archived group, hint line, connection + Reconnect / Commands / Settings | `#sidebar` |
| Conversation | Context header (four blocks), turns, tool blocks, reply footers, approvals, composer, activity bar | `#timeline`, `#approval-strip`, `#composer-form` |
| Details sidebar | Tabs (Session, Tools, Agents, Trees, Logs); `SESSION`, `MODIFIED FILES`, `MCP SERVERS` | `#inspector` |
| Overlays | Command palette, pickers, text dialogs, context/tools dialog, Settings | `.overlay`, `.dialog` |

Responsive behavior is also unchanged: sessions dock from 960px, details from
1280px, otherwise they open over the chat column; below 700px the top bar keeps
only Logs; no horizontal page scroll (asserted in
`tests/playwright_web_check.py`).

## Visual system

### Canvas

- Every surface is a flat, solid color: `--canvas` for the conversation,
  `--sidebar` for the top bar and both sidebars, `--elevated` for the composer,
  user blocks, and code. No grid, texture, or gradient.
- Frames and hairlines, not background patterns, give the page its structure.

### Surface and color tokens

`tokens.css` keeps the **same role names** as today (they map role for role to
`ui/tui/theme.py`), with Signal values. Dark is the default.

| Token | Dark | Light | Use |
| --- | --- | --- | --- |
| `--canvas` | `#141414` | `#f4f4f2` | Conversation background |
| `--sidebar` | `#0f0f0f` | `#ececea` | Top bar, sessions, details |
| `--elevated` | `#181818` | `#fafaf8` | User block, composer, code blocks |
| `--raised` / `--hover` | `#1f1f1f` | `#e6e6e2` | Hover |
| `--active` | `#262626` | `#dcdcd7` | Selected neutral row |
| `--border` | `#2e2e2e` | `#d2d2cc` | Hairlines |
| `--border-strong` | `#4a4a4a` | `#a9a9a2` | Dividers, controls |
| `--frame` | `#d6d6d6` | `#1a1a1a` | Focus frame (composer focus, open dialog) |
| `--text` | `#ededed` | `#141414` | Body |
| `--muted` | `#a0a0a0` | `#555555` | Secondary |
| `--quiet` | `#6b6b6b` | `#8a8a86` | Metadata, hints |

### Signal roles

Five signal colors plus one alarm color. Each has three derived shades:
**tint** (tag and bar fill), **ink** (bright text on tint, equal to the signal),
and **shade** (offset shadow).

| Signal | Dark | Light | Role (same meaning as the TUI) | Token |
| --- | --- | --- | --- | --- |
| Orange | `#f55b1b` | `#cc4a10` | Accent: current selection, primary action, working | `--accent` |
| Cyan | `#3dbefa` | `#0b7fb5` | Info: tools, links, the Tools context share | `--info` |
| Yellow | `#fdd329` | `#9a7400` | Warning: needs approval, context warning, Settings accent | `--warning` |
| Magenta | `#f274f5` | `#b03ab4` | Purple: agents, thinking, system-prompt share | `--purple` |
| Green | `#7ee34a` | `#3a8a18` | Success: done, allowed, added lines | `--success` |
| Red | `#ff4d6a` | `#cf2448` | Danger: errors, deny, removed lines | `--danger` |

Derived shades are computed, not hand-copied:

```css
--accent-tint:  color-mix(in srgb, var(--accent) 20%, var(--canvas));
--accent-shade: color-mix(in srgb, var(--accent) 55%, #000);
```

(`--info-tint`, `--warning-shade`, … follow the same formula. In light mode the
tint uses 14% and the shade 70% so it still reads as a darker step.)

The existing `*-soft` tokens stay as aliases of the tints. The agent color
(`--agent-color`, from the host or the TUI's name hash) follows the same
tint/shade rules via `color-mix` at the point of use.

Contrast: bright ink on its own tint must meet WCAG AA for 13px text. The dark
values above do; in light mode use the darker values listed. Never rely on hue
alone. Each state also has its word or glyph.

### Type

- One family: **Monaspace Argon** (vendored, 400). Bold is synthesized; use it
  only where the TUI is bold (titles, selected rows, agent names).
- Body and all UI text: 13px on a 20px row.
- **Labels** (section titles, tags, status, reply agent, dialog titles): capitals
  with `letter-spacing: .06em`, 12–13px. This is the reference boards' voice:
  `SESSION`, `MODIFIED FILES`, `NEEDS APPROVAL`, `BUILD`.
- Session titles, messages, file paths, and code are **never** capitalized by
  CSS.

### Shape

- `--radius: 0` and `--radius-sm: 0`. Every border-radius in `app.css` and
  `context-preview.css` resolves to 0, including kbd, chips, dialogs, toasts,
  segmented controls, bars, and dots.
- Hairlines are 1px. A 2px bar marks the current item (left edge of the active
  session row, the selected palette row, the selected details tab underline).
- Status dots (`.connection-dot`, `.mcp-dot`, legend keys) are **squares**
  (6px, or 8px in legends), the swatches of the reference board.

### Depth: the offset shadow

The one elevation primitive. It is solid, has no blur, and falls down and to
the right:

```css
box-shadow: 4px 4px 0 var(--btn-shade);      /* buttons */
box-shadow: 8px 8px 0 #000;                  /* floating dialogs, dark */
box-shadow: 8px 8px 0 var(--border-strong);  /* floating dialogs, light */
```

- **Hover** lifts: `translate(-1px, -1px)` and the shadow grows to 5px.
- **Press** lands: `translate(4px, 4px)` and the shadow goes to 0. The button
  visibly drops into its shadow.
- `prefers-reduced-motion: reduce` keeps the colors and drops the translation.
- Docked panels (sidebars, top bar, composer) never cast shadows.
  Only things floating above the page do.

### Motion

- `--fast` 120ms, `--ease` `cubic-bezier(.2, .75, .25, 1)`. Transitions cover
  color, background, border, transform, and box-shadow only.
- Streaming updates text in place. No height animation per token.
- The working indicators are **stepped**, never smooth, matching the terminal:
  the braille spinner, and the activity bar sweep in 8px steps
  (`steps()` timing).
- Reduced motion removes transitions and the sweep. The status word remains.

### Focus and selection

- `:focus-visible`: 1px `--frame` outline plus a 2px offset, square. On signal
  buttons the outline takes the button's ink.
- Text selection: `--accent` at 35%.

## Components

Each component names its TUI counterpart. Behavior and wording come from that
counterpart.

### Primitives

**Tag** (`.tag`, also used by existing classes below): inline, 0 radius,
`padding: 0 8px`, tint background, ink text, capitals. Variants by role:
`accent`, `info`, `warning`, `purple`, `success`, `danger`, `neutral` (grey
tint, muted text). **Solid tag**: signal background, `--on-signal`
(`#111`) text. Used for the context-header chips and the Needs-approval top-bar
status.

**Swatch**: an 8×8 (legend) or 6×6 (status) solid square in a signal color.

**Bar**: a tinted rectangle with a 1px signal-colored border. Its width is the
quantity. Used for context shares.

**Signal button** (`.btn-signal`): tint fill, 1px ink border, ink text,
capitals, trailing `→` or `↵` where the action goes somewhere, offset shade
shadow. Neutral variant uses `--active` fill, `--border-strong` border, and
`--border` shadow.

**Section heading with rule**: `LABEL` + a 1px rule filling the rest of the
line + an optional count. Example: `MODIFIED FILES ──────────── 1`. Uses a flex
row with an `::after` rule. Used for sidebar groups and details sections.

### Top bar (`TopBar`)

- 44px, `--sidebar` surface, 1px bottom hairline.
- `▌` and `▐` are square 28px toggle buttons. Off: quiet glyph. On (panel
  visible): accent glyph on a 2px accent underline, as in the TUI where toggles
  are accent-colored while their panel is visible.
- Title: 13px bold, sentence case, ellipsized. `New session` before the first
  message.
- Context / Logs / Export: plain quiet text buttons, hover to `--text` over
  `--active`.
- **Status** is a tag: `IDLE` neutral, `WORKING` accent tag with the stepped
  spinner, `NEEDS APPROVAL` solid yellow tag, `FAILED` danger tag.
- `+` is a square glyph button.

### Sessions sidebar (`SessionSidebar`)

- 272px, `--sidebar`, 1px right hairline.
- **New session** is a full-width accent signal button: `+ NEW SESSION` left,
  `ctrl+n` right in its ink at 60% opacity, 4px offset shadow.
- Filter: square field, 1px `--border`, focus shows the `--frame` hairline.
- Group headings use the rule heading: `SESSIONS ─────── 2`, `ARCHIVED ─── 1`,
  grouped by day exactly as `/sessions`.
- Rows, 28px: glyph column (`●` current, braille spinner working, `✓` done, `·`
  idle, `◇` archived), title, status or age, `×`. Glyph colors follow signal
  roles (accent working, yellow input, green done).
  - Hover: `--raised`.
  - Current: `--accent-tint` fill with a 2px accent left bar, title bold.
  - `×` hover: danger tint square.
- Footer: 6px square connection swatch (green live, red offline) + label, then
  Reconnect / Commands / Settings as 24px square icon buttons.

### Conversation canvas

The timeline sits on the flat `--canvas`. Content keeps a 16px gutter.

**Context header** (`tui_context_header.py`), four blocks: `SYSTEM PROMPT`,
`TOOLS`, `SKILLS`, `MCP`. The chip is a **solid tag** in the agent's color
(neutral grey when empty), as the TUI draws it. The body sits on one shared
indent. Hover gives the whole block an `--active` fill with a 1px `--border`
frame. The Tools body stays a column grid of tool names.

**User block** (`UserMessage`): `--elevated` panel, 1px `--border` frame,
0 radius, 2px left bar in `--agent-color`. The `▼`/`▶` chevron toggles the turn
fold, as in the TUI. It spans the column (not a right-aligned bubble).

**Thinking** (`ThoughtLine`): `Thought:` in magenta ink at 70%, expands to muted
text under a 1px magenta left rule.

**Tool blocks** (`ToolActivityWidget`): glyph + capitalized tool name + target,
then output and `Click to expand`. The glyph gets its state color (accent
pulsing while running, danger failed). Hover shows a `--raised` fill with a 1px
`--border` frame. Failed tools show a
`FAILED` danger tag at the end of the heading.

**Diffs**: the TUI's side-by-side split is kept. Cells are square. The number
gutter is `--raised`. Added lines get `--success` tint with a green `+`.
Removed lines get `--danger` tint with a red `−`. The dashed separator above
the split stays as a 1px dashed `--border-strong` rule. Unified diffs in the
details panel follow the same colors.

**Assistant Markdown**: headings in accent ink, capitals, with a trailing rule
line (the rule heading). Bullets are `▪` in accent. Inline code is yellow ink
without a fill. Code blocks are `--elevated` with a 1px `--border` frame and 0
radius.

**Reply footer**: agent as a tag in the agent color (`BUILD`), then
`model · 1.2s` quiet. Failed / cancelled show a danger / warning tag.

**Agent cards** (`TaskActivityWidget`): a magenta constellation node as the
glyph (6px bright core in an 14px magenta-tint square with a 1px magenta
border), agent name, task, and a status tag. Opening the child transcript is
unchanged.

**Errors / turn boundary**: plain danger lines, as in the TUI, with a 2px danger
left bar.

### Approvals (`PermissionScreen`)

- The card replaces nothing and sits where the TUI puts it, above the composer.
- Frame: 1px yellow border, `--elevated` fill, a solid yellow heading tag
  `APPROVAL NEEDED · WRITE`, and an 8px yellow-shade offset shadow (it is an
  interrupt, so it floats).
- Target rows: quiet labels and text values on `--canvas`.
- Choices are signal buttons in TUI order: **Allow once** (green), **Allow for
  session** (neutral), **Deny once** (red, default focus), **Deny for session**
  (neutral). Escape still means Deny once.

### Composer (`ChatInput` + `RootAgentBar`)

- Square frame, `--elevated`, 1px `--border`. When focused the frame becomes
  1px `--frame`, the light frame from the first board.
- Row 1: transparent editor, 13px, `--quiet` placeholder.
- Row 2: agent (bold, agent color), model, provider (italic muted), effort,
  hint, then Stop at the right while a turn runs.
- **No send button**, as in the TUI: Enter sends, Shift+Enter inserts a
  newline. **Stop** is a danger-outlined square `■ stop` without shadow, shown
  only while a turn runs.
- Row 3: context meter `3k (2%)` right-aligned, quiet (yellow when warn).
- **Activity bar** under the composer: a 2px square track in `--border`. Idle
  shows the context fill in the agent color. During a turn, an 8px-stepped
  accent block sweeps across (yellow while waiting for approval).

### Slash menu and pickers (`ListPanel`)

Square panel on `--list`, 1px `--border`. Rows are 28px with dim text. The
selected row gets `--active` fill, a 2px accent left bar, and bold text. The
scrollbar thumb is accent, like the TUI's orange scrollbar.

### Details sidebar (`DetailsSidebar`)

- 336px, `--sidebar`, 1px left hairline.
- Tabs (browser-only affordance, kept): capitals, quiet. The selected tab is
  accent with a 2px accent underline.
- Sections use the rule heading: `SESSION ───`, `MODIFIED FILES ─── 1`,
  `MCP SERVERS ───`.
- `SESSION` rows keep the `11ch` label column. Status shows as a tag. The
  `Context` row adds a **bar** (outlined, tinted by agent color) under the
  value, from the same `context_usage` numbers.
- Modified files: `▸` chevron, `A`/`M` kind in green/yellow, path, `+n −n`.
  Expanded file uses the unified diff on `--canvas`.
- MCP rows: square health swatch (green ready, yellow connecting/degraded, red
  failed, quiet unknown).

### Context and Tools dialogs (`ContextDetailsScreen`, `ToolsModal`)

- The summary uses the **legend board**: one row per share (System, Tools,
  Conversation), each with a swatch, a tag with the name and token count, and a
  bar proportional to its share. System is magenta, Tools cyan, Conversation
  orange.
- Groups and entries below are unchanged collapsible rows with square hover
  fills.

### Overlays and dialogs

- Scrim: `--scrim`, no blur.
- Dialog: `--sidebar` surface, 1px `--frame` border, 0 radius, 8px offset
  shadow. No fade-scale. It appears in one 120ms opacity step (none with reduced
  motion).
- Dialog titles use capitals in accent ink with a rule line.
- Command palette: square input row, list rows as in the slash menu, key hints
  as square kbd tags in the footer.
- Settings: left nav (`Appearance`, `Layout`, …) with the selected item in
  `--active` and a 2px accent left bar. Radio cards and segmented controls are
  square. The checked card gets a 1px accent border and a 4px accent-shade
  offset shadow. Theme swatches show the canvas and signal colors, not generic
  shapes.
- Toasts: bottom right, square, `--raised`, 1px `--border-strong`, 4px offset
  shadow.

### kbd

A square neutral tag: `--elevated` fill, 1px `--border-strong`, 11px, no bottom
"key" edge (flat, like the boards).

### Loading, disconnected, and error states

Same wording and places as today. The connection banner is a full-width yellow
tint strip with yellow ink. The sync error is a red tint strip. Skeleton rows
are static `--raised` blocks, without shimmer.

### Empty session

The TUI shows the context header and the composer. The web does the same. The
only decoration allowed is a small magenta/cyan/orange constellation mark
(pure CSS squares, ≤48px) left of the first empty-state line. It is
`aria-hidden` and has no motion.

## Light theme

Light keeps every rule above: an off-white canvas (`#f4f4f2`), near-black text, darker signal values (table above), and
offset shadows in `--border-strong` for neutral elements or the role shade for
signal buttons. The focus frame becomes near-black.

## Three-level presentation model

The setting is a presentation preference over the same complete browser projection. Each control changes visibility, grouping, or default expansion. It never changes what the daemon records, what the reducer computes, what permissions allow, or what any other client sees.

| Information | Focused | Balanced (default; “Recommended” is supporting text only) | Complete |
| --- | --- | --- | --- |
| Assistant narration | Final/user-facing response remains complete. Intermediate assistant narration is a short preview (maximum 2 lines or 240 characters), expandable to full exposed text. The trailing message of an active turn remains full and visibly marked as streaming. | Full assistant prose, naturally grouped by message; intermediate progress prose remains visible. | Full prose and every distinct exposed assistant message, including intermediate narration. |
| Reads and searches | Adjacent successful activity may aggregate. Summary counts only when tool type is known; expand to each call. | Individual calls, compact card; collapse completed low-impact details by default. | Every call individually represented; any aggregate is only a navigation summary and is expanded by default. It never replaces or hides individual call cards. |
| Commands | Adjacent calls can aggregate as `Ran N commands`; failures/running calls stay individually visible. Expand for exact displayed command and result. | One compact card per call with status and short result preview. | Every call individually represented; any aggregate is only an expanded-by-default navigation summary, never a substitute for calls. Show all available parameters, progress, bounded result, status, and timing. |
| Edits and diffs | Summary may say `Edited N files` only with reliable file identity/count. Diff collapsed; file list and every available hunk are one deliberate expand away. | Edit card per call/file when IDs permit; show path and added/removed counts if supplied; diff collapsed by default. | Every edit call/file is individually represented; all host-exposed diff lines/hunks are shown in the inspector by default. A user may locally collapse long content; it starts expanded and the edit identity/card remains visible. Never exceed host caps. |
| Tool parameters/results | Hidden from summary except safe bounded preview. Per-call expand reveals all available sanitized parameters/results. | Status plus concise preview; parameters/results expandable. | Every call’s available sanitized parameters/results are expanded and visible by default. A user may locally collapse long payload content; cards stay individually represented and start expanded, subject to explicit host clipping/bounds. |
| Nested agents | Identity/state/task always visible; narration preview max 2 lines or 240 chars; active trailing narration remains full and visibly streaming; child transcript opens on demand. | Agent card with task/state and short latest/final preview; open child transcript. | Rich exposed lifecycle/progress, nesting, model/usage metadata, and full child transcript access. Each child tool call is individual; summaries are expanded navigation only. Do not invent unavailable events. |
| Reasoning / thinking, if exposed | Hidden behind an explicit “Provider-supplied thinking” disclosure; never included in aggregate preview. | Collapsed by default with explicit label. | Visible as a separate labeled block; never blend into user-facing assistant prose. |
| Timestamps / metadata | Turn start/end and current running/approval/failure states remain visible; per-call times and low-value metadata in expansion. | Message times and meaningful call duration/status available on card or focus; secondary metadata in inspector. | All exposed timestamps, IDs, model/provider, stop reason, usage, and call metadata displayed in quiet labeled metadata. |
| Inspector | Overview remains available; tools/agents list condensed, select any entry to inspect. | Overview, Tools, Agents with normal summaries and expandable records. | Inspector exposes all host-projected session/tool/agent diagnostics and details within host bounds. |
| What is never hidden | User messages, final assistant answer, pending approval/action, active/running status, errors, denial, cancellation, connection-loss notice. | Same | Same |

**No loss rule:** collapsed or summarized content remains in the in-memory projection and durable host record. A visible “Show details” / “Expand N activities” control provides access. Export uses the existing host export response and is independent of the setting.

### Assistant narration classification

Use fields already present in the web projection: each `MessageView` has `role`, `event_seq`, `done`, and text/blocks; its enclosing turn has `phase`. Within a turn, order assistant messages by `event_seq`, with stable message ID as a tie-breaker. Apply the rule independently per agent transcript/turn:

1. **Active turn** (`phase === "active"`): the last assistant message with `done === false` is the trailing in-progress message. Render its entire current text without preview truncation, label it “Streaming”, and keep it visible while it grows. Earlier assistant messages are intermediate and may use Focused previews. If no unfinished assistant message exists (for example while tools run), do not infer completion; render existing messages and keep turn status visible.
2. **Completed turn** (`phase === "completed"`): the last assistant message in order is final/user-facing and always fully visible. Earlier assistant messages are intermediate and may use Focused previews. If there is no assistant message or its text is empty, show no fabricated answer; retain completed state and other available turn content.
3. **Failed or cancelled turn**: do not infer a final answer. Render assistant messages in full and keep explicit failure/cancellation outcome and any host error visible. No narration preview may obscure the outcome.
4. **Unknown phase, missing `done`, ambiguous order/identity, or inconsistent state**: fail open by rendering full assistant messages and a visible status/metadata label. Do not collapse based on text content, count alone, or guessed terminal state.

The current projection supplies the needed fields, so client derivation is deterministic; no host schema addition is required. Apply this classification to nested agents only when their projected turn phase/messages are available; otherwise show all exposed narration in full.

## Focused-level activity aggregation

### Eligibility and boundaries

1. Derive the timeline from projected message/tool/agent records and their stable IDs/order metadata. Do not concatenate raw events or run a second reducer.
2. Aggregate only contiguous tool activity belonging to the same session, turn, and agent scope. A group consists of adjacent eligible tool calls in canonical timeline order; preserve original order within the group.
3. Flush the group before any assistant prose/message, user message, approval request or decision, error/failure, turn completion/cancellation, agent/task boundary, unknown activity type, or explicit projection gap/resync boundary. Session sequence numbers may legitimately skip between event frames; a numeric sequence gap alone is not evidence of missing projection data.
4. Never combine across turns, sessions, parent/child agent boundaries, or an item whose ordering is unknown. Unknown order means render individual cards.
5. Pending approvals, failed calls, cancelled calls, and currently running calls are not absorbed into a completed summary. A running call is an individual live row; previously completed adjacent calls may form a group before it.
6. Keep Edit/MultiEdit identity recoverable per call/file. A group may summarize edits only if each underlying call remains individually expandable and every exposed diff remains available in its detail view.

### Summary grammar and honesty

Use one compact line with clauses in first-occurrence order, separated by ` · `:

```text
Read 2 files · Edited 2 files · Ran 1 command
Searched 3 locations · Read 1 file
Used 2 tools
```

- Use singular/plural correctly (`Read 1 file`, `Read 2 files`; `Ran 1 command`, `Ran 2 commands`; `Edited 1 file`).
- Count calls, not inferred effects. Say “Edited N files” only when distinct file identities are supplied; otherwise say “Edited N times” or `Used ToolName`.
- Distinguish Glob/Grep/search from file reads and shell commands. Do not turn a Bash command into a file edit based on command text.
- Do not include paths, raw command text, parameters, or result snippets in a one-line summary unless the summary remains within host-provided safe display data. Details are available by expansion.
- Do not report success for unknown, clipped, running, failed, or cancelled activity. A summary’s status is `N completed` only if all members are completed; eligible groups are otherwise formed only from completed calls.

### Expansion, keyboard, and streaming behavior

- Summary is a real button/disclosure with an accessible label such as “3 completed tool activities; expand to inspect each call”. Enter/Space expands or collapses. Left/Right may collapse/expand when the control uses tree/disclosure semantics; do not hijack normal page navigation keys.
- Expanded calls retain stable `call_id` keys and individual status labels. Focus moves to the first call on expansion only when expansion was initiated from keyboard; collapsing returns focus to the summary button.
- Announce a new completed summary once through a polite live region. Do not announce every token, every progress update, or the full summary again on unrelated repaint.
- Build groups from stable projection identity and canonical order. During streaming, keep an existing group stable while eligible completed calls append; do not move a card because counts changed. Flush at a boundary immediately. A call changing to failed/running/approval state is removed from an aggregate and restored as an individual keyed card without losing focus or expansion context.
- If a host patch causes an authoritative resnapshot, recompute grouping from the snapshot while restoring expansion by group member IDs where the exact membership still matches. Never key groups only by array index or sequence number.

## Preference persistence and scope

- Browser default resolves to Balanced when unset. Save browser default, workspace default, and session override as separate browser UI preferences, not in session log, host config, URL, export, or protocol.
- Precedence is session override > workspace default > browser default (or Balanced if unset). The active-session selector writes only the session override. Its “Use workspace default” action removes only that override. Settings configures the workspace default and has “Use browser default” to remove it; Settings configures the browser default and has “Reset browser default” to return it to unset/Balanced. Show effective level plus source and accessible scope description at each selector.
- Switching sessions resolves target session override, then workspace default, then browser default, then Balanced. Reload restores the same result. Clearing browser site data resets to Balanced. New sessions have no session override and inherit the effective workspace/browser default without writing a session fact.
- Theme and detail level are never encoded in `/s/<session-id>` or query/fragment. Browser Back/Forward changes session navigation only. Copy/share URL opens the same session with that browser’s own local preference.
- Markdown/JSON/JSONL exports are exactly the host’s session export and are independent of the rendered level. No omitted projection content may be inserted into an export by the client.
- Do not synchronize this preference across browser profiles or expose it to Textual. It cannot alter daemon events, reducer state, permissions, model prompts/selection/behavior, tool execution, queue state, security bounds, or what another attached client sees.

## Accessibility and keyboard requirements

- Use semantic landmarks (`header`, `nav`, `main`, `aside`), one `h1`, ordered headings, real buttons/links, and programmatic labels. Session list and inspector tabs follow the appropriate list/tab patterns and expose selected state.
- All functions have mouse and keyboard paths. Focus order follows sidebar → top bar → transcript controls → composer → inspector/overlay without focus traps outside modal dialogs.
- Modal palette/approval/settings dialogs trap focus while open, announce a title, close/resolve predictably, and restore focus to the invoking control. Approval Escape resolves as Deny once, with visible wording and announcement. The detail selector exposes effective value, source, write scope, and clear/override action in its accessible description. Arrow keys move among radio options; selecting in the active-session control writes the session override; Tab reaches “Use workspace default” without changing scope.
- Maintain visible focus, text contrast, non-color status labels, and at least 44×44 px touch targets on narrow/touch layouts; desktop controls are at least 36×36 px. Do not depend on hover for essential controls or metadata.
- `role="log"` is not used to read the entire transcript on every update. Announce user-visible state transitions and newly completed activity in a polite region; keep streamed token updates silent. Use assertive announcements only for an urgent actionable approval where appropriate.
- At 200% zoom and narrow view, all primary tasks remain available without two-dimensional page scrolling: switch session, send/stop, select model/agent, inspect tool/diff and child, decide approval, change detail setting, reconnect.
- Respect reduced motion. Support browser text zoom, OS contrast settings, native text selection, screen-reader names for icon buttons, and high-contrast focus boundaries.
- Diff add/remove information includes `+`/`−` text or equivalent accessible labels, not color alone. Disclosure labels report expanded state and meaningful item counts.


## Implementation notes

- Files: `ui/web/styles/tokens.css` (tokens, signal tints and shades),
  `ui/web/styles/app.css` (components), `ui/web/styles/context-preview.css`.
  No framework, build step, remote font, or image asset. Swatches, bars, and
  constellation marks are CSS only.
- Keep role token names. `tokens.css` maps role for role to `ui/tui/theme.py`
  (`nx-accent` → `--accent`, `nx-blue` → `--info`, …). The web uses the
  Signal values. If a role is added in one place, add it in the other.
- Radius: the tokens are 0, and no literal non-zero `border-radius` remains in
  the web stylesheets.
- Keep every id and class that `tests/playwright_web_check.py` selects on (list
  in [docs/web.md](docs/web.md)). Restyle; don't rename.
- Untrusted text still goes through `textContent`. Capitals are CSS
  `text-transform`, never string changes, so copy/paste and screen readers get
  the real text.

## Acceptance criteria

- Region positions and sizes match the TUI: top bar 44px, sessions 272px,
  details 336px, docking at 960/1280px, overlays below that, Logs only under
  700px, no horizontal scroll at 1440, 1024, and 400px.
- No element in the shell renders a non-zero `border-radius` (Playwright:
  query all elements and check computed `border-*-radius`).
- No surface has a background pattern, and the composer has no send button.
- Primary buttons show the offset shadow and press into it. Reduced motion shows
  no translate.
- Every signal color appears only in its role. Text on tints meets AA in both
  themes.
- Screenshots at 1440×900, 1024×768, and 400×844 in dark and light, plus
  palette, Settings, approval, and a diff, are reviewed side by side with a
  `run_test(size=(200, 55))` TUI screenshot of the same session (docs/web.md,
  "Working on it").
