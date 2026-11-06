# Native inspector — 2026-11-06

Both clients use `interface/scenarios/inspector.json`: one turn, a model request,
a failed tool and an overlapping live child request. Screenshots use the real
client renderers and mock data, not provider calls.

- `nsh-timing.png`: NSH web, 1280×900, dark, selected model details.
- `nsh-narrow.png`: NSH web, 390×844, dark; chart body scrolls independently
  while refresh, copying, paging and dismissal remain reachable.
- `tui-timing.png` / `.svg`: Rust renderer, 160×50, dark, the same timing data.

Verification:
- Core native-inspector, turn-summary and storage-compaction checks: 37 passed;
  changed-file Ruff clean.
- Rust native-inspector checks: 2 passed, including native navigation, stale
  response rejection, cache values, time axis, zoom and details.
- NSH native-inspector browser checks: 4 passed, including the real App command
  palette route, cache values, child lanes, detail selection, zoom, paging and Esc.
- NSH type check has only the existing two `app.clock` nullability errors and
  `newsession.tsx`'s unsupported `Button.onFocus`; no inspector errors.
- Shared manifest/theme checks pass.
- Rust release build and NSH web packaging succeed.
- Full Rust suite: 15 failures outside inspector (one Markdown, six Manager,
  seven shell and one timeline). The tree already had documented failures and
  concurrent UI work was present; these were not changed as part of this task.

The inspector is a refreshable diagnostic snapshot. It does not auto-poll or
add diagnostics to the conversation timeline. Cache writes remain explicitly
unknown; provider-reported input/cache counts come from the durable ledger.
The standalone browser inspector remains available for its additional Questions,
kernel detail and live-stream controls.
