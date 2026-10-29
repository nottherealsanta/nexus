# Reference inventory: old Nexus TUI (Step 12.1, item 2)

Read-only review of `~/repos/nexus/rust/` and `~/repos/nexus/docs/ratatui-parity.md`
(branch `nexus01`). `~/.nexus` was not read. Old Nexus's AGENTS.md, host bridge
(`bridge.rs`, snapshot JSON, Python-owned data), config (`config.rs`, `tui.json`)
and state model do not apply: in the old design Python built every datum and Rust
only placed, painted and hit-tested it. The new TUI talks to the Core HTTP API
and owns its own view state, so only the **rendering, geometry, input and widget
code** transfers.

Paths are relative to `~/repos/nexus/rust/` unless they start with `docs/`.
Sizes are line counts. "Copy" means copy deliberately with attribution in the new
file header (`Adapted from old Nexus <path>`) and a docs/porting-ledger.md row if
behavior changes. Nothing here has been copied yet.

Not verified: no old-TUI screenshots or runnable app were used. The parity doc
itself says many native checks were "written, not run" or "visual review
outstanding"; treat its visual claims as intent, not proof. Visual comparison
with the runnable old app is still open for 12.2.

## Summary of what carries over

| Area | Verdict |
| --- | --- |
| `widgets/` kit (theme, focus, hit, keys, glyphs, anim, components) | Best asset. Copy almost whole; rename roles to the new token set (12.2) |
| `tui/src/markdown.rs` | Reuse approach (pulldown-cmark + ratatui-markdown, own table fitter); rewrite streaming-fence handling |
| `tui/src/editor.rs` | Copy for the composer (grapheme editor, undo, paste collapse). It is not the file editor |
| `tui/src/render/{toasts,notifications}.rs` + `components/feedback.rs` | Copy the toast stack and drawer; make the drawer durable-ish per client |
| `tui/src/render/dialogs.rs`, `sticky.rs`, `jump.rs`, `copy_button.rs`, `graphics.rs` | Copy pieces (modal frame, dim backdrop, prompt card layout, sticky user card, jump pill, OSC 52, image protocol probe) |
| `tui/src/transcript/` (groups, thought, tools, fold, header, look, signal) | Adopt the quiet-row language; replace verb groups with INTERFACE_PLAN's one-line-per-call tools |
| `tui/src/sidebar.rs`, `details_sidebar.rs`, `picker.rs`, `render/sessions.rs` | Keep geometry and key patterns; the data model and tabs change |
| `tui/src/settings_page/` | Keep the typed-block page model and widget renderer; blocks come from the shared manifest instead of a host |
| `render/manager.rs`, `live_*`, `splash.rs`, `mermaid*`, Grok/voice/live-call, external workers | Omit |

## 1. Widget kit (`widgets/src/`)

Stateless Ratatui components that draw into a `Buffer`, register a focus stop and
a hit rectangle on one per-frame `Ui`, and return a `Response`. They never mutate
app state; owners map `keys::Intent` values to actions. Hover and focus change
colour only, never geometry. Everything is bounded (4096 focus stops, 4096 hits).

### theme.rs (284)
- **What it does.** `Theme` with dark, light and mono modes. Roles: `bg`, `text`, `muted`, `quiet`, `accent`, `surface`, `raised`, `element`, `element_hi`, `border`, `border_strong`, `focus_bg`, plus `blue/purple/cyan` and `success/warning/error`. `mix()` blends colours. Neutral role methods (`gray`, `rail`, `thinking`, `running`, `header`). `Level` (info/success/warning/error). Mono mode uses `Color::Reset` and modifiers only (`strong`, `dim`).
- **Retain.** The role split (text, muted, quiet), `raised`/`element` surfaces, `focus_bg` as a mix of surface and accent, mono as a real mode, and tests that roles never use the accent. Dark `#141414` page, `#e1e1e1` text, accent `250,178,131`; light `#f5f5f5`, `#262626`.
- **Improve.** Add the semantic tokens INTERFACE_PLAN requires and the old file lacks: selection, diff additions/deletions, a distinct context/notice role, and execution-diagnostic role. Name roles by meaning (no `blue/purple/cyan`). Load from the shared token file (12.2) so NSH uses the same values. Check contrast numerically; the old palette was matched to Grok by eye.
- **Omit.** Grok-specific naming (`Grok Night/Day`) and the agent-colour logic in `render.rs`.
- **Copy.** `widgets/src/theme.rs` (`mix`, `Level`, mono helpers).

