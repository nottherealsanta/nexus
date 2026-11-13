# 45. Everything is a tab: editor groups and splits

User request (2026-11-13): files and terminals open as tabs; tabs can be dragged and the area split and reshaped
"very similar to how VS Code works". Terminal-friendly controls: split down, split right, join down, join right. Desktop
and web only (TUI and NSH desktop/web, iPad included); the phone client is unchanged.

## Decisions (user, 2026-11-13)

| Question | Answer |
| --- | --- |
| What is a tab? | Everything in the centre: session timelines (they are already tabs), files, diffs, terminals, the browser resource when it ships. The inspector stays its own web page. |
| Do files and terminals share groups? | Yes. One tab area; the bottom terminal dock goes away. |
| Join down / join right | Merge the focused group into its neighbour below / to the right: its tabs move there (appended, the moved active tab stays active) and the split closes. |
| Drag in the TUI | Yes: mouse drag of tabs and dividers in Ghostty, as well as keyboard actions. |
| Nesting / limits | Splits nest freely; a split that would leave a pane below the minimum is refused with a toast saying why (TUI about 20 × 5 cells). |
| Where layout lives | Client-local (TUI prefs, NSH Storage), per project worktree. Not shared through Core. |
| Editor behaviour | One shared buffer when a file is open in two groups; closing a tab with unsaved changes asks; no preview (italic) tabs. |
| iPad | Full behaviour (it runs the desktop/web client). |

This reverses the plan 9 editor decision "no tabs … Files and diffs open in a centred modal"; that line in
[09](09-backlog.md) now points here.

## Today

- Session tabs: one header strip (`nexus-nsh/src/ui/header.tsx` "Session tabs", fixed 208 × 28 tab box; TUI
  `render.rs` tab strip). One session is on the canvas at a time.
