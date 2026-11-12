# TUI design language

NSH follows the Void design instead (archived plan: `git show e438b61:VOID_DESIGN_PLAN.md`; ledger row 2026-11-01):
`nexus-nsh/theme-void.toml` overrides `theme.toml` for NSH only, and the NSH sections of
these rules are historical. The rules below describe the TUI.

## Shared modal rules (2026-11-04)

User clarification (2026-11-06): NSH desktop/web keeps the TUI's control placement,
wording and behavior while using theme-tinted SVG icons and polished screen-native
surfaces, including rounded modals and picker rows. Manager consent uses a
translucent solid fill rather than backdrop blur. This presentation-only difference
is recorded in the porting ledger.

These rules apply to both clients, including NSH's Void palette. Modal widths have three
classes: small (TUI 60 cells / NSH 500 px) for confirmations and short input; medium
(72 cells / 600 px) for palettes, pickers, provider details, usage and notifications;
large (88 cells / 740 px) for Settings, context documents, resources and images.
Clamp once to the window's edge gutters (2 TUI cells / 12 NSH px). Every modal is
centred vertically and horizontally, including the command palette. Content determines
the height, capped at 70% of the window; scrollable bodies reserve space for the title,
controls and footer. The existing TUI long-confirmation fallback can exceed this cap
until its body supports scrolling; never hide confirmation text just to meet a size rule.
The file editor keeps its dedicated workspace geometry, and a row menu remains anchored
to its row because it is a popover rather than a modal.

Titles use the full content width, without a duplicate Esc chip. Key hints occupy the
last row: navigation, primary action, then dismissal. Use **close** to leave a modal,
**back** to return one level, and **cancel** to discard input. The TUI uses `hint_row`,
NSH `ModalFooter`; input dialogs use the same footer as other modals. Settings selection uses `interactive.settings_selection`: a neutral gray band across the
whole section or setting row (including its value). A white left bar marks the focused
pane (a dark bar in the light theme); inactive selections retain their gray background. NSH uses
the same fill for selected sections and hovered or keyboard-focused setting rows.
Settings values are segmented controls, as NSH draws them: the options sit in a quiet
well at the row's end, with the current one on `interactive.selection`. In the TUI
Left/Right move the focused segment (wrapping at the ends, as the old cycling did) and
Tab or Esc returns focus to the section heading; in a detail page Left still returns to
its heading. Shift+Right also steps a value backward, and model effort cycles back to
default after the final level. The search field retains normal caret movement. Settings uses a compact section rail with no empty rows between sections and keeps
its search and section rail visible while its rows scroll. This first pass establishes
the shared geometry; a complete footer, focus and provider/tool-grid review is still
tracked in PLAN.md 8.0.

Working uses the shared `[motion.shimmer]` recipe: a 12-frame pass at 125 ms per
frame (1.5 s), secondary text at rest and `text.highlight` at the crest (white in
dark mode, dark in light mode). Both clients use the same travelling smoothstep
curve and retain plain primary text when reduced motion is enabled.

## TUI surfaces

The visual reference is the old Nexus TUI in `../nexus/rust/tui/src`. This
client adapts its patterns to the Core API and the one-timeline model. The
shared source of colours, terminal fallbacks and glyphs is `theme.toml`.

- Chrome and page: the header band and both sidebars (and the rail) share one
  chrome surface, `surface.sidebar`; the centre column is the page,
  `surface.canvas`. No rule separates them: the surface step is the edge, and
  only where surfaces collapse (ANSI-16, monochrome) a `│` in `border.default`
  is drawn at each sidebar edge. `border.strong` is decorative (diff split rule,
  tool-detail box, block-quote rail). Under a modal the buffer is dimmed by `paint::scrim`: each
  cell's colours move 50% toward the canvas, or `DIM` where colours cannot be
  blended. Panel surfaces hold the composer; raised surfaces hold user messages and modals.
  One modal recipe (palette, pickers, Settings, confirms, help, notifications): the
  scrim over the whole frame, then a borderless `surface.raised` block with 2 cells of
  padding left and right and 1 row top and bottom; the title (bold) is the first row
  inside, key hints (bold key, muted label, including Esc) the last row;
  at most 70% of the frame high and within 2 cells of its edge. The fill resets
  modifiers. Rows use the selection band and the `▌` focus bar, section headings are
  UPPERCASE bold muted, a disabled row is muted with its reason in the footer.
  Borders are quiet hairlines. A focused row uses the focus bar as well as its selection
  surface so the state remains visible without colour.
- Primary text carries content and headings. Secondary text carries labels;
  muted text carries metadata and instructions. Status colour is paired with
  a word or glyph. Agent colours identify agent names, not whole rows.
