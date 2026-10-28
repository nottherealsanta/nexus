# Design plan: a polished TUI and NSH

Status: decided 2026-10-28; I0–I4 landed for both clients (see Progress). The user chose option A for every decision
(`nexus-design-mockup/choices.json`), with the notes folded into D01, D05, D10, D11,
D13, D15 and D18 below. Mock-ups for every decision are in the temporary
folder [`nexus-design-mockup/`](nexus-design-mockup/README.md) (open
`nexus-design-mockup/shots/index.html`). Delete that folder when this plan has
landed. Captures of the TUI as it is today are in `nexus-design-mockup/before/`,
and the old Nexus references are in `nexus-design-mockup/reference/`.

The goal is to make the TUI look like old Nexus (branch `main`, `rust/tui`) again,
then polish it further. NSH gets the same decisions, translated to pixels. This plan sits beside
[INTERFACE_PLAN.md](INTERFACE_PLAN.md) (the normative spec) and
`interface/design-language.md`. Where a decision here changes either of them,
the change is listed under [Spec and ledger changes](#spec-and-ledger-changes)
and lands in the same increment as the code.

## What is wrong today

From the captures in `before/`, at 120×36 and 160×48. They were taken before
commits `43586c2`…`0624c37` landed on `nexus10` (context chips, a right-aligned
footer, a composer rail and status band, session tabs and detail levels, row
menus). Those commits fixed parts of this list in a different direction. The
increments below start from the current code and replace what conflicts with
the decisions (for example, the composer rail and the session tab strip).

1. **No frame.** The header row floats on the canvas. It has empty space over the sidebars and no
   band, so the window has no top edge. The sidebars are separated from the timeline only by
   thin column rules.
2. **Loose timeline rhythm.** The fold marker `▾` hangs outside the user block,
   in the gutter. The turn footer (`<1 s · 3 tools ✓ done`) sits above a hairline
   and reads as a header for the next turn. `<1 s` is not a duration format.
3. **Noisy code fences.** Each fence draws a language label, a long `────` rule
   and a `│` rail, all three. Inline code is drawn in the accent colour, so
   accent spreads over the prose.
4. **Broken composer foot.** The location strip's accent underline renders as a
   row of dashes. The composer is a flat block with no relation to the
   location strip.
5. **Cramped right sidebar.** Keys are lower-case and the values are clipped
   (`/privat…s-demo`, `codex/gpt-6.1-…`). Context, tokens and cost are missing.
   The section headings are fold rows in the same style as the timeline.
6. **Unframed overlays.** The palette has a frame, but Settings is a borderless
   panel with a full accent-filled row and violet headings. Neither dims what is
   underneath, so the session text competes with the dialog.
7. **Weak new session.** "New session" is a centred line of help text under a
   key/value list. Old Nexus and the main-branch mock-ups show the starting context as
   the content.
8. **Jump to latest overlaps content** (`↓ jump to latest · end` drawn over the
   user block).

Old Nexus did each of these better: a framed top bar, the user card with its
`#N · time` tag, the `Working` shimmer and its tree of the newest tools, `Thought for 3.2s`,
side-by-side diffs, the composer card with the `╱` meter, rounded dialogs over a
dimmed backdrop, and the sticky user header.

## Principles for this pass

- **Chrome and page.** The header band and both sidebars share one chrome
  surface (`surface.sidebar`). The centre column is the page (`surface.canvas`).
  This is where the frame comes from. Do not add rules between them.
- **Old Nexus first.** Where old Nexus had a pattern, use it, then polish it.
  Do not invent a new one.
- **Accent is rationed.** Use it for the focus bar, the active tab mark, the
  waiting mark, links and the primary action, and nothing else: not inline code,
  not headings, not section titles.
- **Meta right, content left.** Durations, counts, tokens and times go in the
  right block of their row. Titles truncate first.
- **Big terminals are the main case.** Design at 200×50, then check 120×36 and
  80×24. The extra width goes to whitespace and split diffs, not to wider text.
  Prose wraps at the reading column, capped at 120 cells and centred in the page (wide enough for a split diff).

## Decisions

Each decision lists its TUI anatomy and its NSH translation. The shot ids refer
to `nexus-design-mockup/shots/` (`tui/<id>-<mode>.png`, `nsh/<id>-<theme>.png`).

### D01 — Tokens and surfaces

- `interface/theme.toml` values stay. Two additions:
  - `border.strong`: dark `#4a4a50`, light `#c2c3c0` (256: 239 / 250; 16:
    `bright_black` / `white`; mono: `[]`). It is decorative and used only for the
    diff split rule and the open tool-detail box (`┌ │ ╰`). **Modals and popovers have
    no border** (user choice).
  - `surface.scrim`: no colour of its own. It is defined as a 50% blend of every
    cell's fg and bg toward `surface.canvas`. The TUI applies it to the buffer under a modal. NSH
    uses `rgb(0 0 0 / .45)` in dark and `rgb(0 0 0 / .18)` in light.
- Surface roles: `surface.sidebar` is the chrome (header band and both sidebars), `canvas` is the page,
  `panel` holds the composer, code fences, the waiting card and notices,
  and `raised` holds the user band, modals, toasts and popovers.
- Type levels are unchanged (INTERFACE_PLAN "Scale"). New rule: section labels
  in the sidebars and modals are **UPPERCASE, bold, muted**. This replaces the
  violet Settings headings and the bold fold rows in the right sidebar.
- Shots: `d01-tokens` (TUI swatch sheet), NSH `d01-tokens`.

### D02 — Shell frame and header band

```
◧ ≡ ▤ ◫   · Refactor the loader ×  ◆ Review the CJK… ×  ◐ Release notes ×  +        ◐ working 12s   ● 1  ◨
```

Decided with the user: keep the old-Nexus **session tabs** that landed in `5409281`, and give
them the D02 chrome band. The breadcrumb from the first mock-up is used only for child
sessions (see below).

- **Header band:** row 0, the full width, on `surface.sidebar` (the chrome), no rule under it.
  - The left toggle sits at x=1, then the left sidebar's three icons (`≡ ▤ ◫`, the active one
    bold primary, the others muted; label on hover). They stay in the band over the sidebar,
    so the sidebar's own first row is free for its search row.
  - **Session tabs** start at the centre column's text edge: `{glyph} {title} ×` per opened
    session (spinner running, `◆` waiting, `●` unread, `✕` failed, `·` idle), separated by one
    cell. The **viewed tab** is bold primary on `surface.canvas`, so it reads as joined to the
    page below it; other tabs are muted on the band. `+` (muted) ends the strip. Behaviour is
    unchanged from `5409281` (close, `alt+[`/`alt+]`, rename on click, shortening, leading `…`).
  - **Child sessions:** the viewed tab's title becomes the breadcrumb `root › child`
    (earlier crumbs muted and clickable).
  - The run status is right-aligned at the centre column's right edge: `◐ working 12s`,
    `◆ waiting for you`, `✕ offline`.
  - At the far right: the attention count (`● 1`), then the right toggle.
