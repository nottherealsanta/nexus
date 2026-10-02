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
python -m playwright install chromium               # once, for browser checks
.venv/bin/python tests/playwright_web_check.py      # real-browser web client check
.venv/bin/python tests/playwright_tui_check.py      # Textual shell served to a browser
python -m tests.provider_conformance                # provider conformance matrix (offline)
ANTHROPIC_API_KEY=… pytest -m live tests/test_anthropic_live.py   # gated live test
```

CI (`.github/workflows/ci.yml`; skipped for docs/markdown-only changes) runs
`ruff` and `pytest -q` on Linux, Python 3.13, **ignoring** the timing-sensitive
Textual pilot files (`test_ui_tui.py`, `test_mock_tui.py`,
`test_tui_integration_render.py`, `test_tui_model_selection_integration.py`);
those run on the developer's machine before each commit. A second job builds the
wheel and runs `test_model_data_package.py` and `test_install_script.py`. The one
required check is `ci-ok`. See [release.md](release.md).

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
  call a fake response (`tests/test_tui_panels.py:PanelTransport`).
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
| TUI | `test_ui_tui.py`, `test_tui_*.py` |
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
| `tests/playwright_tui_check.py` + `browser_serve.py` | real Textual shell through textual-serve with a Shift/Ctrl+Enter bridge, deterministic fixture transport |
| `tests/visual_tui_check.py` + `visual_tui_demo.py` | TUI screenshots to `artifacts/visual-tui/` |
| `tests/playwright_context_controls_check.py` | skill/MCP controls through the real TUI at wide and narrow sizes; `artifacts/context-controls/` |
| `tests/playwright_context_web_check.py`, `playwright_message_check.py` | web context and queued-message flows |
| `tests/playwright_mock_llm_check.py` + `mock_llm_serve.py` | scripted model through the real stack |
| `tests/tui_keyprobe.py`, `tui_e2e_probe.py` | raw key-protocol and PTY probes (Shift+Enter, Ctrl+Enter, Ctrl+J) |

`artifacts/` holds ignored screenshots and benchmark output.

## Rules

- New behavior gets a test next to its peers (`tests/test_<area>_*.py`). UI changes
  also get a browser or Textual check where one exists.
- Keep tests deterministic: no sleeps for correctness, injected clocks and
  fetchers, `tmp_path` workspaces.
- Live tests are opt-in (`-m live`, credentials in the environment); never commit
  credentials, headers, encrypted reasoning or private output in fixtures.
- Never weaken, skip or delete a test to make it pass.

`tests/playwright_project_sessions_check.py` starts two offline workspace daemons
sharing an isolated home. It checks duplicate project/session names, date groups,
path filtering, opening the owning project, reconnect, and light/dark screenshots
at 1440, 1024 and 400px (`artifacts/project-sessions/`).