- The composer is a soft card (half-row edges, no rail, no status band) whose
  control row names the agent in its colour and ends with the `╱` meter and
  `82k · 41%`, explained on hover (tokens, price tier and window). A model change requested during a
  turn is marked until the next call; narrow rows shed effort and provider
  before names or the agent.
- Timeline items follow model context order. The user message is a band across
  the whole page width on the raised surface: the rail `▌` at its left edge
  (muted, the focus colour when focused), padding rows above and below, the fold
  marker inside at the text edge and `#n · time` at the right. Once its text has
  scrolled off, a copy is pinned under the header (three rows at most, then a `░`
  shade row in `border.strong` with `↑ N lines of this turn above`), pushed up by
  the next band. The turn footer is one muted right-aligned row,
  `▪ Agent · models · effort · tools · duration · tokens · cost`, with no rule,
  no end status and no continue hint (a failed one puts a `✕` error row above);
  the usage units (`i`, `o`, `r`, `c`) are fainter than their counts. Durations
  read `0.4s`, `4.2s`, `1m 05s`, `1h 02m`. Consecutive activity folds into a verb
  group; a live group is `Working` (old Nexus's soft shimmer) over its newest five
  members in a `├─`/`╰─` tree, a finished one a count by verb with `+N −M` and
  failures that stay visible. Members lead with a verb glyph (`[glyphs.verb]` in
  `theme.toml`) that the status glyph replaces while running, failed, denied or
  waiting; their detail is the old `┌ │ ╰` box in `border.strong` with capped
  output and a `raw` toggle. A single-item row shows that item's own icon where a
  group's fold would be: a thought is `◇ Thought · preview` (ASCII `o Thought`;
  italic muted preview, with raw Markdown emphasis stripped) and one finished
  background shell is `$ Background Shell Done: … · show full output` (the `→`
  prefix is gone). Groups with more than one item keep `▸`/`▾`. Folded content
  remains accessible, and any clipped output is labelled. Reduced motion and
  monochrome stay static.
- A finished background subagent is one row: the child glyph `⣿` in the agent's
  colour, `Subagent done`, then `· Agent · title` muted, and `failed` or
  `interrupted` in the error colour. Its modal is titled `Subagent done`, shows
  the agent, title, model and duration, then the full notice text exactly as the
  model received it (scrollable and selectable), and its footer action opens the
  child session (`enter open session` and `esc close` in the TUI; an
  `Open session` button and `esc close` in NSH). Other context notices keep
  `context · … · show details`.
- Turn history floor: where a session's older turns have no stored turn records (pruned by retention or
  older than turn records), one muted row at the start of the loaded history says `the turns above have no
  recorded details (tokens, models, timings): pruned, or older than turn records` (TUI `…` prefix, NSH the
  same text). It replaces per-turn footers; no event log is read to rebuild them.
- The header row is a band on the chrome surface. The left toggle is at x=1,
  then the Sessions / Files / Projects icons in the band over the left sidebar
  (the active icon bold primary, the others muted; label on hover). Session
  tabs start at the centre column: `{glyph} {title} ×` per open session
  (spinner running, `◆` waiting, `●` unread, `✕` failed, `·` idle). The viewed
  tab is bold primary on the page surface so it joins the page below; others are
  muted on the band; `+` ends the strip. A viewed child session shows
  `root › child` with the earlier crumbs clickable. The run status (`◐ working 12s`, `◆ waiting for you`, `✕ offline`) is right-aligned
  at the centre column's right edge; the notification count and right toggle are
  at the far right. The rail keeps its icons as a column. Sidebars and panels reserve
  consistent gutters; the left sidebar is 34 cells from 160 columns and 28 below, the right 36 and 30. The left sidebar opens with a search row, then an UPPERCASE project header
  (`⌥ branch` sub-groups for linked worktrees). Session rows show a mark slot, title and
  time, then the agent in its colour, `⌥ branch` and the child count, and end with
  `+ New session`; the open session is bold on the selection band, focus is a `▌`
  on both lines, hover is a surface, and attention is marked. The right sidebar
  uses UPPERCASE bold muted section labels and a 9-cell key column. The left sidebar resizes
  by its edge and collapses to a rail of one letter per session whose hover
  shows the title and place. Settings is the modal recipe over the scrimmed
  session: search, UPPERCASE muted section headings, values right-aligned, the selected
  row on a full-width neutral gray band with a white focus bar, → to change and ← to return to sections. Escape clears the search, then closes.
