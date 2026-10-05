# AGENTS.md

Guidance for coding agents working on **Nexus**, a provider-agnostic Python agent
harness. A per-workspace daemon owns sessions and turns. Four surfaces are clients
of it: a terminal chat client (the native Ratatui client), a one-shot CLI and JSONL stream, and a plain HTML/CSS/JS
browser app, and a Rust GPUI desktop client.

**The idea of Nexus is that the context is clearly presented to the user and to
agents.** Keep this in mind for every change: show everything that matters
(every parameter, every output), labelled and readable, never a raw JSON dump,
and never hide information from the user that the agent can see.

**`docs/` is the source of truth** for how Nexus is built and why: architecture,
every subsystem, and the design decisions. Start at [docs/README.md](docs/README.md)
(a "find it by task" index), then open the doc for the area you are changing.
Update the matching doc in the same change as the code.

| You are working on… | Read |
| --- | --- |
| Layers, the five contracts, request path, invariants | [docs/architecture.md](docs/architecture.md) |
| Why something is the way it is; known gaps | [docs/decisions.md](docs/decisions.md) |
| Config keys, defaults, env vars, on-disk layout | [docs/config.md](docs/config.md) |
| Events, the reducer, `ConversationView` | [docs/events-and-view.md](docs/events-and-view.md) |
| Providers, routing, registry, tiers, sign-in | [docs/models.md](docs/models.md), [docs/provider-onboarding.md](docs/provider-onboarding.md) |
| The loop, turns, limits, failure handling | [docs/loop.md](docs/loop.md) |
| Prompt assembly, budget, compaction, caching | [docs/context.md](docs/context.md) |
| Sessions, SQLite storage, locks, export | [docs/sessions.md](docs/sessions.md) |
| Tools, permissions, bundles, shell jobs | [docs/tools.md](docs/tools.md) |
| Subagents, agent definitions, worktrees | [docs/agents.md](docs/agents.md) |
| Hot reload, skills, hooks, MCP, Settings files | [docs/extensions.md](docs/extensions.md), [docs/extending.md](docs/extending.md) |
| Host commands, daemon, transports, presence | [docs/host.md](docs/host.md) |
| CLI subcommands and chat slash commands | [docs/cli.md](docs/cli.md) |
| Anything both UIs must do the same way | [docs/surfaces.md](docs/surfaces.md) |
| `nexus web`, the browser client (**to be deprecated**; `nexus/ui/web/`, `nexus/host/web.py`) | [docs/web.md](docs/web.md) |
| Native Ratatui terminal client | [docs/ratatui-parity.md](docs/ratatui-parity.md) |
| Rust GPUI desktop client | [docs/desktop.md](docs/desktop.md), [GPUI skill](skills/gpui-nexus/SKILL.md), [native screenshot skill](skills/native-app-review/SKILL.md) |
| Local dictation | [docs/voice.md](docs/voice.md) |
| Trust boundaries, network, credentials | [docs/security.md](docs/security.md) |
| Tests, fakes, browser checks | [docs/testing.md](docs/testing.md) |
| Dev mode, mock scenarios, benchmark, examples | [docs/devtools.md](docs/devtools.md) |
| Releases, versioning, CI, publishing, `nexus update` | [docs/release.md](docs/release.md) |
| Finding the file for a module | [docs/module-map.md](docs/module-map.md) |

Root files are background; search them, don't read them end to end: `README.md`
(user-facing behavior and commands), `ARCHITECTURE.md` and `design.md` (older
architecture and visual notes; `docs/` supersedes them), `CHANGELOG.md`
(generated). `plans/` holds the historical plan ledgers that docstrings cite as
"plan section X.Y" (`STATE_PLAN`, `MOCK_PLAN`, `VOICE_PLAN`, `webplan.md`, …).
Where a plan and the code disagree, the code wins, then `docs/`.

## Setup and commands

```sh
uv sync --extra dev            # or: pip install -e '.[dev]'  (Python ≥3.13; the venv is .venv/)
.venv/bin/python -m pytest -q  # full offline suite; `live` tests are deselected by default
.venv/bin/python -m pytest -q tests/test_host_facade.py -k name   # one area
ruff check nexus tests         # ruff is installed separately
python -m playwright install chromium                            # once, for browser checks
.venv/bin/python tests/playwright_web_check.py                   # real-browser web client check
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
   `nexus.ui_support`, `nexus.ui`, and the standard library. Presentation toolkit imports stay outside the
   Python clients, except Rich in
   `nexus/ui_support/context.py` for Markdown rendering.
   Checked by `tests/test_ui_layering.py`.
3. **No line caps.** Line counts are recorded in the Phase 3 baseline report
   (`tests/test_phase3_exit.py`) for information only. New terminal behavior still
   goes in its own module (e.g. `ui/ratatui/workflows.py`) rather than growing the bridge,
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

- **The web app is to be deprecated.** Do not add new features to it or port
  new work to it unless the user asks; terminal clients come first. Until it is
  removed, it mirrors the TUI: `nexus web` has the same functionality as
  `nexus chat`, with everything in the same place: top bar (`▌` title … status
  `+` `▐`), sessions sidebar, the context header opening every conversation,
  timeline, composer rows, details sidebar, the same chat commands (from
  `ui/cli/commands.py`) and the same Control-key shortcuts. It should feel and
  behave the same. It may look more modern (softer corners, taller lines,
  floating dialogs) and uses sans-serif interface text, stroke icons, and Monaspace Argon for code. Existing
  features stay in sync, but new features need not be ported. Details are in [docs/web.md](docs/web.md).

- **Present context clearly.** Tool calls, results, context blocks and errors
  render as labelled, structured rows that miss nothing (`ui_support/tool_details.py`
  and its web port `ui/web/js/tool-details.js`); clipping is always announced.

- **Docs travel with code.** A behavior or design change updates the matching
  `docs/` page in the same commit; a new module gets a row in
  [docs/module-map.md](docs/module-map.md) (`tests/test_docs.py` fails otherwise);
  a design choice worth remembering goes in [docs/decisions.md](docs/decisions.md).
  Write the contract and the reason, briefly; say "not verified" where it is not.

- Match the surrounding style: module docstrings state the contract and cite the
  plan section. Structs are frozen `msgspec` or dataclasses. Errors are redacted
  before crossing the host boundary. Everything is bounded (sizes, counts,
  timeouts).
- Commit subjects and PR titles use Conventional Commits (`feat:`, `fix:`, `perf:`,
  `deps:`, `docs:`, `chore:`, `refactor:`, `test:`, `ci:`); release-please turns them
  into the version bump and changelog. Don't edit `version` in `pyproject.toml` or
  `CHANGELOG.md` by hand. Enable the local check once per clone with
  `git config core.hooksPath scripts/hooks`.
- Version bumps: a request to bump the version defaults to the next patch number
  (the third number) and authorizes the full release: create and merge the change
  PR when needed, then merge the release-please PR and verify publication. Do not
  stop at PR creation or ask again for merge approval. A minor bump (the second
  number) requires explicit user approval. Never edit version files by hand.
  Follow the complete workflow in
  [docs/release.md](docs/release.md#agent-contract-a-version-bump-request-includes-the-merge).
- Security posture: loopback only, credentials never leave the daemon, and tool
  paths are checked by `tools/permissions.py`. Don't weaken these to make a
  feature easier.
- New behavior gets a test next to its peers (`tests/test_<area>_*.py`). UI
  changes also get a browser or native PTY check where one already exists.
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
