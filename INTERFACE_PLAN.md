# Nexus interface plan (TUI and NSH)

One interface, two clients: the Ratatui TUI (`nexus-tui/`) and NSH
(`nexus-nsh/`: browser, desktop, iOS). They share the same workflows and information architecture; NSH may show
richer details within that structure. Both speak only the Core HTTP API (`docs/api.md`) and differ only where the medium
forces it (mouse vs keys, pixels vs cells). This file is the spec for both.
Core work it depends on is tracked in [PLAN.md](PLAN.md) (Step 12 and the
steps before it).

This file is the normative spec, including parts not built yet. What is built
is described in `nexus-tui/README.md` and `nexus-nsh/README.md`; what remains,
with its Core dependencies, is in [PLAN.md](PLAN.md) ("Next") and
`interface/capability-matrix.md`. Clients list unbuilt commands as disabled
with a reason, never as a control that appears to work.

Shared data both clients build from lives in [`interface/`](interface/README.md):

- `manifest.toml`: command ids, slash names, default keys, `when` rules,
  unavailable reasons, Esc precedence, settings sections and rows, navigation
  map. Checked by `python3 interface/check_manifest.py`.
- `theme.toml`: semantic tokens, light/dark/ANSI/monochrome themes and glyphs.
  Checked by `python3 interface/check_theme.py`.
- `capability-matrix.md`: each feature here classed as API ready, Core work,
  client only or deferred, with the Core blockers and unplanned gaps.
- `reference-inventory.md`: what to retain, improve and omit from old Nexus's
  TUI, with copy sources.

Decisions taken while freezing the manifest (proposals until reviewed):

- **Esc** runs the first matching rule, once: close overlay, close dialog,
  leave inline text edit, close narrow drawer, dismiss focused toast/banner,
  interrupt, close editor, back from child, back from settings, focus
  composer. Esc never clears the composer. Backspace navigates back only when
  no text field has focus.
- **Availability:** `when` is a list of terms that must all hold (`!` negates).
  Unavailable commands show disabled with their reason, never hidden. Commands
  waiting on Core carry `status = "blocked_core"`; deferred ones `"deferred"`.
- **Default keys:** palette Ctrl+P (TUI) / Cmd+P (NSH); new session Ctrl+T;
  sidebars Ctrl+B / Alt+B; left tabs Alt+1–3; while busy Enter steers and
  Alt+Enter queues; Shift+Enter inserts a newline.

## Principles

1. **One timeline.** What the user sees and what the agent sees are the same
   context line as closely as possible. Every item the model receives (system prompt, user messages,
   tool results, notices, context insertions, handoffs) appears in the session
   view, in order, labelled. A child launch is in the parent's context line;
   clicking it opens the child's own context timeline. Activity indicators may
   show that child running and stop when it finishes without inserting model
   context. Label inspection details separately from model-visible content.
   Folding and collapsing change only how much is shown, never what exists.
2. **Context first.** Presenting context well is a core idea of Nexus: a new
   session shows its full starting context, every turn shows what it cost, and
   every clip is announced.
3. **Same features, same names.** A feature, command or setting exists in both
   clients with the same name, the same id and the same behavior. Where one
   medium cannot do something (inline images in some terminals), it falls back
   and says so.
4. **Append-only context.** The UI never offers an action that rewrites
   context already sent to a provider (it is cached, and editing it would
   invalidate the cache). Changes go forward as new items. Actions that will
   invalidate the cache anyway (switching model or agent) warn first.
5. **Core decides, clients render.** State lives in Core. Clients hold only
   view state (open panels, scroll, drafts) and resync from Core at any time.

## Design language and cross-client consistency

