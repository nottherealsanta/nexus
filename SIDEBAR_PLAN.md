# Sessions sidebar and top bar for `nexus chat`

Status: implemented (2026-09-27). Scope: the Textual shell only. The browser
client (`nexus web`) already has a sessions sidebar and a topbar with a sidebar
toggle (`nexus/ui/web/index.html` `.sidebar` / `.topbar`). This plan brings the
terminal shell to the same shape.

## Goals

1. The left sidebar lists **all sessions the way `/sessions` does**: grouped by
   day (`Today`, `Yesterday`, `Sat Sep 26 2026`, …), one compact line per
   session with a status glyph, title, and status label, current session
   highlighted, searchable. Archived sessions follow under their own `Archived`
   group.
2. A **top nav bar** spans the shell. It has a left sidebar toggle, the
   session title, the live session status,
   and right-side actions (new session, details-sidebar toggle).
3. The toggle works at **every width**. When the docked sidebar fits, the toggle
   flips the persisted `sessions_sidebar` preference. When it does not fit, the
   toggle opens the sidebar as a temporary overlay over the chat column, which
   closes on Escape, on picking a session, or on toggling again.

Non-goals: no host protocol changes (the sidebar keeps using `SessionList`,
`SessionListArchived`, `SessionDelete`/`Restore`, `SessionUnarchive`), no web
changes, no change to the `/sessions` dialog itself (it stays as the
keyboard-first modal on `ctrl+o`).

## Current state (before)

- `ui_support/tui_panels.py:SessionSidebar` renders brand + workspace, a
  3-row "New session" button, a filter, and 3-row cards (`title` over
  `status · 2m ago`) in a flat list. The `/sessions` dialog
  (`SessionsScreen`) groups by day with one line per session. The two views
  disagree on density and grouping.
- There is no top bar in the TUI. The sidebar is toggled only with `ctrl+b`,
  and at widths under 110 (170 with the details sidebar) it is force-hidden,
  so `ctrl+b` silently does nothing there.

## Design

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ▌  Give the login flow a quick visual pass                      Idle  + ▐ │ top bar
├───────────────────────┬──────────────────────────────────────┬───────────────┤
│ + New session  ctrl+n │                                      │ SESSION       │
│ Filter sessions       │   conversation timeline              │ …             │
│ SESSIONS  6           │                                      │               │
│ Today                 │                                      │               │
│ ● Give the login flo… │                                      │               │
│ ⠹ Fix flaky daemon  W…│                                      │               │
│ ● Add MCP health  In… │                                      │               │
│ Sat Sep 26 2026       │                                      │               │
│ · Refactor provider…  │   composer                           │               │
│ ↵ open · del · ctrl+z │                                      │               │
└───────────────────────┴──────────────────────────────────────┴───────────────┘
```

### Top bar — `ui_support/tui_panels.py:TopBar`

- `Horizontal(id="top-bar")`, one row plus a bottom rule, `$nx-panel` background, above
  `MainLayout` in `NexusTextualApp.compose`.
- Children: `#topbar-sidebar-toggle` (`▌`, accent when the sidebar is visible),
  `#topbar-crumb` (the session title, ellipsized, `1fr`), `#topbar-status` (`⠋ Working`, `● Needs input`, `Idle`),
  `#topbar-new` (`+`), `#topbar-details-toggle` (`▐`).
- Pure presentation. It posts `TopBar.SidebarToggled`, `TopBar.DetailsToggled`,
  `TopBar.NewRequested`; the app owns every action. Tooltips name the key
  (`ctrl+b`, `ctrl+l`, `ctrl+n`).
- `set_session(title, status)`,
  `set_toggles(sessions=…, details=…)`.
- The brand and workspace lines are removed from the sidebar, which gives rows
  back to the list. The top bar shows neither.

### Sidebar — `SessionSidebar` / `SessionRow`

- `SessionRow` becomes one line: glyph (2) · title (`1fr`, ellipsized) · status
  label or relative time (auto, dim) · delete `×` (2). Classes (`-working`,
  `-input`, `-done`, `-archived`, `-active`), the spinner, keyboard handling
  (↵/space open, del delete, ctrl+z undo, ↑↓/jk move) and click targets stay.
- Day headers are non-focusable `Static.session-day` rows using the same
  `_day_label` helper as `SessionsScreen`, so both views group identically.
  Archived rows follow under an `Archived` header.
- Reconciliation keeps rows keyed by session id (no remount, so focus and
  spinner timers survive a poll); day headers are rebuilt each render and
  placed before the first row of their group.
- "New session" shrinks to one row. Escape inside the sidebar posts
  `SessionSidebar.Dismissed`.

### Toggle and overlay — `ui/tui/panels.py`

- `_sessions_docked_fits()` = `width >= (170 if details else 110)` (unchanged
  thresholds).
- `_sync_panels`: docked → visible iff `prefs["sessions_sidebar"]`; not docked →
  visible iff the in-memory `_sessions_overlay` flag, with class `-overlay`
  (`position: absolute`, `layer: overlay`, full height, shadowed edge).
- `action_toggle_sessions`: docked → flip the preference (persisted);
  otherwise flip `_sessions_overlay` and focus the filter when opening.
- The overlay closes when a session is opened or created, or on Escape
  (focus returns to the composer). It is intentionally not persisted.
- `_sync_topbar()` refreshes the crumb, status and toggle states. It runs from
  `_sync_panels`, `_poll_sessions`, and `_sync_timeline` (one line in `app.py`).

### Layering and budgets

- Widgets and CSS live in `ui_support/tui_panels.py` and `ui/tui/app.tcss`
  (not counted). `ui/tui/panels.py` gets the wiring; `ui/tui/app.py` gets only
  the compose line and the `_sync_timeline` hook, staying under the 2,400-line
  pure-client cap. `ui/` stays under 5,000.
- No new host commands; rule 4 is satisfied by the existing session commands.

## Tests

- `tests/test_tui_panels.py`:
  - top bar renders workspace, current title and status; clicking the toggle
    hides/shows the docked sidebar and persists the preference; the details
    toggle mirrors `ctrl+l`; `+` creates a session.
  - sidebar groups rows under day headers matching `SessionsScreen`
    (`Today` first) and puts archived rows under `Archived`.
  - at a narrow width `ctrl+b` opens the sidebar as an overlay without
    changing the preference; Escape closes it; opening a session closes it.
- Existing sidebar tests (status classes, spinner, delete/restore, archive)
  keep passing unchanged.
- Visual check: `app.run_test(size=…)` + `export_screenshot()` rendered to PNG
  with Playwright at 200×50 (docked, both sidebars), 200×50 with the sidebar
  toggled off, and 100×40 with the overlay open. Screenshots go to
  `artifacts/sidebar/`.

## Docs

- `docs/textual.md`: file map (`TopBar`), "Look" section (top bar, day-grouped
  sidebar, overlay at narrow widths).
