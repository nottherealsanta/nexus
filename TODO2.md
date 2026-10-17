# Additional request follow-up

- [x] Cap the composed GPT Live user message at 200,000 characters. Keep earliest text, mark the exact omitted count inside the message, and emit a durable user-visible error while preserving real-time transcription events. Covered by live bridge/manager bound and transcription tests; service-side cap remains assumed, not independently verified.

- [x] Render Question Tool calls compactly: prompt plus options, a marked selected answer, free-text answer, or `awaiting answer`; detailed labelled payload remains available through inspection/verbose mode. Focused Python projection tests pass. A completed question no longer appends the generic tool summary to the compact card.
- [x] Suppress the expected `Error: supervisor closing` display for shutdown-cancelled agent/turn state, preserving errors for other cancellations/failures. Focused reducer tests cover it.
- [x] Present pending queue inputs in distinct **Steering** and **Queued** groups, preserving their real queue numbering and order. Movement controls are enabled only for an adjacent message in the same group; disabled arrows are not drawn and cannot trigger an operation. Rust tests cover boundary restrictions and rendering.

These describe behavior implemented/test-covered in the current worktree, not independent claims of visual parity. See `docs/ratatui-parity.md` for rendering and verification caveats.
