# 53. Question choices: arrows select, multi-select is clear

User request (2026-11-25): "when questions are asked - I can't use arrow to select the answer" and
"also work on questions where user can select multiple. this UI /UX needs to be better". TUI and
NSH (desktop/web) in the same change; the phone keeps its compact card.

## What was wrong

- Left/Right on a lone question opened a review page with "not answered" instead of moving the
  choice. A lone question never has a review page.
- A single-choice option list only moved its highlight with the arrows. After Space had selected
  an option, arrows did not change the answer, so Enter could send an option the user had already
  moved away from.
- NSH reset its question highlight whenever the answer changed. Ticking a multi-select pulled the
  cursor back to the first ticked option.
- Enter on a multi-select with nothing ticked only showed an error. The list had no selection count,
  and a ticked row's violet state could be hidden by the cursor band in the TUI.

## Interaction now (both clients)

- **Single choice:** the answer follows the highlight; Up/Down and (on a lone question) Left/Right
  move it and fill the radio. Space keeps the highlight, Enter sends/continues. Typing before any
  arrow leaves the choice empty, so a free-text-only answer still works.
- **Multi-select:** a question line says `Select all that apply`, then `· N ticked`; rows draw
  checkboxes. Arrows move, Space or a number ticks/untacks (a click too), and Enter with nothing
  ticked ticks the highlighted option and stays (the hint says `enter tick`, then `enter
  send`/`next`). The cursor never hides a tick: the TUI keeps the violet `interactive.multi` row
  under the focus band.
- **Pages:** a lone question never opens a review, including via Left/Right. With several questions,
  TUI Left/Right still pages while the options have the keys; NSH uses Back/Next.
- **Phone:** unchanged; its compact card keeps the free-text field's autofocus and its touch choices.

## Files

- TUI: `src/app.rs` (`ask_move`, `ask_confirm`, `ask_page`, `ask_key`), `src/render.rs` (radio and
  checkbox marks, `Select all that apply` note, hints), `tests/session_controls.rs`.
- NSH: `src/ui/composer.tsx` (`highlightAnswer`, arrow bindings, `confirmQuestion`, page highlight,
  count line, radios on desktop), `i8.test.tsx`.
- Docs: `docs/tools.md`, both READMEs, `interface/design-language.md`.

## Verification

- TUI `cargo test`: the whole suite passes, including new arrow-selection, no-review and
  multi-select Enter tests (session-controls 43/43). `rustfmt --check` on the changed files and
  `cargo clippy --all-targets` are clean apart from one pre-existing `tests/timeline.rs` warning.
  Live `--dump-frame` against the scripted Core confirms the radio marks, Right/Left selection and
  no review page.
- TUI screenshots: [reports/53-question-choice-ux](../../reports/53-question-choice-ux/README.md),
  single choice selected by an arrow and a multi-select with one tick, cursor on the next row.
- NSH tests updated but not run here: this checkout has no `node`/`nsh`; run
  `node scripts/nsh-web.mjs test --target web i8.test.tsx` and the session-controls suite on a
  machine with the NSH toolchain.

Not verified: NSH on a running browser/desktop build (its tests were not run here).