### layout.rs (36), hit.rs (63), ui.rs (68)
- **What they do.** `size_of(width)`: narrow < 90, medium < 140, wide. Helpers `inset`, `centered`, `row`, `split_label`. `HitMap` stores `(Rect, id, Part)`; later registrations win (overlays drawn after the page); `translate_from` moves hits registered inside an off-screen scroll buffer and clips them; `rect_of_part` anchors popups. `Ui` holds theme, glyphs, `FocusRing`, `HitMap`, hover blend per id, frame clock, and a `still` flag (reduced motion). `Ui::stop(id, rect)` registers focus and hit together.
- **Retain.** "The rect drawn is the rect registered" (12.3 item 3 requires this). `still` for reduced motion. Later-wins hit order.
- **Improve.** Breakpoints should come from the shared layout spec (12.1 item 4), not 90/140. Add keyboard-visible focus for every hit target by construction (`stop` already does). Hit ids as typed enums rather than strings to avoid typos.
- **Omit.** Nothing.
- **Copy.** `hit.rs` and `ui.rs` whole, `layout.rs` helpers.

### focus.rs (193)
- **What it does.** `FocusRing` rebuilt every frame: `register` collects the order; at `end_frame` a focus whose widget vanished moves to the next surviving id in the previous order, else the previous one, else first. Per-`Region` memory (`Composer`, `Transcript`, `Sessions`, `Details`, `Overlay`), `push_overlay`/`pop_overlay` restoring the opener's focus, `move_by`, `cycle`. `escape(EscapeState)` is a pure function returning what Esc does (close popup, clear search, leave search, leave inner, close overlay, sidebar to composer, composer). `Typeahead` (800 ms buffer).
- **Retain.** All of it. This is the answer to 12.3 item 4's "Esc must not compete" and to focus restoration.
- **Improve.** Add "interrupt" and "back from child" to the Esc ladder explicitly (old Esc also stopped a turn). Add a `Region` for the waiting card and child view.
- **Omit.** Nothing.
- **Copy.** `focus.rs` whole (including its `escape` table and tests).

### keys.rs (63)
- **What it does.** Maps a `KeyEvent` to an `Intent` (Up, Down, Activate, Toggle, Escape, Search, MoveUp/Down for reorder, Remove, NextRegion `F6`, NextTab `Ctrl+PgDn`, Char). `text_input` suppresses `/`, `?`, Space.
- **Retain.** Intents decoupled from keys, text-input guard.
- **Improve.** Drive from the shared command/keybinding manifest so rebinding in Settings works.
- **Copy.** Structure only; regenerate the mapping.

### glyphs.rs (173)
- **What it does.** `Glyphs::unicode()` and `ascii()` sets (focus bar `▌`, toggles, check, radio, status dots `● ◐ ○ ✕`, tree `├─ ╰─`, rails `│ ┃`, spinner braille, meter `█ ░`), selected by `NEXUS_ASCII=1` or `TERM=linux`. Tests assert single-cell widths and ASCII purity.
- **Retain.** Paired sets with width tests. Parity doc: block elements and `☰ ◫ ⧠ ◨` are banned for icons because fonts widen them; a stray-glyph bug was never reproduced.
- **Improve.** Add a status vocabulary that is not colour-only (running, done, error, denied, waiting) for the tool line; add file-status marks (M A D ? !) and a wait glyph. Reserve ASCII equivalents.
- **Copy.** `glyphs.rs` plus its width tests.

### anim.rs (111)
- **What it does.** 100 ms hover `Blend`; `wave`/`pulse` and `shimmer_spans` (per-grapheme colour wave), constants for speed.
- **Retain.** Hover blend (colour-only), shimmer for running text, all gated by `still`.
- **Improve.** Fewer animated things (old doc lists fold animation, write-head glow, pop counters); keep shimmer for running state and nothing that costs idle CPU. Honor `NO_COLOR` and reduced motion.
- **Omit.** Fold-on-complete animation, notification counter pop, write-head glow (`transcript/fold.rs`, parts of `look.rs`).
- **Copy.** `Blend`, `shimmer_spans`.