The TUI takes its primary visual reference from `../nexus` and develops that
familiar environment into a substantially more polished interface. Design is an
early deliverable, with repeated real-app review; a colour change at the end is
not sufficient. Implementation milestones and reference files are in
[PLAN.md, Step 12](PLAN.md#step-12-clients).

- Use quiet neutral canvas/panel surfaces, strong readable primary text,
  subordinate metadata and restrained accent colour. Align status marks,
  labels, token counts and key hints consistently. Give context, execution
  diagnostics and temporary UI feedback distinct treatments.
- Define semantic tokens for surfaces, text, borders, accent, focus, selection,
  success/warning/error and diff additions/deletions. Share theme definitions
  and hierarchy; map spacing and typography appropriately to cells and pixels.
  Light, dark and monochrome terminal modes preserve meaning without colour.
- Specify component states and anatomy once: headers, session rows, context
  sections, turns, tool rows, child cards, composer, waiting cards, settings,
  menus, dialogs and alerts. Build representative component galleries and review
  real screen compositions before broad feature implementation.
- Maintain a shared manifest for command IDs, labels, settings order, navigation
  destinations and availability. The TUI, browser and desktop use the same
  organization. Platform conventions may adapt key modifiers, native menus,
  input, window chrome and touch behavior.
- NSH may display additional metadata, thumbnails, split diffs and richer
  previews inside existing sections/expansions. More available space does not
  justify moving a common action or changing its name. Details fetched for
  inspection are labelled separately from bytes actually sent to the model.
- Settings always follow this order: Appearance, Providers, Models, Agents,
  Tools, Skills, MCP, Context, Dictation, Keybindings, Notifications, About.
  Shared rows retain their relative order; platform-only rows stay within the
  matching section. Unsupported capabilities show their reason.
- Use the [NSH design guide](https://nsh.dev/docs/design-guides/) and
  [base components](https://nsh.dev/elements/) for the graphical implementation.
  Preserve Nexus's interface map while using each platform's focus, text input,
  selection and accessibility behavior.

## Design language

The data lives in [`interface/theme.toml`](interface/theme.toml): colour
tokens for `dark` and `light`, timeline roles, 256-colour and ANSI-16
fallbacks, the monochrome mapping and the glyph table. Both renderers read or
generate from it; neither hard-codes a colour. `python3 interface/check_theme.py`
verifies contrast (text 4.5:1, meaningful marks 3:1, in hex and in the 256
fallback), completeness and glyph rules. This section is the prose around that
file. It is the reference for the later component galleries.

The look, in order of priority: a **quiet neutral canvas** with panels one
small step apart, **strong primary text**, **subordinate metadata** (counts,
paths, durations, key hints), and **one restrained accent** (warm amber-orange,
carried from old Nexus). Everything else is neutral or a status colour. If a
screen reads as coloured, it has too much colour.

### Tokens

| Token | Dark | Light | Use |
| --- | --- | --- | --- |
| `surface.canvas` | `#0B0B0B` | `#f7f7f5` | Timeline, editor, app background |
| `surface.panel` | `#1e1e1e` | `#efefec` | Composer, cards, notices |
| `surface.raised` | `#242424` | `#ffffff` | Dialogs, menus, toasts, user message |
| `surface.sidebar` | `#141414` | `#f0f0ed` | Left sidebar and session rail (TUI) |
| `text.primary` | `#e6e7e8` | `#1f2124` | Titles, user and agent text, tool names |
| `text.secondary` | `#a4a9ae` | `#4f555b` | Tool arguments, summaries, labels |
| `text.muted` | `#868c93` | `#5c636a` | Metadata, counts, timestamps, key hints, reasoning |
| `border.default` | `#2a2a2a` | `#d4d5d2` | Hairlines and dividers (decorative) |
| `border.focus` | `#e8a46e` | `#a84e16` | Focus ring or bar |
| `interactive.accent` | `#e8a46e` | `#a84e16` | Active tab, links, primary action, waiting |
| `interactive.focus` | `#e8a46e` | `#a84e16` | Keyboard focus indicator |
| `interactive.selection` | `#2c2824` | `#f0e3d8` | Selected row or range background |
| `interactive.hover` | `#1e1e1e` | `#e9e9e6` | Pointer hover background |
| `status.success / warning / error / info` | `#8fc46a #e0af68 #f27d8f #7aa7f0` | `#2f7a1f #8a5d00 #c0283f #2a5fc4` | Status marks and their text |
| `agent.*` | eight identity colours | darkened matching hues | Agent names; built-ins pinned, custom names hashed |
| `diff.add`, `diff.del` (+ `_bg`) | `#8fc46a #f27d8f` on `#1c2a1a #2e1b20` | `#2f7a1f #c0283f` on `#e1f0dc #f8e1e4` | Diff lines |

- **Surfaces step, they do not contrast.** Canvas, panel and raised differ by
  a few percent of lightness. Structure comes from spacing and one-cell rails
  first, hairlines second, background steps third. A surface is never the
  only cue for state.
- **Text has three weights of grey and no more.** Primary is for what the user
  reads; secondary for what explains it; muted for what can be skipped. Muted
  still meets 4.5:1 on every surface.
- **Accent is rationed**: the active tab, the focus bar, links, the primary
  action and the waiting mark. It is not used for
  decoration, titles or tool names.
- **Status colours** only ever appear with a glyph and a word (see below).
- **Selection vs focus.** Selection is a background (what is chosen); focus is
  a bar or ring (where the keyboard is). A selected, focused row has both.
- **Hairlines are decorative** and exempt from the 3:1 rule. Anything that
  conveys state (focus, error borders) uses a token that meets it.

### Timeline roles

Roles let the user tell, at a glance, who said a thing and whether the model
has it. Each has a rail colour, an optional label and a text style; they
never rely on colour alone.

| Role | Text | Surface | Rail (left edge) | Label / style |
| --- | --- | --- | --- | --- |
| User text | primary | raised | muted `▌` at the band's left edge (focus colour when focused) | none; full-width padded band |
| Agent text | primary | canvas | none | none; flush text |
| Reasoning | muted | canvas | hairline `│` | `Thought`, italic |
| Context notice | secondary | panel | info `┃` | `context` |
| Context insertion | secondary | panel | accent `┃` | `inserted` (steering, settings) |
| Execution diagnostic | secondary | panel | warning `┃` | `diagnostic` (retries, hooks, provider errors) |
| Tool activity | secondary | canvas | none | foldable group with `├─`/`╰─` members; detail in a `┌ │ ╰` box |
| Child card | primary | panel | info `┃`, boxed | `child` |

Rules: only user text has a filled block; only notices, insertions and
diagnostics have the heavy rail, and the label says which. Consecutive tools
and reasoning form a foldable activity group; child launches and agent text
end the group. Waiting items stay open and failures are visible when folded. Errors that end a
turn use the `error` token with `✕` and the word "error" in the footer, not
a new role. Anything shown that the model did not receive is not in the
timeline (see [Timeline](#session-view-timeline)); inspection data is labelled
`inspect` and drawn in the NSH side panel or a dialog.

### Colour tiers and monochrome

- **Truecolor** uses the hex values. **256-colour** uses the xterm indices in
  `fallback256`, hand-picked to keep the same contrast. **ANSI-16** collapses
  surfaces to the terminal's own background, so panels are told apart by rails,
  borders and labels; `text.muted` uses bright black (with `dim` where
  available).
- **Monochrome** (`NO_COLOR`, `TERM=dumb`, or the Appearance setting) uses
  only `bold`, `dim`, `reverse` and `underline`: muted text is dim; titles and
  the accent are bold; selection is reverse; hover is underline; focus is
  bold plus the focus bar; error is bold and underlined; diff lines are told
  apart by their `+` and `-` prefix (additions bold, deletions dim). Roles
  keep their rail glyph and label, so a context notice and a reasoning block
  remain different without colour. No italics, no background fills.
- The theme picked by the terminal's background query or the setting is
  `dark` or `light`; named themes may override tokens but not the checks.

### Status: colour, glyph and label

Every status shows **all three**, never one. The glyph is the one in
`theme.toml`; the label is the word, shown where there is room (rows with
width, tooltips and accessible names always).

| Status | Glyph | ASCII | Label | Colour token |
| --- | --- | --- | --- | --- |
| Running | `◐` (spinner while live) | `~` | running | `text.primary` |
| Done | `✓` | `+` | done | `status.success` |
| Error | `✕` | `x` | error | `status.error` |
| Denied | `⊘` | `-` | denied | `status.error` |
| Waiting | `◆` | `?` | waiting | `interactive.accent` |
| Interrupted | `◼` | `!` | interrupted | `status.warning` |
| Cancelled | `⊖` | `o` | cancelled | `text.muted` |

- Running is primary, not a colour: live work looks active by motion. The
  spinner honours reduced motion and falls back to the static `◐`.
- Attention marks (session rows, projects, tab titles) rank **waiting `◆`**
  over **error `✕`** over **unread `●`**; only the highest one shows, with a
  count when several sessions share it.
- Glyphs are single-cell, non-emoji. ASCII glyphs are used when the locale or
  terminal cannot draw Unicode; the label is unchanged.
- Fold markers: `▾` open, `▸` closed (ASCII `v`, `>`). Sidebar toggles: `◧`
  and `◨` when the sidebar is open, `□` when closed (ASCII `[|`, `|]`, `[ `,
  ` ]`).

### Scale

**One hierarchy, two scales.** Five levels: *title* (session title, dialog
title), *body* (user and agent text, composer), *secondary* (tool rows,
labels), *meta* (counts, paths, key hints, timestamps) and *chrome* (rails,
rules, bars). The same level has the same token and the same relative
emphasis in both clients; sizes are chosen per medium.

**Terminal** (cells; no fractional sizes):

- Hierarchy comes from colour token, bold and dim, never from size.
  Title = primary + bold; body = primary; secondary = secondary; meta = muted.
- Spacing: **0** inside a row; **1 cell** gutter between a rail or status glyph
  and its text, and between inline segments (separator `·` padded by one cell
  each side); **1 blank row** between timeline blocks and between turns' major
  parts; **2 blank rows** are never used. Cards and dialogs have 1 cell of
  horizontal padding and no vertical padding; a card's first row is its title.
- Indent is 2 cells per nesting level (tool inside code, child inside parent),
  capped at 3 levels; deeper levels flatten with a "depth N" meta label.
- Sidebars are 28 cells by default (min 22, max 40); the timeline column
  never goes below 40. Below 100 cells total sidebars become drawers.
- Truncate the middle of paths, the end of titles and commands, always with
  `…`. Never wrap a status mark or a count away from its row.

**NSH** (CSS pixels; rendered from the same levels, not by multiplying cells):

- Base unit 4 px; spacing steps 4, 8, 12, 16, 24, 32. A row is 28-32 px, a
  compact row 24 px; touch targets are 44 px on phone widths.
- Type: Title 15/20 semibold; Body 14/22 (agent prose may go to 15/24 in the
  reading column); Secondary 13/20; Meta 12/16, tabular figures for counts and
  durations; code uses the Monaspace family at 13/20. These are starting
  values: the user can scale all sizes (Appearance, NSH-only row), and the
  levels keep their ratio.
- Radius 6 px for controls and rows, 10 px for cards and dialogs, none on the
  timeline blocks themselves (they are separated by space and rails, as in the
  terminal). One-pixel borders in `border.default`; 2 px focus ring in
  `border.focus`, offset 1 px, never clipped.
- The extra room is spent on whitespace and previews, not on larger text or
  decoration: a 720 px reading column, split diffs, thumbnails. Alignment
  rules below apply unchanged.

### State vocabulary

Every interactive component supports these states, drawn consistently:

| State | Terminal | NSH |
| --- | --- | --- |
| Default | `text.secondary`/`primary` on its surface | same |
| Focused | focus bar `▌` in `interactive.focus` at the row's left edge; label bold | 2 px focus ring |
| Selected | `interactive.selection` background | selection background, accent edge |
| Hovered | `interactive.hover` background (mouse terminals only) | same, 120 ms ease |
| Pressed | reverse video for the frame of the press | hover colour one step darker, 1 px inset |
| Disabled | `text.muted`, no hover, reason shown or in the tooltip; never hidden | same, `aria-disabled`, reason as description |
| Loading | spinner glyph and the word ("loading…") in place of the value; layout does not shift | spinner or skeleton in the same footprint |
| Error | `✕`, `status.error` text and the error message in the row; border in error on inputs | same, with `aria-invalid` |
| Empty | muted sentence that says what to do next, plus the key hint; no illustration | same |

Disabled controls always carry the reason ("needs a model", "turn is running");
a state never changes a component's size or position.

### Alignment rules

- **Left column** is a fixed 2-cell slot for the status mark or fold marker
  (glyph, then a space). Labels start in the same column in a list, so marks
  form a vertical line.
- **Right column** holds meta, right-aligned in one block: counts, durations,
  tokens, key hints. Within a column, numbers right-align; units follow
  (`12 s`, `1.2K`, `+14 −3`). Never let meta push the title: the title
  truncates first.
- **Segment order** in any row: mark, title/name, key argument, summary, then
  the right block. Separator is `·`. Order of meta in the right block: count,
  tokens, duration, status word.
- **Key hints** are muted, lowercase keys in the form `enter open · esc
  close`, ordered by importance, placed on the surface they act on (dialogs and cards), in the command
  palette or F1, and in the composer for stop. There is no status footer.
  Keys that conflict with text input are not shown there.
- **Counts** use plain digits with thousands abbreviated at 1,000 (`1.2K`, `3.4M`).
- Align `+N −M` as two fixed-width groups so adjacent rows stack.

### Component anatomy

Mockups are 80-cell terminal sketches; NSH uses the same order and content
with its own spacing. `▌` is the focus bar, `◆` and friends are the status
glyphs above.

#### Workspace header

```
 ◧  Sessions  Files  Projects     nexus10 › research agent › child: sweep   ● 1  ◨
```

One row. Left toggle at the leading edge, then **session tabs** as in old
Nexus: `○ Title ×` per opened session (status glyph: spinner running, `●` unread,
`✕` failed, `○` idle), the viewed tab raised and bold, then `+` for a new session.
A tab opens when its session is opened; `×` (or `alt+x`) closes the tab and
leaves the session in the list, `alt+[` / `alt+]` move between tabs, clicking
the viewed tab renames. When tabs do not fit, titles shorten to 8 cells, then
the farthest tabs drop behind a leading `…`. The new-session screen and
Settings show their own raised tab. Right toggle at the trailing edge; the run
or connection status sits left of it (`◐ running`, `✕ offline`).
- Focused: toggles show the focus bar to their left. Hovered: crumb underlines.
- Disabled: a toggle is never disabled. Loading: title is replaced with
  `loading…`. Error: `✕ offline` in error. Empty: new-session view title is
  `New session`.
- Narrow: the title truncates with `…` from the left of the crumb chain
  (`… › child: sweep`); toggles stay.

#### Session row

Sessions use two lines and a blank spacer: the title follows a two-cell
attention/focus slot, and the second line shows the project and, when known,
`⌥ branch` (`~` in ASCII). The open session's title is bold; archived titles
are muted. Hover fills both lines, and either line opens the session. Running
titles shimmer unless reduced motion is enabled. Branch data comes from one
project checkout listing, so rows in a project load together.
The list ends with a muted `+ New session` row. Scope and archived filters
are palette commands and Settings rows, not sidebar chrome.

#### Session rail

The left sidebar sits on `surface.sidebar`. Its inner edge is a drag handle
(20–60 columns); dragging below 14 columns collapses it to a 6-column rail:
`⌕` (session switcher), one letter per session (the title's first letter, or
the attention glyph or spinner), and `+`. Each item is three rows with the
glyph in the middle; the open session's band is filled. Hovering a letter
shows a popover beside the rail with the title (bold) and `project ⌥ branch`.
Settings › Sidebar cycles full, rail and hidden. Children are reached
from their parent rather than listed as separate root rows.

#### Context section

A section of the timeline that groups what the model received before the first
turn (system prompt, tool list, instructions, agent, child agents).

```
 ▾ Context · 5 items · ~2.9K tokens · 1.4% of window
   ▐ System prompt ▌ ~1.0K tokens
    You are a coding agent running in Nexus, a coding agent…

   ▐ Tools ▌ [9] ~1.9K tokens
    read  bash  bash_output  bash_kill  edit  write  ask  +2 more

   ▐ Instructions ▌ AGENTS.md loaded ~0.7K tokens
    /work/AGENTS.md · 2.8 KB, part of the system prompt

   ▐ Agent ▌ build
    builtin · Does the actual work…

   total  ~2.9K tokens · 1.4% of the window
```

Header: fold marker, title (secondary, bold), `·`-separated meta in muted, with
the share of the model's window when the catalog reports one. Body, as in old Nexus: each item's name is a bold label on a filled chip
(reversed in monochrome), followed by a count and the estimated token figure,
with muted detail rows indented under the chip's text (first prompt line, tool names in at
most two rows then `+N more`, the instruction file's path and size). A blank
row separates items; a `total` row closes the list. Estimates are
characters ÷ 4 and carry `~`; the provider's own counts appear in the turn
footer. Instructions are part of the system prompt, so the total counts only
the system prompt and the tool schemas. Collapsed by default after the first
turn. Focused: focus bar on the header. Hovered: header background. Loading:
`loading context…`. Error: `✕ failed to load context` with retry key hint.
Empty: `No context yet`. Clipped items show a `context` notice line stating what
was clipped and why.

#### Turn header

In the TUI, the user-message block is the turn header: a band across the whole
page width on the raised surface, with the rail `▌` at its left edge (muted,
the focus colour when focused) and one padding row above and below. The fold
marker sits inside the band at the text edge and the text starts two cells
after it. The first text row shows tags (`steer`, `queued`, `via voice`,
`from parent`), then the turn number and local time at the right (`#2 · 3:44 PM`);
the text wraps before it. A folded turn is one row: the first message line, and
`#2 · 3 tools · 4.2s · ✓` at the right. A hovered or focused band shows `copy` on its last
text row. Large messages have a separate expand control and are always sent and
copied whole. Once the band's text has scrolled off the top while its turn is
still on screen, a copy is pinned under the header: at most three rows, ending
in `…`, then a `░` shade row with `↑ N lines of this turn above` at the right.
The next band pushes it up, and a click scrolls back to the band. The matching
NSH presentation remains in A8.

#### Turn footer

```
                       ▪ Build · gpt-6.1-terra · medium · 3 tools · 4.2s · 12k in · 2k out · $0.04
```

One muted row, right-aligned at the end of the turn after a blank row, with no
rule: `▪ Build` (agent in its colour, bold), the models used (`a → b` when the
model changed), effort, tools, duration, tokens (`in`, then `cached` if any,
then `out`), cost. It never states how the turn ended: no `done`, `interrupted`
or `error` word or glyph. An interrupted turn (or one stopped at the step limit)
adds `/continue` after the footer (`enter continue` while the composer is
empty); a failed turn puts the first line of its error, `✕ …` in the error
colour, on the row above. Durations read `0.4s`, `4.2s`, `1m 05s`, `1h 02m`,
never `<1 s`. When the row does not fit, the segments break from the left onto
extra right-aligned rows. Cost is computed from the catalog's prices (uncached
input, cached input, output) and is absent when the model has no price.
Running: absent until the turn ends (the header says `◐ working 12s`, and
`◆ waiting for you` while a question or approval is pending). Hovered: tokens
expand into a breakdown tooltip (NSH) or a `i` key (TUI).

#### Tool activity

In the TUI, consecutive tool calls and reasoning form a foldable group. A live
group shows `▾ Working` (the old soft shimmer; reduced motion keeps it static)
with its elapsed time, and under it the newest five members in a `├─`/`╰─` tree
with `… N earlier` above them. A finished group folds to verbs in first-seen
order with counts (`Read 2 files, ran 1 command`), `+N −M` for edits, and
elapsed time at the right; its glyph flashes the accent for six frames when it
finishes (never with reduced motion). Denials and failures are counted
separately (`⊘ 1 denied`, `✕ 1 failed`) and stay visible when folded, even on a
narrow row. Pending approvals and questions keep their group and turn open.

An expanded group lists its members as `{tree} {glyph} {Verb}  {key argument}  {summary}`
with the duration at the right. The glyph is the verb's (`→` read, `←` edit and
write, `$` shell, `*` search, `λ` code, `?` ask, `↳` session, `↗` web; ASCII
`> < $ * L ? ^ ^`), replaced by the status glyph while the member runs
(spinner), fails (`✕`), is denied (`⊘`) or waits (`◆`). Reasoning members read
`▸ Thought for 2.1s · preview`; open, the summary is italic behind a `│` rail;
encrypted reasoning says `encrypted, replayed to the provider` and has no
expansion; running reasoning is `▾ Thinking… 2.4s` with its last five rows
behind `┃`. Opening a member shows the old box: `┌ Shell cmd  raw  3.4s`, the body behind `│ `
(`border.strong`) and `╰` on the last row. The command shows its first two rows,
the output its last eight behind `↑ N earlier lines · enter show all`; diffs and the raw
view keep a head and a tail. `raw` toggles the exact arguments and result.
Execution-only detail is labelled apart from model context. NSH's matching
presentation is still tracked in A8.

#### Child card

```
 ┃ ◐ child · research agent · "Survey loaders"           waits · 1m 12s
 ┃   running: grep "def load" …                        4 tools · 8.2k tok
```

Heavy info rail, boxed with the `child` label, in panel surface. Row 1:
status, label, agent, quoted title, then right block (waits or not, elapsed).
Row 2: current activity (or first lines of the answer when done) and the
counts. Open with Enter or a click; the answer expands in place.
- Waiting: `◆ waiting for input` and the question's first line; also bubbles to
  the parent's waiting card. Done: `✓` and the first answer lines.
  Error: `✕` and the error. Interrupted: `◼`. Empty: no activity yet →
  `starting…`. Focused: focus bar replaces the rail's first row. Hovered:
  raised surface.

#### Composer

The composer is a card on `surface.panel`, inset two columns from each edge of
the centre column, with half-row edges (`▄` above and `▀` below in the panel
colour over the canvas; `─` rules in mono and ASCII). There is no agent rail and
no status band. Rows: the input at three cells in, a blank row, then the control
row. The placeholder is `Type a message…` with the cursor on its first letter;
disconnected sessions show the reason. The focus bar at the card's left edge is
drawn only in ANSI-16 and monochrome.

The control row reads `Agent · model provider · effort`: the agent capitalised,
bold in its identity colour; the model uses the catalog display name with the
provider muted; effort is secondary. A change requested during a turn reads
`old → new (next call)`. On the right sit the old Nexus meter (ten `╱` cells,
filled in the agent colour over `border.default`) and `82.0K · 41%`. Narrow rows
shed effort, then the provider, then the meter. While busy, a live row above the
card reads `◐ Working · 12s · ↓4.1k` (Working shimmers) and the control row's
right side reads `esc stop`; an interrupted turn offers `continue` there.
Completion (`/` and `@`) is a borderless `surface.raised` block exactly as wide
as the card and flush with its top edge: ten rows at most, the selection band
and focus bar on the chosen row, key hints as the last row.
The project strip is a canvas row below the card: the project name (bold), its
path (muted, `~` form), `⌥ branch` (secondary) and `checkout` (muted) or
`worktree` (bold violet, linked worktrees only), with `ctrl+p commands` muted
and right-aligned while idle; no accent underline. The path opens the directory
control and the branch opens the worktrees control. For an existing session, Core’s worktree listing supplies
its branch and linked worktree marker, refreshed on directory changes; before a
session exists, `GET /api/project/{id}/worktrees` supplies the project's own
branch the same way. The context figure estimates occupancy from the latest
request’s input tokens; hovering it explains the numbers, including occupancy of
each price-tier boundary the catalog reports. Before a session exists the agent, model and
effort pickers set a draft that goes into the session's creation request. Attachments and queue are tracked in
PLAN.md (Step 12a).

#### Waiting card

```
 ┃ ◆ waiting · edit src/loader.py                              1 of 2  tab next
 ┃   @@ -12,7 +12,10 @@ …
 ┃   [ allow once ]  [ allow session ]  [ deny ]  [ deny with note ]
 ┃   a once · s session · d deny · n note
```

Docked directly above the composer, accent rail, panel surface. Row 1: mark
`◆`, the word, the tool and key argument, count and cycle hint at the right.
Body: full parameters or the questions with their options. Options row:
bracketed buttons; the focused one is reverse, the default is bold.
- A choice Core does not offer is shown disabled with its reason, not hidden.
  Questions render options as `( )`/`(*)` or `[ ]`/`[x]` with a free-text field
  and `enter submit`.
- Loading (submitting): buttons replaced by `◐ sending…`. Error: `✕` and the
  error above the buttons; the card stays. Empty: no card exists. Cancelled:
  the card closes; the timeline row says `⊖ cancelled: interrupted`.

#### Settings row

```
 ▌ Theme                              dark                         ▾
   Colour theme for all clients.
   Font size                          14        NSH only
```

Label (primary) on the left, value or control at a fixed column on the right
(about 60% of the width), with a one-line muted description under the label.
Order and section grouping follow [Settings](#settings). Client-only rows
carry a muted `NSH only` or `TUI only` tag at the far right.
- Focused: focus bar, label bold. Selected (open editor): selection
  background. Hovered: hover. Pressed: reverse. Disabled: label and value
  muted, reason replaces the description ("sign in first"). Loading: value is
  `◐`. Error: value in `status.error` with `✕` and the message below. Empty
  value: muted `not set`. Toggles show `[x]`/`[ ]` and the word on/off.

#### Dialog and confirm

```
 ┌ Delete session ─────────────────────────────────┐
 │ Delete "Refactor loader" and its 2 children?    │
 │ This cannot be undone.                          │
 │                                                  │
 │            [ Cancel ]  [ Delete ]                │
 │ enter confirm · esc cancel                       │
 └──────────────────────────────────────────────────┘
```

On the `raised` surface over a dimmed canvas (terminal: no dim, the dialog is
bordered and the content underneath is not redrawn). Title in the top border,
body, one button row (safe choice first, destructive last and in `status.error`
when irreversible), key hints in the footer. Width 44-72 cells, centred.
Focus is trapped and returns to the invoker on close.
- Focused: the focused button is reverse; Esc always cancels. Disabled: a
  button shows its reason in the footer. Loading: the confirm button becomes
  `◐ working…` and Esc is disabled until it ends. Error: `✕` and the message in
  the body; dialog stays open. Empty: n/a. Blocking dialogs (version
  mismatch) have no cancel and say which side to update.

#### Banner

```
 ✕ Disconnected from Core · reconnecting in 4 s                 r retry · × hide
```

One row at the top of the centre column, panel surface, status glyph and
colour in the mark and the first words only; the rest is secondary. Right
block holds actions and a dismiss (if dismissible). Banners are for lasting
state: they stay until the condition clears. Levels: info `i`, warning `!`,
error `✕`. Loading state uses `◐`. Focused: the action takes the focus bar.
Empty: n/a (no banner). Multiple banners stack with the most severe on top and
a `+N` count.

#### Toast

```
                                              ┌────────────────────────┐
                                              │ ✓ Copied               │
                                              └────────────────────────┘
```

Raised surface, bottom-right of the centre column in the TUI and NSH, one line
(two at most), glyph then text, no actions except `undo` where one exists.
They expire in 4 s (8 s for errors, no auto-dismiss while hovered or focused),
stack up to three, newest at the bottom, and every toast is also kept in the
notifications drawer. Levels: success `✓`, info `i`, warning `!`, error `✕`.
Loading: not used (use a banner or inline spinner). Hovered: pauses the timer
and shows `×`. Pressed: dismisses. Disabled and empty: n/a. Screen readers get
the text as a polite live region (assertive for errors).

### Review checklist

For galleries and screen reviews: contrast check passes; nothing relies on
colour alone (view in monochrome); status marks form a vertical line; titles
truncate before meta; muted text is metadata only; accent is on no more than a
handful of elements per screen; focus is visible in every state; ASCII mode
loses no information.

## Shared navigation anchors

The workspace header has the **left sidebar toggle at its leading edge**,
**session tabs after it**, and **right sidebar toggle at its
trailing edge**. Toggles remain available when panels close. Use the same
locations in terminal, browser and desktop, accounting for native window chrome.
At narrow widths they open drawers rather than relocating to unrelated menus.

Left sidebar tabs are **Sessions → Files → Projects**; in the TUI they sit in
the header row after the leading toggle. **Settings** remains a
fixed bottom entry in that sidebar and a palette command. The waiting-for-input
card stays immediately above the composer. Additional desktop menu or keyboard
entry points invoke the same actions. Preserve focus and selected tab when a
sidebar is hidden and reopened.

## Layout

```
┌──────────────┬─────────────────────────────────────┬──────────────┐
│ Left sidebar │ Session view (timeline)             │ Right sidebar│
│  Sessions    │  breadcrumbs (child sessions only)  │  Session info│
│  Files       │  turns, tools, children, notices    │  Git status  │
│  Projects    │                                     │              │
│              ├─────────────────────────────────────┤              │
│              │ Waiting-for-input card (if any)     │              │
│              │ Composer                            │              │
└──────────────┴─────────────────────────────────────┴──────────────┘
```

- Both sidebars can be hidden (keys and palette commands). Narrow widths
  (phone, small terminal) show one pane at a time: sidebars become drawers or
  overlays.
- The centre column is the session view (or the new-session view, or an open
  file in the editor).
- In the TUI, Settings opens as a centred modal over the dimmed session, from
  the palette, Ctrl+, or the fixed sidebar entry: title and `esc`, a search
  line (typing filters), then every section's rows under violet headings with
  values right-aligned. The selected row is on the accent fill; ←/→ changes
  it, Enter acts, the row's note or unavailable reason shows below the list.
  Esc clears the search, then closes. NSH retains its own Settings view.

## Focus and scrolling

Both clients support keyboard-only navigation with visible focus, a predictable
focus order and focus restoration after closing drawers, child views or dialogs.
Streaming follows the latest item only while the user is already at the bottom.
Scrolling upward suspends following; incoming text, folding and loading older
turns preserve the visible anchor. A **jump to latest** action restores following
and indicates new content below. Test these behaviors at narrow widths too.

## Left sidebar

Three icon tabs representing **Sessions**, **Files** and **Projects**. Hovering
an icon shows its word label: "Sessions", "Files" or "Projects". Keyboard focus
also exposes the label; retain accessible names on all three icons.

### Sessions

- **Scope toggle:** current project only, or all projects. With all projects,
  sessions are grouped under their project.
- **Grouped by worktree** when a project has worktrees: the main checkout first,
  then one group per worktree (branch name, path on hover or detail line).
- Each row: title (or "Untitled" until the title arrives), status (idle, busy,
  waiting for input, error), relative time of last activity, and the number of
  child sessions. Child sessions are not listed as rows; they are reached from
  their parent (see Child sessions). A session continued by a handoff shows as
  a chain (see Compaction).
- One-turn sessions (titles, `/btw`) are hidden here; they are reachable from
  their parent and the inspector.
- **Archived** sessions are hidden behind an "Archived" filter.
- **Search** (palette or a field at the top) matches session titles and user
  messages only, never tool output or assistant text. Core keeps an index of
  exactly these fields, so search is one indexed query, not a scan.
- Actions on a row: open, rename, archive or unarchive, delete, copy id, open
  in inspector.

### Session actions and where they live

Every session action is a palette command. The session row menu carries the
common ones.

| Action | Palette | Row menu | Notes |
| --- | --- | --- | --- |
| Rename | yes | yes | Also by clicking the title in the header |
| Archive / unarchive | yes | yes | |
| Delete | yes | yes | Confirm; whole subtree |
| Export | yes | yes | Markdown transcript or JSON (messages and events) |
| Handoff | yes | | Same as `/compact` (see Compaction) |
| Change session directory | yes | | Move to an existing path |
| Worktrees | yes | | List, create or delete; creating does not move the session |
| Integrate / merge a worktree | yes | | TBD with leaving a worktree |
| Interrupt | yes | | Esc |
| Continue | yes | | `/continue`: send existing context after interruption/failure |

No fork or revert: a session only moves forward. Export and integrate need
Core work (PLAN.md Step 10).

### Archive and delete

- **Archive** hides a session tree from the default list. Nothing is removed;
  it can be unarchived and resumed. Archiving a busy session (or any descendant)
  is refused until it is idle or interrupted.
- **Delete** removes the session, its whole subtree, its one-turn sessions and
  its stored events. It asks for confirmation through the unified alert
  (naming how many child sessions go with it). There is no undo.
- Worktree cleanup when a session in a worktree is archived or deleted: archiving
  never touches a worktree; delete removes only the session record and its
  history. Deleting a worktree stays an explicit control operation.

### Files

- The current project's files as a tree: folders expand and collapse, loaded
  lazily one directory at a time. Expanded folders keep their place; a failed
  listing says why.
- Not specific to a session: it shows the project checkout, the worktree of the
  open session, or a selectable root when there is more than one checkout.
- Git status markers per file (modified, added, untracked, deleted; ignored
  shown dimmed, with a toggle to hide them).
- Opening a file shows a read-only view in the centre column with its `~` path
  and line numbers. The built-in editor replaces this viewer when it lands.
- Actions: open in your editor (`$EDITOR`/`$VISUAL`), copy path, insert as
  `@path` into the composer. The built-in editor is not wired yet.

### Projects

- Every project Core knows: name, path, session count, last activity.
- Select to make it current (the Sessions and Files tabs follow). Add a
  project by path; remove a project from the list (its sessions go with it,
  after a confirmation).

### Built-in editor

A simple editor, not an IDE: open, edit, save, find, syntax highlighting,
line numbers and a dirty marker. Saves are refused if the file changed since
it was read; reload before saving, preserving unsaved text for recovery.

- TUI: [ratatui-code-editor](https://github.com/vipmax/ratatui-code-editor).
- NSH: the [NSH editor element](https://nsh.dev/elements/editor/).
- Reads and writes go through Core (`GET`/`PUT` file endpoints), confined to
  the project, as for the tools.

## Right sidebar

For now it holds only two sections.

### Session info

- Title, id (copyable), agent, model, effort, directory
  (with worktree branch if any).
- Started at, last activity, total duration of turns.
- Turns, model calls (steps), tool calls (by tool, e.g. `bash 12 · edit 4`).
- Tokens: input, cached input, output, reasoning; context used against the
  model's context window (a bar); cost when the price is known.
- Child sessions launched from here: count, how many running, how many failed.
- Enabled and disabled tools.
- Kernels this session tree uses.

Not in the right sidebar for now: Changes (files the session modified, with
diffs), Jobs, Logs, and Cost across sessions and providers. Revisit
after the first release; Cost may go to Settings.

### Git status

Of the session's directory (the worktree, if the session is in one): branch,
ahead/behind, changed files with `+N −M`, staged vs unstaged. Clicking a file
opens its diff.

## New session view

A new session does not start empty. It opens on its **starting context**, so
the user sees and can change it before the first message:

- Agent (selector), model and effort, directory or worktree.
- System prompt: the agent's rendered prompt, as the model will get it.
- Instructions: `AGENTS.md` from the session's current directory, shown with
  its path. Directory changes load the destination's file as new context.
- Agent definitions are checked for changes whenever a root or child session
  is created. The session snapshots its agent and available tool descriptions;
  later turns do not reload edited agent files. Unavailable tools are omitted
  from its starting system prompt and schemas.
- Tools: each with its guidance and schema, each with a switch.
- Skills and MCP servers (PLAN.md Step 11a): listed with switches, source
  paths and load/connection status. Browser-use is the default MCP server.
- Token count of the whole starting context.

Edits here (switches, agent, model, a prompt override) change the session
before anything is sent. Once the first message is sent, this context is
frozen and shown at the top of the timeline as the first item, collapsed. The
composer is at the bottom as usual; sending starts the session.

Core: a session draft (`POST /api/session` with `draft: true`, or a preview
endpoint) that returns the starting context as labelled sections and their
token counts, and accepts the edits above.

## Session view (timeline)

### Turns

- A **turn** is one user message and everything until the agent stops. Turns are
  numbered (`#1`, `#2`…), and the number is a jump target (palette:
  "go to turn").
- Inside a turn: assistant text, reasoning, tool calls and results, child
  session cards, notices, in order.
- **Folding.** Any turn folds to its header line: number, first line of the
  user message, tools used, duration, status. "Fold all but last" is a
  command. Folding is view state only.
- **Turn footer** at the end of each turn: models, effort, tools, duration,
  tokens (input / cached / output / reasoning) and cost. How the turn ended is
  not part of it (interrupted turns add a `/continue` hint, failed turns a
  `✕` error row above).
- **Reasoning** is shown collapsed ("Thought for 12s · preview"), expandable to the
  summary text. Encrypted reasoning is labelled as such (it exists, it is
  replayed, it is not readable).
- **Notices and insertions** (child finished, session directory changed,
  clipping, steering messages, context insertions from settings) are timeline
  items with their own style, because the model sees them too.
- **Errors and retries** (provider, auth, interrupted) are timeline items with
  the error text and, for provider errors, the status and body. Each retry
  (when Core retries) is a row with attempt number and wait time.
- **Handoff markers** (see Compaction), **directory-change rows** (session moves: old
  and new directory and branch), **hook rows** and **diagnostic rows** (when
  Core has hooks or diagnostics feeding context) are their own row styles. If
  they reach the model, they are in the timeline; if they do not, they are
  not (they go to Logs in the inspector).

### User messages

- Markdown as typed, with attachments as chips. In the TUI, a focused or hovered
  user block exposes a copy action; `c` also copies the focused block.
- Large user messages are shown as a preview with an expansion control; the
  full message is copied and sent. In the TUI composer, pastes of at least 200
  characters or three lines become a compact marker, and expand to their full
  text before sending.
- **Images** show as thumbnails (NSH inline; TUI inline when the terminal
  supports the kitty, iTerm2 or sixel protocol, otherwise a chip with name,
  size and dimensions that opens externally).
- Queued and steering messages are marked as such (see Composer).

### Markdown rendering

Assistant text is CommonMark plus GFM: headings, emphasis, lists, task lists,
tables, block quotes, links, inline code, fenced code with syntax highlighting
and a language label, and horizontal rules.
In the TUI, each code fence is individually focusable and has a copy action: a
panel block whose header row holds the language (muted) and, on focus or hover,
`copy`, with the code inset by 2 and no rule or rail (DESIGN_PLAN.md D08).
Supported Mermaid diagrams appear above their original source; unsupported
formats and ASCII mode keep the source visible.

- Streaming renders incrementally; an unfinished fence or table is rendered as
  plain text until it closes, so the layout does not jump.
- TUI: tables fit the width and wrap cells; links show as underlined text with
  OSC 8 hyperlinks where supported; headings use weight and colour, not size.
- File paths like `src/x.py:42` are links that open the file in the editor at
  that line.
- Code blocks have a copy button (NSH) or copy key (TUI).

### Copy

One click (or one key on the focused item) copies: a user message, the agent's
final message of a turn, any assistant text block, a code block, a tool's
input or output, a session id, a file path. A short confirmation appears in
the unified alert area. NSH writes to the system clipboard; the TUI uses OSC 52
(so it works over SSH), with a fallback to the platform clipboard command.

## Tool rendering

Every tool call is **collapsed by default** to one line, and expands on click
(or Enter) to a tool-specific detail. A tool's line updates live while it
streams its arguments and runs.

**Collapsed line:** status icon (running, done, error, denied, waiting), tool
name, the key argument, a short result summary, and duration.

| Tool | Key argument | Summary on the collapsed line |
| --- | --- | --- |
| `read` | path and line range | lines read |
| `bash` | command (first line) | exit code, output lines |
| `edit` / patch | path (or file count for a patch) | `+N −M` in green and red |
| `write` | path | `+N` (new) or `+N −M` (overwrite) |
| `code` | first line of the cell | nested calls, `Out` type, kernel id |
| `session` | agent and title | child status (see Child sessions) |
| `web_search` | query | result count and source links |
| `web_fetch` | URL | status, returned lines and clip notice |
| MCP tool | server and tool name | status and result summary |
| Skill loading | skill name | source and loaded instructions |
| `ask` | first question | the answer, once given |
| `expose_workspace_tools` | activate workspace controls | directory and worktree tools available |
| `change_session_directory` | destination path | the new directory |
| `worktree` | list, create or delete | worktree paths or operation result |

**Expanded detail:**

- **edit / patch:** the diff, with line numbers where the patch has them. Split
  (old | new, `╱` hatch on the empty side) when the column is at least 110 cells
  (TUI) or the window 1100 px (NSH), unified below (D09). A patch shows one collapsible diff per
  file. A failed edit shows the error and the attempted change.
- **write:** the diff against the previous file, or the new content when the
  file is new.
- **read:** the content that was read, with its line numbers and range, exactly
  as the model got it (including any clip notice).
- **bash:** the command (full), its working directory, then the output in a
  scrollable box with a fixed maximum height, then the exit code and duration.
  While running, output streams into the box.
- **code:** the cell source, stdout, `Out [n]`, and the nested tool calls as
  nested collapsed tool lines, each expandable like a direct call.
- **ask:** the questions, options and the user's answers.
- **Errors and denials:** the error text in the error colour; a denial says
  who denied it.

**Large output.** The box is scrollable and capped. The client shows at most a
fixed amount (for example the first 200 and last 200 lines, or 64 KB), with
"N lines hidden · show all" between. Show all fetches the full result from
Core. The model-side clip (what the model actually got) is a separate notice,
so the user can tell "the UI hides this" from "the model never saw this".

**Arguments and raw.** Every expanded tool has a "raw" toggle that shows the
exact arguments JSON and the result text as sent to the model.

## Child sessions

**In the parent's timeline**, a child is a card at the point of its `session`
tool call:

- Agent, title, status (running, waiting for input, done, failed,
  interrupted), whether the parent waits for it, elapsed time.
- While running: the child's current activity (last tool line or a streaming
  line of text), its tool count and tokens.
- When done: the first lines of its answer, expandable to the whole answer
  (which is what the parent received).
- A non-waiting child's later "finished" notice appears in the parent's
  timeline where the parent received it, linked to the card.
- Waiting-for-input inside a child bubbles up: the card shows it and the
  parent's waiting area shows it too, labelled with the child.

**Opening a child** (click the card) opens the child session in the centre
column. It looks exactly like a parent session view, with two differences at
the top: its own tab (parent sessions keep theirs) and a **back button** (and Esc / Backspace) to return to the parent at the
card's position. A child's composer can prompt the child directly.

## Waiting for user input

In Core, approvals (`permission.asked`) and questions (the `ask` tool) are
separate things. In the interface they are **one idea: the agent is waiting for
the user's input.** They share one component, one place and one set of keys.

- **Where:** a card docked above the composer, and a marker on the tool line
  in the timeline. The session row in the sidebar shows "waiting". If several
  sessions (or children) are waiting, the card shows a count and cycles.
- **Approval:** the full parameters (not a summary), with a diff preview for
  edit, patch and write and the full command and working directory for bash.
  Four choices: allow once, allow for this session, deny, deny with a note to
  the agent. A choice Core does not offer for this call (for example "allow
  for this session" on a tool that is not session-grantable) is shown
  disabled with the reason, never hidden.
- **Question:** one or more questions, each with options (single or
  multi-select) plus free text, and an optional preview per option. Answer
  all, then submit.
- **No timeouts.** A question waits until it is answered or the turn is
  interrupted.
- **Cancelled.** If the turn is interrupted (by the user, a parent, or a daemon
  restart) while waiting, the card closes and the timeline keeps the question
  or approval with "cancelled: interrupted", and the tool result the model
  got ("interrupted by the user"). Answers typed but not submitted are kept
  as a draft only for this view.
- **Attention from elsewhere.** A wait in another session or project: the
  session row and its project get an attention mark, a banner offers "go to",
  the notifications drawer lists it, and the OS notification fires if the
  window is not focused (see Notifications). A wait inside a child shows on
  the child's card and in the parent's waiting area.

Core: the `ask` task tool and `question.asked` / `question.replied` contract
are implemented in PLAN.md Step 10, before workspace controls. This interface renders
those questions alongside the existing permission events.

## Composer

- Multi-line Markdown input. Enter sends, Shift+Enter (or Alt+Enter in
  terminals that cannot tell) adds a line.
- **Controls in the composer:** agent, model and effort selectors, usable at
  any time, including mid-turn. A change applies from the next model call.
  Already-running calls keep their original configuration. Tool/MCP changes
  affect only subsequent calls too; directory changes apply after active tools
  finish. Show when a requested change is pending and when it takes effect.
  Switching shows a unified alert first: the prompt cache may be invalidated
  (a different model or provider has no cache for this prefix; a different
  agent changes the system prompt). The change is recorded as a timeline row
  at the point it took effect, and the turn footer names every model used.
- **Attachments:** durable Core storage with explicit upload and total size
  limits and visible oversize errors; a button and drag-and-drop for files and images (NSH), a
  paste of an image (NSH, and TUI where the terminal passes it), and `@path`
  mentions with completion from the project's files (both). Attachments show
  as removable chips with size. Large pastes become a collapsed chip.
- **Voice dictation (important).** A mic button (NSH) or push-to-talk key (TUI)
  records speech and transcribes it into the composer as editable text. It
  never sends by itself. There is no live voice conversation with the model
  (no GPT Live call bar) in the composer. Options for transcription (decide in the step that
  builds it): the platform's speech recognizer (Apple Speech on macOS/iOS, the
  browser's API), a local model, or a provider transcription endpoint through
  Core. The TUI records in its own process.
- **While the agent is working**, the composer stays usable and offers two
  ways to send:
  - **Steer** (default while busy, e.g. Enter): the message is added to the
    running turn at the next model call, after the current tool results, so
    the agent sees it before its next step. Shown in the timeline at the point
    it was inserted.
  - **Queue** (e.g. Alt+Enter or a toggle): the message waits and is sent as
    the next turn when this one ends. Queued messages are listed above the
    composer, editable and removable until sent.
- **Interrupt:** Esc (TUI) or a stop button.
- **Drafts** are kept per session (view state).
- `/` at the start opens command completion (see Commands).

Core: steering and queueing endpoints (today a prompt while busy gets 409).

## Continue after interruption

`/continue` resumes from the existing session context without inserting a new
user message. Offer it after an interrupted or failed turn, once Core has
reconciled any incomplete calls; disable it while a turn is running. Show the
continuation linked to the interrupted attempt. Previously completed tools are
not rerun. Unknown tool outcomes and any partial output that cannot be replayed
are labelled visibly. Core owns this recovery contract (PLAN.md Step 11):
`POST /api/session/{id}/continue`, `/continue` in chat and
`nexus session continue ID` are implemented.

## One-turn sessions

A one-turn session runs exactly one model call and ends. It is a real session
(stored, inspectable, linked to its parent with a kind), hidden from the main
list.

- **Title.** After the first user message, Core runs a one-turn session on a
  cheap model with that message to produce the title. The title appears in the
  sidebar and header when it arrives.
- **`/btw <question>`.** While the agent is working (or idle), ask a side
  question without disturbing it. Core sends the current session's context up
  to its last completed tool call, plus the question, as one call with no
  tools. Because the prefix is the same, it reuses the parent's prompt cache.
  The answer appears in an overlay or side panel tied to the parent, not in
  the parent's timeline, and the parent's context does not change. Each btw
  can be reopened from a "btw" list in the session info.

## Compaction is a handoff

At a configurable percentage of the model's context window (default **80%**),
or when the user asks, the session **hands off** to a new root session of the
**same agent**. Settings accepts a numeric percentage rather than fixed presets.
Core reserves room for the summary and handles failed handoffs visibly:

- The old session ends with a handoff turn: the agent writes the summary (the
  state of the work, open threads, files touched), visible in its timeline.
- A new root session starts with that summary as its starting context, plus a
  link to the old one. Its new-session view shows exactly what it carries.
- In the sidebar the two show as one chain (newest on top, "continued from").
  The timeline of the new session starts with a "continued from …" item that
  opens the old one.

So nothing the model saw is ever hidden or replaced: each session's timeline
is still exactly what its model saw.

## Session directory and worktrees

Core exposes an always-available `expose_workspace_tools` entry point.
When the agent requests it, its result adds the definitions of two tools to
context and makes them callable from the next model call:

- `change_session_directory`: move this session to an existing directory.
- `worktree`: list, create or delete worktrees. Creating returns a path;
  a separate directory-change call moves the session there.

Activation is shown in the context line and persisted per session. It requires
no user permission and introduces no worktree settings. Each new child starts
with the entry point and activates independently. User commands can invoke the
same control operations directly through Core.

A directory change shows old/new paths and branch, loads the destination's
`AGENTS.md` as forward context, updates Files/git/header, and moves the session
to its worktree group when appropriate. Running children retain their paths.
Worktree deletion refuses dirty or in-use paths; leaving a worktree means first
changing directory. Integrate/merge remains future work.

Edits inside the session directory require no approval and have no permission
setting. Core still enforces path confinement and a fresh model-visible read
before changing an existing file. A refusal explains which file must be reread.

## Commands and the palette

- **Command palette:** Cmd+P (macOS NSH), Ctrl+P (TUI and others). Almost every
  action is a palette command: new session, switch session/project, search,
  archive, delete, fold all, go to turn, toggle sidebars, change model, effort,
  agent, tools, open settings, theme, sign in, open
  inspector, copy actions, `btw`, compact/handoff, interrupt.
- **Slash commands** in the composer are the same commands (`/model`,
  `/effort`, `/agent`, `/btw`, `/tools`, `/compact`, `/new`, …), with
  completion. A command has one id, title, optional slash name, keybinding and
  "available when" rule, defined once and shared by both clients (a single
  command list file in the repo that both build from).
- Keybindings are listed in the palette next to each command and can be
  changed in settings.

## Unified alerts and notifications

One system for telling the user things, in both clients, with levels:

- **Toast:** brief confirmations (copied, saved, title set).
- **Banner:** lasting state (daemon disconnected, reconnecting, a provider
  needs sign-in, version warning).
- **Confirm:** before an action with a cost or no undo (delete, switching
  model/agent mid-session and cache invalidation, removing a project).
- **Blocking:** a breaking client/Core version mismatch.

Timeline notices are not alerts: they are context, and the model sees them.

**Notifications drawer.** Every toast, banner and attention event is also
kept in a drawer (palette: "notifications"), newest first, with a link to the
session. Unread items show a count.

**Attention marks.** A session row, its project, and (NSH) the window or tab
title get a mark for: waiting for input (strongest), finished while not in
view (unread), error. Opening the session clears it.

**Sound and OS notifications.** A completion sound (on/off) when a turn ends out
of view; OS notifications on desktop and iOS, the terminal bell or OSC 9/777
notification in the TUI. Each kind is switchable in Settings.

## Connection to Core

- **Handshake.** On connect, the client reads `GET /api/info` (Core version and
  API version). Same API major: go. Newer minor: carry on, banner if the client
  lacks features. Different major: **halt** with a blocking alert that says
  which side to update. No requests are sent past a breaking mismatch.
- **Starting the daemon.** If none is running, the client starts it (as the CLI
  does), or tells the user how.
- **Reconnect.** When the event stream drops, the client shows a banner and
  retries with backoff. On reconnect it catches up from the last event id
  (stored log, `?after=`), then resyncs the open session and the session list
  from the API using a snapshot watermark. Catch-up transitions atomically to
  live events, with stable ids for deduplication. Cursors survive restarts or
  identify a new log generation. Overflow, an expired cursor or an explicit
  continuity failure triggers full resync. A gap in global `seq` on a filtered
  stream is normal and does not by itself trigger resync. Restore pending waits
  and queued messages too. Retried sends use request ids to avoid duplicate
  turns; competing clients receive explicit revision/conflict responses.
- **Daemon restart.** A new daemon (different pid or token) means: reread the
  token, re-handshake, full resync. Turns that were running when the daemon
  died show their partial state and "interrupted by daemon restart".
- **Client access.** Nexus is served on its configured port. Any client that
  can reach that port can use the Core API under the normal API access rules;
  there is no device-registration workflow.

## Loading and empty states

- **Daemon starting:** a "starting Nexus Core" screen with the log tail if it
  takes long or fails.
- **Daemon restarting / connection lost:** a banner, the last view stays
  visible but read-only, then a resync.
- **Wrong protocol version:** the blocking alert (see Connection).
- **First run:** no provider signed in: a short welcome that leads to sign-in,
  then to choosing a project.
- **Empty project:** no sessions yet: the new-session view directly.
- **Session too large to load:** the timeline loads by turn, newest first,
  older turns on scroll (folded), with a count of what is not loaded yet.
  Search and "go to turn" fetch the turn they need.

## Settings

- **Appearance:** theme (light, dark, system, plus named themes shared by both
  clients as one token file), font and size (NSH), density.
- **Providers:** each provider with its status (signed in, account, expiry),
  sign in (browser or device code flow, shown in the UI), sign out, and
  default model. Adding a new provider (an OpenAI-compatible endpoint with a
  key, when Core supports custom providers).
- **Models:** default model and effort, the catalog with refresh.
- **Agents:** loaded agents (built-in, global, project), their mode and
  source; open an agent file in the editor; the default root agent.
- **Tools:** tools on by default for new sessions.
- **Skills:** discovered skills, source paths, diagnostics, and defaults for
  new sessions; loaded instructions remain inspectable in their context line.
- **MCP:** server configuration, enabled defaults, status, discovered tools and
  connect/disconnect/retry. Browser-use is configured by default; missing setup
  shows an actionable message. Secrets are never displayed in diagnostics.
- **Context:** automatic handoff threshold as an editable percentage, default
  80% of the model context window.
- **Dictation:** engine, language, push-to-talk key.
- **Keybindings:** the command list with editable keys.
- **Notifications:** when to notify (waiting for input, turn done, error),
  completion sound, OS notifications.
- **About:** client version, Core version and API version, daemon pid, home
  directory, link to the inspector.

Settings that are Core state (providers and defaults) are
read and written through the API, so they are the same in both clients. Pure
view settings (theme, density, keys) live per client.

## Inspector

The inspector page (`docs/inspector.md`) stays the deep view (Steps, Requests,
Raw). Both clients link to it from a session and from any tool line ("open in
inspector"), and both have a wire view toggle that shows the request bodies and
raw events inline.

## Core work this needs

Tracked in PLAN.md. In short: session drafts and starting context; frozen
system prompt; `ask` tool and question events; steer and queue; one-turn
sessions (title, btw); handoff instead of compaction; on-demand directory and worktree tools; archive (done), delete, search index; files, file read/write and
git status endpoints; attachments and images in both dialects; transcription
(maybe); API version in `/api/info`; stream resume; sign-in and settings over
the API; skills and MCP (including default browser-use); web search/fetch;
durable artifact storage and size limits; enforced read-before-edit
checks; idempotent mutations and multi-client conflicts. Core is accessible to
clients through its configured port.

## Open questions

- Dictation engine per platform.
- Focus ring colour: shared with the accent (current) or a separate cool hue.
- Status glyph rendering across terminals (`◐ ✓ ✕ ⊘ ◆ ◼ ⊖`; several are
  ambiguous-width).
- Approval choices Core does not yet offer (allow for session, deny with a
  note); see `interface/capability-matrix.md` for this and other unplanned
  Core gaps.
- Worktree cleanup on archive/delete, and leaving a worktree.
- Whether a user can steer a child session from the parent's card.
- Output caps (lines vs bytes) and whether they are a setting.
