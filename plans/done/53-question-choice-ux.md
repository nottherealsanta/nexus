# 53. Question choices: arrows select, Space goes on, multi-select is clear

User request (2026-11-25): "when questions are asked - I can't use arrow to select the answer" and
"also work on questions where user can select multiple. this UI /UX needs to be better". TUI and
NSH (desktop/web) in the same change; the phone keeps its compact card.

Follow-up (2026-11-25, user approved): Space on a highlighted single choice behaves like Enter in
both clients — it advances, opens the review on the last multi-question page, or submits a lone
question at once. Multi-select Space still toggles and stays; Space in the free-text line types;
Space on the review does nothing.

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
- Space only selected, so a highlighted single choice still needed a second key; users expected the
  choice list to go on with either Space or Enter.

## Interaction now (both clients)

- **Single choice:** the answer follows the highlight; Up/Down and (on a lone question) Left/Right
  move it and fill the radio. On the options Space and Enter are the same key: they lock the answer
  and move to the next page, open the review after the last page, or send a lone question at once.
  The hint reads `space/enter send|next|review`. Typing before any arrow leaves the choice empty, so
  a free-text-only answer still works.
- **Multi-select:** a question line says `Select all that apply`, then `· N ticked`; rows draw
  checkboxes. Arrows move, Space or a number ticks/untacks and stays (a click too), and Enter with
  nothing ticked ticks the highlighted option and stays (the hint says `space tick`, then `enter
  tick`, `enter send`/`next`). The cursor never hides a tick: the TUI keeps the violet
  `interactive.multi` row under the focus band.
- **The context line:** Space types normally there. The TUI and NSH hints show only `enter ...`
  while the line has the caret.
- **Pages:** a lone question never opens a review, including via Left/Right. With several questions,
  TUI Left/Right still pages while the options have the keys; NSH uses Back/Next. On the review,
  Space does nothing; Enter sends.
- **Phone:** unchanged; its compact card keeps the free-text field's autofocus and its touch
  choices, and Space never submits there.

## Files

- TUI: `src/app.rs` (`ask_space`, `ask_choose`, `ask_move`, `ask_confirm`, `ask_page`, `ask_key`),
  `src/render.rs` (radio and checkbox marks, `Select all that apply` note, `space/enter` hints),
  `tests/session_controls.rs`, `tests/shell.rs`, `tests/alerts.rs`.
- NSH: `src/ui/composer.tsx` (`chooseAnswer`, `highlightAnswer`, arrow/space bindings,
  `confirmQuestion`, `QuestionHints`, count line, radios on desktop), `i8.test.tsx`,
  `session-controls.test.tsx`.
- Docs: `docs/tools.md`, both READMEs, `interface/design-language.md`, `plans/decisions.md`,
  `docs/porting-ledger.md`.

## Verification

- TUI `cargo test`: the whole suite passes, including new Space tests (single advancement, lone
  submission, final review, free text, multi-select) in session-controls. `rustfmt --check` on the
  changed files and `cargo clippy --all-targets` are clean apart from one pre-existing
  `tests/timeline.rs` warning.
- NSH tests: `node scripts/nsh-web.mjs test --target web i8.test.tsx session-controls.test.tsx`.
- Screenshots: paired captures of the single-choice and multi-select states and their hints (see
  the report).

