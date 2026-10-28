# Interface work plan: the old-Nexus look, then the rest of Step 12

> **Direction changed:** The TUI should reuse the old Nexus TUI broadly,
> adapting its presentation and interactions to the Nexus 0.10 Core API and
> one-timeline model. Settings and modal pages also follow old Nexus, with a
> cohesive, polished design language. The detailed targets below are a historical
> work inventory; where they conflict with this direction, old Nexus wins. NSH
> parity is not the current objective. The reference is `../nexus/rust/tui/src`;
> old Nexus remains read-only.

Status: in progress (updated 2026-10-28). This is the working order for the
client work that remains in [PLAN.md, Step 12](PLAN.md#step-12-clients). The product spec
stays [INTERFACE_PLAN.md](INTERFACE_PLAN.md); when a phase here lands, update
the matching spec section (tokens, anatomy, mockups) in the same change and
tick the phase off here. Core work comes from
[PLAN.md, Step 12a](PLAN.md#step-12a-core-for-the-interface).

Part A brings the TUI (and with it NSH) to the look of old Nexus
(`~/repos/nexus`, branch `nexus01`): the composer, the context timeline, the
left sidebar, agent colours, the canvas colours, files, and no status footer.
Part B is the rest of Step 12 (12.2 to 12.6) in build order.

Reference code in old Nexus (read, copy deliberately, attribute):

| What | Old file |
| --- | --- |
| Verb groups (`▾ Working`, tree, `▸ Ran 2 commands · 1.8s`) | `rust/tui/src/transcript/groups.rs`, `look.rs`, `fold.rs` |
| Turn header chip, user block | `rust/tui/src/transcript/header.rs` |
| Composer chrome, agent-coloured name | `rust/tui/src/render/chrome.rs` (`control_spans`), `render.rs` |
| Sidebar rows, status marks, spinner | `rust/tui/src/sidebar.rs` (`row_line`, `mark`) |
| Agent palette | `nexus/ui_support/context_header.py` (`AGENT_COLORS`) |
| Shimmer of live text | `rust/tui/src/live_anim.rs`, `transcript/look.rs` (`live`) |
| Copy button on hover | `rust/tui/src/copy_button.rs` |
| Editor | `rust/tui/src/editor.rs` |

---

## Current checkpoint (2026-10-28)

The current objective is the old-Nexus TUI and a cohesive design language.
The detailed Part A/B material below is historical inventory; it is not a
claim that all of those cross-client features have landed.

- **Shell and transcript:** neutral shared theme, two-line session rows, user
  blocks, foldable verb groups, measured reasoning time and the old Nexus soft
  shimmer are present. The old CommonMark renderer and grapheme-aware styled
  wrapping are adapted into the timeline. Supported Mermaid diagrams render above their original source;
  unsupported diagrams and ASCII mode retain source. Fences are independently
  selectable and expose copy actions.
- **Composer and Settings:** the copied old editor backs the composer,
  including grapheme-safe movement, selection, undo/redo and paste chips
  that expand before sending. The composer shows a capitalised agent name in
  its colour, catalog model names with their provider, a pending model change
  as `old → new (next call)`, effort, and context occupancy against the
  catalog's price tiers and window, with a hover explanation. The location
  line uses the project's own worktree listing before a session exists.
  Settings is a rounded centered modal with a
  searchable root and drill-in pages. Current settings rows remain backed by
  the shared manifest; many controls are still unavailable.
- **Files and Git:** Core provides read-only listing, file content and Git
  status/diff. The TUI opens directories lazily in a tree with Git letters,
  ignored dimming/hiding, a root selector when there are several checkouts, and
  a read-only file view; copy path, `@path` insertion and `$EDITOR` opening are
  wired. The editor/save flow, file search and highlighting, change
  notifications, diff UI, timeline path links, `@` completion and NSH parity
  remain. A real isolated Core/PTY file-open scenario passed. Work begun in NSH
  before the TUI-only direction is not a parity claim.
- **Review:** current captures and source-based findings are in
  [interface/review.md](interface/review.md); the design tokens and states are
  described in [interface/design-language.md](interface/design-language.md).
  A live old/new comparison was not run: the old application's stall logger
  and an old Python state store can access `~/.nexus` directly, which this
  workspace must never touch. Exact old-client equivalence remains unverified.
- **Checks:** the current full run passes 322 TUI tests, 349 Core tests,
  370 theme checks, the manifest checker, 14 NSH browser tests and four host
  tests. Clippy (`-D warnings`) and `cargo fmt --check` are clean. The shell and
  composer captures in `interface/review/` are from this build.

## Part A: the look

### A0. Order and review

Build A1 (tokens) first, since every other item reads them. Then A2
(composer) and A6 (remove the footer) together, because the footer's content
moves into the composer and header. Then A3 (timeline), A4 (agent colours),
A5 (sidebar). A7 (files) has a Core dependency and runs in parallel from the
start.

After each item: capture the TUI at 80×24, 120×36 and 160×48 (dark), compare
with the reference images and with old Nexus running side by side, and fix
before moving on. NSH follows in the same phase (A8), from the same tokens.

### A1. Canvas, composer and user-message colours

Progress: neutral dark tokens, canvas sidebar backgrounds and docked sidebar
hairlines are implemented in both clients, with TUI surface buffer coverage.
Dark TUI captures at 80×24, 120×36 and 160×48 are in
`/tmp/nexus10-turn-review/png/` (before the final gutter adjustment); the final
120×36 capture is in `/tmp/nexus10-turn-final/png/`. Comparison with the
references and old Nexus remains, so this phase is not yet complete.

New dark surfaces in `interface/theme.toml`:

| Token | Now | New | Used for |
| --- | --- | --- | --- |
| `surface.canvas` | `#121314` | `#0B0B0B` | App background, timeline, sidebars |
| `surface.panel` | `#181a1c` | `#1e1e1e` | Composer, cards, notices, waiting card |
| `surface.raised` | `#212427` | `#242424` | User message, dialogs, menus, toasts |
| `interactive.hover` | `#212427` | `#1e1e1e` | Hover rows (one step up from canvas, so it never matches a user block) |
| `border.default` | `#2e3236` | `#2a2a2a` | Hairlines (sidebar edge, turn footer rule) |

- The surfaces become neutral grey (no blue tint). Keep the text greys;
  checked against the new surfaces they still pass: primary 12.5:1, secondary
  6.6:1, muted 4.6:1 on `#242424` (the worst case). Muted on `#242424` is near
  the 4.5 floor, so do not darken it further.
- `fallback256`: canvas 232 (`#080808`), panel 234 (`#1c1c1c`), raised 235
  (`#262626`). ANSI-16 keeps the terminal's own background, as today.
- Sidebars sit on the canvas (as in the sidebar reference), separated from the
  timeline by a one-column hairline in `border.default`, not by a fill.
- Light theme: keep the current values; re-run the check. A light pass is a
  later review item, not part of this change.
- Files: `interface/theme.toml`, `interface/check_theme.py` (no rule change),
  `nexus-tui/src/theme.rs` (reads the file; check that nothing hard-codes the
  old hex), the NSH generated theme (`nexus-nsh/gen/`).
- Tests: `check_theme.py` passes; a TUI buffer test asserts canvas, composer
  and user-block backgrounds by cell.

### A2. Composer

Progress: TUI panel geometry, input scrolling to eight rows, selector hit
areas, stop/continue and directory line are implemented. The agent name is
capitalised and coloured; catalog display names and providers, a pending
model change (`old → new (next call)`), latest-call input tokens, price-tier
and window percentages, a hover explanation, responsive truncation and the
project's branch before a session exists are implemented. Session and
multi-line composer captures at 80×24, 120×36 and 160×48 dark are refreshed;
pending, price-tier and hover states are covered by buffer tests, so their
visual review is still outstanding. TUI captures are in
`/tmp/nexus10-composer-review/truecolor/` and `interface/review/`.

Target (reference image 3), at 120 cells:

```
  ┌ panel #1e1e1e, no border ────────────────────────────────────────────────┐
  │                                                                          │
  │  Type a message…                                                         │
  │                                                                          │
  │  Orchestrate · GPT-5.5 codex · high                 11.1k · 4.1% · 1.1%  │
  └──────────────────────────────────────────────────────────────────────────┘
  ~/repos/nexus10  nexus10  worktree
```

(The box lines above only mark the panel extent; nothing is drawn there.)

**Panel.** A filled `surface.panel` block with no border and no divider. One
blank row above the input, one between the input and the control row, none
below. Two cells of left and right padding inside the panel; one cell of
canvas between the panel and the column edges. The input grows to 8 rows,
then scrolls (keep the `n/m` row counter, top right, muted).

**Input.** Placeholder `Type a message…` in muted (was `Ask the agent…`).
Disconnected keeps the reason as the placeholder (`Disconnected · read-only`).
No session selected: `Message to start a new session` (unchanged).

**Control row**, left to right, single row:

1. Agent name, capitalised, in the agent's colour (A4). Click opens the agent
   picker.
2. ` · ` muted separator.
3. Model display name and provider (`GPT-5.5 codex`) in primary. Use the
   catalog display name; fall back to the model id. A pending change shows
   `GPT-5.5 → Claude Opus (next call)`.
4. ` · ` then effort in muted (`high`; `default` when the provider decides).
5. While busy: ` · ` then `esc stop` in muted. While interrupted:
   ` · ` then `continue` (click or `/continue`). These replace the old
   `◐ running [ stop ]` and `[ continue ]` chips. The live state is shown by
   the shimmering `Working` row in the timeline (A3), not in the composer.

Right block, muted, right-aligned: context tokens used (`11.1k`), then
context use as a percent of each price-tier boundary the catalog reports, then
as a percent of the model's window (`4.1%`; the third number is the next tier,
not a provider quota, matching old Nexus `_context_label`). Hover explains each
number in a toast-sized tooltip. The title truncates first: when narrow, drop
the effort, then the provider, then extra percentages; agent and at least one
context percent stay.

**Focus.** No border, so the cursor is the focus cue in truecolor and 256
colours. In ANSI-16 and monochrome the panel has no fill, so draw the focus
bar `▌` in `interactive.focus` in the first padding column of the input rows.

**Location line** below the panel, on the canvas, normal weight:

- The session directory with `$HOME` shortened to `~` (`~/repos/nexus10`),
  muted. Truncate in the middle.
- Two spaces, then the branch in secondary, when the directory is in a Git
  repository.
- Two spaces, then `worktree` in muted, only when the directory is a linked
  worktree (not the main checkout). Say nothing for the main checkout.
- No project name, no bold, no accent underline, no tab look. Clicking the
  path opens "Change session directory"; clicking the branch opens the
  worktree list (disabled with its reason until the worktree UI exists).
- Source: `Session.directory`; branch and worktree from the session's worktree
  listing (entry whose `path` matches `session_directory`; worktree when it is
  not `main`), refreshed on `session.directory_changed`. A new session with no
  session yet uses `GET /api/project/{id}/worktrees` and shows the project's
  path and branch the same way.

Files: `nexus-tui/src/render.rs` (`draw_composer`, `composer_inner`,
`heights`, `cursor_position`), `layout.rs` (composer height + location row),
`api.rs`/`sync.rs`/`state.rs` (project worktree lookup and cache), `app.rs`
(cached worktrees per session and project), `text.rs` (capitalisation, counts).
Tests: buffer snapshots for idle, busy, interrupted, pending model, narrow
(60 cells) and disconnected; hit targets for agent, model, effort, path,
branch and context numbers; hover tooltip.

Core: context tokens and the window come from the latest call's usage and the
catalog (already in the snapshot/usage events). Catalog `ModelInfo` now carries
`tiers` (models.dev `cost.tiers` context boundaries) for the price-tier
percentages; `GET /api/project/{id}/worktrees` supplies the branch before a
session exists. Both are implemented.

### A3. Context timeline

Progress: TUI user blocks have raised fill and vertical padding, with a muted
rail only where surfaces collapse. Hovering or focusing a user block exposes
its copy action. TUI activity groups now fold consecutive tools and reasoning,
keep waiting items open, expose failures and announce live clipping; expanded
members use verb/tree rows and retain their existing details. The TUI Working
heading shimmers in the agent colour unless reduced motion is enabled. The TUI
turn header is merged into the user block, with local time, folding, copy and a
separate large-message expansion control. Reasoning timing is measured from
its stream and shown as `Thought for 3.5s` in the group header and member row.
NSH parity and visual review remain.

Target (reference image 4):

```
▾ ┌ raised #242424 ──────────────────────────────────────────── Copy ┐
  │                                                                 │
  │  hi there                                         #1 · 10:40 PM │
  │  run a bash command to tell the time                            │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘

    ▾ Working
    ╰─ Shell date

    It is 10:40 PM.

    ───────────────────────────────────────────────────────────────
    codex/gpt-5.5 · high · 4 s · 2 calls · 1 tool · 12k in … · ✓ done
```

**User message.** A full-width block on `surface.raised`, one blank row of
padding above and below, text indented 4 cells from the column edge (2 of
canvas gutter + 2 of padding). No accent rail any more: the fill is the cue,
and in ANSI-16/monochrome (no fill) a `▌` rail in `text.muted` replaces it.
The right edge of the first text row carries `#N · h:mm AM` in muted. `Copy`
shows at the top-right corner on hover or keyboard focus only (old
`copy_button.rs`); `c` copies the focused block. The turn's fold marker
(`▾`/`▸`) sits in the left gutter column, outside the block, muted.
A folded turn shows only the user block's first line (still filled) with
`· N tools · 42 s · ✓ done` in muted at the right, replacing the separate
turn-header row.

**Agent activity: verb groups**, copied from old Nexus `groups.rs`. A run of
consecutive tool calls (and reasoning between them) between two pieces of
agent text is one group:

- Running: header `▾ Working` in primary, the word shimmering (A4 colour
  highlight sweep; static under reduced motion). Under it, a grey tree of the
  newest 5 members, `├─ Shell date` / `╰─ Read src/app.rs`, with
  `… N earlier` first when clipped. Members are verb + key argument, muted,
  no icons, no status colour. A running `bash` shows its live seconds.
- Finished: the tree folds away and the header settles to
  `▸ Ran 1 command, Read 3 files · 1.8s` in muted (verbs summed per kind:
  Ran N commands, Read N files, Edited N files, Wrote N files, Ran N cells,
  Searched N times, Asked N questions, Started N sessions, Thought for 3.5s).
- A failed member ends in muted ` · failed`; a denied one ` · denied`. The
  group header adds ` · 1 failed` in `status.error` with its `✕` glyph, so a
  failure is visible while folded (the one place this deviates from old
  Nexus, which never coloured failures: the spec requires glyph + word for
  errors).
- Expanding the group lists every member as a one-line row (`▸ Shell date
  0.4s`); expanding a member opens today's tool detail unchanged (diff,
  output box, raw, `code` nesting) behind a `│` rail.
