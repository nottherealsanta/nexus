# 0.13.0 validation — 2026-11-16

This is not a release approval. Plans 34 and 37 remain open, the full gate is incomplete, and the release PR has not been merged. Tests use scratch `NEXUS10_HOME`; no paid provider calls or real-home migration were performed.

## Local checks

| Check | Result |
| --- | --- |
| Core full offline suite, Ruff and `uv lock --check` | 1608 passed, 2 skipped (271.03 s); Ruff and lock check pass |
| TUI `cargo test --no-fail-fast` | 1005 passed, 10 ignored |
| Real-Core TUI PTY (`--test terminal_core -- --ignored`) | 1 passed |
| TUI `cargo fmt --check`, Clippy all targets with warnings denied, release build | Pass |
| NSH `npm run check` | Initial run failed on two TypeScript errors in the pending-turn fixture and nullable event session ID; after the type corrections, pass |
| NSH pending-turn focused suite | 5 passed |
| NSH full web suite | Running; no final result yet |
| NSH host tests | 11 passed |
| NSH web package build | Pass, with existing unknown-theme-class warnings |
| Shared manifest / theme overlay | Pass: 232 commands, 38 slash names, 13 settings sections, 42 rows; 602 contrast checks |
| Offline installer fixtures | Pass |
| Mermaid all-features | 35 passed (34 unit, 1 scenario) |
| Annotation all-features | 7 passed |
| Core wheel / sdist | Pass; built from main's pre-release 0.12.0 package metadata, not the release PR |

One combined command initially failed because it used the nonexistent directory `nexus-annotation`; rerunning with `nexus-annotate` passed. The earlier successful checks in that command were not rerun.

## Release and PR scope

`main` through `99c69bff` was pushed; a subsequent NSH typecheck fix is committed locally. PR #101 was open when checked. Its listed `ci-ok`, `changes`, `lint` and `pr-title` checks passed, but Core, TUI, NSH and Mermaid jobs were skipped, so this is not a substitute for the local gate. The PR refresh, version files and changelog have not yet been verified. No release workflow or published installer verification has been run.

## Still open

Plan 34 child-card state, Inspector Timing span and parent-wait test; plan 37 scripted timings; plans index; remaining gate checks and final results. Hand checks and carried-over non-blockers remain in `reports/pre-release-2026-11/user-checklist.md`.

## Released as is (2026-11-16, user decision)

The user asked to release 0.13 as it stood and to stop the test suite. The NSH full web suite was stopped before it
finished, so it has no result. Plans 34 and 37 ship with their open items. PR #101 was squash-merged at `e05f1db5`
after its `changes`, `lint`, `pr-title` and `ci-ok` checks passed (Core, TUI, NSH and Mermaid jobs were skipped).
[Release workflow 43871610506](https://github.com/nottherealsanta/nexus/actions/runs/43871610506) passed: four TUI
builds, Core wheel/sdist, attach; NSH web skipped as before. All eight assets are on
[v0.13.0](https://github.com/nottherealsanta/nexus/releases/tag/v0.13.0); all seven payload hashes verify against
`SHA256SUMS`, and the macOS arm64 TUI binary runs `--help`. The published installer was not run in an isolated home.
