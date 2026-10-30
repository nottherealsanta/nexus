# TUI design language

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
  inside with `esc` muted on its right, key hints (bold key, muted label) the last row;
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
  output and a `raw` toggle. Reasoning is `▸ Thought for 2.1s · preview` (italic
  muted preview). Folded content remains accessible, and any clipped output is
  labelled. Reduced motion and monochrome stay static.
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
  row on the selection band with the focus bar, ←/→ to change. Escape clears the search, then closes.
- The waiting card is docked above the composer on the panel surface with a heavy accent
  rail: `◆ Allow bash?`, `waiting on you · 0:42`, the parameter table, the diff capped at
  12 rows and the choices as key chips on `surface.raised` (the selected row on the
  selection band; a choice Core does not offer is muted with its reason). Toasts are
  raised blocks top-right under the header (level `▌`, glyph, bold title, `×`, no
  lifetime hairline), three at a time with `+N more`; clicking one opens its full text in
  the modal. The banner is one row at the top of the page and pushes the timeline down;
  the jump-to-latest pill is raised, right-aligned on the row above the composer card.
- The location strip under the composer shows the path and branch in muted text.
  A linked worktree alone gets a muted, normal-weight worktree icon; there is no
  project-name prefix, checkout/worktree text, command hint or accent underline.
  Counts abbreviate as `1.2K` and `3.4M`.
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
- Narrow layouts keep the centre readable and use drawers for sidebars. Large
  text, CJK and combined graphemes must fit without splitting; keyboard and
  mouse hit targets follow the painted geometry. Empty, loading, error and
  offline states must say what happened in user-facing language.

This is an implementation direction, not visual sign-off. See `review.md` for
captured evidence and open review items.
