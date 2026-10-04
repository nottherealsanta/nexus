# Desktop (GPUI) design overhaul plan

Status: in progress · Phase 1 started · 2026-10-04 · scope `rust/desktop/`, the private presentation
bridge in `nexus/ui/ratatui/prototype.py`, `nexus/ui_support/shortcuts.py`, and
`docs/desktop.md`.

The GPUI client works, but it looks generic, has several bugs and hitches during
streaming, resizing and typing. This plan has four parts:

1. **Performance:** make it fast first. Fix the bridge and render pipeline so
   that the work per token, keystroke and frame stays bounded.
2. **Redesign:** a visual language drawn from the reference screenshots
   (translucent window, centred reading column, a floating composer, folded
   activity trees, a review pane).
3. **Parity:** keyboard shortcuts, the Ctrl+X leader, slash commands and every
   workflow behave exactly like the Ratatui client.
4. **Polish:** motion, states, edge cases and a real native-screenshot review
   before anything is called done.

The Nexus principle still governs every screen: *everything the agent sees is
visible to the user, labelled and readable*. Folding is allowed. Hiding is not.
Every folded row opens to its complete parameters and results, and any clipping is
announced.

---

## 0. Success criteria (measurable)

| Area | Target | How it's measured |
| --- | --- | --- |
| Streaming | 60 fps while a reply streams into a 2,000-block session; no full transcript re-layout per token | `NEXUS_DESKTOP_TRACE=1` frame log (new, §3.6), p95 frame < 12 ms on an M-series release build |
| Typing | keystroke → painted glyph < 16 ms p95; no wait on the Python round-trip | trace log `input→paint` |
| Scroll | 120 Hz trackpad scroll with no dropped frames on a marathon replay fixture | trace log + native capture |
| Resize | live window drag with no transcript jump and no full re-measure per frame | manual + trace |
| Bridge | ≤ 8 KiB per streaming token update (today the whole growing block text, sessions, logs, history and images are re-sent) | bytes/update counter in trace |
| Parity | every row of `ui_support/shortcuts.py` (main + leader) and every `commands.SPECS` entry has a desktop route; a test enforces it | `tests/test_desktop_keymap_parity.py` (new) |
| Visual | every state in §7.3 captured in dark and light, reviewed against this spec | `artifacts/desktop/review-ledger.md` |

---

## 1. Diagnosis: what's wrong today

Found by reading the code. Items marked *(confirm)* need a repro before fixing.

### 1.1 Lag causes

1. **Debug builds get launched.** `nexus/ui/desktop/run.py::binary_path` picks the
   most recently modified of `target/release` and `target/debug`, so a fresh
   `cargo build` (debug, opt-level 0) beats an older release build. GPUI in
   debug is several times slower at layout and text shaping. `[profile.dev]` sets
   only `debug = 0`, with no dependency optimisation.
2. **Every streamed token re-sends a full snapshot.** `prototype.run.update()` runs
   `project()` on every event and writes the whole snapshot: up to 1,000 session
   rows, logs, history, the details panel, settings nav, items, and base64
   **inline images (up to 4 MiB)**. Schema-2 block patching skips unchanged
   *leading* blocks, but the streaming markdown block is re-sent whole each time,
   so a turn costs O(n²) bytes. The no-change check also `json.dumps` the entire
   snapshot a second time.
3. **No coalescing.** Python doesn't throttle updates to a frame rate, and Rust
   applies every queued snapshot, re-rendering each one in turn.
4. **Each keystroke round-trips to Python.** `InputEvent::Changed` sends both
   `draft_changed` and `complete` on every character. Python re-projects and
   re-sends a full snapshot, so the UI thread parses JSON and re-renders while
   the user types. Ratatui debounces completion by 120 ms.
5. **Markdown is re-parsed on every frame.** `transcript.rs` calls
   `markdown::render(&block.text, …)` inside the list item closure, so every
   visible block is parsed again on each paint and each scroll tick.
6. **Each paragraph is its own editor entity.** `selectable()` creates a read-only
   `Input` per paragraph in a 512-entry `HashMap`. Eviction removes an
   *arbitrary* key, which can evict visible paragraphs (flicker, lost selection),
   and the cache is cleared on every session switch.
7. **One entity holds everything.** `Desktop` owns the snapshot and renders the
   title bar, sidebar, transcript, composer, details pane and overlays. Any
   `cx.notify()`, including a 3 s session poll or a log line, rebuilds the whole
   tree.
8. **Resizing resets the transcript.** `render()` calls `transcript.reset()`
   whenever the viewport width changes. During a drag that happens every frame,
   so every item is re-measured and the scroll position jumps.
9. **Render does work it shouldn't.** `render()` writes `details_visible` to
   stdout and pushes theme colours into four input entities on every frame.
   `render_block` clones each `Content` (`.cloned()`) per item per frame.
10. **Images are decoded on the UI thread.** `apply()` base64-decodes up to
    8 × 4 MiB images synchronously.
11. **Picker filtering runs repeatedly.** Each filter keystroke runs
    `format!("{} {}").to_lowercase()` over all items about three times
    (navigate, count, row index, render). Matching is substring only, while
    Ratatui uses fuzzy ranking.
12. **Long diffs aren't virtualised.** Every `diff_rows` row becomes a flex row,
    even for multi-thousand-line diffs.

### 1.2 Functional and UX bugs

1. **Keymap conflicts with Ratatui** (full table in §5):
   - `Ctrl+P` opens Sessions; in Ratatui it opens Commands.
   - `Ctrl+F`, `Ctrl+S` and `Ctrl+R` are bound only to model-picker actions,
     so outside the picker they do nothing instead of Fork, Settings and
     Reconnect.
   - The leader's `Ctrl+X E` cycles effort; in Ratatui it toggles Logs, and
     `T` cycles effort.
   - `Shift+Tab` moves focus back instead of cycling the agent.
   - `Ctrl+End` moves the cursor to the end of the editor instead of jumping
     to the latest message.
   - Missing entirely: Ctrl+O/G/T/E/Q, most of the leader, Esc-twice cancel,
     PageUp/PageDown, Up/Down history on single-line drafts, Ctrl+J newline,
     Tab transcript navigation, and the details-pane `[` `]` keys.
2. **Follow mode breaks easily.** `follow` is set from `is_scrolled`, so a
   one-pixel nudge stops auto-follow, and there's no "Jump to latest"
   affordance to recover.
3. **The error toast is badly placed.** It is fixed at `left: 300px; bottom: 180px`,
   so it lands mid-screen when the sidebar is collapsed. It never times out,
   offers no action, and stacks a single string.
4. **The drawer overlay assumes the title bar height.** It is fixed at
   `top: 46px`.
5. **Sidebar search is limited.** Enter opens the first match only; there's no
   arrow-key navigation, highlighted match or clear button.
6. **Selection stops at paragraph edges.** Text can't be selected across
   paragraphs or messages. A permanent "Copy reply" button under every
   message adds visual noise.
7. **Diff rows are hard to read.** Deletions use the sidebar colour (no red
   tint), the two halves split 50/50 even at narrow widths, and gutters are
   unstyled.
8. **Tab is overloaded.** It does `CompleteFirst`, otherwise `focus_next`. This
   differs from Ratatui (empty draft → transcript navigation; otherwise force a
   completion request).
9. *(confirm)* The `restore`/`insert` one-shots can race a keystroke that was in
   flight, causing the cursor to jump or text to duplicate.
10. *(confirm)* A narrow-to-wide resize while a drawer is open leaves
    `compact_pane` set and the drawer reappears later.

---

## 2. Design direction

### 2.1 Thesis

> **A quiet, translucent reading room for agent work.** Chrome recedes into the
> wallpaper; the conversation is a calm centred column of prose; machine activity
> folds into one-line summaries that unfold into precise trees; the composer floats
> above the work; review lives beside it.

