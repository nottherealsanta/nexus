# 30. Questions: no needless confirm step, locked composer, free-text focus

User requests (2026-11-11), items 11 and 12. Both clients in the same change; mobile's compact card too.

## Progress checkpoint (2026-11-11)

- Follow-up: phone-idiom headless question route passes; physical device remains unverified. Desktop captures now use the same question and preserved draft; surrounding session histories differ. TUI shows a focused choice and keyboard hints, NSH a reading hint and elapsed time.
Both clients implement and pass the focused single-/multi-question, free-text, composer-lock and multiple-question
scenarios, including failed-answer retry coverage.

## Before this change

- A waiting question shows a card; answers are reviewed, then submitted with `waiting.submit_answers`
  ("Submit answers", TUI `ctrl+enter`, NSH `cmd+enter`). TUI renders "rows, then review answers one at a time"
  (`render.rs` ~5759); NSH `composer.tsx` ~1060–1090 has the option buttons and a `Submit answers` button.
- The question UI's readability pass (wrapping, scrolling) landed 11-08 ([02-terminal.md](../02-terminal.md) side job).

## 30.1 One question: answer and done

- [x] When the ask contains **exactly one question**: choosing a single-choice option submits it at once (Enter or
  click). No review page, no second confirm. A multi-select question still needs one submit (there is no other
  way to say "done"), but no review page either.
- [x] Two or more questions keep the current step-through + review flow.
- [x] A misclick risk: keep the chosen answer visible in the timeline as it is today (the answer is model context).
- [x] Focused single-choice, multi-select, free-text and two-question tests pass in both clients; TUI also verifies failed-answer retry.

## 30.2 Composer locked while a question is on screen

- [x] While a question card is pending in the viewed session, the composer is disabled (no typing, no send, no
  steer/queue), with a short reason in its placeholder ("Answer the question above"). Esc still interrupts.
  User confirmed 2026-11-11: Deny/"tell it what to do instead" for approvals stays as is (approvals are not questions).
- [x] Draft text already in the composer is kept and comes back when the question is answered.

## 30.3 Free-text answers

- [x] When a question asks for a string (free-text option or a text-only question), the input field is **focused
  immediately**, and it is **not shown with a highlighted background** (it reads as the answer line, not a
  selected row). Focus stays in it until the user presses Enter, which submits (with 30.1, at once for a single
  question). Arrow keys move the caret, not the option selection, while it is focused.
- [x] TUI: the caret is visible in the field, keys go to it first (`keys.rs` waiting context). NSH: autofocus the
  `TextInput`, no `bg-selection` on its row.
- [x] Tests: typing straight after the card appears goes into the answer; Enter submits; composer text is untouched (both clients).
