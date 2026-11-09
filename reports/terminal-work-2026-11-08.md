# Terminal implementation — 2026-11-08

Implemented PLAN section 2 and the terminal-only portions of 9.22. Browser work,
agent typing, paid models, real-home migration and deployment were excluded.
Pre-existing editor/test-cleanup changes in the checkout were preserved.
Implementation is not blanket release approval: remaining gates are listed below.

## Shipped in this change

- Core immediately detaches slow viewers and releases writer authority on queue
  overflow. End sentinels survive later output; healthy viewers keep monotonic
  sequences. Restart clears controller identity in every persisted status.
- Core's diagnostic pyte parser bounds OSC/CSI accumulation at 4,096 characters,
  drops oversized controls through their genuine terminator, and never changes
  the raw client stream or falsely declares an unsafe replay boundary safe.
- Actual Core SIGKILL subprocess coverage confirms SIGHUP delivery to a
  separately grouped foreground job on macOS. Normal shutdown also covers a job
  ignoring HUP/TERM. Escaped sessions and jobs ignoring HUP on abrupt Core death
  are not promised forced termination.
- Both clients revoke input on exit, reject late control responses after exit,
  and reconnect read-only without replaying pending input. Take control is needed
  again. TUI omits closed resources and prefers running resources, matching NSH.
- Fixed a real NSH creation race: competing list refreshes could omit a newly
  created resource before selection, suppressing automatic control acquisition.
  The creation response is now installed before selection, matching the TUI.
- Both clients freeze selected rendered text into an immutable, scrollable
  **Attach terminal output** preview. **Add to draft** appends without replacing
  selected draft text or attachments. Empty drafts gain no leading separator.
  Nothing is sent automatically. Changed/read-only destinations fail visibly.
- Evidence includes frozen resource provenance, sequence watermark, explicit
  untrusted-data warning, JSON-encoded text, and exact UTF-8 included/omitted
  selected-byte counts under a 16 KiB code-point-safe limit. Unknown earlier
  terminal history is disclosed rather than assigned fabricated byte counts.
  Recorded source session/turn/tool/job IDs are included when present, never
  guessed from the destination draft; both clients verify the same shared source fixture.
  DEL/C1 characters are JSON-escaped before preview, preventing the TUI composer
  filter from silently changing the previewed payload; the shared regression
  confirms decoding preserves the original selection and draft bytes stay exact.
- Tool-linked terminals have sanitized inspector Timing bars, filters and full
  records, with recorded lifetimes independent of the launching turn. Durable
  `terminal.updated` metadata events invalidate both clients on lifecycle/control
  changes and recovered endings, without polling, output bytes or model usage.
- Fixed question-card hint wrapping/overlap in both clients and verified complete
  question/option/preview scrolling at short and narrow sizes.
- Updated API, daemon, stream-events, tools, inspector and terminal docs, both
  client READMEs, the shared manifest/scenarios, ledger and PLAN.

## Verification

| Check | Result |
| --- | --- |
| Focused Core terminal/job/inspector suites | **58 passed**, including sanitized invalidation/restart tests |
| Full Core Ruff | **Passed** |
| Full TUI suite | **865 passed, 10 ignored**, after final append/provenance parity fixes |
| TUI Clippy (`--all-targets -- -D warnings`) and build | **Passed** |
| TUI real-Core integration (`terminal_core -- --ignored`) | **1 passed**, actual isolated Core/PTY |
| NSH full web suite, earlier checkpoint | **362 passed, 0 failed**, 14m 3s; compiled before latest additions |
| NSH focused browser suites | Inspector 12, evidence logic 4, evidence UI 2, API 3, controller 6, dock 1, waiting/alerts 10: **38 passed** across focused runs |
| Actual xterm page | **1 passed**: keys, paste, alternate screen, Unicode, reset, explicit copy, request-correlated selection capture and OSC 52 blocking |
| NSH type check and packaged web build | **Passed** |
| Real-Core NSH smoke | **Passed**: create, rename, attach/control, Ctrl+C, 500-line output, selected-output capture, scrollable preview, cancel, confirm into draft, **zero automatic sends**, confirmed close and mobile exclusion |
| Shared checks | **211 commands**, **602 contrast checks**, `git diff --check` passed |

Core focused command:

```sh
cd nexus-core
uv run pytest -q tests/test_terminal_diagnostics.py tests/test_terminals.py \
  tests/test_terminals_http.py tests/test_terminals_lifecycle.py \
  tests/test_terminals_backpressure.py tests/test_terminals_abrupt_exit.py \
  tests/test_native_inspector.py tests/test_bash_jobs.py
uv run ruff check nexus tests
```

NSH tests use `node scripts/nsh-web.mjs test <file> --target web --browser <path>`
(one file per invocation), with Playwright's cached Chromium 1200 executable.
Plain `npm test` could not discover a system Chrome. Two attempted pytest
commands named nonexistent test files and ran no tests; corrected commands passed.

### Full Core suite is not a final green gate

An earlier full scratch-home run passed **1287 tests, 1 skipped** before the last
invalidation/restart additions. Later full runs exposed timing-sensitive tests
outside terminal paths: **1285 passed / 2 failed / 1 skipped**, then **1282 passed /
5 failed / 1 skipped**. No terminal-focused failure occurred. The seven affected
Manager/kernel checks all passed in isolated reruns; the refused-move Manager
case needed one further isolated rerun. These failures were not fixed or hidden:

- `test_manager_authority.py::test_a_message_to_an_archived_session_brings_it_back_and_says_so`
- `test_manager_service.py::test_cancel_before_any_side_effect_interrupts_the_turn`
- `test_manager_stage.py::test_a_refused_move_is_reported_as_a_failure_and_the_card_stays`
- `test_manager_stage.py::test_a_move_the_client_never_answers_times_out_honestly`
- `test_manager_stage.py::test_the_users_view_reaches_the_manager_once_and_again_only_when_it_changes`
- `test_manager_store.py::test_a_manager_episode_is_in_no_default_list_search_or_snapshot`
- `test_server.py::test_code_nested_write_needs_no_approval_and_kernel_api`

A transient TUI question-card height expectation and new dialog Clippy warning
were corrected; the final full TUI run passes. Real-browser work also found and
fixed the creation race and clipped Attach tooltip. Smoke setup/selector issues
(port 0 host validation, empty terminal indicator, wrapped IDs and textarea
selection) were corrected rather than counted as successful runs.

## Visual evidence

- [Real Core paired terminal docks](terminal-clients-2026-11-08/README.md).
- [Evidence preview/draft and current toolbar captures](terminal-evidence/README.md).
- Inspector: `section14/inspector/{tui,nsh}-terminals.png`, same shared fixture,
  expanded exited-resource record on the shared 0–10 s axis. Both were read and
  compared: labels, duration, diagnostic detail and terminal legend agree.
- [Short/narrow questions](question-readability/README.md), including a final
  TUI frame that proves the custom-answer field and complete footer are reachable.

The initial TUI dump pump ignored terminal effects; it now processes real
attach/stream/stop effects, avoiding blank-terminal screenshot false positives.

## High-output profile

A disposable in-memory TerminalService spawned Python writing 256 × 65,536 `X`
bytes, with a concurrent asyncio 10 ms ticker. This includes pyte parsing and ring
handling, not UI rendering. Other suites were running concurrently, so it is a
smoke profile rather than an isolated baseline.

| Measure | Result |
| --- | --- |
| Output | 16,777,216 bytes |
| Wall time / throughput | 9.846 s / 1.63 MiB/s |
| Retained / announced discarded | 1,048,576 / 15,728,640 bytes |
| Maximum extra timer delay | 12.55 ms |
| Exit code | 0 |

## Remaining release gates

- Linux/other OS execution, native AppKit WebView runtime and an authenticated
  remote failure/reconnect matrix have not been verified.
- A final all-green consolidated Core/NSH run remains separate from focused gates.
- Real delayed WebView replies across tab/session switches are guarded in code
  but not independently exercised end to end; payload, preview/draft switching,
  actual capture and cancellation paths are tested separately.
- Inspector paging follows launching turns; terminal-only filters can produce an
  empty page with older pages available. Manual terminals without actual recorded
  tool provenance deliberately have no lane.
- No interactive agent-control grant exists. The reviewed policy is user-only
  attachment authority, not shell-command allow patterns posing as protection.

All capture Cores/hosts started for this work were stopped. Tokenless non-loopback
mode still refuses terminal operations. No personal daemon was modified.
