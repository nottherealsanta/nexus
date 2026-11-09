# Long-question visual check — 2026-11-08

Captured the real Rust TUI and NSH web renderer against one isolated scripted Core, using `interface/scenarios/question-readability.json`. No provider calls or real-home state.

## Captures inspected

| State | TUI | NSH |
| --- | --- | --- |
| Short desktop, first question | `tui-short.png` (120×27 cells / 1008×486) | `nsh-short.png` (1024×480) |
| Narrow, question/preview wrapping | `tui-narrow.png` (80×24 / 672×432; second question) | `nsh-narrow.png` (640×480; first question) |
| Narrow, end of choices | `tui-narrow-end.png` | `nsh-narrow-end.png` |
| Short desktop, end of choices | not separately captured | `nsh-short-end.png` |

Read all seven PNGs with the image tool. Both use dark theme and the same question data. First-question labels/previews wrap without ellipsis. The long second prompt is fully wrapped in the TUI narrow capture; scrolling reaches the final option's complete preview and custom-answer field. NSH narrow/end captures show wrapped labels, the complete final preview and reachable Submit answers. Content outside the viewport is scrollable rather than discarded.

Existing client differences remain visible: TUI pages questions individually and has a separate review step; NSH places all questions in one scrolling card. These captures do not establish approval for that existing interaction difference. Follow-up fixes: TUI reserves footer rows and wraps complete key/label pairs, so `enter review` and `esc stop` remain whole at narrow widths. NSH reserves a non-shrinking reading-hint region above the scroll viewport, preventing overlap. Recaptured affected images and inspected the narrow TUI and scrolled NSH end states. The capture helper now asserts the hint's bottom does not cross the scroll viewport's top at either size; this geometry check passes.

Narrow navigation: selected the seeded session at desktop width, then resized the existing session, avoiding the hidden narrow session rail. TUI uses `--open first`, not rail search.

## Reproduction

- Added the `readability` prompt branch to `nexus-nsh/e2e/scripted_core.py`, loading the shared fixture and emitting a genuine `ask` call.
- Scratch Core home `/tmp/nexus-question-capture/home`, workspace `/tmp/nexus-question-capture/ws`, Core port 48731, NSH port 48732. These are disposable, not personal state.
- TUI: `--dark --dump-frame 120x27 --open first --keys 'alt+w' --svg`; narrow: `80x24`, `alt+w alt+right pagedown pagedown pagedown`; end: `alt+w alt+right down down` plus eight `pagedown` presses. SVGs and text frames are retained beside PNGs.
- NSH capture helper: `QUESTION_SHOT_SESSION=<seeded id> node e2e/question-shots.mjs` from `nexus-nsh`; optional `QUESTION_SHOT_URL`. It captures at desktop size before resizing and renders TUI SVGs with Chromium. Browser executable is the locally supplied Playwright Chromium 1200 path.

Final checks: full TUI suite **865 passed, 10 ignored**, Clippy passed; NSH waiting-card/alerts suite **10 passed**. Production follow-ups reserve/wrap TUI hint rows and separate the NSH reading hint from its scroll viewport.

Supplemental `tui-narrow-end-verified.png` is captured from the real TUI test renderer with the same shared question data after 100 Page Down presses. It visibly includes the custom-answer field and complete footer hints; the test also asserts the field is present in the final frame. The earlier scripted-Core `tui-narrow-end.png` stopped after only eight one-row scroll steps and is not the actual end after the footer gained a row. Both images were inspected; this was insufficient capture scrolling, not lost content.