- **Sidebars:**
  - The left sidebar is 34 cells at ≥160 columns, 28 at 120 and a drawer below 100. The right
    sidebar is 36 / 30 / drawer.
  - Both sit on `surface.sidebar`, with no vertical rules; the surface step is the edge.
  - In ANSI-16 and mono, where surfaces collapse, a `│` in `border.default` is drawn at
    each edge.
- **Centre page:** 2-cell gutters, a reading column capped at 120 cells and
  centred. Full-bleed blocks (the user band) span the whole page width.
- **NSH:**
  - The 44 px header uses `--surface-sidebar`, with no bottom border. The left sidebar is 264 px
    and the right 300 px, both on `--surface-sidebar`; the centre is `--surface-canvas`.
  - Sidebar icons are 32 px icon buttons in the header's left slot. Session tabs follow,
    aligned with the reading column: 28 px tall, 6 px radius, the viewed one on
    `--surface-canvas` with primary text, others muted with a hover fill, `×` on hover.
- Shots: `d02-shell-200x50`, `d02-shell-120x36`, NSH `d02-shell`.

### D03 — Sessions sidebar

```
 ⌕  Search sessions        ctrl+k

 NEXUS-DEMO
▌  Refactor the loader        now      ← open: selection band, bold; focus: bar
   build · ⌥ main
 ◆ Review the CJK width hand…  3m      ← mark slot: ◆ waiting, ◐ running, ✕ error, ● unread
   orchestrate · ⌥ main       2 ↳
   + New session
                                    …
 Settings                    ctrl+,
```

- A search row at the top (muted `⌕ Search sessions`, key hint on the right).
  It opens the session switcher; it is not an inline field.
- **Group header:** the project name in UPPERCASE bold muted, shown always (it
  doubles as the scope label). With worktrees, sub-groups follow:
  `⌥ feat/cache  ~/…/nexus-demo-cache`, in secondary, with its rows under it.
- **Rows:** two lines and a blank spacer.
  - Line 1: the mark slot (x=2), the title (x=4, bold when the session is open), the time
    right-aligned.
  - Line 2: the agent in its identity colour, `·`, then `⌥ branch` muted, and the child count
    `2 ↳` right-aligned.
  - Running titles shimmer.
- **Open session:** an `interactive.selection` band across both lines. **Focus:**
  `▌` in `interactive.focus` at x=0 on both lines. **Hover:** an `interactive.hover` band.
- **Row menu** (`.` or a right click): a small raised popover beside the row with
  Rename, Archive, Delete, Copy id and Open inspector. Each item has its key, and Delete is in
  `status.error`.
- The `Settings` entry is pinned to the bottom row, with `ctrl+,` muted.
- The rail mode is unchanged in behaviour. Each item is 3 rows, and the open item gets the
  selection band.
- **NSH:**
  - Rows are 52 px with 8 px padding and a 6 px radius. The title is 13/20 medium, line 2 is
    12/16 muted, and the agent name is in its colour.
  - Group headers are 11 px uppercase with 0.06em tracking.
  - The row menu appears on hover as a `⋯` button; the context menu holds the same items.
- Shots: `d03-sessions`, `d03-sessions-worktrees-menu`, `d03-rail`, NSH
  `d03-sessions`.

### D04 — User message band

- A `surface.raised` band across the **whole page width**.
  - The rail `▌` sits at the band's left edge: `text.muted`, or `interactive.focus` when focused.
  - One padding row above and below the text, as in old Nexus.
  - The fold marker sits **inside** the band at the text edge, and the text starts 2 cells after it.
  - `#2 · 3:44 PM` is muted and right-aligned on the first text row; the text wraps before it.
  - When focused, `c copy` shows on the last text row, right-aligned.
- **Tags** go before `#N` (as in old Nexus): `steer`, `queued`, `via voice`, `from parent`.
- **Folded:** the padding is removed, the band shows one line with the first message line, and
  `3 tools · 4.2s · ✓` sits in the right block.
- **Sticky header** (from old Nexus `sticky.rs`):
  - Once the band's first row scrolls off the top, a copy is pinned under the header: at most
    3 text rows, ending in `…`.
  - Its last row is a `░` shade in `border.strong`, with `↑ 34 lines of this turn above` muted
    and right-aligned.
  - The next band pushes it up. Clicking it scrolls back to the band.
- **NSH:**
  - The full column width (760 px), `--surface-raised`, a 10 px radius, 12×16 px padding and a
    2 px left accent bar only when focused.
  - `#2 · 3:44 PM` is in meta type on the right. Copy and Fold are hover icon buttons.
  - Sticky: the header bar is position-sticky under the 44 px header and collapses to one line.
- Shots: `d04-user-band` (open, focused, folded, tags), `d04-sticky`, NSH
  `d04-turn`.

### D05 — Turn footer

```
                                  ▪ Build · gpt-6.1-terra · medium · 3 tools · 4.2s · 12k in · 2k out · $0.04
```

- The footer is right-aligned at the end of the turn, with one blank row before it and no hairline.
  It is the last row of the turn, so it no longer reads as a header for the next one.
- Segments, in order:
  - `▪` and the agent (title case, bold, in its colour);
  - the models used (`a → b` when the model changed);
  - effort, tool count and duration;
  - tokens: `in`, then `cached` if any, then `out`;
  - cost when known.
- **No end status** (user choice): the footer never says `done`, `interrupted` or `error`.
  How a turn ended shows elsewhere: an interrupted turn adds the `/continue` hint
  (`enter continue` when the composer is empty) after the footer; a failed turn has the first
  line of its error in `status.error` with `✕` on the row above the footer. The session row,
  the header status and the right sidebar still carry the session state.
- Durations are formatted as `0.4s`, `4.2s`, `1m 05s` or `1h 02m`, never `<1 s`.
- **Narrow width:** the footer wraps from the left onto a second right-aligned row.
- **NSH:** a right-aligned meta row with no status chip. The token segment has a tooltip with
  the breakdown.
- Shots: `d05-footer` (done, interrupted, error, model change, narrow), NSH `d04-turn`.

### D06 — Activity groups and tool lines

Old Nexus `transcript/groups.rs` and `transcript/tools.rs`, polished:

```
▸ Read 2 files, ran 1 command                                                      1.4s   ← folded, done
▸ Edited 1 file  +12 −3 · ✕ 1 failed                                               0.9s   ← failure visible when folded
▾ Working                                                                          12s    ← live: shimmer
  … 3 earlier
  ├─ → Read  src/nexus/loader.py  1-120                                            0.1s
  ├─ * Search  "parse_manifest" in src  4 matches                                  0.2s
  ╰─ ◐ Shell  pytest tests/test_loader.py -q                                        3.4s
```

- **Folded summary:** verbs with counts (`Read 2 files, ran 1 command, edited 1 file`),
  `+N −M` for edits, then failures and denials in `status.error` with `✕`. The duration is
  right-aligned.
