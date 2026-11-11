# Session controls — plans 29–32 (2026-11-11)

## Scope and decisions

Both desktop clients share the same behavior. Mobile keeps the compact Sessions/Manager/Settings client.

- Archive events close tabs for every archived tree member, regardless of source; `/archive` exposes the existing Core operation. `/search [text]` opens focused search (desktop switcher; mobile Sessions list).
- Exactly one question skips review: single choice and free text submit immediately, multi-select has one submit. Multiple questions retain step-through/review. Questions lock and preserve composer drafts; approvals stay unchanged.
- **User clarification:** only queued rows have edit/up/down/remove icons, with no Steer button. Existing keyboard actions remain. Icons use same-group bounds.
- Worktree icon has a “Worktree session” hint and opens a small modal with full copyable worktree path, branch and main-checkout path; no Manage link.

## Shared inputs

- `interface/manifest.toml`: `/archive`, `session.worktree_info`, `session.linked_worktree` availability.
- `interface/theme.toml`: `control.worktree` (`⑂`, ASCII `*`).
- `interface/scenarios/session-controls.json`: question, preserved draft, pending inputs and linked/main worktree data for both clients.

## Verification

- **Plan 31 selected/held parity:** both clients now select pending rows with Up from an empty composer and show the same selected-row key hints. A held queue offers both `n` and a clickable **Send next** in both; questions disable release. NSH keeps touch access to that control. The matched held/selected images `tui-queue-held-selected-120x36-dark.png` and `nsh-queue-held-selected-dark.png` were read: same queued/steer order, held marks, selected third queue row, `n send next` hint, four queued-only actions and held-only control. TUI's narrower content clips more of the hint; the NSH image uses a wider content column. Physical phone/touch remains unverified.
- Follow-up checks: TUI `cargo test --test session_controls` **36 passed**; NSH web `session-controls.test.tsx` **19 passed**; NSH build and manifest/theme checks passed. Earlier Core archive/input contracts and focused interrupt tests remain recorded below; no provider calls.
- **Final geometry and queue follow-up:** DOM `elementFromPoint` at the failed 24px copy control's center hit the icon child inside the button, not an overlay; the scroll viewport was 397px high with 744px content and the button was within it. The 24px class overrode the kit's normal 32px button geometry. Restoring that default fixes the center hit without changing shared kit or NSH framework code. A packaged production-component fixture driven by Playwright now copies the exact 1,156-character path with a center click, scrolls down, and center-clicks the main checkout copy. The full NSH session-controls test also copies the exact long path. This demonstrates the local geometry fix; no claim is made about the deeper framework hit-test implementation.
- The unheld NSH queue header is removed, matching TUI. Held rows say `held`; the pre-existing held-only Send next control is retained. The busy empty composer retains `Enter steer · Tab queue`. Reviewed the refreshed `nsh-queue-actions-dark.png` against `tui-queued-actions-120x36-dark.png`: same unheld row order, hovered second row, action order and no steer icons; no extra unheld heading. Reviewed the refreshed NSH worktree modal against its TUI counterpart.
- Latest checks: NSH web session-controls **17 passed**, pending queue **2 passed**, interrupt **1 passed**; headless phone routes **3 passed**; TUI session-controls **35 passed**; NSH production build, manifest and theme checks passed. Real Chromium pointer regression passed: package `WorktreeInfoFixture.tsx` with `node scripts/nsh-web.mjs package WorktreeInfoFixture.tsx --target web --out /tmp/nexus-worktree-fixture`, serve its `Nexus/` directory on localhost:49383, then run `BROWSER=<Chromium> node e2e/worktree-long-copy.mjs` from `nexus-nsh`. Physical phone/touch is unverified. No real provider calls.

### Prior investigation (superseded by the fix above)

