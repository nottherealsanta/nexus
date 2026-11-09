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

PR #100 merged at `a17354d3` after committing the tested tree; tag `v0.12.0` points to that commit. Its release branch was deleted. No open PRs remain; #99 and older superseded release PRs were already closed. GitHub PR history is retained, not deleted. No unrelated branch history was pruned.

[Release workflow 42564022608](https://github.com/nottherealsanta/nexus/actions/runs/42564022608) passed: macOS arm64/x86_64 and Linux aarch64/x86_64 TUI builds and help smoke tests, Core wheel/sdist, and artifact attachment. All eight expected assets are published on [v0.12.0](https://github.com/nottherealsanta/nexus/releases/tag/v0.12.0). Downloaded every asset and independently verified all seven payload hashes against `SHA256SUMS`. The existing workflow excludes the NSH web bundle; successful local web packaging does not imply a published web bundle.

### Published macOS arm64 installer smoke

Ran the downloaded, checksum-verified `install.sh --release v0.12.0` against GitHub in an isolated temporary `HOME`, `PREFIX`, `UV_TOOL_DIR`, `UV_TOOL_BIN_DIR`, XDG directories and `NEXUS10_HOME`. The installer downloaded and verified the release payloads, built `claude-agent-sdk` from source, and installed Core plus TUI successfully. Installed distribution metadata is **0.12.0**. Confirmed `nexus tui --help`, daemon start/stop on an isolated loopback port, `nexus tui --once`, and plain `nexus` in a 120×36 PTY: it rendered New session / Settings and exited cleanly with Ctrl+Q. No real installation or daemon was modified.

`nexus doctor` reached the isolated daemon and validated config/MCP/rg; its nonzero status is expected with no provider sign-ins, models cache, search service or voice models in the fresh home. The existing daemon info/version display still says `0.10.0.dev0`; distribution metadata, tag and package versions are verified separately as 0.12.0. That diagnostic-label discrepancy remains open.

Smoke-harness corrections: neither Core nor TUI supports `--version`; use distribution metadata. TUI flags go after `nexus tui`. The daemon requires a concrete unused port (not `NEXUS_PORT=0`), and an explicit scratch home must be created private (`0700`). Initial attempts using those unsupported flags/port and a non-private override directory were corrected before the successful run; these were not hidden as passing checks.

Linux published installer execution and the wider per-platform runtime matrix remain unverified locally; Linux builds/help smoke and macOS/Linux automated suites passed in GitHub.
