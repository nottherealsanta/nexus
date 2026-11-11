# 31. Pending queue rows: hover actions on the right

User request (2026-11-11): steer and queued messages are listed separately above the composer. When the pointer
hovers over one of those rows, **edit, move up, move down and close (remove)** appear as icons on the right of that
row. Both clients in the same change. This was already planned in the backlog (9.6, the pending-row items). Those
lines are moved here below, and the order now follows the user's request.

## Progress checkpoint (2026-11-11)

TUI implements queued-only icons, group bounds, row styling and keyboard parity. The final focused TUI session-controls
suite passes 34/34, including the corrected remove confirmation (`delete` action). NSH styling/actions are implemented,
but its browser test still shows no pending rows even though `GET /api/session/{id}/input` is requested. The focused NSH
run fails queued-action, edit/remove and question-lock-with-queue cases. The root cause is not yet isolated. NSH queue
screenshots and paired comparison remain blocked; continue debugging the live-data update path into `PendingStack`.

**User clarification (2026-11-11):** the four icons are for **queued messages only**;
do not show a Steer button or action icons on steer rows. Existing keyboard actions remain.
This supersedes the original steer-row and Steer-button proposals.

## Before this change

- **NSH** (`nexus-nsh/src/ui/composer.tsx` ~673–735, `PendingActions`): hover/focus reveals icons on the right, in
  the order **edit, remove, Steer (queued only), up, down**. Down uses `chevron-down` while up uses `arrow-up`, so
  they don't match. On touch and narrow windows, tapping a row shows the actions below it. Actions call
  `PATCH /api/session/{id}/input/{input}`, `…/move` and remove.
- **TUI:** no per-row hit targets or hover icons (no `Target::PendingRow` / `PendingAction` yet). Actions are
  keyboard only, on the selected row.

## Plan

- [~] **Order and icons, both clients:** `✎ edit`, `↑ move up`, `↓ move down`, `× remove`, right-aligned on the row.
  ASCII TUI: `e ^ v x`. NSH: `pencil`, `arrow-up`, `arrow-down` (not `chevron-down`), `x`.
  Queued rows only; no Steer text button (user confirmed 2026-11-11).
- [~] Up is disabled on the first queued row and down on the last queued row. Core's move API permits global
  cross-mode reordering, but these UI actions use neighbours in the queued group only.
- [~] Shown only while the row is hovered (or keyboard-selected/focused). The row text goes to primary while hovered.
  Touch keeps NSH's tap-to-reveal. Mobile: same compact behaviour.
- [~] Clicking an icon does exactly what the matching key does. Edit puts the text back in the composer as today.

Backlog requirements carried forward (updated to the user's 2026-11-11 clarification):

- [~] Both clients: the mark is `S` / `Q` (decided) in primary text, not accent and not bold, and NSH's rows are
  rebuilt to match the TUI's layout (one card, the mark two cells left of the text, the same hint); the selected or hovered row's text goes to primary.
- [x] TUI: hit targets per row (`Target::PendingRow(i)` and `Target::PendingAction(i, Edit|Up|Down|Remove)`),
  drawn right-aligned in the card only while the row is hovered (`Cx::hovered`, as the sidebar's hovered-row `×`,
  `ui.rs:541`) or selected; glyph buttons (`✎ ↑ ↓ ×`, ASCII `e ^ v x`) with the keyboard hint kept for the selected
  row. Click runs the same action as the key.
- [~] NSH: same order and glyphs as the TUI (edit, up, down, remove on queued rows only); use `arrow-down`
  rather than `chevron-down` so up and down match; disable up on the first row and down on the last.
- [~] Tests: TUI behaviour test clicking each action (`tests/behaviour.rs`), NSH browser test hovering a row and
  clicking each action; the existing interrupt-returns-the-queue tests still pass.

- [~] Docs: both client READMEs (pending inputs), `interface/design-language.md`; screenshots of a hovered queued
  row in both clients, with no icon actions on steer rows.
