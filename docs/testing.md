# Testing

Tests need **no network and no credentials**. They use temporary workspaces,
`ScriptedProvider`, recorded fixtures under `tests/fixtures/`, and a private
`NEXUS_HOME` per test (`tests/conftest.py`, autouse), so nothing ever touches the
real `~/.nexus`. `pytest-asyncio` runs in `auto` mode; `live` tests (real
providers, credentials) are deselected by default (`addopts = -m 'not live'`).

## Commands

```sh
uv sync --extra dev                                 # or: pip install -e '.[dev]'  (Python ≥ 3.13)
.venv/bin/python -m pytest -q                       # full offline suite
.venv/bin/python -m pytest -q tests/test_host_facade.py -k name
ruff check nexus tests                              # ruff is installed separately; rules E4 E9 F E713
./rust-build-test.sh                                # locked Rust build and unit tests (Ratatui)
python -m playwright install chromium               # once, for browser checks
.venv/bin/python tests/playwright_web_check.py      # real-browser web client check
.venv/bin/python tests/playwright_ratatui_check.py  # native PTY screenshots
python -m tests.provider_conformance                # provider conformance matrix (offline)
ANTHROPIC_API_KEY=… pytest -m live tests/test_anthropic_live.py   # gated live test
```

CI (`.github/workflows/ci.yml`) runs locked Rust builds/tests, Ruff and the full
Python offline suite on Linux, Python 3.13. A second job builds the wheel and runs
packaging/installer tests. The required check is `ci-ok`. See [release.md](release.md).

The worktree is often dirty with unrelated work. If an unrelated test already
fails, say so; do not change it.

## Fakes and seams

- **Scripted runs:** `ScriptedProvider(text_response(...), tool_response(("id", "edit", {...})), [MessageStart(...), TextDelta(...), Wait(event), ...])`.
  Each script is consumed once, in order, across all sessions; `ScriptExhausted`
  when it runs out.
- **Full stack in-process:** `Runtime(path, config=Config(...), providers={"scripted": provider})`
  wrapped in `Daemon(workspace, socket_path=..., runtime_factory=...)`; see
  `tests/playwright_web_check.py:main()`. **macOS limits Unix socket paths to
  ~104 bytes**, so keep `socket_path` short or relative.
- **Loop with fakes:** the loop takes protocols, so `test_core_loop.py` passes fake
  sessions, assemblers and dispatchers.
- **Host doubles:** UI test transports reject unknown commands; give any new host
  call a fake response (`tests/test_ratatui_journeys.py`).
- **Offline catalogue:** the registry accepts an injected fetcher and cache paths,
  so no test reaches models.dev.

## Suites by area

| Area | Tests |
| --- | --- |
| Layering and budgets | `test_layering.py`, `test_ui_layering.py`, `test_phase3_exit.py` |
| Config | `test_config_*.py` |
| Events / view | `test_errors_events.py`, `test_events_surface_amendment.py`, `test_view_reduce.py` (+ `fixtures/view/`) |
| Loop / core | `test_core_*.py`, `test_capability_degradation.py`, `test_thinking_*` |
| Providers | `test_*_provider.py`, `test_provider_*`, `provider_conformance/` (text, single and parallel tool calls, partial-JSON args, malformed args, thinking/signature replay, refusal, 429-then-success, mid-stream disconnect, usage), `test_auth_*.py`, `test_model_*.py` |
| Context | `test_context_*.py` |
| Sessions | `test_session_*.py`, `test_runtime_shared_state.py` |
| Tools | `test_tool*.py`, `test_builtin_*.py`, `test_patch_*.py` |
| Extensions | `test_ext_*.py`, `test_hot_*.py`, `test_skills*.py`, `test_hooks_manager.py`, `test_mcp_*.py` (fixtures in `tests/fixtures/{extensions,hooks,mcp}`) |
| Agents | `test_agents_manager.py`, `test_subagent_*.py`, `test_worktree_*.py`, `test_host_worktrees.py` |
| Host | `test_host_*.py`, `test_uds_*.py`, `test_http_*.py`, `test_web_transport.py` |
| Security | `test_security_regressions.py`, `test_extension_security.py`, `test_outbound_*.py`, `test_config_secrets.py` |
| TUI | `test_ratatui_*.py`, `test_prompt_history.py` |
| Dev mode | `test_mock_*.py`, `test_benchmark.py` |
| Release | `test_install_script.py`, `test_update_*`, `test_model_data_package.py` |
| Docs | `test_docs.py` |

Notable adversarial tests: the 200-reload leak test and the
write-a-tool-and-call-it walkthrough (`test_hot_extension_*`); crash-resume and
cancellation at each await point (`test_core_loop.py`, `test_session_send.py`).

## Browser and visual checks

| Script | What it does |
| --- | --- |
| `tests/playwright_web_check.py` | end-to-end web check; screenshots to `artifacts/web-e2e/`; a few minutes |
| `tests/playwright_context_web_check.py`, `playwright_message_check.py` | web context and queued-message flows |