- Files and diffs: centred editable modal (TUI `draw_file_edit`/`file_edit_key`, NSH `editor.tsx`).
- Terminals: bottom dock with its own tab strip, toggled by Ctrl+\` ([docs/terminals.md](../docs/terminals.md);
  TUI `layout.rs` `dock_height`, NSH `ui/terminal.tsx`).

## Model (shared by both clients)

- **Layout** is a tree: a *split* node has a direction (row / column), children and ratios; a leaf is a *group* with
  ordered tabs, an active tab and an id. One group is focused. The tree sits between the two sidebars and replaces the
  header session strip, the file modal and the terminal dock.
- **Tab kinds:** `session` (timeline + its composer), `file` (editor, with the markdown/Mermaid preview toggle it has
  today), `diff`, `terminal`, `browser`. A tab is identified by kind + resource id, so the same file or session can
  appear in two groups and share one buffer / one live state.
- **Opening** goes to the focused group; if the tab already exists there it is activated. "Open to the side" (Ctrl/Cmd
  + click, or the action) opens in the group to the right, creating it if needed.
- **Split right / split down** on the focused group: a file, diff or session tab is shown in a new group beside it
  (same buffer); a terminal tab creates a **new terminal** in the new group (VS Code's terminal split), because one
  PTY's control and size cannot be shared by two views.
- **Join down / join right** as decided above; disabled (and not shown in the menu) when there is no neighbour that way.
  Closing the last tab of a group removes the group and gives its space to its sibling.
- **Drag:** within a strip reorders; onto another strip moves (Alt/Option copies); onto a group body shows five drop
  zones (left, right, top, bottom → split; centre → move into that group); dividers resize. Esc cancels a drag.
- **Terminal behaviour** keeps the [terminals](../docs/terminals.md) contract (control, read-only banner, running-close
  prompt, detach vs terminate). Hiding no longer exists: closing a terminal tab terminates (with the prompt when
  running); Ctrl+\` focuses the most recent terminal tab, or opens a new one in a bottom split of the focused group if
  none exists. The terminal activity indicator does the same.
- **Unsaved files:** the tab shows a dot; closing it, or closing a group that holds it, asks Save / Discard / Cancel.
- **Restore:** on start, a client rebuilds the saved layout for the worktree. Tabs whose resource is gone (closed
  session, deleted file, terminated terminal) are dropped quietly; empty groups collapse. A fresh worktree gets one
  group with its sessions.
- **Narrow widths:** below the width that fits the layout's minimums, the client shows only the focused group, with a
  marker in its strip that other groups are hidden; the saved layout is kept, not rewritten.
- **The model sees none of this.** Tabs, splits and opened files are UI state only; the timeline rule is unchanged.

## Controls

Actions live in `interface/manifest.toml` (one entry each, both clients generated from it), in the per-group menu (`⋯`
at the strip's right end) and in the command palette:

| Action | Leader (Ctrl+X then) | Menu |
| --- | --- | --- |
| Split right | `\` | ✓ |
| Split down | `-` | ✓ |
| Join right | `shift+right` | ✓ (when possible) |
| Join down | `shift+down` | ✓ (when possible) |
| Focus group left / right / up / down | `left` `right` `up` `down` | — |
| Move tab to group left / right / up / down | `alt+arrow` | — |
| Close tab | `w` | ✓ |
| Next / previous tab in group | Ctrl+Tab / Ctrl+Shift+Tab (existing session-tab keys, if bound) | — |

Leader keys were checked against the taken set; the final choice is recorded at the Work item below (`.` for the group menu is new).
manifest entries are written; the table is the proposal.

## Work

- [x] **Manifest and docs first (2026-11-13).** Add the layout section, tab kinds, actions, keys and strings to
  `interface/manifest.toml`; run `check_manifest.py`. Write a `docs/layout.md` (or a section of the matching doc) for
  the model above and rewrite the client-behaviour part of `docs/terminals.md`. Update 9's editor line.
  Landed: `layout.*` commands, `[navigation.layout]`, vocabulary terms `layout.*` and [docs/layout.md](../docs/layout.md);
  terminals.md rewritten; NSH `src/gen/manifest.ts` regenerated. **Final leader keys (Ctrl+X then):** `\` split right,
  `-` split down, `shift+right` / `shift+down` join, `left` `right` `up` `down` focus, `alt+left` `alt+right` `alt+up`
  `alt+down` move tab, `w` close tab, `.` group menu; open to the side has no key (Ctrl/Cmd+click). None clashed with
  the taken set. Alt+X, Alt+[ and Alt+] stay on `session.tab_*` and act on the focused group. **Strings** are parked in
  `[layout.strings_pending]` because `check_manifest.py` rejects unused `[strings]` keys: each client moves keys into
  `[strings]` when it first uses them. **Superseded, still present** (client agents remove with the modal/dock):
  `[navigation.panel]` and its checker rule, `terminal.close`, `terminal.focus_list` list semantics, the `terminal.open`
  dock toggle behaviour.
- [x] **TUI layout core (2026-11-13).** `nexus-tui/src/groups.rs` (`layout.rs` is the existing screen geometry, so the
  tree has its own module): tree, split/join/move/close, drops, ratio clamping with the 20 x 5 minimum, focus by
  geometry, narrow collapse, restore; `prefs.rs` `layouts` keyed by project + worktree. 33 unit tests, one per operation
  including refusal at the minimum and restore with missing and damaged layouts.
- [x] **TUI rendering and input (2026-11-13).** Header strip, file/diff modal and terminal dock are gone; group strips
  and bodies (`render/groups_ui.rs`), glue in `app/tabs.rs`; leader keys, `⋯` menu (with **New terminal**), Ctrl+click
  and **Open to the side**, tab and divider drag with a reverse-video drop zone, Esc cancel. 18 new integration tests
  (`tests/layout_tabs.rs`) and the old dock/modal tests rewritten. **Differences from NSH, for the integrator:** every drawn terminal tab streams
  (one attachment and stream task each; only the focused one has control and input), matching NSH. An unfocused
  *session* tab shows its own read-only timeline (per-session pinned views, `Watch` history loads; the focused group
  alone has the composer; in-flight tool scratch is not carried). Layouts re-key when the viewed session's worktree
  changes. Esc no longer closes a file (matches NSH); the unsaved prompt covers a single tab (no group close exists
  in the TUI, so `layout.unsaved.group` and `layout.restore.dropped` are unused there). **Not verified:** real Ghostty mouse drag
  (only synthetic events in tests), paired TUI/NSH screenshots.
- [x] **NSH layout core (2026-11-13).** `nexus-nsh/src/layout.ts` (pure tree: split / join / move / close / resize with
  a 200 x 120 px minimum, focus by geometry, narrow collapse, serialise / parse / restore) and `src/layout-state.ts`
  (signals, `Storage` key `nexus.layout.<project>.<worktree or "main">`, reconciliation with sessions, terminals and
  files, every action). 38 unit tests in `layout.test.ts`; the minimum is recorded in [docs/layout.md](../docs/layout.md).
- [x] **NSH rendering and input (2026-11-13).** `src/ui/groups.tsx`: group strips on the existing tab box, bodies,
  dividers, the `⋯` menu, tab drag with drop zones and a highlight, Esc cancel, `layout.*` commands and leader keys,
  Ctrl+` and the shell indicator. Terminals live in one flat layer keyed by terminal id, so moving a terminal tab
  between groups keeps its mounted WebView (by construction; not measured in a browser). The header strip, the file
  modal and the dock are gone. Tests: `layout-ui.test.tsx` (13). **Unverified:** the leader's arrow keys
  (`left` ... `shift+right`) did not reach the keymap in the headless run (`\` and `-` do), Alt-drag copy, Esc during
  a drag, Cmd/Ctrl+click read from the pointer's last mouse move, real-Chrome drag by hand.
- [x] **Editor buffers, NSH half (2026-11-13).** One buffer per file in `store.ts` (`fileBuffers`), shared by every
  tab of it; dirty dot on the tab; closing a dirty tab asks Save / Discard / Cancel (the group variant,
  `closeGroupTabs`, exists but no control reaches it). The TUI half is its own item above.
- [x] **Phone (NSH).** `Centre` renders the unchanged phone conversation when `phone` (no groups, no strips, no
  layout code runs: `installLayout` is skipped); phone tests below.
- [x] **Several sessions at once, NSH (2026-11-13, user: "multiple sessions should be available to see at the same
  time").** NSH showed an "Open session" card for a session tab in an unfocused group; it now shows that session's live
  read-only timeline, as the TUI does (`PeerTimeline`, `Model.pin` / `withView`, `Sync.watch`). Tests: a two-group
  restore in `layout-ui.test.tsx`, the pin/cache test in `App.test.tsx`. The phone plan-45 test was fixed to check the
  tabs on the list screen (the conversation page has no tab bar).
- [x] **A composer in every session group; the highlight follows an opened session (2026-11-13, user).** Both clients
  draw each unfocused session group's own composer (its draft, agent, model, effort); pressing it focuses the group and
  the live composer. NSH: opening a session did not move focus to its group because `rootNow` was a `computed` that
  never re-ran (it read the model without `m()`); fixed, and opening the session already in view also brings its group
  forward (`sessionOpened`). TUI: `open_session` now focuses the group already showing the session instead of adding a
  second tab in the focused group. Tests: 8 in `layout-ui.test.tsx` ("several sessions side by side"), 5 in
  `tests/layout_tabs.rs`.
- [ ] **Verification.** TUI `cargo test`, NSH focused tests (`node scripts/nsh-web.mjs test --target web …`), manifest
  check. Paired TUI / NSH screenshots (both skills) for: one group; split right; split down; nested 2 × 2 with a
  terminal; drag in progress with drop zones; join; narrow collapse; unsaved-close prompt. Drag tested by hand in real
  Ghostty and NSH web.
- [ ] Tick here, add the [landed.md](landed.md) line, move to [done/](done/).

## Open details (defaults applied unless the user says otherwise)

- The leader keys in the table above.
- Alt/Option-drag copies a tab instead of moving it.
- Narrow-width behaviour: show the focused group only.
