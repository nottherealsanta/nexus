# Nexus documentation

`docs/` is the source of truth for how Nexus is built and why. [AGENTS.md](../AGENTS.md)
holds the rules and commands; everything else an agent needs to find lives here.
Code docstrings cite "plan section X.Y" or `STATE_PLAN §N`: those are the
historical ledgers in [`plans/`](../plans/), kept for archaeology only. Where a
plan and these docs disagree, **the code wins, then these docs, then the plans**.

The idea of Nexus: **context is clearly presented to the user and to agents.**
Every parameter and output is shown, labelled and readable, and nothing the
agent can see is hidden from the user. Keep this in mind for every change.

## Find it by task

| I want to… | Read | Code starts at |
| --- | --- | --- |
| Understand the layers, the five contracts, the request path | [architecture.md](architecture.md) | `nexus/runtime.py`, `nexus/core/loop.py` |
| Know *why* something is the way it is | [decisions.md](decisions.md) | |
| Change config keys, defaults, file locations, env vars | [config.md](config.md) | `nexus/config/` |
| Add or read an event; change what the UI can draw | [events-and-view.md](events-and-view.md) | `nexus/events.py`, `nexus/view/` |
| Add a provider, model behavior, tiers, sign-in | [models.md](models.md), [provider-onboarding.md](provider-onboarding.md) | `nexus/model/`, `nexus/auth/` |
| Change how a turn runs (iterations, retries, limits, cancel) | [loop.md](loop.md) | `nexus/core/` |
| Change the prompt, budget, compaction, caching | [context.md](context.md) | `nexus/context/` |
| Change session storage, locks, fork, export, archive | [sessions.md](sessions.md) | `nexus/session/` |
| Add or change a tool, permission rule, shell job, patch | [tools.md](tools.md) | `nexus/tools/` |
| Subagents, worktrees, agent definitions | [agents.md](agents.md) | `nexus/agents/` |
| Skills, hooks, MCP, hot-loaded extensions, Settings files | [extensions.md](extensions.md) | `nexus/ext/`, `nexus/skills/`, `nexus/hooks/`, `nexus/mcp/` |
| Add a host command; daemon, transports, presence | [host.md](host.md) | `nexus/host/`, `nexus/host_support/` |
| Add a CLI subcommand or a chat slash command | [cli.md](cli.md) | `nexus/cli.py`, `nexus/ui/cli/` |
| Something every client must do the same way | [surfaces.md](surfaces.md) | `nexus/ui_support/` |
| Work on `nexus chat` | [textual.md](textual.md) | `nexus/ui/tui/` |
| Build the Rust/Ratatui replacement | [ratatui-parity.md](ratatui-parity.md), [ratatui-feasibility.md](ratatui-feasibility.md) | `nexus/ui/ratatui/`, `rust/tui/` |
| Build or run the GPUI desktop client | [desktop.md](desktop.md) | `nexus/ui/desktop/`, `rust/desktop/` |
| Work on `nexus web` | [web.md](web.md) | `nexus/ui/web/`, `nexus/host/web.py` |
| Local dictation | [voice.md](voice.md) | `nexus/voice/` |
| Network, credentials, sandboxing, trust boundaries | [security.md](security.md) | |
| Write or run tests; fakes; browser checks | [testing.md](testing.md) | `tests/` |
| Dev mode, mock scenarios, benchmark, examples | [devtools.md](devtools.md) | `nexus/devtools/`, `benchmark/` |
| Write a tool / skill / agent / hook / MCP config | [extending.md](extending.md) | `examples/` |
| Release, version bump, CI, `nexus update` | [release.md](release.md) | `.github/workflows/` |
| Find the file for a module | [module-map.md](module-map.md) | |

## Reading order for a newcomer

1. [architecture.md](architecture.md): the layers and the one-picture request path.
2. [events-and-view.md](events-and-view.md): events are the spine; views are reductions.
3. [host.md](host.md): the only API a UI may use.
4. Then the area you are changing.

## Keeping docs true

- A behavior change updates the matching doc in the same commit. A new module
  is added to [module-map.md](module-map.md); `tests/test_docs.py` fails when a
  module is missing from it or a relative link is broken.
- State the contract and the reason; do not narrate code. Prefer a table and a
  file path over a paragraph. Say "not verified" where something is not.
- Design decisions (what was chosen, what was refused, why) go in
  [decisions.md](decisions.md), not scattered across feature docs.
- Root files `README.md` (user-facing), `ARCHITECTURE.md`, `design.md` and
  `CHANGELOG.md` (generated) are background; they defer to these docs.
