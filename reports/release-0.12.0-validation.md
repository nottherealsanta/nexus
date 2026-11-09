# 0.12.0 validation — 2026-11-09

This is the final automated-test follow-up to the November 8 consolidation. Physical devices, native audio, GPUI rendering, remote reconnect/platform runtime matrices and remaining visual-parity work are separate gates, not claimed complete here. No paid provider calls or real-home migration were performed.

## Local checks

| Check | Result |
| --- | --- |
| Core full offline suite, scratch `NEXUS10_HOME` | 1290 passed, 1 skipped (209.72 s) |
| Core Ruff and `uv lock --check` | Pass |
| TUI `cargo test --no-fail-fast` | 861 passed, 10 ignored in the final full rerun (including the new wide-sidebar regression) |
| Real-Core TUI PTY test (`--test terminal_core -- --ignored`) | 1 passed |
| TUI formatting / Clippy, all targets, warnings denied | Pass; formatted the fork timeline test |
| NSH `npm run check` | Pass |
| NSH full web suite | Final full confirmation: **371 passed, 0 failed** (13m 31s). Initial run: 370 passed, 1 inspector test-host disconnect; focused inspector rerun: 12 passed. Full rerun passed unchanged |
| NSH host tests | 11 passed |
| Browser terminal-page test | 1 passed |
| Native NSH library tests | 9 passed |
| Mermaid all-features / annotation / vendored editor | Pass |
| Shared manifest / theme overlay | Pass |
| Offline installer fixtures | Pass |
| Core wheel/sdist, release TUI, NSH web package builds | Pass (before release version bump) |

NSH web tests use the installed Playwright Chromium via `npm test -- --browser "$(node -e "console.log(require('playwright').chromium.executablePath())")"`; automatic browser discovery found no system Chrome. The build still emits existing unknown-theme-class warnings; these are not test failures.

The earlier Core shutdown failure did not recur in the full concurrent run or 20 additional paired repetitions (40 test executions). The twelve stale/failing TUI shell/editor checks from consolidation are resolved by the existing cleanup, and the final shell suite passes 165 tests with one opt-in screenshot test ignored. See [the cleanup report](test-cleanup-2026-11-08.md) for implementation details and repeat shutdown checks; known editor parity gaps remain open in PLAN.md. Added a wide-sidebar test clicking every manifest tab, including Git, and replaced unconditional Git exclusion with width-aware coverage; the default-width Git-tab visibility limitation is explicitly retained in PLAN.md.

## Non-gating replay benchmark

Debug build, 160×48, reduced motion, 200 messages / 902 events: first frame 77.80 ms; burst draw mean 9.50 ms / p95 10.48 ms; paced draw mean 10.55 ms / p95 13.65 ms; chrome-only redraws made zero timeline builds; RSS 44,544 KiB. This is a diagnostic, not a release performance threshold.

## Existing release-PR checks

[Run 42562862647](https://github.com/nottherealsanta/nexus/actions/runs/42562862647) passed for the refreshed release PR: Core macOS/Linux, TUI tests and release builds macOS/Linux, lint, Mermaid/annotation native and wasm checks, and `ci-ok`. The existing workflow skips NSH; its local full web confirmation passed separately (371/371). No CI configuration was changed.

## Release and PR scope

Use the existing release-please PR #100 for 0.12.0 after committing the tested tree. It was the only open PR at the start; #99 and the older superseded release PRs were already closed. GitHub PR history is retained, not deleted. No unrelated branch history is pruned.

The existing artifact workflow excludes the NSH web bundle. Expected published artifacts are four macOS/Linux TUI binaries, the Core wheel and sdist, `install.sh` and `SHA256SUMS`. Successful local web packaging does not imply a published web bundle. Published asset and clean-install verification remain pending until the release workflow finishes.
