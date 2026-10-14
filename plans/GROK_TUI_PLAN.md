# Grok-style native TUI plan

Goal: the native Ratatui client (`rust/tui`, `rust/widgets`, Python projection in
`nexus/ui/ratatui/`) adopts the look and several layouts of Grok Build. The Grok
source is vendored read-only at `reference/grok-build/crates/codegen/` (pager crate:
`xai-grok-pager/src`, theme: `xai-grok-pager-render/src/theme/groknight.rs`,
`grokday.rs`, backdrop: `xai-grok-pager-render/src/render/color.rs::dim_area`).
Reference screenshots (user-supplied) are described inline below.

Non-goals: the web client (deprecated; do not port) and the GPUI desktop client.
The **composer card** and the **context/activity bar** under it (the meter rule and
the `Build · model · effort … 140 · 0.1%` row) keep their layout and behavior; only
their colors follow the new palette.

Every workstream: keep the host contract (rule 4 in AGENTS.md), add/adjust tests next
to peers, run `cargo test` in `rust/tui` and `rust/widgets` plus the touched
`tests/test_ratatui_*.py`, and update `docs/ratatui-parity.md` (and
`docs/decisions.md` for design choices). Do not touch unrelated dirty files.

## WS-A  Palette, backdrop dim, chrome (no left sidebar)

Files: `rust/tui/src/render.rs` (Palette, regions, draw), `rust/widgets/src/theme.rs`,
`rust/tui/src/render/chrome.rs`, `rust/tui/src/render/dialogs.rs`, input/mouse
handling for removed regions in `main.rs`/`input.rs`, Python prefs/settings rows that
mention the sessions sidebar.

1. **Dark palette = Grok Night** (both `Palette::new(false)` and `Theme::dark()`):
   background `#141414`; text `#e1e1e1`; muted `#787878`; quiet `#585858`;
   panel/dialog `#141414`; element (user-message band, selected row) `#242424`;
   element_hi (hover) `#2c2c2c`; composer `#1c1c1c` (darker than today's `#1e1e1e`,
   still lifted off the page); border `#323237`; border_strong `#505058`;
   blue `#7aa2f7`; purple `#bb9af7`; cyan `#7dcfff`; success `#9ece6a`;
   warning `#e0af68`; error `#f7768e`; diff_del `#420e14`; diff_add `#063806`.
   Keep the Nexus accent. **Light palette = Grok Day** (read `grokday.rs`, map the
   same roles). Update colour assertions in tests; don't change semantics.
2. **Modal backdrop**: port `dim_area` (blend every cell's fg and bg 50 % toward the
   palette background, clear modifiers; on non-RGB colours set `DIM`). Call it over the
   whole frame immediately before drawing any modal: Settings, pickers/menus,
   inspectors/context dialogs, prompts (permission/question), confirm dialogs, logs
   panel when floating, toasts excluded, the slash-completion dropdown excluded.
   One helper (`render/appearance.rs` or `dialogs.rs`), one call site per modal kind.
3. **Remove the left Sessions sidebar completely** (drawer and docked column, its
   filter row, project chips, hit-tests, shortcuts, preference/settings row, tests
   asserting `SESSIONS N`). The Sessions dashboard (`/sessions`) replaces it. The
   right details sidebar stays.
4. **In-session top bar** (Grok, one row, `quiet` separators `│`):
   left `Title (truncated…) │ branch ~/path` (branch quiet, path text);
   right `used / window` (e.g. `72K / 256K`, from context_used/context_window) `│`
   `‹ i/n ›` (cycle open sessions/tabs; arrows clickable, hidden when n ≤ 1) `│`
   `[Dashboard]` (clickable; opens `/sessions`). Replaces the current tab/breadcrumb
   row; keep the update notice if present (right side, before the counter).
   Add a shortcut to open the dashboard if none exists (reuse existing binding if any).

## WS-B  Sessions dashboard (Grok screenshots 5, 6, 9)

Files: `rust/tui/src/render/sessions.rs`, dashboard parts of `render.rs`/`main.rs`,
`nexus/ui/ratatui/prototype.py`/`workflows.py` only if data is missing.
Reference: `reference/.../xai-grok-pager/src/views/dashboard/{render,row,row_title,
row_activity,preview,peek,peek_tail,chrome,layout}.rs`.

Layout top→bottom, 2-col left/right gutters:
- Row 0: `branch ~/path [Choose Ctrl+L]` (branch quiet, path text, bracket quiet) …
  right: status chips `⁙ 1 working │ ◇ 3 idle` (glyph in status colour, counts text).
- blank, then `+ New Agent` (text; green when focused/hovered) … right:
  `Open Previous /resume │ Worktree Ctrl+W` (label muted, key quiet; focused one green).
- blank, then groups: `▾ Working 1 ─────────…` (caret+name bold muted, count quiet,
  rule in border colour to the right edge). Groups: Needs input, Working, Idle, Done.