- **Live group:** a `Working` header shimmering from muted to primary
  (old Nexus algorithm), then the newest 5 members in a tree (`├─` / `╰─` in
  `border.default`) and `… N earlier` muted. The running member shows the spinner.
- **Members** (expanded group): `{tree} {glyph} {Verb}  {key argument}  {summary}` with the
  duration right-aligned. Verb glyphs carried over from old Nexus:
  `→` read, `←` edit/write/patch, `$` shell, `*` search, `λ` code, `?` ask,
  `↳` session, `↗` web. The status glyph replaces the verb glyph for
  running, error, denied and waiting.
- **Member detail** (Enter on a member): old Nexus's box.
  - `┌ Read src/nexus/loader.py  0.8s`, with the corner in `border.strong`.
  - The body sits behind `│ `, and the last body row's rail is `╰`.
  - Output is capped at the first 2 command rows and the last 8 output rows, with
    `↑ 214 earlier lines · enter show all` muted.
  - A `raw` toggle on the right shows the exact arguments and the result sent to the model.
- On finish, the live tree collapses to the folded summary, and the header flashes the accent
  for 6 frames (reduced motion: no flash).
- **NSH:**
  - The group is a 28 px row with a chevron, the summary in secondary 13/20 and the duration in meta.
  - Members are 24 px rows with an 18 px indent, a 1 px tree line in `--border-default` and a
    16 px icon per verb. The detail opens inline in a `--surface-panel` box with a 6 px radius.
  - The output box is capped at 320 px in height and scrolls, with `Show all` at the foot.
- Shots: `d06-groups` (folded, live, expanded, member detail), NSH `d06-tools`.

### D07 — Reasoning

- **Finished:** `▸ Thought for 2.1s · Looking for where the manifest is parsed`. `Thought` is bold
  muted, the duration muted, and the preview (the first summary line) muted italic, truncated.
- **Open:** the summary text in muted italic behind a `│` rail in `border.default`, indented 2.
- **Running:** `▾ Thinking… 2.4s`, then the last 5 wrapped rows behind `┃` in
  `border.strong`, with `│ … N earlier lines` above them.
- **Encrypted:** `▸ Thought for 2.1s · encrypted, replayed to the provider`, with no
  expansion.
- **NSH:** the same rows. The preview is a single line with an ellipsis, and the body opens in
  13/20 italic secondary with a 2 px `--border-default` left rule.
- Shots: `d07-reasoning`, NSH `d06-tools`.

### D08 — Markdown and code fences

- **Inline code:** `text.primary` on `surface.panel`, padded by one space each side.
  Never accent.
- **Fences:** a `surface.panel` block across the reading column. The header row holds the
  language (muted) on the left and `c copy` (muted, shown on focus or hover) on the right. The
  code sits below with a 2-cell inset. There is **no rule and no rail**.
  - A focused fence gets the focus bar in its left gutter.
  - Syntax colours come later (the library is still open), from a small 6-colour subset that
    passes check_theme.
- **Headings:** H1 and H2 are bold primary with a blank row before them, and H1 also gets a muted
  `─` underline the width of the text. H3 and below are bold secondary.
- **Lists:** `•`, then `◦` for nested items, with a hanging indent. Task lists use `[ ]` and `[x]`
  (`✓` in success).
- **Tables:** box-drawn with `border.default` and a bold header row; columns fit the width and
  cells wrap.
- **Links** are underlined and in `status.info`, with OSC 8. `path:line` is a link that opens the
  file view at that line.
- **Block quotes** sit behind a `│` rail in `border.strong`, in secondary text.
- **Mermaid** is unchanged: the diagram, then `◇ mermaid · source below`, then the fence.
- **NSH:** prose is 15/24 in the reading column. Fences use `--surface-panel`, a 10 px radius and
  a header bar with the language and a Copy button. Inline code is 13 px Monaspace on
  `--surface-panel` with a 4 px radius.
- Shots: `d08-markdown`, NSH `d08-markdown`.

### D09 — Diffs

- The file header is `src/nexus/loader.py  +12 −3`, with the path secondary and the counts in
  `diff.add` / `diff.del`, followed by the hunks.
- **Split view** when the reading column is ≥ 110 cells (old Nexus):
  - old number | old text │ new number | new text.
  - The split rule is `│` in `border.strong`.
  - Removed lines tint the left side with `diff.del_bg` and added lines tint the right with
    `diff.add_bg`.
  - The empty side of an add or delete is filled with `╱` in `border.default` (a hatch).
- **Unified view** below 110 cells:
  - a gutter with the new line numbers, then `+` or `-`, then the text on the tint;
  - hunks separated by a muted `⋯` row with `@@ -12,7 +12,10 @@`.
- Tinted text is primary and context lines are muted, as in old Nexus.
- **NSH:** split view at ≥ 1100 px window width. Monaspace 13/20, a 48 px number gutter, and the
  tints from the tokens.
- Shots: `d09-diff-split`, `d09-diff-unified`, NSH `d09-diff`.

### D10 — Starting context and the new-session view

- The **starting context** is a list of sections (main-branch mock-up style):
  ```
  ◆ System prompt   2,140 tok · build · 2 sections
    You are Nexus, a provider-agnostic coding agent working in the user's workspace. …
  ◆ AGENTS.md       3,880 tok · ~/repos/nexus · 1 file
  ◆ Tools           4,780 tok · 9 tools · 8 on
    read        bash        edit        write      (4-column grid; off tools struck through, muted)
  Context total · 17,580 tokens · 8.8% of the window
  ```
  - **No bar** (user choice): the total is text only. Each section header keeps its token
    count.
  - `◆` is muted, the title bold primary and the tokens secondary; the meta is muted.
  - One preview line per text section, and a column grid for the inventories.
- After the first turn the whole section folds to
  `▸ Context · 4 items · 17.6k tokens` at the top of the timeline.
- The **new-session view** shows this section as its content, under a header block:
  ```
  New session in nexus-demo
  Build · gpt-6.1-terra codex · medium · ~/repos/nexus-demo ⌥ main
  Tab agent · ctrl+m model · ctrl+e effort · ctrl+d directory
  ```
  The centred "Type a message below" text is removed.
- **NSH:** each section is a card on `--surface-panel` with a 10 px radius and a header row
  (title, tokens and meta). Tools are a 4-column grid of 24 px chips with switches. The total is
  a text row, with no meter bar.
- Shots: `d10-new-session`, `d10-context-folded`, NSH `d10-new-session`.

### D11 — Composer

- **Card:**
  - `surface.panel`, inset by the 2-cell gutter, with half-row edges (`▄` above and `▀` below
    in the panel colour; old Nexus used `▀`). It reads as a card without spending full rows.
  - Rows: the input, a blank row, then the control row.
- **Replaces `5409281`'s composer** (decided with the user): remove the agent-coloured rail
  and the darker status band; the context figure moves back onto the control row as the `╱`
  meter, and `ctrl+p commands` moves to the project strip.