- The waiting card is docked above the composer on the panel surface with a heavy accent
  rail: `◆ Allow bash?`, `waiting on you · 0:42`, the parameter table, the diff capped at
  12 rows and the choices as key chips on `surface.raised` (the selected row on the
  selection band; a choice Core does not offer is muted with its reason). A multi-select
  question's card uses the violet `interactive.multi` rail and ticks. A single-question ask
  submits a single choice or free-text answer immediately; multi-select keeps one submit
  action, without a review page. Multi-question asks retain their step-through and review
  page (listing every question and answer), using the `text.primary` rail. A free-text
  answer line takes focus immediately, without a selected-row background; its arrow keys
  move the caret. While a question card is on screen in the viewed session the composer
  preserves its draft but is locked with “Answer the question above”; Esc still interrupts.
  Approval prompts keep their existing deny/redirect behavior. Toasts are
  raised blocks top-right under the header (level `▌`, glyph, bold title, `×`, no
  lifetime hairline), three at a time with `+N more`; clicking one opens its full text in
  the modal. The banner is one row at the top of the page and pushes the timeline down;
  the jump-to-latest pill is raised, right-aligned on the row above the composer card.
- The file and diff view is one centred modal over the dimmed page, at every
  width; the page keeps its layout underneath and a click outside closes it. Long
  lines wrap. It has its own
  `‹ Back  path  ●` header. The modal keeps its editor, undo history and scroll
  across a resize. When it hides a session that needs approval or an
  answer, its header carries a `⚠ N waiting` badge. Wide mode appears without
  motion; narrow mode slides in (NSH) or steps in over two 125 ms frames (TUI).
- The location strip under the composer shows the path and branch in muted text.
  A linked worktree alone gets a muted, normal-weight worktree icon; there is no
  project-name prefix, checkout/worktree text, command hint or accent underline.
  The shared `control.worktree` glyph (NSH: theme-tinted bundled SVG) becomes primary
  on hover and hints “Worktree session”. Clicking opens the small **Worktree** modal:
  “This session is in a worktree.”, full wrapped worktree path with **Copy**, branch,
  main-checkout path and `esc close`. Copy confirms with “Copied”; no management link.
  Mobile has no location strip or worktree-details surface.
  Counts abbreviate as `1.2K` and `3.4M`.
- Pending input rows use primary, normal-weight `S` / `Q` marks, two cells before
  the text, in one card. Hovered/keyboard-selected text becomes primary. **Queued**
  rows alone reveal right-aligned edit, up, down, remove actions (`✎ ↑ ↓ ×`, ASCII
  `e ^ v x`; NSH `pencil`, `arrow-up`, `arrow-down`, `x`). Up/down are disabled at
  that queue group's bounds. Touch uses tap-to-reveal. There is no Steer row button;
  pending steer rows retain their existing keyboard actions without action icons.
  The unheld card has no header. The busy NSH composer keeps the Enter/Tab send-mode hint in its empty
  placeholder. Up from an empty composer selects a pending row; the selected row shows the same key hints in
  both clients. Held rows are marked `held`, with `n send next` in the selected-row hint and a shared
  **Send next** control; the control is disabled while a question is pending.
- Collapsed sidebar content starts at the same vertical origin as the expanded
  sidebar. Context omits a separate Child agents section. Each section heading
  opens a scrollable modal with stored content and metadata. The top Context label
  and count open the overview; only its chevron folds the section. Prompt and instruction
  bodies come from the frozen snapshot; tool definitions are labelled as stored
  definitions because they may change during a session.
- Breakpoints (D17): both sidebars docked from 120 columns (34 / 36 from 160, else 28 / 30),
  the left only at 100-119, and below 100 drawers of 34 / 36 cells over the scrim with a header
  of toggles, status and a left-truncated breadcrumb. The empty and connecting screens carry the
  `_/` N splash logo in the accent colour (bold in mono), one block centred above the title.
  The launch splash uses the same logo in `text.primary` (white in the dark theme) above a
  30-segment milestone bar, its percentage and the next step; the bar follows the five startup
  milestones, never the clock.
- Narrow layouts keep the centre readable and use drawers for sidebars. Large
  text, CJK and combined graphemes must fit without splitting; keyboard and
  mouse hit targets follow the painted geometry. Empty, loading, error and
  offline states must say what happened in user-facing language.

This is an implementation direction, not visual sign-off. See `review.md` for
captured evidence and open review items.

## Git tab

The left Git tab (both clients, desktop and web only) shows the active worktree's changes as a summary:
`folder · branch`, `N files changed · +A −D`, `S staged · U unstaged · T untracked`, then one collapsed row per
top-level folder with a file count (`▸ src/  2`; `./` holds root files); expanding shows the file rows. Empty state
is one muted `No changes` line. No branch list. The strings are in both clients' code (the manifest has no string
table).
