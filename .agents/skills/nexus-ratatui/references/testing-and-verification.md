# Testing and verification

## Layers

| Check | Command | Proves |
| --- | --- | --- |
| Rust units | `cargo test --locked --manifest-path rust/tui/Cargo.toml` | editor, wrap, card widths, snapshot schema, layout |
| Projection | `pytest tests/test_ratatui_projection.py tests/test_ratatui_prototype.py` | block order, gaps, context/details data, cache reuse |
| Commands/actions | `tests/test_ratatui_actions.py` | slash command contracts, shortcut table bound in `main.rs` |
| Workflows/journeys | `tests/test_ratatui_workflows.py`, `tests/test_ratatui_journeys.py` | settings, providers, worktrees (real git), attachments through the real host facade |
| Completion | `tests/test_ratatui_completion.py` | shared completion and model picker logic |
| Integration | `tests/test_ratatui_integration.py` | real daemon + scripted provider; live == replay projection |
| PTY | `tests/test_ratatui_pty.py` (needs the debug binary built) | real controlling terminal: submit, completion, permission, settings, quit, terminal restore |
| Launch/CLI | `tests/test_ratatui_launch.py`, `tests/test_cli.py` | renderer selection (`auto`), binary discovery |
| Layering/docs | `tests/test_ui_layering.py`, `tests/test_layering.py`, `tests/test_docs.py` | toolkit-free Python clients, module-map rows |
| Screens | `tests/playwright_ratatui_check.py` | real native terminal rendering |

Unit tests do not establish visual parity. Screenshots do not establish
interaction parity. State which you ran.

## Screenshots

`tests/playwright_ratatui_check.py` captures the native terminal and writes
`artifacts/ratatui-parity/` (ignored by git): `ratatui.png`, draft/narrow images,
and per-state screenshots. `NEXUS_RATATUI_STATE` selects recorded fixture states.
The helper uses vendored MIT-licensed xterm assets, a loopback WebSocket server,
and a controlling PTY; it has no production role.
Add new states in `tests/ratatui_browser_demo.py`, then inspect their PNGs.

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
  `ContextInspectResult`); unwrap fields (`result.report`).
- Two argparse help tests fail: `FORCE_COLOR` is set. Use `env -u FORCE_COLOR`.
- `command not found: timeout` / `sed: -i may not be used`: macOS. Use Python.
- PTY test fails to start: build the debug binary first.
- A projection test spies `_project_turn` and sees extra calls: the cache `flags`
  changed; update the test, not the cache semantics.
- Layout change broke clicks: update the hit-test in `main.rs`.
- A snapshot "never arrives": identical snapshots are deduplicated; change something
  or send a one-shot field.
- Docs test fails after adding a Python module: add a row to `docs/module-map.md`.

## Before reporting done

Run Rust tests, `pytest -k ratatui`, `tests/test_ui_layering.py tests/test_docs.py`,
`ruff check nexus tests`, and (for visuals) the Playwright check; then the full
suite if shared `ui_support`, host or CLI code changed. Report numbers, name
anything unrun, and update `docs/ratatui-parity.md`.


Responsiveness: run `tests/test_ratatui_responsiveness.py` and the independent
`tests/test_ratatui_pty.py -k local_disclosure` check. The latter drives keyboard
and mouse with Python silent. Rust cache regressions prove Arc pointer reuse.
Run `tests/tui_responsiveness_bench.py --label current --native` with
`PYTHONPATH=.:tests`; inspect native local-disclosure captures. Record initial-load
and large-output costs separately, and never claim end-to-end targets from Python
microbenchmarks alone. Keep desktop wire/schedule regressions passing.