- Waiting for approval or an answer is never folded away: the member row
  shows `◆ waiting` in the accent, and the group stays open.
- Child-session launches are not folded into a group: they stay child cards,
  with the agent name in its colour (A4).

**Agent text** is flush at the 4-cell indent, primary, Markdown as today.
**Reasoning** is a group member (`Thought for 12.0s`), expandable to the
summary. **Notices, insertions, diagnostics** keep their rails and labels.
**Turn footer** keeps today's content and hairline.

What does not change: the starting-context section at the top, folding being
view state only, every model-visible item present in order. A folded group
hides rows, never items; its header always counts them.

Files: `nexus-tui/src/timeline.rs` (new group builder between message parts
and tool rows; `BlockKind::Group`; user block layout), `render.rs`
(`draw_timeline`: gutter fold column, hover copy target), a small shimmer
helper in `theme.rs` or a new `anim.rs`. Spec: rewrite INTERFACE_PLAN.md
"Tool row", "Turn header" and the user/tool rows of "Timeline roles" to match.
Tests: group building (consecutive tools, interleaved text, reasoning,
waiting, failure, child launch boundary), fold/unfold keeps block keys and
scroll anchor, live → finished transition, buffer snapshot of the reference
scene.

### A4. Agent colours

Progress: shared dark/light palettes, pinned slots, 256/ANSI fallbacks, NSH
CSS generation and contrast checks are in place. Both clients assign and render
agent names in the composer, picker, child card and session info, with matching
hash tests. Working-header shimmer is implemented in the TUI; NSH shimmer and
visual review are still outstanding.

