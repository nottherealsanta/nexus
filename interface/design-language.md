# TUI design language

The visual reference is the old Nexus TUI in `../nexus/rust/tui/src`. This
client adapts its patterns to the Core API and the one-timeline model. The
shared source of colours, terminal fallbacks and glyphs is `theme.toml`.

- Chrome and page: the header band and both sidebars (and the rail) share one
  chrome surface, `surface.sidebar`; the centre column is the page,
  `surface.canvas`. No rule separates them: the surface step is the edge, and
  only where surfaces collapse (ANSI-16, monochrome) a `│` in `border.default`
  is drawn at each sidebar edge. `border.strong` is decorative (diff split rule,
  tool-detail box). Under a modal the buffer is dimmed by `paint::scrim`: each
  cell's colours move 50% toward the canvas, or `DIM` where colours cannot be
  blended. Panel surfaces hold the composer and the
  settings modal; raised surfaces hold user messages and modal panels. Borders are
  quiet hairlines. A focused row uses the focus bar as well as its selection
  surface so the state remains visible without colour.
- Primary text carries content and headings. Secondary text carries labels;
  muted text carries metadata and instructions. Status colour is paired with
  a word or glyph. Agent colours identify agent names, not whole rows.
- The composer names the agent in its colour and explains context occupancy
  (tokens, price tier and window) on hover. A model change requested during a
  turn is marked until the next call; narrow rows shed effort and provider
  before names or the agent.
- Timeline items follow model context order. The user message is a band across
  the whole page width on the raised surface: the rail `▌` at its left edge
  (muted, the focus colour when focused), padding rows above and below, the fold
  marker inside at the text edge and `#n · time` at the right. Once its text has
  scrolled off, a copy is pinned under the header (three rows at most, then a `░`
  shade row in `border.strong` with `↑ N lines of this turn above`), pushed up by
  the next band. The turn footer is one muted right-aligned row,
  `▪ Agent · models · effort · tools · duration · tokens · cost`, with no rule
  and no end status (an interrupted turn adds `/continue`, a failed one a `✕`
  error row above). Durations read `0.4s`, `4.2s`, `1m 05s`, `1h 02m`. Consecutive activity folds into a verb
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
  consistent gutters; the left sidebar is 34 cells from 160 columns and 28 below, the right 36 and 30. Session rows show title and `project ⌥ branch` on
  separate lines and end with `+ New session`; the open session is bold, focus
  is a bar, hover is a surface, and waiting is marked. The left sidebar resizes
  by its edge and collapses to a rail of one letter per session whose hover
  shows the title and place. Settings is a centred modal over the dimmed
  session: search, violet section headings, values right-aligned, the selected
  row on an accent fill, ←/→ to change. Escape clears the search, then closes.
- The location strip under the composer names the project in bold over an
  accent segment of the closing rule, then branch and `worktree` (violet, only in a linked worktree). The composer panel has a rail in the agent's colour and a darker status band with the context figure. Counts abbreviate as `1.2K` and `3.4M`.
- Narrow layouts keep the centre readable and use drawers for sidebars. Large
  text, CJK and combined graphemes must fit without splitting; keyboard and
  mouse hit targets follow the painted geometry. Empty, loading, error and
  offline states must say what happened in user-facing language.

This is an implementation direction, not visual sign-off. See `review.md` for
captured evidence and open review items.