- Each session = 2 rows + 1 blank: `◇ Title · branch project` … right `age`
  (`just now`, `3m`); second row indented under the title: one-line summary
  (last agent line or activity, muted). Working rows use an animated braille glyph
  (`⁙`-family) in purple; idle `◇` quiet; needs-input in warning.
- Selected row: background `element` across the full width, a `│` left rail in the
  border_strong colour, title bold. Hover = same without the rail.
- **No inline expansion any more.** Instead the selected/hovered session's preview is
  a bordered box (border colour, square corners) that sits directly above the composer
  and shares its width: header `Response` (quiet) … right age; then `❯ last user
  message` (bold text); `…` if clipped; then the tail of the last agent message
  rendered as Markdown (bounded rows, at most ~half the screen); then
  `Worked for 13s` (quiet). Live-updating from the existing
  `SessionDashboardPreview` data. The composer under it is unchanged and Enter with
  text still sends to the selected session (the "quick message").
- Empty list: muted "No sessions yet" line under the header.
Keep `/sessions` and the title **Sessions**; keep keyboard nav and mouse hit tests.

## WS-C  Settings modal (Grok screenshot 8)

Files: `rust/tui/src/settings_page/{draw,input,model}.rs`, `render/dialogs.rs` panel
geometry for settings, `nexus/ui_support/settings_page.py`,
`nexus/ui/ratatui/settings_pages/*.py` (+ host routing if the overview needs it).
Reference: `reference/.../xai-grok-pager/src/views/settings_modal/`.

- A centered floating modal over the dimmed backdrop (WS-A helper): width
  `min(120, area-8)`, height `area-4`; border colour `border`; title ` Settings `
  bold in the top-left border; `[×]` (quiet, clickable, closes) in the top-right.
- Row 1 inside: search field `/ to search` (quiet placeholder) with a rule under it.
  `/` focuses it; typing filters rows by label/description across all sections;
  Esc clears then closes.
- **Root view is one scrolling list** (no left nav column). Sections in Nexus order:
  Appearance, Layout, Keyboard (their rows inline), then Configure sections whose
  pages are complex — Providers, Models, Voice & speech, Agents, Tools, MCP servers,
  Skills (and any other existing page) — each a single drill row `▸ Providers  … ›`
  showing a short summary value (e.g. "3 signed in") when cheap to compute.
  Section header: `Appearance ───` (quiet bold label, border rule).
- Row: `▸ Label` left; value right-aligned with ` ›` suffix when Enter opens a
  choice or subpage; toggles show `on` (text) / `off` (quiet). Selected row: `element`
  background full width, bold label. Description of the selected row may show as a
  quiet line under it (Nexus shows descriptions; keep them, selected row only).
- Drilled page: same modal, title ` Settings › Providers `, existing block renderer
  restyled to the same row grammar; Esc/← returns to the root list with the selection
  kept.
- Footer (centered, wraps): `↑/↓/j/k nav | g/G top/btm | Space toggle | Enter edit |
  → expand | / search | d reset | Esc close` keys bold text, labels quiet, `|` quiet.
  Above it an italic quiet tip line, e.g. `Tip · Saved in ~/.config/nexus/tui.json ·
  applies at once`.
- Fix: the legacy toggles (`[x] Animate activity Alt+A`, `[ ] Monochrome`) currently
  render below the page; they belong in Appearance rows only.

## WS-D  Above the composer (Grok screenshot 7)

Files: `rust/tui/src/render.rs` queue box + above-card rows, `bridge.rs` Snapshot
fields, `nexus/ui/ratatui/prototype.py` (projection) if timing/token fields are missing.

- While a turn runs, one status line above the composer card:
  `⠋ Waiting for response… 2.0s` left — spinner (agent colour), current activity label
  (text), elapsed in that activity (quiet) — and right `4m40s ↓121k [stop]` — turn
  elapsed, output tokens so far (quiet), `[stop]` (quiet, hover text; click cancels like
  Esc/Ctrl+C does today). Activity label from existing projection (e.g. waiting,
  thinking, running `tool`, writing). Hidden when idle. If the transcript already shows
  an equivalent live "working" row, remove that duplicate.
- Queue becomes Grok's unboxed section (replaces the bordered queue box):
  `▾ Queued 1` (caret+label bold muted, count quiet), then `#1 text` rows (`#n`
  quiet, text truncated to one line), max 4 + `+N more`. Keep existing queue controls
  (edit/steer/remove) reachable: show them on the hovered/selected row at the right.
  Click on the caret collapses/expands.
- One blank row between these and the composer card. Composer itself unchanged.

## Verification

Build (`cd rust/tui && cargo build`), then screenshots via
`PYTHONPATH=. .venv/bin/python tests/playwright_ratatui_live.py "/mock hello" "/sessions"`
(and `"/settings"`, `"/mock question"`) into `artifacts/ratatui-live/`. Compare with the
Grok screenshots. Status (2026-10-14): all four workstreams and the settings subpages implemented and reviewed in dark and light captures; inline General rows done; Providers drill summary not done.