- **Placeholder:** `Type a message…`. The cursor block sits on the first letter, as in old
  Nexus; there is no focus rail in truecolor. In ANSI-16 and mono, the focus bar is drawn at
  the left edge.
- **Control row:**
  - On the left: `Build · gpt-6.1-terra codex · medium`. The agent is bold in its colour, the
    provider muted and the effort secondary.
  - On the right: the old Nexus meter, ten `╱` cells filled in the agent colour over
    `border.default`, then `82k · 41%`.
  - Hovering the meter explains the tiers.
  - Narrow rows shed effort, then the provider, then the meter.
- **Busy:** a live row above the card: `◐ Working · 12s · ↓4.1k`, with Working shimmering.
  The control row's right side becomes `esc stop`.
  - Enter while busy steers once Core supports it. Until then the row says
    `steering needs Core (Step 12a)`.
- **Completion** (`/` and `@`): a borderless `surface.raised` block sitting directly on the
  card's top edge, **exactly as wide as the composer card** (user choice). It shows ten rows;
  the selected row has the selection band and focus bar. Key hints are its last row, muted.
- **Project strip** below the card, on the canvas:
  - `nexus-demo` (bold primary), `~/repos/nexus-demo` (muted), `⌥ main` (secondary), and
    `checkout` (muted) or `worktree` (bold violet, as in old Nexus).
  - `ctrl+p commands` is muted and right-aligned when idle.
  - The accent underline is removed.
- **NSH:**
  - The card uses `--surface-panel`, a 12 px radius, a 1 px `--border-default` border
    (`--border-focus` when focused) and 12×16 px padding.
  - The control row holds pill selectors (agent in its colour, model, effort), the meter as an
    80×4 px bar in the agent colour, and the send/stop button on the right.
  - Completion is a borderless raised block the width of the card, flush with its top edge.
- Shots: `d11-composer` (idle, draft, busy, completion, narrow), NSH `d11-composer`.

### D12 — Right sidebar

- Three sections, each with an UPPERCASE bold muted label and a blank row before the next:
  - **SESSION:** Agent, Model, Effort, Status, Turns, Tools (top 2 by count), Started and Id,
    in a 9-cell muted key column with primary values. Long values truncate in the middle
    and are copyable on Enter.
  - **CONTEXT:** the meter (`╱` cells) with the percentage, `82k of 200k`, `12k in · 2k out`
    and Cost.
  - **GIT:** `⌥ main ↑1`, then the Staged, Unstaged and Untracked groups. Each group has a count,
    and each file row is `M path  +2 −0` with the letter coloured (`M` warning, `A`/`?` success,
    `D` error).
- **NSH:** the same sections, with 11 px uppercase labels, a 2-column key/value grid (96 px
  keys), a 4 px meter and file rows with hover.
- Shots: `d02-shell-200x50` (it shows the sidebar), NSH `d02-shell`.

### D13 — Overlays: palette, pickers, settings and confirm

One modal recipe for every overlay:

- **Block, no border** (user choice): a filled `surface.raised` block with generous padding,
  2 cells left and right and 1 blank row top and bottom.
  - The title (bold primary) is the first row inside; `esc` (muted) is right-aligned on it.
  - The scrim (D01) is applied to everything underneath; with no frame, the scrim and the
    surface step are what separate the modal from the session.
  - Width is 64 cells for the palette and pickers, 88 for Settings, and 44–60 for confirms,
    centred. Height is capped at 70% of the frame.
- **Search row:** `⌕` and the query, with the placeholder muted. Below it, a hairline in
  `border.default`.
- **Rows:** a 2-cell mark slot, the label, and the key or value right-aligned.
  - **Selected:** the `interactive.selection` band and the `▌` focus bar. This replaces the
    full accent fill in Settings.
  - **Disabled:** muted, with the reason shown in the footer when selected.
- **Section headings:** UPPERCASE bold muted, with a blank row before each one except the first.
- **Footer:** key hints as the last row inside (`↑↓ move · enter run · esc close`), muted, with
  a count (`12 of 99`) on the right.
- **Settings** has 12 sections, more than fit at the 70% height cap. At ≥ 160 columns it gets a section index on the left (the 12 names, the current one on the selection band); narrower, the list scrolls and the footer names the next section.
- **Confirm:** a body text, then the buttons: `[ Cancel ]` (the safe default, selected) and
  `[ Delete ]` in `status.error`.
- **NSH:** a 10 px radius dialog with 24 px padding, **no border**, a shadow
  `0 12px 40px rgb(0 0 0 / .4)` and the scrim. The search input is 40 px. Rows are 32 px with
  selection and a 2 px accent bar.
- Shots: `d13-palette`, `d13-settings`, `d13-model-picker`, `d13-confirm`,
  NSH `d13-palette`, `d13-settings`.

### D14 — Waiting card (approval and question)

- Docked above the composer, across the reading column. It uses `surface.panel` with a heavy
  `┃` rail in `interactive.accent`.
- **Header:**
  - `◆ Allow bash?` (`◆` accent, the title bold), with `1 of 2 · tab next` muted and
    right-aligned.
  - Below it, `waiting on you · 0:42` in muted.
- **Body:** the full parameters as a key/value table (`command`, `cwd`, `policy`). For edit,
  write and patch, the diff (D09) is capped at 12 rows.
- **Choices:** a list in the old Nexus style, one per row:
  `▸  y  Allow once`, `   a  Allow for session`, `   n  Deny`, `   m  Deny with a note`.
  - The key is a chip in bold primary on `surface.raised`.
  - The selected row has the selection band and `▸`.
  - A choice Core does not offer is muted, with its reason.
- **Question:** each question is a bold row, then its options (`( )`/`(*)` or `[ ]`/`[x]`) and a
  free-text field. The footer reads `enter submit · tab next question`.
- **NSH:** a card with a 10 px radius, `--surface-panel` and a 3 px accent left border. Choices
  are buttons in a row (Allow once is primary), each with its key hint.
- Shots: `d14-approval`, `d14-question`, NSH `d14-waiting`.

### D15 — Alerts: toasts, banners and jump to latest

- **Toasts:** top-right of the page, under the header, 36–48 cells wide, newest at the bottom,
  three at most, then `+N more`. Old Nexus anatomy:
  - `raised`, a `▌` in the level colour, the level glyph, a bold title and `×` on the right.
  - **No lifetime hairline** (user choice).
  - **Full text on click:** a click, or Enter on a focused toast, opens a borderless modal
    (D13) titled by the toast, with the whole notification wrapped and scrollable (long
    provider errors and bodies included), hints `c copy · esc close`. The notifications
    drawer opens the same modal for past entries.
- **Banner:** one row at the top of the page on `panel`; it pushes the timeline down one row and never covers it. The level glyph and the first words
  are in the level colour; actions are on the right (`r retry · × hide`).
- **Jump to latest:** a raised pill, right-aligned on the row above the composer card:
  `↓ 3 new · end`. It never overlaps content.
- **NSH:** toasts are 360 px, top-right under the header, `--surface-raised`, with a 10 px
  radius and no progress hairline; clicking opens the full-text dialog. The banner is a 36 px strip. Jump to latest is a floating
  pill above the composer.
