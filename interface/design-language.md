# TUI design language

The visual reference is the old Nexus TUI in `../nexus/rust/tui/src`. This
client adapts its patterns to the Core API and the one-timeline model. The
shared source of colours, terminal fallbacks and glyphs is `theme.toml`.

- Canvas holds the timeline and sidebars; panel surfaces hold the composer and
  navigation; raised surfaces hold user messages and modal panels. Borders are
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
- Sidebars and panels reserve consistent gutters. Session rows show title and
  project on separate lines; the open session is bold, focus is a bar, hover is
  a surface, and waiting is marked. Settings uses a centered framed modal with
  a searchable root and drill-in pages. Escape returns through that hierarchy.
- Narrow layouts keep the centre readable and use drawers for sidebars. Large
  text, CJK and combined graphemes must fit without splitting; keyboard and
  mouse hit targets follow the painted geometry. Empty, loading, error and
  offline states must say what happened in user-facing language.

This is an implementation direction, not visual sign-off. See `review.md` for
captured evidence and open review items.