`artifacts/` holds ignored screenshots and benchmark output.

## Rules

- New behavior gets a test next to its peers (`tests/test_<area>_*.py`). UI changes
  also get a browser or native PTY check where one exists.
- Keep tests deterministic: no sleeps for correctness, injected clocks and
  fetchers, `tmp_path` workspaces.
- Live tests are opt-in (`-m live`, credentials in the environment); never commit
  credentials, headers, encrypted reasoning or private output in fixtures.
- Never weaken, skip or delete a test to make it pass.

`tests/playwright_project_sessions_check.py` starts two offline workspace daemons
sharing an isolated home. It checks duplicate project/session names, date groups,
path filtering, opening the owning project, reconnect, and light/dark screenshots
at 1440, 1024 and 400px (`artifacts/project-sessions/`).

### Native terminal checks

Build the debug executable before running `tests/test_ratatui_pty.py`; the test
needs local controlling-terminal permissions and checks keyboard input,
approval decisions, Settings edits and canonical/echo/signal restoration. Native
cell layouts, Markdown and editor transitions use `cargo test --manifest-path
rust/tui/Cargo.toml`. Python tests in `test_ratatui_*` exercise the actual scripted
harness, replay, launch routing, preferences, failed submissions, Settings hash
conflicts and credential-form handling. Source wheel builds require the dev
`setuptools-rust` dependency and Rust ≥1.88.

The native terminal screenshot check is
`PYTHONPATH=. python tests/playwright_ratatui_check.py` after a Cargo build.
It captures recorded events, draft input and resizing under `artifacts/ratatui-parity/`.
`browser_serve.py` serves vendored MIT-licensed xterm assets over loopback and
forwards bounded packets to `ratatui_browser_demo.py`, which owns the controlling PTY.
These helpers are not installed product surfaces.

`PYTHONPATH=. python tests/playwright_ratatui_live.py "/mock question" "blue wins"`
drives the real `nexus --dev chat --renderer ratatui` (real daemon, mock provider,
no credentials) through the same adapter (`ratatui_browser_demo.py --bridge CMD`)
and saves one screenshot per typed line (or `click:X,Y` / `key:Control+p` step; it uses fresh preferences so both sidebars show) under `artifacts/ratatui-live/`. It found
the "context preview unavailable while the session is active" bug that unit tests
missed.

## Native redesign verification

Run `cargo test --locked --manifest-path rust/tui/Cargo.toml` and
`.venv/bin/python -m pytest -q tests -k ratatui`. The two ignored Rust benchmarks
are manual measurements; the 2,000-turn case is
`cargo test --release --manifest-path rust/tui/Cargo.toml streaming_2000 -- --ignored --nocapture`.

Build the release binary before running
`.venv/bin/python tests/ratatui_performance_check.py`. This controlling-PTY test
feeds 2,000 turns, 300 wheel events, 50 keys and 50 suffix patches per second,
drains terminal output, samples native-process CPU, and writes ignored artifacts
under `artifacts/ratatui-parity/`. It excludes Python projection and provider latency.
A controlling TTY and process CPU inspection may require sandbox escalation.

`NEXUS_RATATUI_MATRIX=1 .venv/bin/python tests/playwright_ratatui_check.py`
captures both themes, four sidebar combinations, and 80/120/200-column layouts,
plus expanded tools, context popover, running composer and details tabs.
`NEXUS_TUI_TRACE=1` enables bounded native timing statistics in Logs;
`NEXUS_TUI_TRACE_FILE=/path/report.log` writes an exit report.

The cancellation-during-approval integration check waits up to five seconds for
the durable permission request, rather than counting event-loop yields; session
setup performs disk I/O and yield counts are not a portable readiness bound.


Native responsiveness checks: `tests/test_ratatui_responsiveness.py` covers
section omissions, one-shots, coalescing, changed-tail projection, stale completions
and debounced preferences; `tests/test_ratatui_pty.py -k local_disclosure` drives
keyboard/mouse expansion with no Python reply. Rust cache tests assert row-part
pointer reuse. `PYTHONPATH=.:tests .venv/bin/python tests/tui_responsiveness_bench.py
--label current --native` records short/500-turn and large-output timings under
`artifacts/tui-responsiveness/`. Native probing requires controlling-PTY access.
The baseline mode can load an explicitly supplied pre-change projection module
(`--baseline-module`) and executable (`--binary`); use a saved checkout/build for
repeatable before/after results. It does not emulate provider/network latency.

## Native end to end (bridge + binary)

`tests/test_ratatui_e2e_settings.py` starts the real Python bridge loop and the real native
binary under a pseudo-terminal with a scripted host, sends keystrokes and mouse-wheel escape
sequences, and asserts on the emulated screen. Use it for anything where the host's snapshot and
the client's operations must agree; hand-built snapshots (`test_ratatui_pty_*.py`) cannot catch
that. Build `rust/tui` first (`cargo build`); the tests skip without the binary.