- Shots: `d15-alerts`, `d15-toast-detail`, NSH `d15-alerts`, `d15-toast-detail`.

### D16 — Files

- **Tree:** the root header (`SIDE-PROJECT ⌥ main`), then a 2-cell indent per level with `│`
  guides in `border.default`. Folders are bold, and ignored entries are muted.
  - Git letters are right-aligned and coloured (`M` warning, `A`/`?` success, `D` error).
  - The selected row has the selection band and the focus bar.
- **File view:**
  - The header row holds the `~` path in secondary (the basename bold) and `lines 1-40 of 300`
    muted and right-aligned.
  - Below it, a gutter with right-aligned muted line numbers and `│` in `border.default`.
  - Search is a bottom row: `/ query  3 of 12  n next · N prev · esc end`. Matches are on
    `interactive.selection`, and the current match is reversed.
- **NSH:** the tree has 24 px rows and 16 px indent guides. The file view has a sticky header,
  Monaspace 13/20 and a 48 px gutter.
- Shots: `d16-files`, NSH `d16-files`.

### D17 — Responsive behaviour

- **≥ 160 columns:** both sidebars docked (34 / 36), and the reading column capped at 120 and centred.
- **120–159:** both sidebars docked (28 / 30).
- **100–119:** the left sidebar docked and the right closed by default.
- **< 100:** both sidebars become drawers over the page with the scrim. The header shows only
  the toggles and the breadcrumb, truncated from the left.
- The composer sheds effort, then the provider, then the meter. The footer wraps.
- **NSH:** ≥ 1280 px both docked, 1024–1279 left only, < 768 one pane with drawers and 44 px
  targets.
- Shots: `d17-80x24`, `d17-80x24-drawer`, NSH `d17-phone`.

### D18 — Empty, loading and offline states

- **Empty workspace:**
  - A centred block on the page with the **Nexus splash logo** (user choice), in the accent
    colour, used by both clients wherever a splash or wordmark appears (empty workspace,
    connecting screen):
    ```
        _/      _/
       _/_/    _/
      _/  _/  _/
     _/    _/_/
    _/      _/
    ```
    ASCII mode needs no fallback; monochrome draws it bold.
  - Below it: `No projects yet`, `ctrl+o add a project · ctrl+p commands`.
- **Loading:** a muted `◐ loading sessions…` in place of the list, with no layout shift.
- **Offline:**
  - The header shows `✕ offline`.
  - A banner reads `Disconnected from Core · retrying in 4s · r retry`.
  - The composer stays editable with `Reconnecting to Core… your draft is kept`.
- **NSH:** the same text, and the logo is the same five rows in Monaspace (or an SVG traced
  from it) in `--interactive-accent`.
- Shots: `d18-empty`, `d18-offline`, NSH `d18-states`.

### D19 — Motion and accessibility

- **Shimmer:** the old Nexus algorithm. Per grapheme, from muted to primary, a 54-frame sweep at
  30 Hz, a reach of 4 and smoothstep falloff. Used only for `Working` and running session
  titles.
- **Spinner:** the braille set at 10 Hz. Reduced motion uses a static `◐`.
- **Finish flash:** 6 frames. Reduced motion: none.
- **Mono and ASCII:**
  - every glyph has its fallback;
  - the half-row composer edges become `─` rules;
  - the `╱` meter becomes `/` and `.`;
  - the selection band becomes reverse video.
- **NSH:** 120 ms ease on hover and focus, `prefers-reduced-motion` honoured, and focus rings
  always visible.
- Shots: the `-mono` and `-ascii` variants of `d02-shell-200x50`.

## Choosing

Done: `nexus-design-mockup/choices.json` (2026-10-28) picks A everywhere, with notes.
`nexus-design-mockup/shots/index.html` is the chooser. For each decision it shows
the recommended option (A), the alternatives (several from old Nexus) and today's
TUI. Choose one per decision, add notes, then **Export choices** (it downloads
`choices.json`). Save that file as `nexus-design-mockup/choices.json`. This plan
is written for the recommended options. Where a choice differs, the matching
decision above and its increment below are rewritten before the work starts.
Every non-recommended option already has a mock-up, so a different choice changes
only which recipe is followed, not the order of the increments.

## Progress (2026-10-28, handed back to the user)

Work stopped at the user's request after I4. State on `nexus10`:

| Increment | TUI | NSH | Commit |
| --- | --- | --- | --- |
| I0 tooling | done: `--dump-frame --svg --keys --name`, `tools/shots.sh`, `seed.sh` honours `NEXUS_CMD` | — | `49fe129` |
| I1 tokens, scrim, chrome | done (`paint::scrim`, `border.strong`, chrome surfaces, edge `│` only in ANSI-16/mono) | done (bg-sidebar chrome, `border-strong` from gen.py) | `fea3b41` |
| I2 header band and tabs | done (icons in the band, session tabs restyled, child crumbs, D17 widths) | done (icons in the header, tabs on the reading column) | `fea3b41` |
| I3 timeline rhythm | done (band, footer without status, durations, Thought rows, verb tree, detail box, sticky header, header status) | done (user card, footer, verb icons, detail box, thought rows, sticky bar) | `f83653c` |
| I4 Markdown, fences, diffs | done (`diff.rs` split ≥ 110 and unified; fences; inline code) | done (fence cards, `diff.tsx`) | `12dfa97` |
| I5 composer | done (soft card, `paint::meter` on the control row, Working row, card-wide completion, project strip) | done (soft card, no rail/band, meter, Working row, card-wide completion, strip) | `12dfa97` + the TUI I5 commit |
| I6 sidebars | done (search row, project and worktree groups, D03 rows, row-menu popover, rail band; SESSION / CONTEXT / GIT right sidebar) | done (52 px rows with agent line and `n ↳`, hover `⋯` menu, 11 px uppercase group headers, uppercase right-sidebar labels, 96 px key grid, 4 px meter, Git groups; no search row yet: `session.search` has no NSH handler; no per-row branch: the row has no branch data) | the TUI I6 commit and `interface: NSH sidebars (DESIGN_PLAN I6)` |
| I7 modal recipe | done (`render.rs` `modal`: scrim, borderless raised block, title and `esc`, hint row; palette, pickers, Settings, confirm, help, notifications; UPPERCASE muted Settings headings, selection band; `[ Cancel ]` default) | not started | the TUI I7 commit |
| I8 waiting card, alerts | not started | not started | |
| I9 new-session view | not started | not started | |
| I10 files, narrow, states | not started | not started | |

**Next, in order:** the split-diff wrap fix (continuation rows of a wrapped line in `diff.rs`
start at the `+`/`-` column instead of the text column), then I6–I10 for both clients.

