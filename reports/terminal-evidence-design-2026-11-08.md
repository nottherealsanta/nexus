# Terminal evidence capture — implemented 2026-11-08

Both clients now expose **Attach selected output** from the terminal toolbar and
shared `terminal.attach` command. This supersedes the initial design-only review.

## Contract

1. Capture selected rendered text, never raw escape-bearing PTY bytes. Freeze the
   terminal ID/name, project/workspace and destination draft identity. The stream
   sequence is a watermark, not a selection-coordinate or byte checkpoint.
   Actual originating session/turn/tool/job IDs are included when the resource
   records them, never inferred from the destination draft.
2. Open **Attach terminal output** with the exact eventual canonical text.
   Preview content is immutable and scrollable. **Add to draft** confirms;
   **Cancel** is inert. No send or hidden context insertion occurs.
3. Include an explicit untrusted-data warning and JSON-encode the selection, so
   fences or closing tags cannot impersonate provenance. Cap selected text at
   16 KiB, on UTF-8 code-point boundaries, with exact included/omitted selected
   byte counts. Earlier terminal history is explicitly unknown; rendered text
   cannot truthfully reveal omitted raw PTY bytes.
4. Confirm only into the same still-writable draft. Navigation/read-only changes
   fail visibly rather than rerouting evidence. Normal send/queue/steer commits
   the draft through existing canonical message paths. Confirmation appends
   without overwriting selected draft text or prefixing an empty draft. Later terminal output,
   renames, close or reconnect cannot alter evidence already added.
5. NSH correlates WebView capture replies with a request ID, terminal identity,
   attachment generation and draft identity; reset clears pending capture.
   TUI freezes its selection synchronously. Neither exposes mobile capture.

## Verification

- Shared canonical payload fixture: `interface/scenarios/terminal-evidence.json`.
- TUI tests: shared encoding, adversarial delimiters, exact Unicode clipping,
  inert preview/cancel, immutable source name, draft confirmation, keyboard
  scrolling and visible destination-mismatch rejection.
- NSH tests: shared encoding/clipping, inert cancellation, immutable preview,
  confirmation and draft-switch rejection; actual production preview/composer
  components in `TerminalEvidenceFixture.tsx` test the UI transition.
- Actual xterm page test verifies that capture occurs only after a request and
  echoes the request ID with selected rendered text; OSC 52 remains blocked.
- Preview/draft screenshots: `reports/terminal-evidence/` (both clients). The
  fixture preview data match; backgrounds differ because TUI uses a seeded
  ordinary session and NSH a new-session composer. No mock product rendering.

The actual browser WebView-to-host capture, preview scrolling, cancellation,
confirmation into the draft and zero automatic sends also pass in the real-Core
smoke (`e2e/terminal-core-smoke.mjs`). Delayed replies across real tab/session
changes are guarded but not independently exercised end to end; host guard and
draft-switch semantics have separate tests. Native AppKit runtime and Linux
execution remain unverified; see the main terminal work report.
