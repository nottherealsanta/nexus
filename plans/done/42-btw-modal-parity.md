# 42. `/btw` modal: TUI/NSH parity

**Done (2026-11-11); moved to done/.** Found on 2026-11-11 while retaking plan [23](23-btw.md)'s paired captures
(`reports/sections21-23/tui-btw.png`, `nsh-desktop-btw.png`, `nsh-phone-btw.png`).

The two modals show the same title, folds, `N/10` progress, tool row and answer, but:

- NSH has two extra button rows at the top, "Expand inherited context" and "Expand context"
  (`nexus-nsh/src/ui/btw.tsx`, `btw-inherited` / `btw-context`). They duplicate the `[i] expand` / `[c] expand`
  lines below them. The TUI has no such rows.
- The TUI ends with a `↑↓ scroll · esc close` footer; NSH has none.

- [x] Shared shape (approved by the coordinator, 2026-11-11): make the `[i]`/`[c]` lines themselves tappable in NSH and drop the button rows;
  add the footer hint to NSH desktop/web, leave it off on phone where keys do not apply). A deliberate difference
  needs the user's approval and a ledger row.
- [x] Change both clients in one change, update `session-status-btw` scenario tests if they assert the buttons, and
  retake the three captures with `reports/sections21-23/capture.mjs`.

Result: the NSH `Inherited context · … · [i] expand` and `Context · [c] expand` lines are the tappable controls
(`btw-inherited`, `btw-context`) in TUI order inside the scroll area; the button rows are gone. NSH desktop/web shows
`↑↓ scroll · esc close` (`esc close and cancel` while running); the phone idiom omits `↑↓ scroll` because keys do
not apply there (narrow web is not the phone idiom and keeps it). No test asserted the old button labels;
`session-status-btw.test.tsx` now asserts the footer and the absence of the button text (4/4 pass). Captures:
`reports/42-btw-parity/`. No ledger row: the clients now match.