**Known gaps from the landed increments:**
- TUI: OSC 8 links and `path:line` links not done (ratatui cells carry no escapes; needs hit
  regions). The fence copy button reads `copy` (no `c` key in the manifest). Message tags
  (`steer`, `queued`…) render through a helper but Core sends none. The live `Working`
  shimmer and the narrow footer wrap are covered by tests only (the seed has neither).
  Edit/patch diffs have no line numbers when the tool input has none. The Git diff view has
  no screenshot (the seed workspace is not a Git repo). Split-diff wrap bug above.
- TUI dependency: `ratatui-markdown` was dropped in I4 (unused; `pulldown-cmark` parses).
- NSH: verified on `nsh 0.3.0 (09c1632)` (`nsh check` clean, `nsh test --target web` 28 of 32,
  `npm run build` needs `llvm-ar` on PATH, e2e shots at the three sizes, dark and light). The
  sticky bar's offsets are now summed from measured row heights (an item's `onLayout` frame is
  relative to its own container); the split diff's empty side is a hatch (`repeating-linear-gradient`).
  The four failing tests are harness limits, not app bugs: two need a held SSE stream (the stub's
  stream ends at once, so the view flaps to "reconnecting" and a send is disabled), two use
  `clock: {}`, which hangs on `--target web` in nsh 09c1632. NSH also lacks thought durations
  (parts carry no timing), message tags and the live shimmer; the sticky bar's line count is an
  estimate.

**How to continue:**
- Visual review without node: `cd nexus-tui && NEXUS_CMD=../nexus-core/.venv/bin/nexus
  tools/shots.sh /tmp/nx-root /tmp/nx-shots` (docs/development.md, "Visual review of the
  TUI"). Compare with `nexus-design-mockup/shots/tui/`.
- `nexus-tui/src/theme.rs` is blocked by the organization's web filter for Claude (it reads
  a method call in that file as a `.bg` URL; correlation ID
  `9bc636aa-173c-435d-a595-58d9794d349b`). Agents worked from the Theme API's call sites
  and put new helpers in `paint.rs`; no `theme.rs` edit was needed so far.
- Machine notes: Homebrew is partly broken (node's `libuv`, `openssl@3`, `glib`). The Core
  venv was rebuilt on the python.org Python 3.14 (`uv sync --python
  /usr/local/bin/python3.14`); uv was updated to 0.13.6.

## Implementation

Ten increments, in order. Each one is a reviewable change. It ships its TUI part
and its NSH part together, updates `nexus-tui/README.md`, `nexus-nsh/README.md`,
`interface/design-language.md` and `interface/review.md` where they are
affected, and is checked in the real app:

- **TUI:** `nexus-tui/tools/webterm` shots at 200×50, 120×36 and 80×24, dark and light, plus
  mono and ASCII for the shell. Node is broken on this machine (Homebrew `libuv`
  is missing); fix it with `brew reinstall node`, or use the patched `--dump-frame` SVG
  export described under Tooling.
- **NSH:** `e2e/shots.mjs` at 1440×900, 1024×768 and 390×844, dark and light.
- **Checks:** `cargo test`, `cargo clippy --all-targets -- -D warnings`,
  `cargo fmt --check` and `python3 interface/check_theme.py` for the TUI;
  `npm run check` and `nsh test --target web` for NSH.

Buffer tests that pin today's strings (footer text, `<1 s`, the location
underline, header layout) are updated in the same increment that changes
them. Never delete a test to make room; restate what it checks.

### I1 — Tokens, scrim and the chrome surface (D01, D02 frame)

- **`interface/theme.toml`:**
  - Add `border.strong` (dark `#4a4a50`, light `#c2c3c0`, 256: 239/250, 16:
    `bright_black`/`white`, mono `[]`).
  - Add to the comments that `surface.sidebar` is the "chrome" surface for the header band and
    both sidebars.
  - `interface/check_theme.py`: treat `border.strong` as decorative (exempt from 3:1 like
    `border.default`).
  - `nsh` `npm run gen` picks up the token. Map `--border-strong` in
    `scripts/gen.py` if it filters groups.
- **TUI `theme.rs`:**
  - `Theme::scrim(buf: &mut Buffer, area)` blends each cell's fg and bg 50% toward
    `surface.canvas`. ANSI-16, 256 and mono: add `Modifier::DIM` instead.
  - Add a unit test with one RGB cell and one indexed cell.
- **TUI `render.rs`:**
  - `draw_header` paints row 0 with `surface.sidebar` across the full width.
  - `draw_right` fills its area with `surface.sidebar`.
  - `draw_sidebar_edge` draws the `│` only when the colour tier is ANSI-16 or mono.
- **NSH:**
  - `header.tsx` uses a `bg-sidebar` class and drops `border-b`.
  - `sidebar.tsx` and `right.tsx` use `bg-sidebar` and drop `border-r` and `border-l`.
  - Add a `surface-sidebar` utility if `gen.py` does not emit one.
- Review: `d02-shell-200x50` against a webterm shot of the same fixture.

### I2 — Header band: sidebar icons and session tabs (D02)

- **TUI `render.rs` `draw_header`** (session tabs from `5409281` stay; restyle them):
  - Paint the band on `surface.sidebar`. Move `draw_tab_icons` into the band at x=4 over the
    left sidebar; `draw_left` starts its content one row higher, with the search row first.
  - Session tabs start at the centre's text edge: the viewed tab bold primary on
    `surface.canvas`, the others muted on the band, glyphs per D02, `+` last. Keep the existing
    fitting rules and keys.
  - Child sessions: the viewed tab shows `root › child` with clickable earlier crumbs.
  - `header_status` right-aligned at the centre's right edge; the attention count and the right
    toggle at the far right.
  - Update the `layout.rs` hit rects for the icons, the tabs, the crumbs and the status.
- **`layout.rs`:** widths from D17: left 34 at ≥160 columns, 28 at 120–159; right 36 / 30.
  The saved `ui.left_width` still wins when it is set.
- **NSH `header.tsx` and `sidebar.tsx`:** sidebar icon buttons move into the header's left
  slot; session tabs (from `0624c37`) restyled per D02; the header grid has `264px 1fr 300px`
  columns to match the sidebars.
- Tests: update the header tests in `tests/shell.rs` (tab styles, icon hit rects in the band,
  child breadcrumb).

### I3 — Timeline rhythm: user band, footer, reasoning, groups (D04–D07)

- **`timeline.rs` `turn_block`:**
  - The band spans the full page width, with the rail `▌` muted (focus colour when focused) and
    one padding row above and below.
  - The fold marker sits inside the band at the text edge, and the text indent is 2 after it.
  - `#n · time` is muted on the first row.
  - Tags (`steer`, `queued`, `from parent`) go before `#n`.
- **`turn_footer`:**
  - Becomes the right-aligned `▪ Agent · models · effort · tools · duration · tokens · cost`
    row after a blank row, with no hairline and **no end status** (D05).
  - Interrupted turns add the `/continue` hint after it; failed turns put the error's first
    line (`✕` in `status.error`) on the row above it.
  - Wrap rule: break the segments from the left onto an extra right-aligned row.
  - Remove the footer's header-like placement above the next turn.
