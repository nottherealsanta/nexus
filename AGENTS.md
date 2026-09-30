# AGENTS.md

Guidance for coding agents working on **Nexus**, a provider-agnostic Python agent
harness. A per-workspace daemon owns sessions and turns. Three surfaces are clients
of it: a Textual chat app, a one-shot CLI and JSONL stream, and a plain HTML/CSS/JS
browser app.

Start here, then open the doc for the area you are changing:

| You are working on… | Read |
| --- | --- |
| Loop, providers, tools, context, sessions, managers, runtime, host/daemon, protocol, reducer | [docs/core.md](docs/core.md) |
| `nexus chat`, the Textual shell (`nexus/ui/tui/`, `nexus/ui_support/`) | [docs/textual.md](docs/textual.md) |
| `nexus web`, the browser client (`nexus/ui/web/`, `nexus/host/web.py`) | [docs/web.md](docs/web.md) |
| Releases, versioning, CI, publishing, `nexus update` | [docs/release.md](docs/release.md) |

The longer reference docs at the root are background. Search them; don't read them end to end:
`ARCHITECTURE.md` (layers, five contracts, security), `EXTENDING.md` (add a
tool/provider/skill/agent/hook/MCP server), `README.md` (user-facing behavior and
commands), `PLAN.md` (the historical phase ledger; docstrings cite it as "plan
section X.Y"), `webplan.md` and `design.md` (browser plan and visual spec),
`IMPROVEMENT_PLAN.md` and `TOOLS_PLAN.md` (forward-looking plans).

## Setup and commands

```sh
uv sync --extra dev            # or: pip install -e '.[dev]'  (Python ≥3.13; the venv is .venv/)
.venv/bin/python -m pytest -q  # full offline suite; `live` tests are deselected by default
.venv/bin/python -m pytest -q tests/test_host_facade.py -k name   # one area
ruff check nexus tests         # ruff is installed separately
python -m playwright install chromium                            # once, for browser checks
.venv/bin/python tests/playwright_web_check.py                   # real-browser web client check
.venv/bin/python tests/playwright_tui_check.py                   # Textual shell served to a browser
```

Run the product: `nexus chat` (TUI, needs a TTY), `nexus run "prompt"` (one
turn), `nexus web` (browser), `nexus doctor`, `nexus daemon status|stop|restart|logs`.
`--workspace PATH` selects the workspace. The daemon auto-starts.

Tests need no network or credentials. They use temporary workspaces,
`nexus.model.providers.scripted.ScriptedProvider`, and recorded fixtures under
`tests/fixtures/`. `pytest-asyncio` runs in `auto` mode.

## Rules that tests enforce

1. **One-way layering.** Lower layers never import higher ones:
   `config/errors/events/util` → `view/` → `model/` → `core/` → managers → `runtime.py` →
   `host/` → `ui/`. `core/loop.py` knows only protocols, never a concrete manager.
   Checked by `tests/test_layering.py`.
2. **UI encapsulation.** Code under `nexus/ui/**` may import only `nexus.host`,
   `nexus.view`, `nexus.events`, `nexus.client`, `nexus.host_support`,
   `nexus.ui_support`, `nexus.ui`, and the standard library. Textual and Rich
   imports stay inside `nexus/ui/tui/` (and `nexus/ui_support/tui_widgets.py`,
   `nexus/ui_support/tui_panels.py`, `nexus/ui_support/tui_list.py`,
   `nexus/ui_support/tui_context_header.py`, `nexus/ui_support/tui_archived.py`,
  `nexus/ui_support/tui_diff.py`,
  `nexus/ui_support/tui_settings.py`, `nexus/ui_support/tui_setup.py`,
  `nexus/ui_support/tui_providers.py`, `nexus/ui_support/tui_voice.py`).
   Checked by `tests/test_ui_layering.py`.
3. **No line caps.** Line counts are recorded in the Phase 3 baseline report
   (`tests/test_phase3_exit.py`) for information only. New Textual behavior still
   goes in its own module (e.g. `ui/tui/panels.py`) rather than growing `app.py`,
   and helpers belong in `nexus/host_support/` or `nexus/ui_support/` when they
   fit there.
4. **All UI work goes through the host contract.** A surface never reads session
   files or touches managers. If a UI needs new data or an action, add a command
   to `nexus/host/protocol.py`, handle it in `nexus/host/facade.py`, and call it
   from every client that needs it.
5. **Durable log first.** Sessions are append-only records in the shared
   `~/.nexus/nexus.db` SQLite database; JSONL is the export format. Views are
   reductions of the records (`nexus/view/reduce.py`), and replay must reproduce
   live state. Never add UI state that only exists in memory if it must survive a
   reconnect.

## Conventions

- **The web app mirrors the TUI.** `nexus web` has the same functionality as
  `nexus chat`, with everything in the same place: top bar (`▌` title … status
  `+` `▐`), sessions sidebar, the context header opening every conversation,
  timeline, composer rows, details sidebar, the same chat commands (from
  `ui/cli/commands.py`) and the same Control-key shortcuts. It should feel and
  behave the same. It may look more modern (softer corners, taller lines,
  floating dialogs) and uses the Monaspace Argon font. A feature or wording
  change in one surface goes into the other. Details are in [docs/web.md](docs/web.md).

- Match the surrounding style: module docstrings state the contract and cite the
  plan section. Structs are frozen `msgspec` or dataclasses. Errors are redacted
  before crossing the host boundary. Everything is bounded (sizes, counts,
  timeouts).
- Commit subjects and PR titles use Conventional Commits (`feat:`, `fix:`, `perf:`,
  `deps:`, `docs:`, `chore:`, `refactor:`, `test:`, `ci:`); release-please turns them
  into the version bump and changelog. Don't edit `version` in `pyproject.toml` or
  `CHANGELOG.md` by hand. Enable the local check once per clone with
  `git config core.hooksPath scripts/hooks`.
- Version bumps: when a version bump is needed, a minor bump (the second number)
  always requires asking the user first. Otherwise always bump the patch number
  (the third one). Never edit the version files yourself: a patch bump is a `fix:`
  commit and release-please does the rest. Steps are in
  [docs/release.md](docs/release.md#raising-a-pr-and-bumping-the-version).
- Security posture: loopback only, credentials never leave the daemon, and tool
  paths are checked by `tools/permissions.py`. Don't weaken these to make a
  feature easier.
- New behavior gets a test next to its peers (`tests/test_<area>_*.py`). UI
  changes also get a browser or Textual check where one already exists.
- The worktree is often dirty with unrelated in-progress work. Don't revert or
  "fix" files outside your task. If an unrelated test already fails, say so
  instead of changing it.
- `artifacts/` holds ignored screenshots and benchmark output. Shared session
  records live in `~/.nexus/nexus.db`; locks, caches, and logs are machine state
  under `~/.nexus/`. Project extensions and settings live in
  `<workspace>/.agents/`. Existing `<workspace>/.nexus/` extensions and settings
  remain a read-only fallback. Session records are stored only in SQLite;
  legacy session and trash directories are not imported. Export sessions before
  switching to this storage if they need to be retained.
