# Immediate first submission

Shared input and expected wording: `interface/scenarios/first-prompt.json`.

- `tui-creating.png`: real TUI renderer, before any network reply.
- `tui-created.png`: the created tab is open; its history has not arrived yet.
- `nsh-sending.png`: the actual NSH web new-session component with the same pending input.

The context preview remains above `You · Sending…` and the submitted text. The
chosen agent/model stay in the TUI composer through creation. Pending submission
is not inserted into either client's canonical message list. Confirmed messages
and loaded starting history replace it; failures restore the appropriate draft.

TUI frames use the dark 170×50 renderer; NSH uses a 1440×900 dark browser fixture.
The NSH screenshot focuses on the changed context/message region, without app
chrome. Its isolated build stubs unused native microphone capture because the
normal test runner cannot load `nexus/native`. The header contents differ in
estimated counts because the fixtures intentionally have no real project files
or MCP servers. Structure, wording, ordering and transition behavior match.

No real Nexus home was changed, and no provider call was made.

## Checks

- TUI: five first-send checks and 76 behavior checks pass. The last run uses a
  first-send-only snapshot to exclude concurrent Manager-indicator edits.
- NSH: four first-send and five context-header checks pass in the isolated web
  copy. The normal runner still fails to load `nexus/native`; normal `nsh check`
  reports the pre-existing skill-picker `Button.onFocus` typing error.
- Before the concurrent Manager edits, the full TUI run had 765 passes, seven
  existing failures (Markdown, five shell checks and timeline selection), and
  seven ignored tests. The release build succeeded.
