# Terminal client checks — 2026-11-08

## Changes

- Both clients revoke local input immediately on an exit frame, update the terminal tab status, and prevent a late successful acquire response from making an ended terminal writable.
- TUI terminal lists now omit closed resources and prefer a running resource on open, matching NSH.
- TUI invalid stream frames also revoke local control and drop queued input.
- The TUI `--dump-frame` pump now runs terminal attach/stream/stop effects, rather than silently dropping them. Captures below use actual Core PTY replay, not a fabricated screen.
- NSH real-Core smoke accepts optional `NEXUS_TERMINAL_SESSION` and `NEXUS_TERMINAL_SCREENSHOT` for isolated fixtures and evidence capture.

## Verification

These are the initial lifecycle-slice checks. Final follow-up: **865 TUI tests pass, 10 ignored**, Clippy/build pass,
and the inspector test below is fixed. NSH's final controller suite has **6 passing tests**, including the real
creation/list-refresh race. The current smoke supports empty docks through the palette and also verifies actual
WebView capture, preview scrolling/cancellation, draft insertion and zero automatic sends. See the [final report](../terminal-work-2026-11-08.md).

- TUI `cargo test --test shell terminal`: **15 passed**.
- TUI `cargo test --test terminal_core -- --ignored`: **1 passed**, real isolated Core HTTP/SSE/PTTY output and VT parser.
- TUI `cargo test --bin nexus-tui`: **1 passed**.
- NSH terminal-api/controller/panel browser suites: **9 passed** (3 + 5 + 1).
- NSH `npm run build` and `npm run check`: passed.
- NSH `e2e/terminal-core-smoke.mjs`: passed create, rename, attach/control, Ctrl+C, 500-line output, confirmed close and mobile exclusion. One initial isolated run failed waiting for the new stream/acquire; a subsequent run with request tracing showed successful endpoints and a clean smoke rerun passed. The later smoke falls back to the Terminal palette command when no shell-count indicator exists.
- Full `cargo test` passed its preceding suites, then stopped on the concurrently added inspector test `terminal_resource_lanes_share_axis_and_diagnostic_details`, whose expanded text lacked `diagnostic_only`. It is outside this client-lifecycle slice; do not treat the full suite as green.
- `git diff --check`: passed.

Browser tests used Playwright Chromium 1248 and the Rust toolchain LLVM bin directory on PATH (the default browser and `llvm-ar` were not found otherwise).

## Screenshots, inspected

- `nsh-parity.png` (1440×900) and `tui-running.png` (170×50, rendered SVG at 1428×900) show the same isolated session and running `Terminal parity` resource in light mode. Each has the dock under composer/activity, terminal count, one named running tab, toolbar and actual shell prompt. Both attached and acquired control. NSH uses icons and TUI text for control/evidence actions; pane/text metrics follow their respective renderers. No new client-specific behavior was introduced.
- `nsh-running.png` preserves the real-Core smoke's high-output terminal before close.
- `tui-running.txt` and `.svg` preserve the original TUI frame.

Follow-up closed the reconnect behavior gap: **both clients automatically reattach read-only and require Take control again**, without replaying pending input. TUI has a deterministic reconnect regression test. Fully verifying authenticated remote reconnect, native macOS WebView key handling, all modifier/function combinations and other host platforms remains open. Implementation is not platform release approval.

No paid provider call or real Nexus home was used. Temporary Core/host fixtures were created under `/tmp/nexus-terminal-parity.*`.