Every agent has an identity colour, assigned automatically from a fixed
palette chosen to read well on all three dark surfaces.

Palette (new `[dark.agent]` / `[light.agent]` in `theme.toml`, named slots so
themes can remap them):

| Slot | Dark | On `#242424` |
| --- | --- | --- |
| `amber` | `#d18a38` | 5.5:1 |
| `blue` | `#5c9cf5` | 5.6:1 |
| `violet` | `#a78bfa` | 5.7:1 |
| `green` | `#86b97a` | 6.8:1 |
| `teal` | `#55b9a5` | 6.6:1 |
| `rose` | `#dc8295` | 5.7:1 |
| `sky` | `#69b7d5` | 6.9:1 |
| `gold` | `#c9a94a` | 6.8:1 |

Light values are darkened versions of the same hues; `check_theme.py` gets a
rule that every agent slot meets 4.5:1 on canvas, panel and raised in both
themes, and in the 256 fallback.

Assignment (same algorithm in both clients, documented in the theme file):

1. Built-in agents are pinned so they never collide: `orchestrate` amber,
   `build` blue, `quick` green, `scout` teal, `plan` violet (in
   `[agent_pins]`).
2. Any other agent gets `slots[fnv1a_32(casefold(name)) % 8]`, skipping the
   pinned slots first when another slot is free (so custom agents prefer
   rose, sky, gold). Stable across restarts and clients; no state.
