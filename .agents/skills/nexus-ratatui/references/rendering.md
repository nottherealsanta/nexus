# Rendering reference

## Layout (`render.rs: regions`)

Vertical: top bar (3 rows: tabs, breadcrumb+status, rule) / body / composer (8 rows).
Body horizontal: sessions sidebar (30 cols, only when width >= 110) | transcript |
details sidebar (40 cols, needs 110 wide, or 170 when sessions are shown too).
Below those widths the shortcuts open the same content as transient panels
(`/sessions`, `/details`). Composer rows: attachments/voice strip, editor box
(blue `▌` bar, panel background, one padding row), runtime row, bottom info row,
leader hint.

Hit-testing in `main.rs` mirrors the layout: tabs (`x` starts at 2, each tab is
`title_width + 4`, two apart, `+` at `width - 4`), sessions rows
(`session_rows()` + scroll), transcript rows (`cache.operations[offset + row]`),
prompt choices (`prompt_area` then `prompt_regions`). If you change a layout, change
the matching hit-test in the same commit.

## Palette

`Palette::new(light)` defines native colors: background, panel, element,
element_hi, dialog, border, border_strong, text, muted, quiet, accent, blue, purple,
success, warning, error, cyan. Python sends colour *tokens* (`$nx-blue`,
`$nx-label-neutral`) or `#rrggbb`; `transcript.rs: color()` resolves them. Add a
colour to both palettes at once.

## Row building (`transcript.rs`)

- `build(block, width, palette) -> Vec<(Line, Option<operation>)>`; one entry per
  terminal row so click mapping is exact. `Cache::update_content` keys parts by
  `block.id` + index and reuses unchanged ones (bounded to 4,096).
- `wrap()` breaks at spaces, hard-breaks only words longer than the row, drops a
  space that lands on the margin, keeps graphemes whole.
- Indents follow canonical padding: user card = bar + 2 spaces left, 2 right; agent,
  thought, tool, error = 2; reply (markdown) = 4; summary right-aligned with 2
  right padding; context chip = 2, body = 4.
- Tool rows are truncated with `…`, never wrapped.

## Row mapping

| Presentation contract | Native |
| --- | --- |
| `.timeline-user` panel bg, padding 1 2 | `user()` card with blank rows top/bottom |
| `▼/▶` chevron + `#N` badge (accent on element-hi) | header row with right-aligned tag |
| `.timeline-thought` (`◇` purple, italic, `▸ N lines`) | `thought` kind |
| `◆ Agent` label above first reply | `agent` kind |
| `.tool-card` quiet, failed = error, batch gutter `┌│└` | `tool` kind, `status` |
| `.timeline-summary` quiet right-aligned | `summary` kind |
| `.turn` margin-bottom 1, margin collapse | `gap` computed in Python |
| `ContextBlock` chip `bold $nx-bg on color` + `~N tokens` | `context` kind |
| `DetailsSidebar` SESSION / MODIFIED FILES / MCP SERVERS | `details_lines()` |
| `#chat-input` + `#runtime-info` + `#bottom-info` | composer rows in `draw()` |
| inline permission panel above composer | `prompt_area()` + prompt block in `draw()` |
| modal pickers (`$nx-dialog`, accent title, rule, highlighted row) | `dialog_frame()` |

When native rendering changes, keep hit-testing and row mapping consistent.

## Not yet matched (see the plan)

Inline diff line numbers, details file-row expansion, MCP refresh control, spinner
animation (rows are static between snapshots), voice/worktree/settings screens.


Local disclosure uses complete hidden presentation and output fold boundaries.
Do not clone the whole transcript per frame. Carry patch/disclosure ranges into
`Cache::update_content`; preserve unaffected row-part Arcs. Typing affects only
hints. Native optimistic toggles use `LocalUi` acknowledgements; disclosure must
not acquire a blocking Python round trip. Turns older than the newest two start
folded (`Disclosure::sync_window`); a turn leaving that window has no patch over it,
so `update_content` marks its first block dirty.
