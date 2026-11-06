# Resume archived sessions

The same confirmation state rendered by the real Rust TUI buffer (`tui.svg`,
`tui.png`) and NSH web (`nsh.png`). Both use the existing model/agent confirmation
component, with identical title, body and action labels. The background fixtures
differ; the modal wording and flow are shared by
`interface/scenarios/resume-archived.json`.

Four new TUI tests and two NSH browser tests pass; the 76 TUI behavior tests also
pass (two ignored). Focused tests cover keeping the draft on Cancel, prompting only after unarchive,
explicit Unarchive without a prompt, root-tree targeting from a child, and a
failed unarchive blocking submission.
One repeat browser run timed out at startup with an empty screen; rerunning both
tests passed.

The full Rust suite still fails in unrelated Markdown, Manager, shell geometry/
theme and timeline selection tests. `nsh check` reports the existing nullable
`app.clock` tests and unsupported `Button.onFocus` in `src/ui/newsession.tsx`.
No provider calls or real-home changes were used.
