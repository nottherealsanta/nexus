# Test cleanup — 2026-11-08

Follow-up to the consolidation checkpoint. This report covers automated tests and lints, not release installation on real platforms, device/audio checks, or UI screenshot gates.

## Core

- Full offline suite with a scratch `NEXUS10_HOME`: **1275 passed, 1 skipped**.
- `uv run ruff check nexus tests`: clean.
- Recovery subprocess fixture now explicitly binds loopback: its Bearer-authenticated requests require the loopback token policy, not the new tokenless network default.
- Added deterministic shutdown coverage injecting a process-group `PermissionError`; verifies reaping, output completion and descriptor closure. Production handling was already fixed in `3551ef4d`. The previously failing case and new regression passed 60 paired repetitions (120 executions).

## TUI

- Final full `cargo test`: **857 passed, 10 ignored**; shell suite **163 passed, 1 ignored** (the added screenshot helper is intentionally opt-in).
- `cargo test --test terminal_core -- --ignored`: **1 passed**, exercising real Core PTY input/output in an isolated fixture.
- `cargo fmt --check` and `cargo clippy --all-targets -- -D warnings`: clean.
- Replaced stale read-only viewer expectations with editable-source tests while preserving revision/save/conflict/external-change, search, modal and long-file safety coverage. Long-line tests now cover horizontal scrolling in the vendored editor. NSH still uses soft wrapping: that existing parity gap remains explicitly open in PLAN.md rather than being declared an approved difference or silently counted as fixed.
- Fixed End placement (including Unicode and final lines without newlines), shifted shortcut normalization, Markdown preview dispatch, and editor clipboard/undo/redo routing. These restore the existing shared editor contract.

## Shared checks

- `cargo test --manifest-path nexus-mermaid/Cargo.toml`: 6 passed; `--all-features`: 35 passed (including the shared scenario).
- `cargo test --manifest-path nexus-tui/vendor/ratatui-code-editor/Cargo.toml`: 31 passed.
- `cargo test --manifest-path nexus-annotate/Cargo.toml`: 7 passed.
- `python3 interface/check_manifest.py`: 210 commands, 38 slash names, 11 settings sections, 38 rows; clean.
- `python3 interface/check_theme.py --overlay nexus-nsh/theme-void.toml`: 602 checks; clean.
- `sh scripts/test-install.sh`: passed (offline installer fixtures, not a real published-release install).
- `uv lock --check` in `nexus-core`: clean.
- `cargo test --manifest-path nexus-nsh/native/Cargo.toml --lib`: 9 passed; no physical recording/device access.

## Non-gating replay benchmark

`cd nexus-tui && NEXUS10_REDUCED_MOTION=1 cargo run -- --dark --bench 160x48 --replay`

Debug build, 200 messages / 902 events: first frame 72.41 ms; burst draw mean 9.76 ms / p95 11.31 ms; paced draw mean 13.94 ms / p95 32.04 ms; chrome-only redraws performed zero timeline builds; RSS 37,552 KiB. This is a local diagnostic, not a release performance threshold.
