# Plan 17: Toggle to show session tabs vertically on the left (Ratatui)

Status: proposed (2026-10-25). Nothing in this plan is implemented. Source: root `TODO.md` item 10,
quoted unchanged:

> - a toggle to show tabs as vertical on the left sidebar(new)

## 1. Intent

A preference switches the open-session tabs from the top bar to a vertical list docked on the
left. Off (default) keeps today's top tabs.

Assumption A1: "(new)" marks this as a new feature request. It could also mean "the new left
sidebar"; either way the tabs go in a docked left column, which is what this plan builds.

Assumption A2: the vertical tabs list the **same tabs** as the top strip (open sessions of this
client), not all sessions (that is the Sessions selector).

### Non-goals

- Not a replacement for the Sessions selector overlay (`sidebar::selector_area`).
- No host change: tabs are client state (`shell.tabs`, `nexus/ui/ratatui/actions.py:398-400`).
- Desktop paused; web deprecated.

## 2. Current state (checked 2026-10-25)

- Top tabs: drawn and hit-tested from one layout, `top_layout` → `tab_cells`
  (`rust/tui/src/render/chrome.rs:404-427`); behaviour in `docs/ratatui-parity.md:942-950`
  (status glyph, title ≤ 24 columns, `×`, `+`, overflow keeps the active tab).
- Left sidebar today is an overlay selector, not a docked column; the docked-column code paths
  (`render::regions`, stored `sidebar_width`, divider drag) still exist but "do not change the
  overlay" (`docs/ratatui-parity.md:2225-2228`, `:2246-2258`).
- Preferences: `nexus/ui/ratatui/preferences.py:16-19` (typed defaults, validated `set`), Settings >
  Layout toggles: `nexus/ui/ratatui/settings_pages/layout.py:8-12`, snapshot field example
  `centered_layout` (`nexus/ui/ratatui/prototype.py:1364`).
- Region layout: `render::regions` (`rust/tui/src/render.rs:1798-1860`) carves details sidebar,
  centred layout, top bar, project strip, composer, Live pane.

## 3. Design

### 3.1 Preference and toggles

- New preference `tabs_vertical: bool = False` in `Preferences.DEFAULTS`; sent in the snapshot as
  `tabs_vertical`.
- Settings > Layout row `("tabs_vertical", "Vertical tabs", "Show open sessions as a list on the left instead of tabs in the top bar.")`.
- Quick toggle: a chat command `/tabs vertical|top` (registered in `nexus/ui/cli/commands.py` so
  `Ctrl+X ?`/palette list it). A leader key is optional (Q1); free leader letters exist (see
  `nexus/ui_support/shortcuts.py:35-56`).

### 3.2 Layout

- When `tabs_vertical` is on, `regions` reserves a left column `tabs_rail` of width
  `clamp(stored tabs_width, 16, 32)` (default 22) across the top bar, transcript and composer rows,
  before the centred-layout calculation. Hide it automatically when the conversation would drop
  below 56 columns (the same bound the docked sidebar used, `docs/ratatui-parity.md:2246-2249`);
  then fall back to top tabs for that frame.
- Top bar in vertical mode: no session tabs; title of the current session on the left; the right
  buttons unchanged.

### 3.3 Drawing (new `rust/tui/src/render/tabs_rail.rs`)

```
 Sessions          +
 ● fix-flaky-sync  ×     ← current: bold, panel background
 ◐ docs-pass       ×     ← working: spinner glyph
 ○ release-0-7-1   ×
```

- One row per tab: status glyph (same glyphs as the top tabs), title truncated with `…`, `×` on
  hover/selection; `+` in the heading starts a new session; current tab bold on the panel colour.
- Overflow: scrolls, keeping the current tab visible; wheel scrolls the rail.
- One layout function used by drawing and hit-testing (`TabHit::Session(i)`, `Close(i)`, `New` reused).
- `NEXUS_ASCII=1` fallbacks as in `chrome.rs`.

### 3.4 Interactions with other features

- Sessions selector overlay still opens over everything.
- Subagent page (Plan 12) has no tabs: the rail is hidden there; the back rail takes columns 0–1.
- Manager screen (Plan 10): the manager is not a tab; the rail stays and shows no row for it.
- Auto-close of empty tabs (`docs/ratatui-parity.md:952-959`) applies unchanged.

## 4. Implementation steps

1. Python preference + Layout row + `/tabs` command + snapshot field.
2. Rust `regions` + `tabs_rail.rs` drawing + hit-testing + top bar variant.
3. Docs: `docs/ratatui-parity.md` (tab row section `:942-950`, Layout settings), `docs/cli.md`
   for `/tabs`.

## 5. Tests and acceptance

- Python: preference default/validation; Layout toggle; `/tabs` command; snapshot field.
- Rust: regions with/without the rail; auto-fallback on narrow terminals; rail rows, current tab
  style, `×` and `+` hits match drawing; overflow keeps the current tab visible.
- PTY: toggle in Settings, click a vertical tab to switch, `×` closes it.
- Acceptance:
  - [ ] Settings > Layout and `/tabs` switch between top and vertical tabs, saved in `tui.json`.
  - [ ] Vertical tabs support switch, close and new exactly like top tabs.
  - [ ] Narrow terminals fall back to top tabs without clipping.

## 6. Open questions

- Q1. A leader shortcut for the toggle? (e.g. `Ctrl+X h`; check it is free at implementation time.)
- Q2. Should the rail width be draggable like the old docked sidebar? Default: fixed width.