### components/ (controls, inputs, lists, feedback, scroll, mod; about 1,400)
Inventory of public pieces: `button`, `icon_button`, `toggle`, `checkbox`, `radio_group`, `segmented`, `tabs`, `stepper`; `text_input` + `TextState`, `search_field`, `select` + `select_popup`; `list`, `ordered_list` (Alt+arrows reorder), `section` (collapsible), `badge`, `setting_row`, `kv`, `table`; `ToastStack`, `toast_layout`/`toast_stack`, `meter`, `progress`, `spinner_row`, `callout`, `empty_state`, `key_hints`, `scrollbar`, `modal`; `scrolled` (draw into an off-screen buffer then blit with hit translation); helpers `truncate` (grapheme and width aware, ends with an announced ellipsis), `put`, `put_right`, `fill`, `row_style`, `focus_bar`, `br` (brackets in mono, padding in colour).
- **Retain.** The whole set; `truncate`/`put` for correct Unicode widths; `scrolled` for variable-height lists; `br` mono rule; `callout`/`empty_state` with an action for the unavailable-reason rule.
- **Improve.** Missing for this spec: a waiting card, tool row, child card, turn header/footer, breadcrumbs, banner (callout is close), confirm dialog (modal is close), tree view (Files), and a virtualized list for long timelines. Add the states 12.2 lists (pressed, loading, error, empty).
- **Omit.** Nothing.
- **Copy.** `components/*` whole.

## 2. App chrome and dialogs (`tui/src/render/`)

### chrome.rs (1,324)
- **What it does.** Top bar: session tabs, divider, workspace crumb (`~/path branch worktree`, worktree styled by kind), notice, right buttons (notifications counter, sessions, details toggle) with 3-cell padded hit targets. One layout function produces text and hit ranges for both drawing and clicks (`top_layout_at`, `crumb_layout`, `agent_layout`). Composer controls (`control_spans`) share their layout with hit testing. Full repaint is forced when chrome geometry changes (stale glyph defence).
- **Retain.** Single layout table for draw and hit; single-cell text-presentation button glyphs (`▣ ▤ ▥`) with ASCII fallbacks; crumb truncation rules (home abbreviation only for real home descendants); `needs_full_repaint` on geometry change.
- **Improve.** The new header is fixed by INTERFACE_PLAN: left toggle, centre title/breadcrumbs, right toggle. No session tabs, no manager button. Breadcrumb for child sessions is new. Show connection state in the header or a banner row.
- **Omit.** Session tabs and `Seg::Session/Close/New`, manager toggle, update notice, working-tab spinner, vertical tab rail (parity doc "Vertical-tab overflow").
- **Copy.** Pattern from `top_layout_at` and `layout_key`/`needs_full_repaint` (`render.rs`); glyph choices.

### context.rs (517) and `transcript/header.rs` (168)
- **What it does.** The old "context header" at the top of a conversation: closed one-row form (`Tools [12]`, `Skills [2 3]`, token figure) and open inventories in columns (tools columns = width/20 clamped 1..5, skills 2 columns from width 40), previews of the first line of System prompt and AGENTS.md, an estimated-tokens footer ("~N tokens", `?` when unknown), greyed-out empty blocks. Each heading opens a dialog with the full text.
- **Retain.** The idea that a session opens on its starting context with a count per block, a one-line preview and the full text one action away. Muted/quiet (never agent colour) header styling. Empty blocks greyed rather than hidden.
- **Improve.** This is the New-session view and frozen-first-item of INTERFACE_PLAN, and it must be real data from Core (draft endpoint), editable before sending, with a per-tool switch. Token counts must be labelled estimate vs exact. Strike-through or an "off" mark for disabled tools (old code filtered them out, a listed non-goal).
- **Omit.** Host-owned `context_show` dialog plumbing, `~` token prefix handling, MEMORY.md block.
- **Copy.** `columns()`, `clipped()`, `grouped()` number formatting, total-row percentage-of-window idea.

