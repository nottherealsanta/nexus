# 42. `/btw` modal: TUI/NSH parity

Found on 2026-11-11 while retaking plan [23](done/23-btw.md)'s paired captures
(`reports/sections21-23/tui-btw.png`, `nsh-desktop-btw.png`, `nsh-phone-btw.png`).

The two modals show the same title, folds, `N/10` progress, tool row and answer, but:

- NSH has two extra button rows at the top, "Expand inherited context" and "Expand context"
  (`nexus-nsh/src/ui/btw.tsx`, `btw-inherited` / `btw-context`). They duplicate the `[i] expand` / `[c] expand`
  lines below them. The TUI has no such rows.
- The TUI ends with a `↑↓ scroll · esc close` footer; NSH has none.

- [ ] Decide the shared shape (likely: make the `[i]`/`[c]` lines themselves tappable in NSH and drop the button rows;
  add the footer hint to NSH desktop/web, leave it off on phone where keys do not apply). A deliberate difference
  needs the user's approval and a ledger row.
- [ ] Change both clients in one change, update `session-status-btw` scenario tests if they assert the buttons, and
  retake the three captures with `reports/sections21-23/capture.mjs`.
