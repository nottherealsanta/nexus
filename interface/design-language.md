# TUI design language

The visual reference is the old Nexus TUI in `../nexus/rust/tui/src`. This
client adapts its patterns to the Core API and the one-timeline model. The
shared source of colours, terminal fallbacks and glyphs is `theme.toml`.

- Canvas holds the timeline and the details sidebar; the left sidebar and its
  rail sit on `surface.sidebar`; panel surfaces hold the composer and the
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
- Timeline items follow model context order. User messages use a raised block;
  consecutive activity folds into a verb group. Folded content remains
  accessible, and any clipped output is labelled. Running activity uses the
  old Nexus soft shimmer; reduced motion and monochrome stay static.
- The header row carries the sidebar toggles, the breadcrumb title and the
  bell. The sidebar's first row carries the Sessions / Files / Projects icons
  (label on hover; the active icon bold on a surface). Sidebars and panels reserve
  consistent gutters. Session rows show title and `project ⌥ branch` on
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
