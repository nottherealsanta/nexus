# 4. Fix all tests and lints (complete)

Moved from PLAN.md on 2026-11-11 when the plan became the `plans/` folder; text unchanged unless marked.

**2026-11-09 release validation:** Core **1290 passed, 1 skipped**; TUI **861 passed, 10 ignored**, plus the opt-in real-Core PTY test passed. Ruff, lock consistency, formatting, Clippy, shared manifest/theme checks, Mermaid/annotation/vendored editor, native library and installer fixtures pass. Core, release TUI and NSH web builds pass. NSH type check, 11 host tests and the final full web suite (**371/371**) pass. **Section 4 is complete.** See [the current validation report](../../reports/release-0.12.0-validation.md). Device/audio, remaining visual-parity and platform runtime checks stay in their own sections; this test gate does not complete them.

- [x] Full offline Core and TUI suites pass; Core macOS/Linux and TUI macOS/Linux release-PR checks also pass ([run 42562862647](https://github.com/nottherealsanta/nexus/actions/runs/42562862647)).
- [x] Ruff, lock consistency, formatting and Clippy pass; shared manifest/theme, Mermaid, annotation, vendored editor, native library and installer checks pass.
- [x] NSH type check, host tests and web packaging pass.
- [x] Final NSH full browser confirmation: **371 passed, 0 failed** (13m 31s). Initial run 370/371 had one inspector test lose its test-host connection; all 12 inspector tests then passed in isolation and the full rerun passed unchanged. The transient failure is recorded, not hidden with an assertion change or retry wrapper.
- [x] Record load-only failures and the non-gating replay benchmark in [the validation report](../../reports/release-0.12.0-validation.md): Core shutdown passed 20 additional paired repetitions; debug replay paced draw mean 10.55 ms / p95 13.65 ms, zero chrome-only timeline builds.

The older baseline is superseded; the consolidation failures and cleanup details remain in [the consolidation report](../../reports/consolidation-2026-11-08.md) and [the test cleanup report](../../reports/test-cleanup-2026-11-08.md). NSH web tests need an explicit Playwright Chromium path on this host; see the validation report and `docs/development.md`. Existing theme-class warnings and the separately listed UI/runtime gaps are not claimed fixed by passing tests.