Taken from the references:

| Reference | Take | Leave |
| --- | --- | --- |
| #1 (dark, review split) | unified 38 px title bar with sidebar toggle, back/forward, `+`, agent glyph + title + muted `workspace @ branch`; plain assistant prose; soft right-aligned user pills; one-line activity summaries ("Ran 8 commands · edited 1 file · read 5 files"); floating composer with model glyph, effort, attach, circular send; footer with checkout + branch; right **review pane** with file headers, `+N −M`, unified diff gutters; left-edge **turn minimap** ticks | purple wallpaper tint is the user's desktop, not ours |
| #2 (light, live) | expanded activity **tree with connector lines**, verb + file chips with type glyphs; `Thinking… 6s` live timer; content fades out under the floating composer | video controls |
| #3 (light, done) | collapsed group after completion; ordered lists with accent numerals and bold lead-ins; generous line height | — |

### 2.2 Layout (wide window ≥ 1280 px)

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ ● ● ●  ▯  ← →  +   ✳ Diff sidebar updates   nexus @ main            ⎇ Review   ▯    │ 38px unified, draggable
├────────────┬───────────────────────────────────────────────┬─────────────────────────┤
│ Search  ⌘K │ ┆                              ╭──────────╮   │ Review · Session · Files│
│            │ ┆                              │ user pill│   │ MCP · Logs              │
│ NEXUS      │ ┆                              ╰──────────╯   │ 80 files  +9341 −3630   │
│ ● Session… │ ┆  Assistant prose, 15/24, max 720 px column   │ ▾ ARCHITECTURE.md  +7 −3│
│   Session… │ ━  › Ran 8 commands · edited 1 file · read 5   │  26 26  context line    │
│ OTHER WS   │ ┆  More prose…                                 │  29    − removed        │
│   …        │ ┆                                              │     29 + added          │
│            │ ┆   ░░░░░░░░░░░ fade ░░░░░░░░░░░░░             │                         │
│            │ ┆  ╭────────────────────────────────────────╮  │                         │
│ Archived   │ ┆  │ Ask anything…      ✳ Fable 5 · High  📎 ⬆│  │                         │
│            │ ┆  ╰────────────────────────────────────────╯  │                         │
│            │    ▢ Local checkout   ⎇ main          42% ctx    │                         │
└────────────┴───────────────────────────────────────────────┴─────────────────────────┘
  sidebar 248   minimap rail 12 · column ≤ 720 centred         inspector 360–720, resizable
```

- **Collapse rules.** These replace today's 820/1150 px thresholds, keep the
  same behaviour, and are re-tuned. The inspector collapses below 1180 px,
  the sidebar below 860 px. Collapsed panes open as drawers that slide over
  the content with a scrim. Escape or a click outside closes them, and focus
  returns to where it came from.
- **Empty state.** No starter cards dumped mid-screen. The composer sits
  centred vertically, a context header line ("CONTEXT · SOUL.md · 12 tools ·
  3 skills") is under it, and quiet suggestion chips sit under that. On
  first submit the composer animates down to its docked position (the
  "new-chat canvas transitions into the conversation" from ref #3).

### 2.3 Design tokens (`theme.rs` rewrite)

Tokens are semantic and resolved once per theme change, not per frame.

**Colour.** Two palettes, each with an opaque variant used when vibrancy is off.

| Token | Dark (vibrant) | Dark (opaque) | Light (vibrant) | Light (opaque) |
| --- | --- | --- | --- | --- |
| `window` | `#0E0D12` @ 72% over blur | `#0B0B0B` | `#F6F6F8` @ 70% | `#F7F8FA` |
| `canvas` (reading column) | transparent | `#0B0B0B` | transparent | `#FFFFFF` |
| `sidebar` | `#FFFFFF` @ 3% | `#101113` | `#000000` @ 3% | `#F1F2F5` |
| `elevated` (composer, sheets, popovers) | `#1A1920` @ 88% | `#17181B` | `#FFFFFF` @ 90% | `#FFFFFF` |
| `pill` (user message, chips) | `#FFFFFF` @ 7% | `#1E1F23` | `#000000` @ 5% | `#ECEEF1` |
| `hairline` | `#FFFFFF` @ 8% | `#2A2C31` | `#000000` @ 8% | `#E1E4E8` |
| `text` / `text-2` / `text-3` | `#ECEDEF` / 64% / 42% | same | `#1F2328` / 62% / 40% | same |
| `accent` (links, list numerals, focus ring) | `#8B8CFF` | same | `#5457E0` | same |
| `agent` (glyph, per-agent colour from projection) | from `agent_color` | | | |
| `success` / `warning` / `danger` | `#7DD3A0` / `#E8C06A` / `#F28B96` | | `#1F8A55` / `#9A6A00` / `#C23B4A` | |
| `diff-add-bg` / `diff-del-bg` | success @ 12% / danger @ 12% | | success @ 10% / danger @ 10% | |

The user previously asked for a `#0B0B0B` workspace and neutral grey selection.
The opaque dark palette keeps that exactly. The vibrant palette is the new
default on macOS (§9, decision D1). Selection stays neutral grey. `accent` is
used only for links, list numerals, focus rings and the caret.

**Type.** Interface text uses the system font (`.AppleSystemUIFont`); code uses
`SF Mono`, falling back to `Menlo`. `MonaspaceArgon` is optional for parity with
web, not required.

| Role | Size / line | Weight |
| --- | --- | --- |
| Prose body | 14.5 / 24 | 400 |
| Prose H1 / H2 / H3 | 20 / 28 · 17 / 26 · 15 / 24 | 600 |
| UI label | 13 / 18 | 400–500 |
| Meta / activity summary | 12.5 / 18 | 400, `text-2` |
| Caption / gutter | 11 / 16 | 400, `text-3`, tabular numerals |
| Code | 12.5 / 20 | 400 |

**Spacing.** A 4 px grid. Paragraph gap 10, turn gap 28, user pill padding
10×14, composer padding 12×14.

**Radii.** Window 12 (system), composer 18, user pill and sheets 14, chips and
buttons 8, list rows 6, inline code 4.

**Elevation.** The composer and floating sheets get a 1 px `hairline` border,
shadow `0 8 24 / 30%` (dark) or `0 6 20 / 10%` (light), and an `elevated` fill.
No borders between the column and panes: separation comes from a fill change
plus a 1 px hairline.

**Motion.** 140 ms ease-out for entrances, 100 ms for hovers, and 200 ms for
composer docking and pane slides. Streaming text isn't animated per token;
only the caret and the thinking timer move. With "Reduce motion" on, these
become instant cross-fades. The previous docs ban perpetual decorative
motion, so spinners appear only while work is actually running.

**Icons.** Keep the vendored Lucide set and add `folder`, `git-branch`, `file-code`,
`file-diff`, `chevron-*`, `history`, `arrow-up`, `square` (stop), `sparkle`,
`search`, `pencil`, `eye`, `list-tree`, `panel-right-open`, `columns-2`. Add
file-type glyphs (rs/py/ts/md/json/toml/sh/generic) as small tinted monochrome
SVGs. No brand logos (the provider glyph is a generic sparkle tinted with the
agent colour).

### 2.4 Components

Each component is a reusable function or entity in `rust/desktop/src/ui/`
(§3.2), with hover, focus, pressed, disabled and loading states defined.

