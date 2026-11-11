# 29. Archive closes its tab, `/archive`, `/search`

User requests (2026-11-11), items 8–10. Small; can land in one change, in both clients (and mobile, which gets all
slash commands).

## Progress checkpoint (2026-11-11)

- Desktop TUI and NSH implement archive-event tab closure (including descendants and background-project tabs), `/archive`,
  and focused `/search [text]`. TUI session-controls passes 34/34. Latest NSH session-controls run passed the archive,
  search, question, basic worktree, refusal and answer-retry cases; the four failures are the pending-row rendering/action
  cases and long-path scrolling. Mobile routes are implemented but not tested on a phone route or device. Archive
  row-menu coverage remains open.

## 29.1 Archiving a session closes its tab

- [x] When a session becomes archived (from this client, another client, the Manager or Core auto-archive), its tab
  closes in every client that has it open. Drive it from the reducer on the archive event, not from the command, so
  all sources are covered (TUI `reduce.rs` → `App::close_tab`, `app.rs` ~5416; NSH `store.ts`).
- [x] Focus moves to the neighbouring tab as `session.tab_close` does today; with no tab left, the empty state.
- [x] Archiving a parent closes the tabs of its archived descendants too.
- [~] Mobile: if the archived session is open, go back to the Sessions tab (implemented; phone-route/device test open).
- [~] Tests: event/API and child paths are covered in desktop scenarios; row-menu path remains open.

## 29.2 `/archive`

- [x] Add `slash = "archive"` to `session.archive` in `interface/manifest.toml` (same `when`: has session, not
  archived, not busy; the unavailable reason is shown when refused). Add `"header"` placement if wanted.
- [~] Core refuses a tree with a live background job; NSH refusal message is tested, TUI refusal presentation remains open.
- [x] Together with 29.1, `/archive` leaves the user on the next tab (both desktop clients).
- [x] Manifest check, slash completion tests in both clients (manifest check and desktop completion tests passed).

## 29.3 `/search` opens the sessions modal with focus on search

- [x] `session.search` (`/search`, TUI `ctrl+f`, NSH `cmd+shift+f`) has `key_context = "sidebar.sessions"`
  and placement palette/sidebar. Check what the slash command does today in each client; make it open the
  **sessions modal (switcher)** with the search field focused and empty (or prefilled with `/search <text>`'s
  argument).
- [~] Same behaviour in both desktop clients (passed); mobile route is implemented but not yet verified.
- [x] Desktop tests in both clients: `/search` then typing filters immediately without another click/key.