### sessions.rs (1,710) and `picker.rs` (1,043)
- **What they do.** Sessions dashboard: panel up to 120 wide, search row, list grouped by status with purple headings, one line per session (status glyph, name, last-used), selected row a full-width accent band, bottom row of real shortcuts. `picker.rs` holds only presentation state (query, selection, scroll, rename, archive confirmation) and returns operations; keys `/` filter, `r` rename, `d` archive, `n` new; collapsible groups; `waited()`/`clock()` for "waiting on you · mm:ss". The same dashboard renders inside the sidebar's Sessions tab.
- **Retain.** State-vs-operation split, status grouping, per-group collapse, waiting clock, hint row built from the real key table, selection preserved across list refresh (`reconcile`).
- **Improve.** Group by worktree (not status) with status as a glyph; scope toggle; Archived filter; child count and handoff chain; search backed by Core's index. Spinner glyph must not shift row layout (old doc states frames are one cell wide).
- **Omit.** Manager pinned entry and band (`manager_band`), external-worker rows, project chips.
- **Copy.** `picker::{waited, clock}`, filter/selection/reconcile logic, `hint_line`, `fit`.

### dialogs.rs (1,519)
- **What it does.** `dialog_frame` (rounded border, bold title top-left, `[×]` top-right, dialog background), `dim_backdrop` (blend every cell 50% toward the page background and set DIM where colours cannot blend), completion popup anchored to the real composer position, panel placement (modal/drawer/page), and the **permission/question prompt card** docked above the composer: title row `◆ Allow …?` in warning colour, `waiting on you · mm:ss`, "What happens" (clipped with `… N more`), "Why it asked", then choice rows with a key chip (bold on `element`, no colour cue), `▸` marking the keyboard selection, and disabled choices labelled `(unavailable)`. Also a usage meter (`━ ─ ┃`).
- **Retain.** The prompt card is the closest thing to the new waiting card and already matches "docked above the composer", "disabled with reason, never hidden", and a visible wait clock. Backdrop dim for modals. Anchored completion.
- **Improve.** One component for approvals and questions with multi-select and free text; count and cycle across sessions; "allow for this session" and "deny with note" (needs Core, matrix W6/W7); show the cancelled state in the timeline. Full command, not a 4-row summary, for bash.
- **Omit.** Provider usage meter, Grok-specific modal parity, manager launch confirmation (`what_rows`).
- **Copy.** `dialog_frame`, `dim_backdrop`, `close_rect`, `prompt_rows`/`prompt_choice_line` structure, `completion_area`/`completion_at`.

### toasts.rs (264) and `notifications.rs` (483) with `widgets/components/feedback.rs`
- **What they do.** `ToastStack`: ids from the producer (shown once), per-level lifetimes (info 4 s, success 3, warning 8, error 12), timer pause while hovered, same-key merge within 2 s (`×2`), at most 3 visible plus `+N more`, floats top-right and never reflows, faint countdown hairline, click `×` or the action. Notifications: client-side history (50, newest first), unread counter in the top bar, a 46-cell drawer with level glyph, text and relative age (`now`, `12s`, `4m`, `3h`, `2d`).
- **Retain.** All of it, including the "dismissed toast is never lost" rule (the history).
- **Improve.** The new spec adds banners (persistent state), confirm and blocking alerts as one system, and links from a notification to its session. Esc does not dismiss toasts (old rule; keep). Persist the drawer history across a client restart if cheap.
- **Omit.** Python-assigned toast ids, Logs-tab mirroring, the counter "pop" animation.
- **Copy.** `feedback.rs` `ToastStack` and `toast_layout` (shared by drawing and hit testing), `notifications.rs` age formatting and drawer layout.

