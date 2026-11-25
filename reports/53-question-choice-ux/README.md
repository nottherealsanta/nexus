# 53. Question choice UX

Evidence for [plans/53](../../plans/done/53-question-choice-ux.md), 2026-11-25.

## TUI

- `tui-question-single-120x36-dark` (`png`, `svg`, `txt`): the scripted Core's one-question
  `ask` after `Down`. SQLite is the answer: its radio is filled and highlighted. The same keys
  on `Right`/`Left` behave as another arrow on a lone question, and no review page opens.
- `tui-question-multi-120x36-dark` (`png`, `svg`): a multi-select with Unit tests ticked and the
  cursor on End-to-end tests. The violet tick stays visible under the cursor band, the note says
  `Select all that apply · 1 ticked`, and the hint says `enter send` once something is ticked.
  With nothing ticked the hint says `enter tick` and Enter ticks the highlight without sending.

Both were rendered with the real TUI against the scripted Core (`nexus-nsh/e2e/scripted_core.py`)
at 120x36, dark theme. The multi-select state was captured through the
`NEXUS_SESSION_CONTROLS_SHOTS` test hook.

## NSH

Not captured: this checkout has no `node` or `nsh`, so the web build and browser screenshots
could not run. The matching NSH code and tests (`i8.test.tsx`, `session-controls.test.tsx`) were
updated; run `node scripts/nsh-web.mjs test --target web i8.test.tsx session-controls.test.tsx`
on a machine with the NSH toolchain and compare the same states at 1440x900.