- **`fmt_duration`:** `0.4s`, `4.2s`, `1m 05s`, `1h 02m`. Replace every `<1 s`.
- **`reasoning_block`:**
  - `▸ Thought for 2.1s · <first summary line, italic>`.
  - Running: `▾ Thinking… 2.4s`, then the last 5 rows behind `┃` (`border.strong`) and
    `│ … N earlier lines`.
  - Encrypted: the label from D07.
- **`activity_group`:**
  - The folded summary keeps today's verb counts. Add `+N −M` for edits and failures in
    `status.error` with `✕`.
  - The live header is `Working`, using the existing `theme::shimmer`.
  - Below it, the newest 5 members in a `├─`/`╰─` tree, and `… N earlier`.
- **`tool_block`:**
  - Add verb glyphs to the member rows (`→ ← $ * λ ? ↳ ↗`, with ASCII `> < $ * L ? ^ ^`). Add
    them to `theme.toml` as `[glyphs.verb]`, with the theme check verifying single-width.
  - The status glyph replaces the verb glyph for running, error, denied and waiting.
- **`tool_detail`:**
  - The old box: `┌ Verb arg  0.8s` with the corner in `border.strong`, the body behind `│ `,
    and the last row `╰`.
  - The output cap is 2 command rows and 8 tail rows, with `↑ N earlier lines · enter show all`.
  - The `raw` toggle is right-aligned on the head row.
  - Finish flash: 6 frames of accent on the group glyph, none with reduced motion.
- **Detail levels** (`alt+d`, from `5409281`) stay: low shows only the folded summary rows,
  medium (default) shows the live tree and folded finished groups, high opens every group's
  members. The D06 styles apply at every level.
- **Sticky header** (new `sticky.rs`, adapted from old `rust/tui/src/sticky.rs`):
  - When a band's first row is above the viewport, pin a copy under the header: at most 3 rows,
    then the `░` shade row in `border.strong` with `↑ N lines of this turn above`.
  - The next band pushes it up. A click scrolls to the band.
  - Test: anchor and push-up at 80×24 and 200×50.
- **NSH `timeline.tsx`:**
  - `RowView` for user rows: the full-column raised card, a 10 px radius, 12×16 px padding and
    a 2 px accent bar when focused.
  - Footer: a right-aligned meta row with a token tooltip and no status chip.
  - Groups: 28 px rows; members 24 px with a 1 px tree line and 16 px verb icons
    (add `src/icons/{read,edit,shell,search,code,ask,session,web}.svg`).
  - Thought rows as in D07.
  - The sticky user header with `position: sticky`.
- Tests:
  - Duration formatting.
  - The footer wrap at 80, 120 and 200 columns.
  - Folded failure visibility.
  - Glyph fallbacks in ASCII.
  - The sticky header geometry.

### I4 — Markdown, fences and diffs (D08, D09)

- **`markdown.rs`:**
  - Inline code: `text.primary` on `surface.panel`, padded by one space, never accent.
  - Fences: a panel block with a header row (language muted, `c copy` on focus or hover) and the
    code inset by 2. **Remove the rule and the rail.**
  - H1: a bold primary heading with a muted underline. H3 and below: bold secondary.
  - Lists `•`/`◦`, block quotes behind a `│` in `border.strong`, links in `status.info`
    underlined with OSC 8, and `path:line` links that open the file view.
  - Mermaid output is unchanged.
- **Diffs (`timeline.rs` `tool_detail` for edit, write and patch, and `render.rs`
  `draw_diff_view`):**
  - Add one `diff.rs` module with `unified(width)` and `split(width)`.
  - Split when the column is ≥ 110 cells: the old Nexus layout (`TR:425-607` on `main`) with the
    `╱` hatch on the empty side and the split rule in `border.strong`.
  - Hunk separator: `⋯ @@ … @@` muted.
  - The Git diff view in the centre column uses the same module.
- **NSH:**
  - `timeline.tsx` Markdown: a fence card with a header bar and a Copy button; 13 px inline code
    on panel.
  - A new `diff.tsx` with split view at ≥ 1100 px and unified view below.
- Tests:
  - The fence layout snapshot.
  - Split/unified switching at 109/110 columns.
  - The hatch cells.
  - Wide CJK lines wrapping inside the split columns.

### I5 — Composer and project strip (D11)

- **`render.rs` `draw_composer`:** remove the agent rail and the status band from `5409281`
  (and their tests), then:
  - Half-row card edges (`▄` and `▀` in the panel colour; `─` rules in mono and ASCII).
  - Input at x+3, then a blank row, then the control row.
  - The placeholder cursor sits on the first letter.
  - Drop the focus rail in truecolor and 256; keep it in ANSI-16 and mono.
- **Control row:** `Agent · model provider · effort`, and on the right the `╱` meter (ten cells,
  agent colour over `border.default`) and `82k · 41%`.
  - Keep `draw_usage_tooltip`.
  - Shed effort, then the provider, then the meter.
- **Busy:**
  - A live row above the card: `◐ Working · 12s · ↓4.1k`, with the shimmer.
  - The control row's right side reads `esc stop`.
  - Remove the old busy markers inside the card.
- **Completion (`draw_completion`):** a borderless raised block exactly as wide as the composer
  card, flush with its top edge, with the selection band and focus bar, and key hints as its
  last row.
- **Project strip:** replace the accent underline with the D11 segments. Bold `worktree`
  in `agent.violet`.
- **NSH `composer.tsx`:** the card with a 12 px radius and a 1 px border (focus colour when
  focused), pill selectors, an 80×4 meter in the agent colour, the Working row above and the
  send/stop button.
- Tests: the composer row geometry at 80, 120 and 200; the shedding order; the meter cells;
  mono edges.

### I6 — Sessions sidebar and right sidebar (D03, D12)

- **`render.rs` `draw_sessions`:**
  - The search row (it opens the switcher) and the project group headers.
  - Worktree sub-groups from the existing checkout listing.
  - Rows per D03: the mark slot from the attention data, the agent in its colour and the
    `2 ↳` child count.
  - The open session gets the selection band and focus gets the bar on both lines.
  - The row menu (`.`) is a raised popover listing the session actions; Delete opens the
    existing confirm.
  - `Settings` is pinned to the bottom row.
- **`draw_rail`:** the open item gets the selection band. Otherwise unchanged.
- **`draw_right`, `session_info` and `git_status`:**
  - UPPERCASE bold muted section labels, a 9-cell key column and middle-truncated values.
  - The CONTEXT section: the meter, `82k of 200k`, tokens and cost.
  - Core does not report usage totals yet (PLAN.md Step 12a), so the rows read
    `not reported`.
  - GIT groups with counts, letters coloured as in D12 and `+N −M` right-aligned.
- **NSH:**
  - `sidebar.tsx`: 52 px rows, 11 px uppercase group headers and a hover `⋯` menu.
  - `right.tsx`: `Section` labels uppercase, a 96 px key grid, a 4 px meter and Git rows.
- Tests: the row layout at sidebar widths 22, 28 and 34; mark precedence; the right sidebar
  truncation.

