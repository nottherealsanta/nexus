# 47. Questions live in the composer slot

**Done 2026-11-15** (user request 2026-11-14). Both clients in the same change (TUI is the reference).
Supersedes parts of [30](30-questions.md): 30.2 (locked composer under a separate card) and 30.3 (free-text field
focused at once when a question allows free text).

## What the user sees today (screenshot, TUI)

- A card above the composer, and under it a **disabled composer** reading "Answer the question above": two panels for one
  job, one of them dead.
- The card's own "Add context, or type your own answer…" line has the caret from the first frame (30.3), so **up/down
  move the caret, not the options**; `alt+a` is the only way to reach the options. That is why "arrows don't select".
- Enter on a highlighted option answers at once for a lone question; there is no "select, then continue".

## Wanted

1. The question **replaces the composer**: one docked panel in the composer's slot, not a card plus a locked composer.
2. **Up/down** move through the options.
3. **Space** selects the option (radio for single choice, tick for multi-select).
4. **Enter** goes to the next question; on the last (or only) question it sends. Review page stays for 2+ questions.
5. The composer's own input line is the **"add more context" field** of the answer (no second field inside the card).

## Design

One panel, top to bottom (the composer's status line, `Orchestrate · model · usage`, stays under it):

```
◆ Nexus asks                                   question 1 of 2
The fetched origin/main is 38 commits behind …  How should I proceed?
▸ ( ) 1 Open the PR with all 38 commits
        Disclose the earlier prerequisite work …
  ( ) 2 Pause the PR
        Wait for the earlier work …
▸ Add context…                       <- the composer input, same widget, same paste/image/@ handling
↑↓ move · space select · enter next · esc stop
```

### Focus: options first, composer on demand

- Focus starts on the **options**, so arrows work immediately. This reverses 30.3 for questions that have options.
- **Type to add context**: a printable key moves focus into the composer line and types it. `down` from the last
  option also moves into the line; `up` from the line returns to the options. Esc in the line returns to the options
  first (text kept); Esc on the options keeps its global meaning (stop), resolved by `[[keymap.escape]]` in the
  [manifest](../../interface/manifest.toml).
- **Space** selects while the line is empty; once the line has text, space types (the caret is in it anyway).
- **Digits** pick option N while the line is empty and focus is on the options (as today).
- A question with **no options** (text only) focuses the composer line at once (30.3 stays for this case).
- `enter` with the line focused: next / send, with the line's text as the answer note. `shift+enter` is a newline.
- `alt+a` (`waiting.custom`, TUI-only) is removed from the manifest and hints; nothing needs it.
- Page keys: `alt+left/right` page between questions (kept); plain left/right page when the line is empty and
  focused on options, caret otherwise (as today). `pageup/pagedown` scroll a long question (kept).

### Enter and space, by question type

| Question | Space | Enter |
| --- | --- | --- |
| Single choice, lone question | marks the highlighted option | sends (takes the highlighted option if none marked, as today) |
| Single choice, 2+ questions | marks it | locks it, next question; last one goes to the review page |
| Multi-select | ticks / unticks | nothing ticked: error row "pick at least one"; else same as above |
| Text only | n/a (types) | same as above |

Click and digit keys keep **choose and advance** (today's 30.1 behaviour) so mouse and number users do not need Enter.

### The composer's draft

Typing a context note and drafting the next message cannot share one field. On question appear, **stash** the composer
draft (text, images, pastes) and restore it when the question is answered or cancelled, as pending-input editing does.
The line starts empty (or with the note already typed for that page when paging back). The answer is unchanged on the
wire: `{question_id, selected, text}` (`POST /api/session/<id>/question/<id>`), so **Core, the model context and the
timeline entry do not change**. The note shows in the answer entry as it does now.

### What stays locked

Send, steer, queue, slash commands and dictation are still off while the question is pending (the composer slot is the
question). Pending-queue rows and "Send next" keep their `questionLocked` guard. Approvals are not questions and keep
the card plus composer as today (30.2 note).

### Mobile (NSH phone)

Cut-down client: the compact card stays as the answer surface; no new surface is added. Proposal: keep its own field
(it has no keyboard focus model to fix). **Needs the user's call** (below) because "use the composer" would also touch
mobile.

## Work items

Status 2026-11-15: built in both clients, offline tests pass, NSH and TUI compared in the same state.

- [x] **47.0 Decisions** answered (below) and recorded in [decisions.md](../decisions.md).
- [x] **47.1 Manifest**: `waiting.custom` (`Alt+A`) removed; `waiting.options` (Esc, `escape_chain`) and the term
  `waiting.question_field` added, with an Esc rule `question_field` before `sidebar_back`. `check_manifest.py` ok. Space
  and digits stay client bindings (no manifest key).
- [x] **47.2 TUI layout**: `render.rs` `ask_docked` (terminal at least 20 rows): the waiting card keeps its rows, the
  composer region draws the question's context line (the pick's own `Composer`) with the card's rail continuing through it.
  Shorter terminals keep the line in the card and the old composer lock text.
- [x] **47.3 TUI keys/state**: `app.rs` `ask_key`: options first (`start_question` focuses the line only for a
  text-only question); Down from the last option and typing enter the line; Up from its first line and Esc
  (`waiting.options`) leave it; Space selects without advancing; `ask_choose` (click, digit) chooses and goes on.
  The composer draft is never touched, so there is nothing to stash.
- [x] **47.4 NSH**: `composer.tsx` `EditableComposer` is the line while `dockedQuestion()` (desktop only): the
  draft is shown again afterwards; `QuestionBody` loses its own field on desktop and gains `QuestionHints`; Up, Down,
  Space, typing and Esc via `questionLine` / `questionLineFocus`. The phone keeps its card and field.
- [x] **47.5 Hints and wording**: `↑↓ move · space select|tick · [alt+←→ pages ·] enter next|send|review · esc stop`
  (`esc options` while the line has the caret); placeholder `Add context…` / `Type your answer…`.
  NSH has no page keys, so it omits the `pages` hint.
- [x] **47.6 Tests**: TUI `tests/session_controls.rs`, `tests/shell.rs`, `tests/alerts.rs` updated and a new
  keys test; NSH `session-controls.test.tsx` and `i8.test.tsx` updated plus keys tests, including Esc/Up leaving the
  line with its text kept, and the draft coming back with its pasted image. No shared scenario file was added.
- [x] **47.7 Screenshots and docs**: READMEs, `docs/tools.md` and `interface/design-language.md` updated. TUI captured
  at 120x36, 80x24 and 170x50; NSH at 1440x900 against the same scripted Core (options focused and line focused).
  The comparison found NSH's rail broken between card and line; `StatusRail` `join` now runs it through as in the TUI.
  30.2/30.3 are marked superseded in [30-questions.md](30-questions.md).
- [x] **47.9 NSH focus and highlight fixes** (found while making the tests stable): the composer's caret placement and the
  card's highlight reset ran on every model change (`dockedQuestion()` and `p.w()` read the model), so an incoming event
  could take the caret out of the context line or move the highlight back to the first option. Both now act only when
  the question or page changes; the highlight's key is module-level, so the card's remount after the session's history
  loads keeps it. The tests' `openSession` waits for that load: keys pressed before it reach the old pane.
- [x] **47.8 Ledger row** added.

Screenshots: [reports/47-question-in-composer/](../../reports/47-question-in-composer/README.md).

Not verified: physical phone; terminals shorter than 20 rows only through a unit test; a question while pending
inputs are queued (the queued rows sit above the line in the TUI).

## Decisions (user, 2026-11-14)

1. Click and digit keys **choose and advance at once**, as today. Space only selects; Enter continues.
2. The composer draft is **stashed and restored** around a question; the context line starts empty.
3. **First Esc in the context line returns to the options** (text kept); Esc on the options stops, as before.
4. **Mobile keeps its compact card** and its own field; no phone composer change.

## Risks

- Type-to-focus plus space/digits as commands is the one place keys are modal; the rule "empty line = commands,
  text = typing" must be the same in both clients and shown in the hint row.
- Merging two regions changes the TUI layout budget at small heights (`layout.rs` clamps `waiting` so the timeline
  keeps its rows); check 80x24 and the 6-row minimum.
