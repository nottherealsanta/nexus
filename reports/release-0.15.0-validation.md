# Nexus v0.15.0 release handoff

**Status (2026-11-23): not published.** v0.14.0 is still the latest release. The feature changes are on `main` via [#104](https://github.com/nottherealsanta/nexus/pull/104). The Firecrawl fixture follow-up [#105](https://github.com/nottherealsanta/nexus/pull/105) also shows as merged, but its hosted Core checks failed. [Release PR #103](https://github.com/nottherealsanta/nexus/pull/103) is the version bump; its checks can pass without running Core. Do not interpret that result as validation of the candidate.

The user approved publishing with the known **NSH** failures disclosed. The later **Core** CI failures were not part of that approval and are the current release blocker.

## Continuation (2026-11-23)

- [PR #106](https://github.com/nottherealsanta/nexus/pull/106) removes reverse DNS from the fake Firecrawl server's bind path and logs binding/listening milestones. Core now reports whether the child exited or the last health probe failed. All 16 Firecrawl tests passed locally; the latest hosted macOS run reports no Firecrawl failures.
- Hosted logs showed synchronous Git and bulk file setup in search tests and synchronous SQLite initialization in terminal HTTP tests. Those now run in synchronous fixtures. The Core suite can still use test catalog caches but no longer attempts live models.dev downloads. The **100 ms watchdog is unchanged**. Local macOS: **62 focused tests passed**; after switching the workers-route test to a local cache, all **1,785 test assertions passed**, with 2 skips and one watchdog teardown error in input integration (142 ms wall / 23 ms thread CPU).
- The user requires the NSH web bundle and signed off on first-time Firecrawl 0.1.1 binary redistribution with its licence and source notice. The artifact workflow now enables `nsh-web` using the locally tested NSH revision. NSH type check, **11 host tests**, web build, archive extraction and a real request to the extracted host passed locally. The offline installer test verifies the app and host land in the installed web directory.
- [Hosted artifact dry run](https://github.com/nottherealsanta/nexus/actions/runs/45168609035) **passed** on `e12f45df`: Core wheel and source archive, four TUI binaries, four Firecrawl binaries and the required NSH web archive. The eight binaries match their named architectures. The downloaded NSH archive contains `Nexus/index.html` and `serve.mjs`, and the extracted host served that index locally (HTTP 200). These are dry-run artifacts, not assets from the v0.15.0 tag.
- The [Core matrix on `61decaa9`](https://github.com/nottherealsanta/nexus/actions/runs/45174708746) passed on Ubuntu and both TUI jobs passed. macOS reported eight watchdog errors and one bash-job test failure: the test awaited the internal reader task even after `kill()` had settled it, which can legitimately cancel the task. The test now verifies the terminal job state and checks cleanup without awaiting that cancelled task. Other fixes move the Git and database test setup seen in hosted stacks off the loop. All **93 affected tests** passed locally; hosted confirmation is pending.

## Core: blocking issues

| Hosted run | macOS | Ubuntu |
| --- | --- | --- |
| [Feature PR run](https://github.com/nottherealsanta/nexus/actions/runs/45162026752) | 1,763 passed, 14 skipped, 9 failures, 21 errors | 1,773 passed, 13 skipped, 13 errors |
| [Fixture follow-up run](https://github.com/nottherealsanta/nexus/actions/runs/45164380048) | 1,763 passed, 14 skipped, 9 failures, 33 errors | 1,773 passed, 13 skipped, 6 errors |
| [PR #106, `c230c75e`](https://github.com/nottherealsanta/nexus/actions/runs/45169211926) | 1,764 passed, 14 skipped, 16 errors (9 fixtures, 7 watchdog) | 1,765 passed, 13 skipped, 11 errors (9 fixtures, 2 watchdog) |
| [PR #106, `c27096e8`](https://github.com/nottherealsanta/nexus/actions/runs/45173126853) | 1,773 passed, 14 skipped, 12 watchdog errors | 1,774 passed, 13 skipped, 4 watchdog errors |
| [PR #106, `61decaa9`](https://github.com/nottherealsanta/nexus/actions/runs/45174708746) | 1,772 passed, 14 skipped, 1 failure, 8 watchdog errors | **1,774 passed, 13 skipped** |

### macOS: fake Firecrawl startup (passed on `c230c75e`)

All nine assertion/test failures are in `nexus-core/tests/test_firecrawl.py`. They share a startup problem: the fake `firecrawl-local` never answers `/health` successfully. The refused-target test also fails because the startup error arrives instead of the expected target error. This is evidence about the fixture path, not a verified failure of the real Firecrawl binary.

- Launcher: `nexus-core/tests/test_firecrawl.py`, fixture `fake`. It wraps Python in a shell script and overrides `firecrawl.binary()` to point at that script.
- Child: `nexus-core/tests/fixtures/fake_firecrawl.py`. It binds a Python HTTP server to loopback and serves canned responses.
- Startup and health polling: `nexus-core/nexus/firecrawl.py`, `ensure()` and `_wait_ready()`.
- #105 quotes launch paths and prints the captured child log on failure. The hosted failure persisted and the child log was empty. All **15 Firecrawl tests** passed locally after the change.
- Next diagnostic: run only `tests/test_firecrawl.py` on hosted macOS. Log the child PID and return code, print and flush a marker before and after binding, and record the last health probe result or exception. Distinguish an exited child, a child that never binds, and a bound server that the parent cannot reach. The current logs do not establish which happened.

### Both platforms: event-loop timing errors

`nexus-core/tests/conftest.py` fails a test at teardown if an event-loop callback takes at least 100 ms. Linux had no test assertion failures, but the guard failed on both hosted runs and the number of affected tests changed. Logs report wall time and thread CPU time; for example, the first Linux run included **2,296 ms wall / 25 ms CPU** and **213 ms wall / 209 ms CPU**. Low CPU time does not by itself distinguish runner scheduling from blocking I/O; high CPU time makes CPU work on the loop worth investigating.

Start from the callback timings and test names in the hosted logs. Re-run affected tests in isolation, then rerun the full Core matrix. Move confirmed blocking work off the loop rather than raising the threshold across the suite. See [plan 50](../plans/50-rust-core.md). A previous local Core run passed **1,784 tests, 2 skipped**; that does not clear the hosted errors.

```sh
cd nexus-core
export NEXUS10_HOME=$(mktemp -d)
uv run --locked pytest -q tests/test_firecrawl.py
uv run --locked pytest -q --durations=25
```

## NSH: known failures approved for disclosure

- Full Chrome disconnected repeatedly: **405 passed, 184 failed**. Use Playwright's Chromium headless shell for browser reproduction; see [development docs](../docs/development.md).
- Chromium headless shell: **548 passed, 41 failed** on the earlier candidate. Of those, **23** were tests run on an unsupported target (19 mobile, 4 pointer/keyboard input); all 23 passed on NSH's headless target. Do not run all desktop web tests on that target: many fail with `Invalid URL: /api/info` rather than exercise the browser behavior.
- Some other failures cleared in focused reruns after adding project MCP mocks and waiting for session history. The last full session-controls run was **21 passed, 5 failed**, before the final wait-helper adjustment; `/search` and archive refusal then passed in focused reruns. The multiline queued-edit test still failed: the row shows **Enter edit**, but Enter does not restore the draft.
- Other unresolved browser failures include timeline scroll restoration (**Jump to latest** disappears after returning to a scrolled session), history reload after a simulated Core restart, linked worktree display, and opening annotation from a pasted image. These are test observations; not all have been independently verified in the live app. The full timeline file was **13 passed, 1 failed** after the scrollbar fixture fix.
- NSH type check, host tests, web build, and terminal browser test passed locally. TUI and Mermaid passed on hosted CI. Installer checks and Core packaging passed locally.

## Publication checklist

1. Verify Core is green on macOS and Ubuntu for the current `main` candidate. Confirm the release PR includes any required fixes. Check the artifact dry run, including NSH web and all four Firecrawl binaries.
2. Merge [#103](https://github.com/nottherealsanta/nexus/pull/103) to let release-please create `v0.15.0`. Watch the release workflow through the artifact upload step; a tag alone does not mean the assets are ready.
3. Check the release for Core wheel and sdist, TUI binaries, NSH web bundle, installer, four Firecrawl binaries, third-party notice, and `SHA256SUMS`. Verify the assets against the checksums and disclose the known NSH issues in the release notes.

### Disclosure to add to the GitHub release notes

The NSH web bundle is included. Its type check, host tests and packaging passed, but the full browser suite is not green. In the Chromium headless shell run, 548 passed and 41 failed; 23 of those failures passed on their supported headless target. Remaining observations include queued multiline editing with Enter, timeline scroll restoration, history reload after a simulated Core restart, linked worktree display, and pasted-image annotation. These are test results, not all independently reproduced in the app. See the [validation handoff](https://github.com/nottherealsanta/nexus/blob/main/reports/release-0.15.0-validation.md#nsh-known-failures-approved-for-disclosure).