### I7 — One modal recipe (D13)

- Add **`render.rs` `modal(frame, title, width)`**, used by `draw_palette`,
  `draw_settings_modal`, the pickers, `draw_dialog`, `draw_help` and
  `draw_notifications`:
  - It applies the scrim to the whole frame and fills `surface.raised` with **no border**:
    2 cells of padding left and right, 1 row top and bottom.
  - The title is the first row inside with `esc` on the right; key hints are the last row
    inside.
  - `kit`-style fills must reset modifiers so text styles underneath do not leak in.
- **Settings:** UPPERCASE muted section headings replace violet. The selected row has the
  selection band and focus bar, replacing the accent fill. Reasons show in the footer.
- **Confirm:** `[ Cancel ]` is the selected default; `[ Delete ]` is in `status.error`.
- **NSH `overlays.tsx` and `settings.tsx`:** one `Dialog` shell with a 10 px radius, the scrim
  and the shadow; 32 px rows with selection and a 2 px accent bar.
- Tests: a scrim cell test, no border cells drawn, block geometry at 80×24 (the modal fits within 2 cells of each
  edge), the selected-row style.

### I8 — Waiting card and alerts (D14, D15)

- **`draw_waiting`:**
  - The heavy accent rail, `◆ Allow bash?` and `waiting on you · 0:42`.
  - The parameter table, with the diff from I4 capped at 12 rows.
  - The key-chip choice list, where a choice Core does not offer shows its reason.
  - Questions use `( )`/`(*)` and `[ ]`/`[x]`, plus free text.
- **`draw_toasts`:** top-right under the header, with the level `▌`, glyph, bold title and
  `×`; no lifetime hairline.
- **Toast detail:** a click, or Enter on a focused toast, opens the I7 modal with the full
  notification text (wrapped, scrollable, `c` copies it, Esc closes). The notifications
  drawer (`draw_notifications`) opens the same modal for an entry. Keep the full text on the
  alert record; today's toast may store only the title.
- **`draw_banner`:** one row at the top of the page.
- **Jump to latest:** a raised pill on the row above the composer card, right-aligned. It
  never draws over the timeline.
- **NSH:** `composer.tsx` `WaitingCard` per D14; `alerts.tsx` toasts top-right, the 36 px
  banner, the floating jump pill and a full-text dialog when a toast is clicked.
- Tests: waiting card heights, toast stacking and `+N more`, the pill position at 80×24, the
  toast-detail modal opening from a click and from Enter.

### I9 — New-session view and starting context (D10)

- **`draw_new_session_context`:** the header block, then the context sections in the D10
  layout. The tools grid uses the session's tool switches.
  - No context bar: the total is a text row; each section header keeps its token count.
  - The preview stays client-side until Core session drafts land (PLAN.md Step 12a). Each
    section's token count reads `~` with "estimated" on hover until then.
- **`timeline.rs` `context_section`:** the same section renderer. It folds to one line after
  the first turn.
- **NSH `newsession.tsx`:** the section cards and the tools chip grid.
- Tests: section rendering with and without AGENTS.md, and the folded line.

### I10 — Files, narrow layouts and states (D16–D18)

- **`draw_files`:**
  - Indent guides and the root header with its branch.
  - Coloured Git letters (map them in `git_marker`).
- **`draw_file_view`:**
  - The header row with the bold basename and the line range.
  - A gutter with a `│` and the bottom search row.
- **Narrow layouts (`layout.rs`):** apply the D17 breakpoints. Drawers open over the scrim, and
  the header sheds to toggles and the left-truncated crumb.
- **`empty_state`:** the D18 splash logo (the five-row `_/` N, accent, bold in mono) and the
  next-step sentence. Put the logo rows in one constant shared with the connecting screen.
- **Offline:** the banner text, `✕ offline` in the header and the composer placeholder.
- **NSH:** `sidebar.tsx` Files tab (blocked until NSH files land; see PLAN.md "NSH (12.5
  rest)"), a phone layout with drawers and 44 px targets, and the empty and offline states.
- **NSH:** the same logo rows as a shared constant (generated into `src/gen/` if it moves to
  `interface/`), rendered in Monaspace in the accent colour.
- Tests: breakpoint table, drawer hit-testing, the logo rows and their centring.

## Tooling

- `nexus-design-mockup/` (temporary):
  - `./shoot.sh` renders every TUI mock-up (Ratatui buffers, then SVG, then PNG through
    Playwright's headless Chromium) and every NSH page, then rebuilds the chooser.
  - `./crop.sh` zooms into a region for review.
  - The proposed theme is `nexus-design-mockup/theme.toml` (today's tokens plus D01).
- Real-app captures without node: a patched `--dump-frame` that also writes an SVG of
  the buffer and accepts `--keys`. A working copy is in the session scratchpad, and it is worth
  upstreaming as `nexus-tui --dump-frame … --svg` so reviews do not depend on node.
- When this plan has landed, delete `nexus-design-mockup/`. The decisions survive in
  `interface/design-language.md`, INTERFACE_PLAN.md and the ledger.

## Spec and ledger changes

These land with the increment that implements them:

| Where | Change |
| --- | --- |
| INTERFACE_PLAN "Turn footer" | Right-aligned at the end of the turn, `▪ Agent · …`, no hairline, no end status word (D05) |
| INTERFACE_PLAN "Workspace header" | Header band on the chrome surface holding the sidebar icons and the session tabs (viewed tab on the canvas surface); child breadcrumb in the viewed tab; run status at the centre's right edge (D02) |
| INTERFACE_PLAN "Toast" | Top-right under the header (old Nexus), not bottom-right; no lifetime hairline; click opens the full text (D15) |
| INTERFACE_PLAN "Dialog and confirm" | Borderless raised block with padding over the scrim; title and hints inside (D13) |
| INTERFACE_PLAN "Waiting card" | The choice list with key chips replaces the bracketed button row (D14) |
| INTERFACE_PLAN "Layout" (Settings) | Selection band and focus bar instead of the accent fill; UPPERCASE muted headings instead of violet (D13) |
| INTERFACE_PLAN "Composer" | Half-row card, no agent rail and no status band (replacing `5409281`), `╱` meter on the control row, completion as wide as the card (D11) |
| INTERFACE_PLAN "Context section" | `◆` sections with preview lines and a tools grid; total as text, no bar (D10) |
| INTERFACE_PLAN "Loading and empty states" | The `_/` N splash logo in both clients (D18) |
| design-language.md | Chrome and page surfaces, rationed accent (never inline code), section label style, modal recipe |
| PLAN.md ledger | "Session tabs → kept, restyled in the chrome band"; "Composer rail and status band → soft card with the `╱` meter (user choice)"; "Old modal backdrop dim → scrim on the cell buffer"; "Old sticky user card → kept"; "Old `╱` meter → kept in the composer and the right sidebar" |
| PLAN.md Open questions | "Focus ring colour" is answered by D01 (A shares the accent; C is a cool hue) |