3. Later (Core, optional): an agent file may set `color: <slot>` in its
   frontmatter, reported in the agent listing; clients then use it. Ledger row
   when it lands.

Where the colour appears: the agent name in the composer, the agent picker,
child cards' agent name, the session info agent row, and the shimmer highlight
of a running `Working` header. Never on titles, tool names or whole rows; the
amber accent stays the UI accent, so `orchestrate` (amber) reads as the same
warmth on purpose. Monochrome: names are bold, no colour.

Files: `interface/theme.toml`, `check_theme.py`, `nexus-tui/src/theme.rs`
(`agent_color(name) -> Style`), NSH theme generation. Tests: pins, hash
stability against a fixed table, contrast check.

### A5. Left sidebar

Progress: TUI session rows use two lines and a spacer, with both lines
clickable, project and branch detail, error and unread attention states, title
fading and no selection fill or age/child counts. Project-level worktree
listings now load branches for every row (main checkout and linked worktrees),
and worktree grouping was exercised against real Git. Running titles shimmer
and the scope/archive controls sit in the sidebar header. Visual review of the
attention, error and fade states remains.

Target (reference image 5):

```
  Finishing INTERFACE_PLA░░      ← running: shimmering, faded at the cut
  nexus ⌥ nexus10

  Greeting                       ← selected: primary bold
  nexus ⌥ nexus10
```