- **Controlled-state follow-up:** regenerated and read the TUI/NSH queue captures with the same unheld queue (first, second, third, then steer), the second queued row hovered and no selected row. Both show the four actions in the same order, no steer icons, and muted unhovered rows. NSH has an additional `Pending · enter steer · tab queue` header absent from the TUI. The question captures now both use the shared single question and `Keep this unsent draft`; both show the preserved draft and same choices, while the TUI marks the focused choice and shows keyboard hints and NSH shows a reading hint and elapsed time. The worktree modal captures show the same path, branch and main path; TUI additionally shows its Enter shortcut. The surrounding sessions/history and viewport sizes still differ, so these comparisons control the requested component states, not the whole application screen.
- **Long-path browser investigation:** `WorktreeInfoFixture.tsx` packages the actual modal with a 1,156-character path and `e2e/worktree-long-copy.mjs` drives Chromium on localhost. Focus + Enter copies all 1,156 characters exactly. A real pointer click at the copy button's top-left (2,2) also copies the exact path, but a click at its center does not; a shorter 206-character path accepts the center click. This reproduces outside the NSH test driver, so it is an actual pointer-target usability defect, not merely a test-harness limitation. Setting the icon's pointer events to none did not fix it and was not retained. The defect is **open**, including whether it affects touch; do not mark plan 32 complete.
- Reproduction: `cd nexus-nsh; PATH="/opt/homebrew/opt/llvm/bin:$PATH" node scripts/nsh-web.mjs package WorktreeInfoFixture.tsx --target web --out /tmp/nexus-worktree-fixture`, serve `/tmp/nexus-worktree-fixture/Nexus` on localhost:49383, then `BROWSER=<Chromium path> node e2e/worktree-long-copy.mjs` (keyboard passes) or `BROWSER=<Chromium path> POINTER=1 node e2e/worktree-long-copy.mjs` (center pointer fails). No provider calls.
- Follow-up: NSH input GET returned the correct data, but rendering threw `ReferenceError: TS is not defined`; importing the shared typography constant fixes the rows. The action group's visibility now also hides disabled arrows on non-hovered rows.
- TUI `cargo test --test session_controls`: **35 passed**, including row-menu archive and refusal. Full `cargo test` cannot compile unrelated `tests/scenarios.rs`: `App::open_palette` is private.
- NSH `nsh test session-controls.test.tsx --target web --browser "$BROWSER"`: **17 passed**. `nsh test phone-session-controls.test.tsx` (headless iOS idiom): **3 passed**. `pending-queue.test.tsx` web: **2 passed**. `npm run build` passed after the final visibility adjustment.
- Core scratch-home archive/input tests: **28 passed**; manifest and overlay theme checks passed. No provider calls.
- Earlier captures used mismatched queue/draft states; superseded by the controlled-state follow-up above.
- Reviewed `nsh-worktree-long-path-dark.png`: `final-tail`, branch and main checkout are visible after scrolling. The earlier test failed on the copy press before scrolling; the subsequent real-browser investigation above establishes the pointer defect.
- Physical iPhone and actual touch behavior remain unverified. No plans moved to `done/`; the differences and pointer defect above remain open.

### Earlier checkpoint

- Shared manifest: passed (213 commands, 38 slash names).
- Shared theme with NSH overlay: passed (602 checks).
- Focused unchanged Core contracts: `tests/test_archive.py` and `tests/test_inputs_integration.py` — **28 passed**, isolated scratch `NEXUS10_HOME`, no provider calls.
- Core archive/input contracts: `tests/test_archive.py` + `tests/test_inputs_integration.py` — **28 passed**.
- TUI `cargo test --test session_controls` — **34 passed**; matching question/queue/worktree screenshots captured.
- NSH `npm run build` — passed. Full web session-controls run: **12 passed, 4 failed**. Archive/search, question flows,
  failed-answer retry, archive refusal, and the basic worktree modal/copy passed. Three pending-row tests fail because
  the rows are not visible; the long-path test fails its scroll assertion. A subsequent isolated queue rerun still fails
  to display the rows. Basic NSH question/worktree screenshots are present; the queue capture is a diagnostic
  empty-state screenshot, not a parity screenshot.
- Paired screenshot review, NSH pending-row fix, NSH long-path scroll verification, and phone-route/device checks remain
  open. This report is a partial checkpoint, not a completion claim.