### appearance.rs (148), components.rs (577)
- **What they do.** `appearance.rs`: local checkboxes (animation, monochrome) with Alt+A / Alt+M. `components.rs`: presentation-only hover transitions for surfaces (100 ms, colour only, redraw only while animating, cleared on any layout or generation change).
- **Retain.** Hover never changes focus, geometry or selection; redraw requested only while a transition is pending (idle CPU).
- **Improve.** Appearance becomes a Settings page section (theme, density) driven by the token file.
- **Omit.** `appearance.rs` as is.
- **Copy.** `components.rs` hover-surface tracker idea (the widget kit's `Blend` already covers most of it).

### manager.rs (343)
- Old workspace-manager board (managed roots, `+N more · /sessions`). **Omit entirely**: Nexus 0.10 has no manager session; orchestration is the `orchestrate` agent with child sessions.

## 3. Sidebars

### sidebar.rs (2,844)
- **What it does.** Left sidebar with tabs Projects, Sessions, Files, Git (wrapped tab labels; `tab_cells` is used by painting and hit testing). Width 24..60 (default 34), draggable divider, hidden when the conversation would fall under 56 columns. Files tab: one directory at a time, folders first, `..` row, double-click or Enter previews a regular file in a read-only modal. Git tab: labelled status by section. Focus is never stolen on open; Esc or a click elsewhere returns it. Current state per the parity doc: Projects and Sessions became a fixed selector overlay and Files/Git moved to the details drawer.
- **Retain.** Wrapped tab layout shared by paint and hit; width bounds and "hide before squeezing the conversation"; divider drag with the new width shown at once; not stealing focus on open; stale-answer guards (every async answer carries a sequence and the workspace asked for).
- **Improve.** Tabs become Sessions, Files, Projects with icons and hover/focus labels (accessible names), plus a fixed Settings entry at the bottom. Files is a lazy tree with git markers and an editor opener, not a modal preview. Drawer mode at narrow widths. Persist open/tab/width per client.
- **Omit.** The Git tab (git status goes to the right sidebar), the host-backed directory picker modal, project chips.
- **Copy.** `clamp_width`/`width_for`/`max_width`/`width_at`, `tab_cells`, divider drag, key table for Tab/arrows/Enter/Left/Backspace.

### details_sidebar.rs (1,355)
- **What it does.** Right sidebar with tabs Session, Changes, MCP, Jobs, Logs; docked from 100 columns, otherwise a drawer; width 24..60 (default 40); `geometry()` is a pure function from which paint and hit-testing both derive; copy-id row; scrolling; file rows expand to diffs.
- **Retain.** Pure `geometry` function; drawer below 100; labelled metadata with an explicit unavailable state; empty status stated.
- **Improve.** Only two sections now: Session info and Git status. Session info content is listed in INTERFACE_PLAN and mapped in the matrix (R1-R12). No tab strip is needed for two sections, so use headed sections.
- **Omit.** MCP, Jobs, Logs tabs for now (INTERFACE_PLAN defers them), polling gates, `details_visible` reporting.
- **Copy.** `geometry`, width logic, copy-id hit.

### Sticky and jump (`sticky.rs` 262, `jump.rs` 42)
- Sticky: while a turn is on screen but its user card has scrolled off, up to 3 rows are pinned at the top with a count of rows above; a click scrolls back. Jump: pill on the last row, `↓ N more lines · Ctrl+End to jump`, switching to `↓ New messages` when content arrived while away.
- **Retain.** Both. They implement "follow only at the bottom" and "indicate new content below".
- **Improve.** Pin by turn number; keep the anchor stable while loading older turns.
- **Copy.** `jump.rs` whole, `sticky.rs::select`.

## 4. Settings (`tui/src/settings_page/`, about 3,700)
- **What it does.** One-page Settings: the host sent a typed page (`Heading`, `Note`, `Gap`, `Row` with a control, `Tabs`, `Ordered`, `Buttons`, `Section`, `Table`, `Progress`, `Callout`); Rust renders it with the widget kit and owns focus, scroll, select popups, text editing and section open state. An unknown block is shown as a visible warning, never dropped. Every change is one operation; the page is rebuilt after it. `root.rs`: an area list with search and tips. Keys: arrows move, Left/Right change a segmented control, Space/Enter toggle, `Ctrl+PgUp/PgDn` switch tab, Esc closes popup then Settings, `Alt+1..9` jump to an area, `?` opens the keyboard page.
- **Retain.** Typed blocks, unknown-block-as-note, bounded text, popups owned by the client, `setting_row` with label/description/scope badge/error, `Alt+digit` area jumps.
- **Improve.** Build the page model from the shared settings manifest (stable ids, order Appearance → … → About, enabled conditions, unavailable reasons) so NSH renders the same manifest. Rows backed by Core state apply through the API and show the server's result; view-only rows live in client config. Add the sections old Nexus lacked (Context, Dictation, Notifications, About) and drop Voice/GPT Live, Layout. Settings search was "not done" in old Nexus; add it.
- **Omit.** `grok.rs` (752), GPT Live, external workers, subagent tiers, quota routing (`transcript/route.rs`), scope tabs except where Skills/Tools differ per project.
- **Copy.** `model.rs` block types, `draw.rs` flow-into-offscreen-buffer layout, `input.rs` focus and popup handling.

## 5. Timeline rendering (`tui/src/transcript*.rs`, `transcript/`, about 5,000)
This is the "context" rendering. It was not on the 12.1 list by name but is the core of the TUI, so it is recorded here.
- **What it does.** `transcript/groups.rs`: runs of tool calls fold into a quiet verb-group row (`▸ Ran 2 commands, Read 3 files · 1.8s`, `▾ Working` while running with the last 5 members as a grey tree). `thought.rs`: `▾ Thinking… (2.4s)` running, `▸ Thought for 3.2s` finished, dim rail. `tools.rs`: a tool row shows what it did, no status word; failures red, running bright, rest quiet; expanded shows a "signal" view (`signal.rs` typed rows: code, out, add, path, kv, md, error, clip) falling back to full detail. `notices.rs`: dismissable notices vs non-dismissable durable context rows. `look.rs`: shared glyphs, time bands (`11.6s`, `11m 40s`, `1h 4m`), live-span marker recoloured by the draw loop. `fold.rs`: fold animation. Turn clocks use the send time.
- **Retain.** Quiet-by-default rows, red only for failure, neutral rails, rows built once and cached with only live spans recoloured per frame, the duration format bands, durable-context vs dismissable-notice split (matches "timeline notices are not alerts"), "an announced omission" row kind (`clip`).
- **Improve.** INTERFACE_PLAN wants one collapsed line per tool call with status icon, name, key argument, summary and duration (not verb groups), per-tool expanded details, a raw toggle, and separate labels for UI preview cap vs model clip. Verb groups hide individual calls; if kept at all they must be an optional fold, never the default. Show every model-visible item (system prompt, notices, handoffs) and keep execution diagnostics (retries, wire) visibly separate. Turn footer is richer here (model, effort, tokens, cost, end reason).
- **Omit.** Verb-group grouping as the default, fold-on-complete animation, quota-aware routing row, write-head glow, agent-colour identity, kernel rows tied to old code-mode, Mermaid art.
- **Copy.** `look.rs` time formatting and live-span mechanism, `signal.rs` row kinds for expanded details, `notices.rs` classification, `thought.rs` row shape.

## 6. Markdown (`tui/src/markdown.rs`, 1,230)
- **What it does.** pulldown-cmark events to styled `Row`s with `ratatui-markdown` 0.3.6 for code blocks; custom table fitting (`fit_columns`, `longest_words`, `wrap_cell`) so tables fit the width and wrap cells; hanging-indent lists; quote bar; fence tracking (`fence_sources`, `fence_is_closed`) so copy buttons can copy a fence's literal source; control-character sanitizing (`terminal_safe`); Mermaid rendered as art (omit).
- **Retain.** Own table layout, hanging indents, `terminal_safe`, copy of the literal fence source, URLs kept visible, HTML treated literally.
- **Improve.** New requirements: unfinished fence or table renders as plain text until it closes (the old code only detects a closed fence for labelling, it does not defer tables); OSC 8 hyperlinks (none found in old code: `grep` for OSC 8 found only OSC 52); task lists; `src/x.py:42` path links; language label and syntax highlighting parity; stable layout while streaming. Pin the `ratatui-markdown` version decision again (old pin `=0.3.6` with `default-features = false`).
- **Omit.** Mermaid (`mermaid*.rs`, about 7,000 lines), `ondine`/`resvg` dependencies.
- **Copy.** `fit_columns`/`wrap_cell`/`longest_words`, `fence_sources`, `terminal_safe`.

## 7. Composer editor (`tui/src/editor.rs`, 812; `input.rs` 368)
- **What it does.** Bounded grapheme editor: selection, undo/redo (8 MiB cap), history, word moves, wrapped vertical movement, point-to-cursor mapping for the mouse, and **paste collapse** (200 chars or 3 lines becomes `[Pasted ~N tokens]` and expands on take; pasting the same content again expands in place). `input.rs` has the OSC 52 base64 writer used for copy over SSH.
- **Retain.** All; paste marker semantics map to U3/K12. OSC 52 copy.
- **Improve.** `@path` chips and attachment chips as atomic units (the layout already treats markers as chips), steer/queue send keys, draft per session, IME and bracketed paste verification (old doc: not verified).
- **Omit.** Voice/GPT Live bar (`live_*`).
- **Note.** This is the composer editor only. The built-in file editor is a different component (`ratatui-code-editor`, per INTERFACE_PLAN); nothing in old Nexus implements it (old Files tab previews read-only).
- **Copy.** `editor.rs` whole, `input.rs::base64`.

## 8. Other pieces worth taking
- `copy_button.rs` (231): hover-revealed Copy on user cards and fences; every row of a block carries the same marker operation so focus groups do not lose the inner action. Copy the approach for M5.
- `graphics.rs` (257): probes `/dev/tty` once at startup for Kitty graphics, cell pixel size and Sixel, with an env override and tmux handling, for `ratatui-image`. Copy for U4. Old code needed this because stdin/stdout were bridge pipes; the new TUI owns the terminal, so the probe can use the normal tty.
- `disclosure.rs` (791): bounded LRU of per-session folding choices. Use the idea for fold state per session.
- `panic_log.rs` (117): panic hook that restores the terminal. Copy for 12.3 item 4.
- `splash.rs`, `live_bar.rs`, `live_wave.rs`, `live_anim.rs`, `mermaid*.rs`, `bridge.rs`, `config.rs`, `local_ui.rs`, `trace.rs`, `session.rs`: omit (host-bridge or feature-specific).

## 9. Parity doc (`docs/ratatui-parity.md`, 2,612) takeaways
Retain as design rules:
1. Painting and hit-testing share one layout table, per component.
2. Hover is presentation only (100 ms colour blend; never focus, geometry or selection).
3. Stale async answers are dropped by sequence and by the workspace/session they were asked for.
4. No toast on Esc; every toast is also logged.
5. A "resize/layout change forces a full repaint" guard for stale glyphs.
6. Icons are single-cell text-presentation glyphs with ASCII twins; block elements and some symbols are banned.
7. Expanded user messages bound their preview (16,000 display chars, 200 lines) and announce the omission with a total. Matches the "UI preview limit is not a model limit" rule.
8. Durable context rows cannot be dismissed; only diagnostic notices can.
9. Turn clocks come from durable timestamps, absent facts omit the clock rather than inventing it.

Improve or avoid:
- The doc is a ledger of bridge/host work and many "not run" caveats; none of its native test claims can be reused without rerunning.
- Several features were later removed (left sessions sidebar, then reintroduced as an overlay; worktree modes removed; GPUI desktop removed). Do not adopt a layout from the doc without checking `sidebar.rs` for its current state.
- Open visual issues it records: stray glyphs after closing the sidebar (not reproduced), live-card polish unreviewed, no screenshot approval. Re-review in the new app.

## 10. Attribution and ledger notes for later steps
- When copying, put `Adapted from old Nexus (nexus01) rust/<path>` in the new file header.
- Behavior changes to log in docs/porting-ledger.md when they land: tools as one line per call (no verb groups by default); sessions grouped by worktree not status; Settings built from a shared manifest, not host-sent pages; copy toasts to a durable drawer; markdown streaming defers unfinished tables.
- Dependencies seen in `tui/Cargo.toml` worth re-pinning after a fresh check: `ratatui 0.29`, `crossterm 0.28` (feature `use-dev-tty`), `pulldown-cmark 0.13`, `ratatui-markdown =0.3.6`, `ratatui-image 8`, `unicode-segmentation 1`, `unicode-width 0.2`, `base64 0.22`, `image 0.25`.