- Each session is two rows plus one blank row: title, then
  `project ⌥ branch` in muted (`⌥` U+2325, ASCII `~`). Use the worktree's
  branch, or the main checkout's branch; omit `⌥ branch` outside Git.
- Titles: selected (open) session in primary bold; others in secondary;
  unread in primary (not bold). Archived in muted.
- Running sessions: the title shimmers (same helper as A3). No spinner glyph.
- Overflowing titles fade over their last 3 cells toward the canvas in
  truecolor (the reference look); 256/ANSI/monochrome use `…`.
- Attention: the 2-cell slot before the title holds `◆` (waiting, accent) or
  `✕` (error) or `●` (unread), and is blank otherwise, so titles stay aligned.
- Focus: the focus bar `▌` replaces the slot on the focused row. Selection is
  bold text, not a filled band; hover is the `interactive.hover` fill.
- No age column and no child count in the row; age and children move to the
  hover tooltip / detail line (NSH) and to session info. Children are still
  reached from their parent, not listed.
- Grouping by worktree stays; group headings are muted, one row, no rule.
- The scope toggle and archived filter move from the always-visible first row
  into the sidebar's header row as compact muted text (`this project ·
  archived`), and into the palette.
- Tabs (Sessions, Files, Projects) stay as icons in the header row.

Files: `nexus-tui/src/render.rs` (`draw_sessions`, `row_style`,
`draw_left`), `app.rs` (`side_entries` row height 3, hit-testing by entry).
Tests: buffer snapshots for selected, running, waiting, error, archived,
long title (fade and `…`), no-Git directory; click hit-targets on both rows.

### A6. Remove the status footer

Progress: the TUI status row is removed; the empty view has key hints and F1
shows the Core version. Visual review and cross-client verification remain.

Delete the bottom row (reference image 6: `✓ live · Core 0.10.0.dev0 …
ctrl+p commands · ctrl+b sidebar · f6 focus · f1 keys`). Its content goes:

- Connection: live shows nothing. Anything else is already a header status
  (`◐ connecting`, `✕ offline`) and a banner; keep those.
- Core version: Settings › About and the F1 keys overlay header.
- Key hints: the palette lists each command's key; F1 shows all keys; dialogs
  and overlays keep their own footer hints; the composer shows `esc stop`
  while busy (A2). Add a one-time empty-state line in a new session view:
  `ctrl+p commands · f1 keys`.

Files: `render.rs` (`draw_footer` removed), `layout.rs` (no footer row),
manifest key-hint text, INTERFACE_PLAN.md "Key hints" rule (no status line;
hints live in the palette, F1, dialogs and the composer). Tests: layout gives
the row back to the timeline; no test depends on footer text.

### A7. Showing files

Goal: the Files tab and a file view in the centre column, then editing.
Ordered so that something useful lands before the editor.

**Core (12a "Files and git"), first:**

1. `GET /api/project/{id}/files?path=&session=` lists one directory level:
   name, kind, size, mtime, Git status letter, ignored flag. Gitignore-aware
   (ignored entries returned with `ignored: true` so the client can dim or
   hide them). Confined to the project or the session's worktree; symlinks out
   of the root are listed but not followed.
2. `GET /api/project/{id}/file?path=&session=` returns text content (size cap,
   binary detection), a content revision (hash) and the language guess.
3. `GET /api/session/{id}/git` returns branch, ahead/behind, changed files
   with `+N −M`, staged vs unstaged; `GET …/git/diff?path=` one file's diff.
4. Events: `files.changed` (coarse, per directory, debounced watcher) so open
   trees and views refresh; until then clients refresh on focus and after
   tool results that touched a path.
5. Later, with read-before-edit: `PUT …/file` with `expected_revision`.

**TUI:**

1. Files tab: tree on the canvas, folders `▸`/`▾`, loaded lazily per level,
   Git status letters (`M` warning, `A`/`?` success, `D` error) right-aligned,
   ignored dimmed (toggle to hide). Root selector when the project has
   worktrees: main checkout or the open session's worktree.
2. Enter opens a **read-only file view** in the centre column: path header
   with `~`, line numbers, syntax highlighting (`syntect` or tree-sitter,
   decide on binary size), search (`/`), `Esc` back to the session. Large
   files show a clip notice and load more on scroll.
3. Actions on a file row: open, open in `$EDITOR` (suspend the TUI), copy
   path, insert `@path` into the composer.
4. Paths in the timeline (`src/x.py:42` in agent text, tool key arguments)
   open the file view at that line.
5. `@` in the composer completes from the listing.
6. Editor (after Core 5): `ratatui-code-editor`, dirty marker, save with
   revision, stale-save refusal with reload that keeps the unsaved text.
7. Right sidebar Git status section from Core 3; a changed file opens its
   diff in the centre column.

Core 1–2 and a TUI Files tree now exist. The tree loads one level per folder,
keeps expanded paths, shows Git letters (`M`, `A`/`?`, `D`), dims ignored
entries with a hide toggle, and offers a project/session-worktree root row when
the project has more than one checkout. Enter opens a read-only file view with
a `~` path and line numbers. The selected file can be copied as an absolute
path, inserted as `@path`, or opened in `$VISUAL`/`$EDITOR` with the terminal
suspended. Search, syntax highlighting, timeline path links, `@` completion,
the built-in editor, diff UI and NSH parity remain open.

### A8. NSH follows

Partial: shared theme/sidebar backgrounds and agent-name colours are in NSH.
The composer, timeline, session-row, footer and Files parity review remains.

Apply A1–A7 to `nexus-nsh/` in the same order once each TUI item is reviewed:
regenerate the theme, rebuild the composer (fill, no border, location line),
user block and verb groups, agent colours, two-line session rows with a CSS
mask fade, no status bar, Files tree and file view (NSH editor element later).
Same names, same order, same placement. Capture 390×844, 1024×768, 1440×900
dark and compare with the TUI captures side by side.

### A9. Done when

- The reference images and the running TUI match in layout, colour and
  weight at 120×36, and old Nexus and the TUI look like the same product.
- No footer row; connection problems still visible; every key discoverable
  from the palette or F1.
- Every agent name renders in its colour in both clients; built-ins distinct.
- Files tab lists and opens files against a real Core.
- `cargo test`, `cargo clippy --all-targets -D warnings`, `cargo fmt --check`,
  `python3 interface/check_theme.py`, `check_manifest.py`, NSH tests pass.
- INTERFACE_PLAN.md tokens, composer, timeline roles, tool row, turn header,
  session row and key-hint sections describe the new look.

---

## Part B: the rest of Step 12

What is done is recorded in PLAN.md Step 12 "Progress". This is what is left,
in build order. Each phase names its Core dependency; a phase blocked on Core
ships its UI disabled with the reason, never as a control that appears to
work.

### B1. Finish the design language (12.2 items 4–5)

1. Fixture-driven component gallery in both renderers from one sample session
   (`interface/fixtures/`): header, session rows, starting context, user
   block, verb group live and finished, tool details, child card, composer
   states, waiting card, settings row, dialog, banner, toast.
2. Captures: TUI 80×24, 120×36, 160×48; NSH 390×844, 1024×768, 1440×900; dark
   and light; plus short height, large text, Unicode/CJK titles, long labels.
3. A visible issue list (`interface/review.md`) with each issue, capture and
   fix commit; iterate until closed. Part A's review feeds into this.

### B2. Session list completeness (12.4.1)

- Search: Core FTS5 index over titles and user text (12a "Archive, search"),
  then a search field at the top of Sessions and a palette command. Filtered-
  empty state distinct from empty.
- Rename inline, archive/unarchive, delete with subtree count (wired; verify
  selection and position are preserved across updates and resyncs).
- Worktree grouping verified with real worktrees.

### B3. Starting context and new-session view (12.4.2)

- Core: session drafts (12a) returning labelled sections with token counts.
- TUI: new-session view shows agent, model, effort, directory, system prompt,
  AGENTS.md (with path), tools with switches, skills/MCP (disabled until
  11a), total tokens; edits apply before the first message, which freezes it.

### B4. Tool detail completeness (12.4.3)

- Full-output retrieval from durable artifacts (12a "Attachments") behind
  "N lines hidden · show all", never added to context.
- Kernel inspection for `code`, background job evidence (`bash_output`),
  clip labels distinct from UI preview caps.
- `write` overwrite diff once Core stores previous content (matrix gap).

### B5. Composer completeness (12.4.4)

- Slash completion from the manifest (verify all slash names).
- Steer and queue (12a): Enter steers while busy, Alt+Enter queues; queued
  list above the composer with edit/remove; steering shown in the timeline
  where inserted.
- Paste: large pastes become a collapsed chip; bracketed paste verified.
- Attachments (12a): chips with size, `@path` (A7), images where the terminal
  supports kitty/iTerm2/sixel, otherwise a chip.
- `/btw` (12a one-turn sessions): side overlay, never in the parent timeline;
  btw list in session info.
- Handoff (12a): `/compact`, automatic threshold, "continued from" item and
  chain in the sidebar.
- Dictation: decide engine (open question); push-to-talk key in the TUI.

### B6. Navigation, settings and integrations (12.4.5)

- Child breadcrumbs/back to the card position (verify with deep trees).
- Files/editor and Git status/diff (A7).
- Worktree UI: list, create, delete from the palette and the location line.
- Settings editing beyond theme: Providers (sign-in over the API, 12a),
  Models (defaults, catalog refresh: matrix gap), Agents (list, mode over
  HTTP: matrix gap), Tools defaults, Context threshold, Keybindings,
  Notifications, About (Core version moves here, A6).
- Skills/MCP controls after Step 11a.
- Notifications: drawer exists; add terminal bell / OSC 9/777 and sound.
- Inspector link exists; wire view inline (request bodies, raw events).

### B7. Scroll and focus verification (12.4.6)

- Follow only at the bottom; jump-to-latest with a new-content mark; anchors
  preserved through streaming, group folding (A3 changes row counts) and
  history loading; per-session positions; parent restored at the child card.
- PTY tests for each, at 80×24 and 160×48.

### B8. NSH parity (12.5 remaining)

- Items 1–3 largely done; finish the TUI journeys above in NSH in the same
  order.
- Item 4: richer presentation inside the same structure (split diffs, inline
  images, hover detail, resizable panels).
- Item 5: document hosting and connection (same-origin via the host proxy,
  token acquisition, HTTPS, SSE).
- Item 6: verify the packaged macOS app against Core; list other desktop
  targets as verified or deferred; iOS after desktop is stable, with remote
  Core access decided first.

### B9. Cross-client checks and release (12.6)

1. Scenario fixtures run in both clients: first run/sign-in, starting
   context, long streaming turn, nested tools, child questions,
   denial/cancel, directory change, queued input, archive/export, stale save,
   handoff, disconnect/restart/resync, expired history, conflicts.
2. Tests: reducer/transport, Ratatui buffer, PTY interaction, NSH headless;
   plus manual IME, clipboard, selection, accessibility.
3. Accessibility: keyboard-only, focus restoration, contrast, reduced motion
   (shimmer off), screen-reader labels (NSH), large text, narrow layouts,
   resize during streaming/dialog/pending answer.
4. Performance budgets on named hardware: startup, first frame, input latency
   while streaming, scroll, idle CPU, memory, with a large timeline (verb
   groups reduce rows; measure with them).
5. Side-by-side review of TUI, web and desktop in light and dark, including
   errors and empty states; publish the capability matrix, run instructions,
   deferrals and evidence; update PLAN.md, INTERFACE_PLAN.md and docs.

### Core items this plan adds to Step 12a

- Catalog price tiers in `ModelInfo` (A2 right block; done: `tiers`).
- Project-level worktree listing for a new session (A2 location line; done:
  `GET /api/project/{id}/worktrees`).
- Files listing, file read, git status/diff, `files.changed` (A7; already
  listed as "Files and git", now with endpoint shapes).
- Optional agent `color` slot in frontmatter and the agent listing (A4).

## Open questions

- Syntax highlighting crate for the file view (binary size vs quality).
- Whether the sidebar shows the project name in the second row when the
  scope is "this project" (the reference shows it; it repeats on every row).