1. **Title bar** (38 px, `appears_transparent`, traffic lights at (14, 13)).
   - Left: sidebar toggle, then ←/→ (a desktop-local history of visited
     sessions and agent pages; these don't send host actions, see §5.3),
     then `+` (new session).
   - Centre-left: agent glyph in the agent colour, the session title (double-click
     to rename via the existing rename workflow), and a muted
     `workspace @ branch` from `breadcrumb`.
   - Right: a run-state badge only while running or awaiting input, the
     Review toggle, and the details toggle.
   - The whole bar is a drag region; double-click zooms.
2. **Session sidebar.**
   - Search field with a `⌘K` hint; arrow keys move a highlighted result,
     Enter opens it, Escape clears it.
   - Workspace groups.
   - Two-line rows: title, plus `sub` in muted text. A status dot shows
     state: running = animated ring in `text-2`, needs input = `warning`,
     error = `danger`, idle = none.
   - Hover reveals a `⋯` menu (rename, archive, export, fork).
   - Archived link at the bottom.
   - Keep the existing virtual list (§1 fix already landed) and fold it into
     its own entity.
3. **Turn minimap rail** (12 px, left edge of the column).
   - One tick per user message (long) and per assistant turn (short). The
     current viewport is a brighter band.
   - Hover shows the user prompt's first line; a click scrolls to that turn.
   - Hidden below 900 px.
   - Data is derived from block kinds already in the projection; no host
     change.
4. **User message.**
   - Right-aligned pill, max 80% of the column, `pill` fill, radius 14.
     Body 14.5/22.
   - Attachments render as a thumbnail strip and chips above the text.
   - Hover reveals actions to its left: copy, edit-and-resend (only if a
     host route exists, otherwise omitted), and inspect (opens the existing
     message inspection operation).
5. **Assistant prose.**
   - No card or role label.
   - Markdown spec:
     - paragraphs;
     - H1–H3;
     - ordered lists with `accent` numerals and hanging indent;
     - bullets with `text-3` dots;
     - task lists;
     - blockquotes with a 2 px `hairline` rule;
     - tables with header row tint and hairline grid, horizontally
       scrollable when wide;
     - inline code as a `pill` chip in mono;
     - links in `accent`, pointing-hand cursor, http/https/mailto only
       (unchanged security rule).
   - Hover over a reply shows a floating mini-toolbar at the top right:
     Copy, Copy as Markdown, Inspect. This replaces the permanent "Copy
     reply" button.
6. **Code block.**
   - `elevated` fill, radius 10, header row with language label and a Copy
     button (on hover).
   - Syntax highlighting, horizontal scroll, and line wrap toggle.
   - Over 40 lines it folds to 24 with "Show all N lines" (the clipping is
     announced).
7. **Activity group** (tools / thoughts / explored / tool_group / task).
   - **Collapsed:** `›` + summary in `text-2`, e.g. "Ran 2 commands · read 5
     files · searched 1 time". The summary comes from the projection
     (`_explored_summary`, group titles); the desktop does not invent counts.
   - **Running:** the summary carries a live spinner and the current
     verb ("Reading shell.rs…"), and the newest member streams in
     underneath. When the group finishes it auto-folds unless the user
     expanded it.
   - **Expanded:** a tree with 1 px `hairline` connectors (`├─`/`└─`
     drawn as paths, not glyphs). Each row is icon + verb + a target chip
     (file-type glyph + basename; hover shows the full path) + muted
     arguments.
   - Clicking a row expands it inline into a **labelled parameter/result
     sheet** (a port of `ui_support/tool_details.py` rows: every parameter,
     every output, "truncated at N KiB — Show full output").
   - **Errors:** a `danger` icon plus a one-line error. The group summary
     stays neutral to match Ratatui (no ✓/✗ tallies), but the failing row
     is always visible when the group is collapsed (a "1 row needs
     attention" affordance). See decision D4.
   - **Edits:** the row shows `+N −M` and expands into an inline mini diff
     (max 20 rows). "Open in Review" jumps to the file in the review pane.
   - **Subagent task:** the row shows the agent name in its colour and its
     status; Enter opens the agent page (existing operation).
8. **Thinking.**
   - While streaming: `✳ Thinking… 6s` in `text-2`. The glyph pulses
     (opacity 0.5 ↔ 1, 1.2 s) and the timer ticks locally from the
     projection's start time.
   - When done it becomes "Thought for 6s ›", which expands to the full
     thought text (never hidden).
9. **Composer** (floating, max 720, radius 18).
   - Row 1: the multiline editor, auto-growing to 40% of window height,
     then scrolling. Placeholder "Ask anything, or / for commands".
   - Row 2, inside the frame:
     - left: attachment chips and thumbnails;
     - right: agent·model chip (opens the model picker; Shift+Tab cycles
       agent), effort chip, mic, attach, then the send button.
   - The send button is a circular 28 px button with `text` fill and
     `window` arrow. While running it becomes a stop square. When the draft
     is non-empty during a run it shows a split action "Steer ⏎ · Queue
     ⌃⏎ · Interrupt ⌥⏎" as a hover hint.
   - Below the frame (footer row, `text-3`): `▢ Local checkout` or the
     worktree name, `⎇ branch`, then on the right the context label
     (clickable → context popover) exactly as projected.
   - Queued messages stack as removable chips above the composer.
   - Voice: when recording, the editor area shows a live level waveform
     and preview text, plus Insert / Send / Discard.
   - A 48 px gradient fade masks transcript content scrolling under the
     composer.
10. **Completion popover.**
    - Anchored above the caret token, max 8 visible rows, grouped (Commands
      / Files / Arguments).
    - Each row shows a match highlight, a right-aligned summary, and for
      files a type glyph.
    - Up/Down, Enter/Tab insert, Escape hides it for that token.
11. **Command palette (⌘K / Ctrl+P).**
    - Spotlight-style sheet at the top centre, width 640, with fuzzy search
      over all `/help` items: commands with their key hints, plus recent
      sessions.
    - This is the existing `/help` drawer data, re-presented.
12. **Pickers** (model, agent, effort, sessions, archived, tiers…).
    - The same sheet style as the palette: grouped list, fuzzy filter, a
      right-aligned detail column, favourites star.
    - The footer shows a key-hint bar from `panel_hint`
      (Ctrl+F favourite · Ctrl+S sort · Ctrl+R refresh).
13. **Settings.**
    - A full-height sheet with the left nav from the `nav` projection (scope
      and category).
    - The right side is a list or editor. The editor is a monospace `Input`
      with line numbers, a status line (`form.status`), and Save / Delete /
      Reset buttons.
    - Autosave stays debounced at 700 ms with the same identity, revision
      and generation guards.
14. **Decision sheets** (permission / question).
    - Anchored above the composer, never modal over the whole window; the
      transcript stays readable.
    - Labelled lines (`labelled(permission)`) in a two-column grid.
    - Choices are buttons with key hints (`y`, `a`, `n`…). Disabled choices
      stay visible with their reason.
    - A question has a free-form field.
    - An amber left rule marks it as needing input.
15. **Inspector** (right pane, resizable 360–720, width remembered via host
    preferences if a key exists, otherwise kept locally; see D6).
    - Tabs: **Review** (new) · Session · Files · MCP · Logs. The existing
      details-panel tabs are kept.
    - **Review:** a file list header ("80 changed files vs main +9341 −3630"),
      collapsible per-file sections with `+N −M`, and a virtualised unified
      diff with two gutters, add/del tints and hunk headers.
      - The data source is the host's existing `git_diff` (used by `/diff`).
        Today the bridge only renders it as plain panel text, so this needs
        a structured diff projection; see §3.4 and decision D3.
16. **Toasts / notices.**
    - Bottom-centre above the composer; a stack of at most 3.
    - Kinds: info, warning, error.
    - Auto-dismiss after 6 s for info only; errors persist with a Dismiss
      button. An action button appears when the notice has one (e.g.
      Reconnect, Update help).
    - Replaces the fixed-position error box.

---

## 3. Architecture and performance work

Rust stays presentation-only. Host data and actions still go through the daemon
and the Python bridge (AGENTS rule 4). Nothing here reads session storage.

### 3.1 Build and launch (quick wins, ship first)

- `binary_path()`: prefer `release` over `debug` whenever both exist, and warn
  on stderr when only a debug build is found. Keep `NEXUS_DESKTOP_BINARY`.
- `Cargo.toml`:
  - add `[profile.dev.package."*"] opt-level = 2` so GPUI and its
    dependencies are optimised in dev builds;
  - add `[profile.release] lto = "thin"`, `codegen-units = 1`.
- Update the build commands in `docs/desktop.md` to `--release`.

### 3.2 Split the monolith into entities

```
rust/desktop/src/
  main.rs            — process wiring, reader thread, window, keymap install
  store.rs           — SnapshotStore: topics, Arc<Content> blocks, parsed caches, events
  keymap.rs          — declarative (keys, context, action) table + install (§5)
  bridge_v3.rs       — topic/patch decoding (wraps tui/src/bridge.rs types)
  theme.rs           — tokens (§2.3), vibrancy flag, resolved once per change
  ui/
    shell.rs         — AppShell: layout, drawers, overlay stack, toasts
    titlebar.rs
    sidebar.rs       — SessionSidebar entity (virtual list, search)
    conversation.rs  — Conversation entity (list, minimap, follow logic)
    blocks/          — user.rs, prose.rs, code.rs, activity.rs, thinking.rs, diff.rs
    composer.rs      — Composer entity (editor, chips, completion popover, voice)
    inspector.rs     — Inspector entity (tabs, review pane)
    sheets/          — palette.rs, picker.rs, settings.rs, decision.rs, image.rs
    primitives.rs    — button, chip, icon_button, kbd hint, tooltip, scroll fade
  text/
    selectable.rs    — SelectableText element (§3.5)
    markdown.rs      — parse → ParsedDoc (cached)
    highlight.rs     — syntax highlighting
  input.rs           — existing editor, kept for composer/search/form
```

Each view is an `Entity` that subscribes to `SnapshotStore` events
(`TopicChanged(Topic)`, `BlocksSpliced{range}`, `BlockAppended{id}`) and calls
`cx.notify()` only for its own topics. So a log line repaints only the Logs tab,
a session poll only the sidebar, and a token only the last transcript item.
Add a module-map row per new module, as `tests/test_docs.py` requires.

### 3.3 Bridge protocol v3 (Python ⇄ Rust, private)

The current snapshot is one flat object. v3 sends **topics** with per-topic
hashes, plus **block ops**. The JSONL framing, 16 MiB bound, 32-message channel
and generation guard all stay.

```jsonc
{"schema":3,"generation":7,"revision":912,
 "topics":{"header":{…},"composer":{…}},          // only topics whose hash changed
 "blocks":{"splice":{"from":41,"items":[…]}},     // as today's blocks_from
 "append":[{"id":"t12:md","text":"tokens…","rev":"…"}],  // streaming text tail
 "images":{"put":[{"id":"sha256…","media":"image/png","data":"…"}],"drop":["…"]}}
```

- **Topics:** `header` (title, status, model, agent, effort, theme, toggles,
  breadcrumb, agent_color), `composer` (insert/restore one-shots, completions,
  voice, attachments, queue_lines, context label/usage), `sessions` (+ tabs,
  archived), `details` (panel minus logs), `logs`, `panel` (title, items, nav,
  hint, lines, format, preview image id), `form`, `prompt`, `history` (sent
  once, then only on change).
- **Append op:** while a markdown/thought block grows by suffix, and its
  `rev` lineage is unchanged, send only the suffix. Python keeps
  `sent_text_len[id]`. Any non-suffix change falls back to a splice. This
  removes the O(n²) cost.
- **Images:** content-addressed. Bytes are sent once and Rust keeps an LRU of
  decoded `Arc<Image>` (bounded to the existing 8 inline + 1 preview,
  4 MiB each). Snapshots carry only ids. Decoding moves to the background
  executor.
- **Coalescing:**
  - Python: `update()` marks the state dirty and a single writer task flushes
    at most every 16 ms. User-initiated actions (submit, pick, dismiss) flush
    immediately. Polls never flush faster than they do now.
  - Rust: the receive loop drains the channel and applies only the newest
    topics plus all block ops in order. Ops are never dropped (schema-2
    ordering invariant kept).
- **Fingerprints:** use per-topic `blake2s` of the topic's JSON, computed once.
  Remove the full-snapshot second `json.dumps`.
- Ratatui keeps consuming schema 2 until it opts in. The projection function
  is shared and v3 is a serialisation layer over it, so both clients project
  identical data. Document this in `docs/desktop.md` (wire section) and
  `docs/ratatui-parity.md`.

### 3.4 Small host and bridge additions (contract work, not UI hacks)

- **Structured diff for the Review pane:** project the existing `git_diff`
  result into `{files:[{path, adds, dels, hunks:[{header, rows:[…]}]}],
  truncated}` in the bridge, reusing the diff row parsing the transcript
  `diff` block already uses. If parsing must live host-side, add a field to
  the host result in `host/protocol.py` + `host/facade.py` rather than parsing
  in Rust. Truncation stays announced.
- **Thinking start time and tool elapsed:** check whether the projection
  already carries timestamps. If not, add `started_ms` to the thought/tool
  block projection from the reduced view (replay-safe because it comes from
  durable records).
- **Keymap table:** extend `ui_support/shortcuts.py` with context-scoped rows
  (§5.2) so all three native surfaces read one source.

### 3.5 Text rendering

- **ParsedDoc cache:** `HashMap<(BlockId, Rev), Arc<ParsedDoc>>`, a true LRU
  bounded at 2,048 docs / 16 MiB of source. A `ParsedDoc` holds the markdown
  blocks with precomputed `SharedString`s, `TextRun`s/highlights and link
  ranges. For streaming blocks, re-parse only from the last completed
  top-level block boundary (keep the prefix's parsed blocks and re-parse the
  tail).
- **SelectableText element:** a custom GPUI `Element` built on `StyledText` and
  `InteractiveText` (both present in 0.2.2) with one element per *message*,
  not per paragraph.
  - Supports drag selection across all paragraphs of a message, double-click
    word / triple-click paragraph, Cmd+C, and link clicks.
  - Shaped lines are cached by `(doc rev, width, theme)`.
  - It replaces the `text_cache` of `Input` entities.
  - Cross-message selection is out of scope for v1. "Copy" on the hover
    toolbar covers whole replies.
- **Syntax highlighting:** add `syntect` with the default syntax set loaded
  lazily on a background thread, and highlight off the UI thread into the
  ParsedDoc. Map themes to our tokens (two custom `.tmTheme`s). Fallback
  while loading: plain mono. Watch binary size; `tree-sitter-highlight` is the
  alternative if size matters (decision D5).

### 3.6 Transcript list behaviour

- Keep `ListState` (variable height). On width change, don't `reset()` every
  frame. Instead mark measurements stale and re-measure lazily once the
  resize has settled (≥ 120 ms without change). During the drag, reuse old
  heights scaled from the previous width. The list keeps the anchor item and
  offset.
- **Follow logic:**
  - Follow is on while the bottom edge is within 48 px of the end.
  - A user scroll up beyond that turns it off.
  - When off and new content arrives, a floating "↓ New messages" pill
    appears above the composer (click or `Cmd+J` / `Ctrl+End` to follow
    again).
- Keyboard scroll: PageUp/PageDown scroll by viewport minus one line;
  Home/End (when the composer is empty or unfocused) go to top/bottom.
- `render_block` takes `Arc<Content>`; no clone per frame.
- **Trace mode** (`NEXUS_DESKTOP_TRACE=1`): log per-frame layout and paint
  time, snapshot apply time and bytes, and input→paint latency to stderr.
  An optional corner HUD is toggled by `Cmd+Shift+F12` in review mode only.
  This mirrors `rust/tui/src/trace.rs`.

### 3.7 Input path

- Composer edits are local and instant.
- `draft_changed` is debounced at 250 ms and flushed on submit, session
  switch, blur and quit.
- `complete` is debounced at 120 ms (Ratatui's value), sent immediately on Tab,
  and guarded by the current prefix as today.
- One-shot `insert`/`restore` is applied against the composer *revision*. Rust
  echoes the revision the one-shot targeted; a mismatch merges instead of
  overwriting (fixes 1.2.9).

---

## 4. Slash commands and workflows parity

Slash commands already route through the shared Python `ShellActions` /
`commands.SPECS`, so behaviour is shared. The work is making each command's
*result* present well natively, and proving coverage.

| Command(s) | Desktop presentation |
| --- | --- |
| `/help` (Ctrl+P, ⌘K) | command palette (§2.4.11) |
| `/hotkeys` (Ctrl+X ?) | shortcuts sheet rendered from the shared table, grouped by context, with key caps |
| `/new` `[id]`, `/sessions` `[id]`, `/archived`/`/resume`, `/fork` `[seq]` | new-session animation; sessions picker sheet; archived picker; fork switches session |
| `/model`, `/agent`, `/effort`, `/theme` | picker sheets; `/theme` toggles with a 200 ms cross-fade |
| `/context` (Ctrl+I), Ctrl+X C popover | context sheet: tiers bar, per-block sizes, Enter → full context inspector |
| `/usage` (Ctrl+U), `/cost` | usage sheet with last-fetched report + loading state |
| `/details`, `/tools`, `/mcp`, `/skills`, `/tasks`, `/worktrees` | inspector tabs or picker sheets as Ratatui does |
| `/diff [--staged] [ref]` | opens the Review tab with that diff (needs §3.4) |
| `/settings` (Ctrl+S) | settings sheet |
| `/export`, `/copy` | native save panel / clipboard with toast confirmation |
| `/verbose`, `/reload`, `/reconnect` (Ctrl+R), `/cancel` | toggles/toasts; reconnect banner while disconnected |
| `/review`, `/commit` | submitted as prompts as in Ratatui |
| `/voice`, `/speak` | composer voice state; consent/download sheets |
| `/attach PATH` | chips; plus native file picker (⌘⇧A) and drag-drop |
| `/exit`/`/quit` | closes window (detach; daemon turns continue) |
| `/mock` (dev only) | picker |

A test asserts that every `commands.SPECS` name (and dev specs in dev mode) is
reachable from the completion list and has a non-crashing desktop presentation in
preview fixtures.

---

## 5. Keyboard parity

### 5.1 Principle

**Ratatui's Ctrl-key semantics are canonical and identical in the desktop.**
macOS `Cmd` shortcuts are added as *aliases* only where they don't collide with a
Ratatui meaning. Standard macOS text editing (`Cmd+C/V/X/Z/A`, `Option+←/→`,
`Cmd+←/→`) stays in the editor. On macOS, `Ctrl+A/E` in the editor keep
Emacs line-start/line-end only where Ratatui leaves them unbound in the
composer (see decision D2 for `Ctrl+E`).

### 5.2 Target map

✓ = already matches, ✗ = conflicting, — = missing.

**Global and composer** (from `ui_support/shortcuts.py::SHORTCUTS` and
`rust/tui/src/main.rs`):

| Key (Ratatui) | Action | Desktop today | Plan (+ Cmd alias) |
| --- | --- | --- | --- |
| Enter / Shift+Enter / Ctrl+J | send (steer while running) / newline / newline | ✓ / ✓ / — | add Ctrl+J newline |
| Ctrl+Enter / Alt+Enter | queue / interrupt | ✓ / ✓ | ✓ |
| Ctrl+P | Commands (`/help`) | ✗ opens Sessions | fix; alias ⌘K, ⌘⇧P |
| Ctrl+N | new session | ✓ | alias ⌘N |
| Ctrl+O | sessions | — | add; alias ⌘O |
| Ctrl+F | fork (outside pickers) | ✗ favourite only | context-scoped: picker → favourite, else fork |
| Ctrl+G | agent picker | — | add |
| Ctrl+B / Ctrl+L | sessions / details sidebar | ✓ | aliases ⌘B / ⌘⌥B (⌘L kept) |
| Ctrl+S | settings (outside pickers/forms) | ✗ sort only | context-scoped: picker → sort, form → save, else settings; alias ⌘, |
| Ctrl+I | context | ✓ | alias ⌘I |
| Ctrl+T | cycle effort | — | add (⌘⇧E kept as alias) |
| Ctrl+Space | dictate | ✓ | ✓ |
| Ctrl+E | toggle Logs | — | add (see D2); ⌘⇧L kept |
| Ctrl+U | provider usage | ✓ | alias ⌘U |
| Shift+Tab | cycle root agent | ✗ focus previous | fix (⌘⇧G kept); focus traversal moves to Ctrl+Tab / ⌥Tab |
| `a` (not typing) | agent picker | — | add when focus is transcript/inspector, never in the editor |
| Ctrl+C | copy selection, else return to conversation / cancel turn | partial | match exactly; ⌘. alias for cancel |
| Ctrl+R | reconnect (outside pickers) | ✗ refresh only | context-scoped; alias ⌘R |
| Ctrl+Q | quit | — | add; ⌘Q |
| Ctrl+V | paste / clipboard image | ✓ (editor) | ✓ |
| Escape | return to conversation / dismiss | ✓ | ✓ plus closes overlaying details first (Ratatui order) |
| Escape twice (≤1.5 s) | stop active turn | — | add |
| Up / Down (single-line draft) | history | ✗ needs ⌥↑/⌥↓ | match; keep ⌥↑/⌥↓ alias; multi-line → cursor movement |
| Tab | empty draft → transcript navigation; else force completion | ✗ | match; completion visible → insert |
| PageUp / PageDown | scroll transcript / panel | — | add |
| Ctrl+End | follow latest | ✗ editor End | match; ⌘J alias kept; ⌘↓ stays editor doc-end |

**Ctrl+X leader** (from `LEADER_SHORTCUTS`; 1.5 s timeout as Ratatui; a subtle
"Ctrl+X…" hint chip appears in the composer footer while armed. Ratatui has no
banner, which is acceptable as a desktop affordance):

| Next key | Action | Desktop today |
| --- | --- | --- |
| M | model | ✓ |
| V | dictate now | — |
| N / O / F | new / sessions / fork | — |
| G | agent picker | ✓ |
| B / L | sessions / details | — |
| S / I / U / R | settings / context / usage / reconnect | — |
| E | logs | ✗ (effort) |
| T | cycle effort | — |
| C | context popover | — |
| Z | update help | — |
| ? | shortcuts | — |

**Context-scoped keys:**

| Context | Keys (Ratatui) |
| --- | --- |
| Pickers / menus | ↑/↓, Tab/Shift+Tab, Home/End, Enter, Space toggle, type-to-filter, Backspace, Ctrl+F favourite, Ctrl+S sort, Ctrl+R refresh, PageUp/PageDown, Escape back (nested back stack) |
| Settings | ←/→ switch nav area (not while editing), Ctrl+S save, Ctrl+D delete (confirm), Escape flushes autosave and goes back |
| Read-only panels | ↑/↓/PageUp/PageDown scroll, Enter on "Context · as of" opens full context, Ctrl+U/R refresh usage |
| Transcript navigation | Tab (from empty draft) focuses the last block; j/k or ↑/↓ or Shift+Tab move; Enter/Space opens; Escape or any other key returns to the composer |
| Details focused | `[` / `]` switch tabs, ↑/↓ move, Enter opens file, `c` copies, typing returns to the composer |
| Decision sheet | choice keys; question: ↑/↓/Tab select, Enter (empty → first enabled choice), typing answers |
| Voice | while recording any key stops (and is applied), Enter sends/inserts per Ratatui, Escape discards; while transcribing Escape discards |

### 5.3 Implementation

- `keymap.rs` is a single declarative table of
  `(keystroke, context, Action, source)` rows, where `source` names the
  `shortcuts.py` id. GPUI key contexts: `Nexus`, `Composer`, `Picker`,
  `Form`, `Panel`, `Transcript`, `Details`, `Decision`, `Voice`. More
  specific contexts win, which handles the scoped `Ctrl+F/S/R` cleanly.
- Leader: a small state machine on the shell (`leader_armed_at: Option<Instant>`),
  with the second keystroke dispatched through a `LeaderKey(char)` action. GPUI
  multi-stroke bindings (`"ctrl-x m"`) are used where possible, so the hint
  chip and the timeout match Ratatui.
- Back/forward (title bar): desktop-local only (`⌘[`/`⌘]`), a bounded stack of
  32 visited `(workspace, session, agent_page)` entries. It dispatches existing
  `session_open`/agent-page operations. Ratatui has no equivalent; this is a
  harmless desktop navigation aid (decision D7).
- **Parity test** `tests/test_desktop_keymap_parity.py`:
  - parses `rust/desktop/src/keymap.rs` (a simple stable row syntax);
  - asserts every `SHORTCUTS` and `LEADER_SHORTCUTS` row has a desktop row
    with the same key and action;
  - asserts no desktop binding shadows a Ratatui key with a different
    meaning.
  - A Rust unit test asserts that each table action has a handler.
- `/hotkeys` and the palette render key hints from the shared table, showing ⌘
  aliases next to Ctrl keys on macOS.

---

## 6. Polish checklist (applies to every component)

- Every control has hover, pressed, focus-visible (2 px `accent` ring at 40%)
  and disabled states. The arrow cursor is used for commands, the pointing
  hand only for links.
- Truncation uses ellipses, with the full text in a tooltip; nothing is
  silently clipped. Long paths truncate in the middle.
- Numerals use tabular figures in gutters, counts and timers.
- Pixel alignment: icons are 16 px on an 8 px rhythm, title bar controls on a
  28 px hit area with 2 px gaps (ref #1's papercut about inconsistent gaps).
- Scrollbars are overlay style, appearing on scroll or hover; edges fade where
  content scrolls under chrome.
- Loading and empty states for every pane: sessions (none / loading),
  inspector tabs, pickers (`panel_loading`), usage, review (no changes /
  truncated).
- Disconnected state: a top banner "Disconnected — Reconnect (⌘R)", the
  composer disabled with its draft kept, and the timeline still readable.
- Window: remember size and position locally. Vibrancy turns off
  automatically when the macOS "Reduce transparency" setting is on (fall back
  to opaque tokens), and the same for "Reduce motion".
- Light/dark: follow the system appearance when the theme is `auto`. If
  `auto` isn't a host theme value yet, keep the explicit `/theme` and add
  `auto` only if the host preference supports it (no UI-only state that must
  survive reconnect; rule 5).
- Accessibility: keyboard reachability for everything (already a rule), and
  contrast ≥ 4.5:1 for body text against both vibrant backgrounds over light
  and dark wallpapers (verify on a white and a black wallpaper). Screen
  reader support stays "not verified" unless GPUI 0.2.2 exposes it.

---

## 7. Phased roadmap

Each phase lands as its own Conventional Commit PR with tests and a docs
update. Phases 0–1 are prerequisites; 2–6 can overlap once the store and
primitives exist.

| Phase | Deliverable | Exit check |
| --- | --- | --- |
| **0. Measure + quick wins** (≈2 days) | trace mode; release-first binary selection; dev dep opt-level; remove render-side effects; Arc blocks; debounce draft/complete; background image decode | trace baseline recorded in `artifacts/desktop/perf-baseline.md`; typing no longer blocks on Python |
| **1. Store + entity split + bridge v3** (≈1 week) | `store.rs`, entity per region, topics, append op, content-addressed images, 16 ms coalescing | ≤ 8 KiB per token update; log/poll updates don't repaint the transcript (render counters in tests); schema-2 Ratatui tests still pass |
| **2. Tokens + primitives** (≈3 days) | `theme.rs` rewrite (4 palettes, vibrancy), primitives (button, chip, icon button, kbd, tooltip, fade, toast stack) | preview fixture gallery screenshot dark/light, vibrant/opaque |
| **3. Shell** (≈4 days) | title bar, sidebar entity polish, drawers, resizable inspector, toasts, disconnected banner, back/forward | narrow/wide captures; drawer focus restore tests |
| **4. Conversation** (≈1.5 weeks) | ParsedDoc cache, SelectableText, markdown spec, code blocks + highlighting, user pill, activity tree, thinking timer, follow pill, minimap, lazy re-measure on resize | marathon replay at 60 fps; selection tests; resize keeps anchor |
| **5. Composer** (≈4 days) | floating composer, chips, completion popover, voice state, queue chips, empty-state docking animation, footer | input regression tests; IME check; captures of each state |
| **6. Sheets + inspector** (≈1 week) | palette, pickers, settings, decision sheets, image preview, Review tab (with §3.4 projection) | existing journey tests; new review projection tests (Python) |
| **7. Keyboard parity** (≈3 days, may start after phase 1) | `keymap.rs`, contexts, leader, parity test, `/hotkeys` from the shared table | `test_desktop_keymap_parity.py` green; manual key sweep recorded |
| **8. Motion + polish pass** (≈3 days) | §6 checklist, reduce motion/transparency, contrast check, papercuts from the review | full native review ledger, no open P1/P2 |
| **9. Docs** (continuous, final sweep) | `docs/desktop.md` rewritten (interface, keys, wire v3, verification), `docs/decisions.md` entries D1–D7, module-map rows, `docs/surfaces.md` key table, `skills/gpui-nexus` design-guide update | `tests/test_docs.py` green |

### 7.1 Testing

- **Rust:**
  - store topic and op application (append, splice, out-of-order rejection,
    generation change);
  - ParsedDoc incremental parse equals a full parse;
  - SelectableText selection ranges across paragraphs and graphemes;
  - keymap context resolution;
  - follow and anchor logic;
  - render-count tests proving topic isolation;
  - keep all existing native tests (draft per session, autosave, secrets,
    1,000-session virtualisation, drawers, completion).
- **Python:**
  - v3 serialiser tests (topic hashing, append fallback on non-suffix edits,
    coalescing flushes user actions immediately, image put/drop);
  - review diff projection;
  - keymap parity;
  - SPECS coverage.
- **Visual** (native-app-review skill, real app through `nexus desktop`, not
  mock-ups):
  - states: empty, streaming with a running activity group, folded groups,
    expanded tool with full parameters, long code, wide table, diff/review,
    settings editor, model picker, palette, permission, question, voice
    recording, attachments and thumbnails, subagent page, disconnected,
    narrow drawers;
  - each in dark and light, vibrant and opaque, over light and dark
    wallpapers.
- **Performance:** a scripted replay fixture (the marathon session) plus a
  scripted-provider streaming run of ~20k tokens, with trace numbers in the
  ledger before and after each phase.

### 7.2 Risks

- **GPUI 0.2.2 limits.** Blur works on macOS (`WindowBackgroundAppearance::Blurred`
  exists in 0.2.2); other platforms fall back to opaque. There's no
  accessibility tree. Custom text selection is our code. Upgrading GPUI is
  out of scope unless a phase is blocked; if so, record it as a decision.
- **Vibrancy legibility over busy wallpapers.** Mitigated by a 72% tint
  floor, the contrast checks, and the opaque fallback toggle.
- **Bridge v3 drift from Ratatui.** Mitigated by sharing `project()` and
  layering v3 as serialisation only, with tests on both schemas.
- **Binary size from syntect.** Measured in phase 4; tree-sitter or a
  minimal highlighter is the fallback.

---

## 8. Out of scope

- Deprecated web client changes (AGENTS: no new web features).
- New agent/runtime behaviour. Every new datum goes through host/bridge
  projection, never Rust-side file reads.
- Cross-message text selection, screen-reader support, Windows/Linux visual
  verification (each stays documented as not verified).

## 9. Decisions to confirm

Status: **accepted** = implemented and shipped here; **open** = recommendation
still to be implemented.

| ID | Decision | Recommendation | Status |
| --- | --- | --- | --- |
| D1 | Default window material | Vibrant (blurred, tinted) on macOS by default; opaque `#0B0B0B` palette kept as the exact fallback and as a setting | open (Phase 2) |
| D2 | `Ctrl+E` in the composer: Ratatui = Logs, macOS Emacs = end of line | Follow Ratatui (parity was requested); `Cmd+→` remains end of line | accepted (Phase 7; recorded in `docs/decisions.md`) |
| D3 | Review pane data | Structured projection of the existing `git_diff` in the bridge; host field only if parsing must be shared | open (Phase 6) |
| D4 | Activity group failure display | Neutral summaries like Ratatui, but failing rows always visible while collapsed | partially done — summaries are already neutral; the "needs attention" affordance is open (Phase 4) |
| D5 | Syntax highlighting engine | `syntect` lazily loaded; revisit if the release binary grows > 6 MiB | open (Phase 4) |
| D6 | Inspector width persistence | Host preference key if one is added (survives reconnect, rule 5), otherwise local window state | open (Phase 3) |
| D7 | Title bar back/forward | Include as desktop-only navigation; no host semantics | open (Phase 3) |

The plan's own §2.3 token table lists an indigo `accent` (`#8B8CFF`), but the
user previously asked for neutral grey selection and links. Until that is
confirmed, the theme keeps the neutral accent; this blocks nothing in Phase 2
except the accent hue itself.


## 10. Implementation ledger

### Status summary (2026-10-04)

| Phase | State | Done / verified here | Still open |
| --- | --- | --- | --- |
| 0 Measure + quick wins | done | trace, release-first launch, dev dep opt-level, render side effects removed, debounce, background image decode | release marathon/streaming baseline, input-latency numbers |
| 1 Store + entities + wire v3 | partial | schema-3 topics, append ops, content-addressed images, 16 ms coalescer, cross-stack tests; ≤ 8 KiB/token proven on the serialiser | entity split, per-topic repaint isolation, full projection per event, end-to-end frame numbers |
| 2 Tokens + primitives | not started | — | `theme.rs` 4 palettes + vibrancy, primitive gallery |
| 3 Shell | partial | notice/toast stack, sidebar search nav, drawer offset + re-dock, shared `TOP_BAR_HEIGHT`, disconnected banner + disabled composer | top-bar back/forward, resizable inspector, drawer focus-restore capture |
| 4 Conversation | partial | follow logic + "New messages" pill, debounced resize re-measure, tinted unified diffs, turn minimap | `ParsedDoc` incremental cache/LRU, `SelectableText`, code highlighting + fold, activity tree, thinking timer, lazy re-measure validation |
| 5 Composer | not started | — | floating composer, completion popover polish, voice state, queue chips, docking animation |
| 6 Sheets + inspector | not started | — | palette, settings/picker sheets, Review tab (needs the structured diff projection) |
| 7 Keyboard parity | done (no live sweep) | every shared-table row routed, leader `?`/`c`/`z`, `a`, PgUp/PgDn, `[`/`]`, `Ctrl+S` form, Esc-twice, Tab transcript navigation, parity + handler tests | live manual key sweep on a running window |
| 8 Motion + polish | partial | toasts, diff rows, search, follow pill, drawer re-dock | motion tokens, reduce-motion/transparency, contrast check, full native review |
| 9 Docs | in progress | desktop.md / decisions.md / module-map updated per change | final sweep |

Everything below is the chronological ledger. Verification across the desktop:
`cargo test` 66 tests, `cargo fmt --check` clean; focused Python desktop/docs
suites pass (one unrelated pre-existing attachment failure noted per entry).
No native screenshot or frame-rate claim is made for the changes below.

### Phase 0 — performance foundation (2026-10-04)

Implemented: release-first source launch with a debug fallback warning; dependency
optimization for dev builds; 250 ms draft and 120 ms completion debounce with
flush/cancellation boundaries; background bounded base64 decoding with stale-result
guards; event-side input styling and log visibility; direct borrowed transcript
blocks to eliminate per-frame content clones; opt-in bounded CPU phase, wire-byte
and input-to-paint tracing.

Implementation choice: borrowing `Content` in the list callback already eliminates
the deep clone without a second `Arc` transcript store. The planned `Arc` store
belongs with Phase 1. Trace CPU element phases do not include GPU presentation or
all Taffy layout; the frame-rate targets remain unverified.

Remaining Phase 0 exit work: release marathon/streaming baseline and input latency
measurement on an M-series machine. Full native state matrix, IME, and sustained
scroll/resize performance remain unverified. Phase 1 is started; phases 2–9 are
pending.

Verification: 27 desktop Rust tests and 159 focused Python checks passed; locked
dev build succeeded. Actual isolated native marathon passed 8/8, with light/dark,
multiline draft and settings captures recorded in `artifacts/desktop/review-ledger.md`.
Post-change dev timing smoke baseline is in `artifacts/desktop/perf-baseline.md`;
it is not a release comparison. Schema-2 updates measured about 17 KiB in that
run, so the ≤8 KiB target is still unmet and belongs to Phase 1.

### Phase 1 — schema-3 wire slice (started)

Implemented an initial wire slice: `nexus/ui/desktop/wire.py` emits schema 3
per-topic deltas and transcript block append/splice operations. Stable block IDs
allow Unicode-safe suffix appends; generation and agent-page changes reset state.
One-shot insert/restore values are resent once, and unchanged projections emit no
message. Rust decoding is transactional, rejects stale updates, retains schema 1/2
snapshot compatibility, and avoids cloning transcript blocks for metadata-only
updates.

This is incomplete: Python still builds a full projection, and Rust still applies
updates through the monolithic root entity, including copies of accumulated
transcript text. Entity splitting, coalescing and image addressing remain undone. Native performance and the Phase 1 performance targets
are not verified; this slice makes no performance claim.

### Phase 1 / 7 — status check and keymap parity test (2026-10-04)

Verified in tree: `UpdateCoalescer` (16 ms) wired into the bridge, content-addressed
images in `desktop/wire.py` with transactional Rust decoding, `keymap.rs` with the
Ctrl+X leader and picker-scoped Ctrl+F/S/R. 30 Python wire/schedule/launch tests and
46 Rust tests pass.

Added `tests/test_desktop_keymap_parity.py`: parses `keymap.rs` against
`SHORTCUTS`/`LEADER_SHORTCUTS`. Explicit known gaps (not routed yet): Ctrl+J newline,
`a` agent picker, Esc-twice stop, PageUp/PageDown, Tab transcript navigation,
details `[`/`]`, and `Ctrl+X ?` (opens the palette, not a shortcuts sheet). The
Rust "each action has a handler" test is not written.

Still open in Phase 1: entity split (`Desktop` still renders everything from one
entity), per-topic repaint isolation, a full-projection-per-event cost in Python, and
the ≤8 KiB/token measurement. No performance claim is made.

### Phase 7 — shared-table keyboard parity (2026-10-04)

Closed the keymap parity gaps for the shared tables and the plan's global map:

- `rust/desktop/src/keymap.rs` now routes `Ctrl+X ?` to a new `Shortcuts` action
  (`/hotkeys`, the shared `KEYBOARD_SHORTCUTS` sheet, previously the palette),
  plus `Ctrl+X c` (`ContextPopover`) and `Ctrl+X z` (`UpdateHelp`). Both leader
  rows were added to `ui_support/shortcuts.py`, which the terminal and web already
  handled.
- Scoped routes that must never intercept typing: `a` → agent picker
  (`Nexus && !Editor && !ModelPanel`), PageUp/PageDown → transcript scroll,
  `[` / `]` → inspector tab cycle (all `!Editor`), and `Ctrl+S` → save inside a
  new `Form` key context.
- Escape twice within 1.5 s stops the active turn, matching the terminal
  composer; a single Escape still returns to the conversation.
- `tests/test_desktop_keymap_parity.py` now requires `a` and `show_shortcuts`
  routes, special-cases the timed `escape twice` gesture with a dedicated
  assertion, and matches `Nexus`-prefixed scoped contexts. `KNOWN_GAPS` is
  reduced to the editor-level keys (`enter`, `Ctrl+Enter`, `Alt+Enter`,
  `Shift+Enter`).
- A Rust test, `every_bound_action_has_a_handler`, asserts each bound action is
  wired with `.on_action` in `main.rs`, so a binding cannot silently swallow a
  key.

Tab transcript navigation is now implemented. `tab` (bound at the root) inserts a
visible completion, forces a completion for a non-empty draft, or — on an empty
draft with a target — enters navigation on the newest target (`nav`); j/k,
Up/Down and Shift+Tab move with clamped ends, Enter/Space dispatches the block's
operation, and Escape or any other key returns to the composer. The selected
block is outlined. Focus traversal moved to Ctrl+Tab / Ctrl+Shift+Tab. Covered
by a native test (enter, move, open, leave). Still not done: a live manual key
sweep on a running window. The native state matrix, IME and Phase 1 performance
targets remain unverified.

Verification: desktop Rust tests pass (`cargo test`, 66 tests, `cargo fmt --check`
clean); the focused Python desktop suites pass (35 tests) and the Ratatui
shortcut-binding tests pass. One unrelated pre-existing failure remains,
`tests/test_ratatui_actions.py::test_session_switch_retains_images_and_stable_markers`,
which is about attachment retention and was not touched here.

### Phase 4 — turn minimap (2026-10-04)

Added `rust/desktop/src/minimap.rs`: `ticks(&[Content])` derives the rail purely
from projected block kinds — a long tick per `user` block, a short tick per
`markdown` block, first non-empty line as a bounded label, no invented counts or
timestamps. The shell caches the ticks when a snapshot is applied (no per-frame
derivation), renders a 12 px rail at the left of the column above 900 px, draws a
band for the visible range recorded by the scroll handler, and scrolls the
transcript to a turn on click with a hover hint. Covered by three pure derivation
tests (long/short, bounded label, non-conversational blocks ignored) and a native
test that the ticks populate on apply and the rail renders. The rail's visual
placement is not screenshot-verified.

### Phase 3 — widening re-docks a drawer (2026-10-04)

Fixed §1.2.10: a drawer opened at a narrow width kept `compact_pane` set after
the window widened, so the next narrow resize silently reopened it.
`compact_pane_after_resize` drops the drawer flag once the pane is docked;
covered by a pure-function test. Not screenshot-verified.

### Phase 4 — debounced transcript re-measure on resize (2026-10-04)

Fixed §1.1.8/§3.6: `render` called `transcript.reset()` on every width change, so
a live window drag re-measured every item every frame and jumped the scroll
position. A width change now calls `schedule_transcript_reset`, which bumps a
revision and schedules one reset after 120 ms of quiet; rapid changes supersede
the pending task, so the drag reuses the previous heights and only the settled
width re-measures. The initial window draw queues one reset the same way.
Covered by a native test that two rapid moves leave the reset count unchanged and
settle to exactly one reset. Actual drag smoothness is not measured here.

### Phase 4 — follow logic and "New messages" pill (2026-10-04)

Fixed §1.2.2/§3.6: follow was set from a per-pixel `is_scrolled` flag, so a
one-pixel nudge stopped it permanently and there was no way back. Now
`text::follow_from_visible(visible_end, item_count)` keeps follow on while the
newest item is visible; scrolling above it turns follow off, and when content
arrives while off a bottom-centre "↓ New messages" pill (`follow_pending`)
restores follow, as do Ctrl+End / Cmd+J. The scroll handler ignores the empty
transcript so the initial state is not clobbered. Covered by a pure-function test
and a native test that content while scrolled up raises the pill and `JumpLatest`
clears it. The pill's visual placement is not screenshot-verified.

### Phase 3 — disconnected banner and disabled composer (2026-10-04)

The projection now carries `disconnected` (from the established
`notice.startswith("Disconnected")` convention) and the desktop wire forwards it
in the header topic. When set, the shell shows a top "Disconnected — Reconnect
⌘R" banner, sets the composer read-only while keeping the draft, changes the
placeholder, and refuses submit; Reconnect dispatches `/reconnect` and replay
restores state. Covered by a Python projection test and a native test (read-only,
draft kept, submit refused, reconnect re-enables). Not screenshot-verified.

### Phase 3/8 — sidebar search, diff rows and drawer offset (2026-10-04)

Three §1.2 papercuts:

- **Sidebar search (§1.2.5).** `search_matches` is the single filter source
  (title/id/workspace, case-insensitive); Up/Down move a highlighted match
  through `next_selection`, Enter opens the highlighted result, Escape clears the
  field, and a clear control appears while it is non-empty. The search input runs
  in `menu` mode so arrows navigate results instead of the caret.
- **Diff readability (§1.2.7).** Transcript diffs are unified rows with tabular
  old/new gutters, a `+`/`-` marker and the `diff_add_bg`/`diff_remove_bg` tints
  and `diff_add`/`diff_remove` text colours, replacing the previous sidebar-fill
  50/50 split.
- **Drawer offset (§1.2.4).** Overlay drawers use the shared `TOP_BAR_HEIGHT`
  constant (40 px) instead of a guessed 46 px, so a collapsed pane starts exactly
  under the title bar.

Tested by `search_matches`, `next_selection` and diff/token unit tests plus the
existing 1,000-session virtualisation test. Arrow navigation, the highlight and
the diff appearance themselves are not screenshot-verified.

### Phase 3 — bounded notice/toast stack (2026-10-04)

Replaced the fixed-position error box (§1.2.3) with `rust/desktop/src/notice.rs`
and a toast stack rendered bottom-centre above the composer:

- at most three notices, newest first; repeating the newest text replaces it;
- `Info` auto-dismisses after 6 s (a generation-guarded background timer),
  `Warning`/`Error` persist with a Dismiss control;
- a notice can carry one host action button (`Reconnect`, `Update help`);
- bridge-disconnect errors become error toasts with a Reconnect action, preview
  refusals become error toasts, and a new `update_notice` surfaces once as an
  info toast.

Tested by `notice.rs` unit tests (bounding, dedup, dismiss, action) and a native
test that repeated preview refusals collapse to one bounded notice. The toast
stack is a local, transient affordance; it holds no state that must survive a
reconnect.

Still open in Phase 3: top-bar back/forward, the resizable inspector, the
disconnected banner with a disabled composer, and drawer focus-restore capture.
Visual placement of the toasts is not verified on a running window.

### Phase 1 — streaming byte budget proven on the Python serialiser (2026-10-04)

`tests/test_desktop_wire.py::test_streaming_a_token_into_a_two_thousand_block_session_stays_under_8_kib`
drives a 2,000-block projection and appends 500 tokens to the streaming block.
Each update carries no topics and a single Unicode-safe append op, is ≤ 8 KiB,
and the 500-update total is under 64 KiB. This establishes the ≤ 8 KiB/token
target for the serialiser. It does not measure the Rust side: `Desktop` still
applies updates through one monolithic root entity, and the full-projection
cost per event remains in Python. End-to-end frame measurements are still
unverified.
