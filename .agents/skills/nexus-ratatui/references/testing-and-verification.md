# Testing and verification

## Layers

| Check | Command | Proves |
| --- | --- | --- |
| Rust units | `cargo test --locked --manifest-path rust/tui/Cargo.toml` | editor, wrap, card widths, snapshot schema, layout |
| Projection | `pytest tests/test_ratatui_projection.py tests/test_ratatui_prototype.py` | block order, gaps, context/details data, cache reuse |
| Commands/actions | `tests/test_ratatui_actions.py` | slash commands match Textual, shortcut table bound in `main.rs` |
| Workflows/journeys | `tests/test_ratatui_workflows.py`, `tests/test_ratatui_journeys.py` | settings, providers, worktrees (real git), attachments through the real host facade |
| Completion | `tests/test_ratatui_completion.py` | shared completion and model picker logic |
| Integration | `tests/test_ratatui_integration.py` | real daemon + scripted provider; live == replay projection |
| PTY | `tests/test_ratatui_pty.py` (needs the debug binary built) | real controlling terminal: submit, completion, permission, settings, quit, terminal restore |
| Launch/CLI | `tests/test_ratatui_launch.py`, `tests/test_cli.py` | renderer selection (`auto`), binary discovery |
| Layering/docs | `tests/test_ui_layering.py`, `tests/test_layering.py`, `tests/test_docs.py` | no Textual in native, module-map rows |
| Screens | `tests/playwright_ratatui_check.py` | real rendered terminals of both clients, side by side |

Unit tests do not establish visual parity. Screenshots do not establish
interaction parity. State which you ran.

## Screenshots

`tests/playwright_ratatui_check.py` serves each client in a browser terminal and
writes `artifacts/ratatui-parity/` (ignored by git): `textual.png`, `ratatui.png`,
`*-draft.png`, `*-narrow.png`, and per-state shots (`-permission`, `-picker`,
`-panel`, `-light`). Native states come from `NEXUS_RATATUI_STATE` in
`tests/ratatui_browser_demo.py`, which mutates a real projected snapshot. To check a
new screen, add a state there and a row in the script's list, then *look at the PNG*
(open it) next to the Textual one (`tests/visual_tui_demo.py --state ...`).
The fixture compares against Textual's `reference` state; the live shell can differ
slightly (`app.tcss` `.reference-demo` rules), so confirm against the live CSS.

## Writing tests

- Prefer deterministic fakes: `SimpleNamespace` controller + `ShellActions`, or the
  real host with `ScriptedProvider` (see `test_ratatui_integration.py`).
- Projection tests build `TurnView`/`MessageView`/`ToolCallView` directly and call
  `project(controller, revision, shell=shell)`; set `XDG_STATE_HOME` and
  `XDG_CONFIG_HOME` to `tmp_path` so preferences do not touch the real home.
- Rust tests: build `Content`/`Snapshot` with `..Default::default()`; assert rows,
  widths and styles. `TestBackend` is used for whole-frame smoke tests.
- Every new behaviour gets a test next to its peers; UI changes also get a
  screenshot.

## Common failures

- `'X' object has no attribute 'get'`: the host returns structs (`DoctorResult`,
  `ContextInspectResult`); unwrap fields (`result.report`) as Textual does.
- Two argparse help tests fail: `FORCE_COLOR` is set. Use `env -u FORCE_COLOR`.
- `command not found: timeout` / `sed: -i may not be used`: macOS. Use Python.
- PTY test fails to start: build the debug binary first.
- A projection test spies `_project_turn` and sees extra calls: the cache `flags`
  changed; update the test, not the cache semantics.
- Layout change broke clicks: update the hit-test in `main.rs`.
- A snapshot "never arrives": identical snapshots are deduplicated; change something
  or send a one-shot field.
- Docs test fails after adding a Python module: add a row to `docs/module-map.md`.
- The Textual timing-sensitive group is flaky when run in the full suite; rerun the
  failing file alone before treating it as a regression.

## Before reporting done

Run Rust tests, `pytest -k ratatui`, `tests/test_ui_layering.py tests/test_docs.py`,
`ruff check nexus tests`, and (for visuals) the Playwright check; then the full
suite if shared `ui_support`, host or CLI code changed. Report numbers, name
anything unrun, and update `plans/RATATUI_PLAN.md`.
