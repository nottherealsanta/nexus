# AGENTS.md

This repository (`main`, Nexus 0.10) rebuilds Nexus from the provider wire up. **Read [PLAN.md](PLAN.md)
first** for outstanding work; see [docs/porting-ledger.md](docs/porting-ledger.md)
for deliberate changes from old Nexus.

- Old Nexus is reference only: branch `nexus01` (also tag `v0.7.2`), checked out at `~/repos/nexus-old`
  (`git worktree add ~/repos/nexus-old nexus01`). Copy
  code from it deliberately, and add a row in docs/porting-ledger.md when you change a
  behavior. Its AGENTS.md and docs do not apply here.
- Keep the context visible: wire events are delivered live; finished diagnostic history is dropped only with a
  `session.compacted` announcement, and clipping is announced. Token deltas are live-only
  with request partial checkpoints; canonical model context is always retained.
- TUI and NSH are peers: **every UI change must be reflected in both, in the same
  change**, including layout, styling, wording, pickers, modals, sidebars, shortcuts
  and interactions. Follow
  [.agents/skills/nexus-ui-parity/SKILL.md](.agents/skills/nexus-ui-parity/SKILL.md).
  A client-specific difference needs the user's explicit approval first, and is then
  recorded in [docs/porting-ledger.md](docs/porting-ledger.md).
- The file explorer, the file editor and Git management are for the TUI and NSH on
  desktop and web only. They do not ship on iOS or any other mobile target.
- One timeline: the "timeline" (or "context line"), which is what the user sees
  as a session in the UIs, reflects the agent's context. Everything the model
  receives is shown in order, and the UI shows nothing the model does not have.
  Presenting context well is a core idea of Nexus. **Exception: the Manager**
  ([docs/manager.md](docs/manager.md)). Its own context (hidden episodes, the state
  snapshot and deltas it is given each turn) has no timeline; the user sees its
  header orb and notification dropdown (caption, card and typed input), an explicit read-only backend session
  view for debugging, and the full raw log in the inspector. The rule still holds for every session the
  Manager touches: whatever it sends or answers
  shows there, labelled `via Manager`, and what a card shows is what the Manager
  asked to show. Do not add a separate Manager chat implementation; the explicit read-only backend session is for debugging.
- Task tools (`nexus-core/nexus/tools/`) act on the workspace. Changes to Nexus state
  (session, workspace, worktree, model) belong to the control plane, never a task
  tool; the model reaches control only through exposed control tools.
- State lives in `~/.nexus` (`$NEXUS10_HOME`) once `nexus migrate` has marked it; until then `~/.nexus` is old Nexus's and `~/.nexus10` a pre-release home. Never run `nexus migrate` against the real home from an agent session, and use a scratch `$NEXUS10_HOME` for tests.
- Update PLAN.md when a step lands or a decision changes.

The Python core is `nexus-core/` (a uv project: `pyproject.toml`, `uv.lock`,
`.python-version`, package `nexus/`, `tests/`). Run uv from that folder:

```sh
cd nexus-core
uv sync                        # venv + deps + dev group
uv run pytest -q
uv run ruff check nexus tests
uv run nexus login codex       # or: login codex --device
uv run nexus chat --wire       # see every request and SSE event
uv add <pkg>                   # never pip install
```

The repo launcher `scripts/nexus` gives whole-repo shortcuts from any
directory when linked into `~/.local/bin`: `nexus`/`nexus tui`, `nexus build`,
`nexus web`, `nexus restart`, `nexus check`. See
[docs/development.md](docs/development.md#shortcuts).

## Docs

[docs/README.md](docs/README.md) indexes the docs, which describe the current
code. Read the matching page before changing an area, and update it in the same
change as the code.

| You are working on… | Read |
| --- | --- |
| Providers, dialects, request bodies, replay, redaction | [docs/llm-providers.md](docs/llm-providers.md) |
| Sign-in, `auth.json`, token refresh | [docs/auth.md](docs/auth.md) |
| models.dev catalog, `provider/model` refs, default model | [docs/models.md](docs/models.md) |
| Conversation types, stream events, loop events, JSON codec | [docs/stream-events.md](docs/stream-events.md) |
| read, bash, edit, write, ask, code | [docs/tools.md](docs/tools.md) |
| The agent loop and approval | [docs/loop.md](docs/loop.md) |
| uv, tests, ruff, repo layout | [docs/development.md](docs/development.md) |
| Agent files (frontmatter + prompt) | [docs/agents.md](docs/agents.md) |
| Session inspector web page | [docs/inspector.md](docs/inspector.md) |
| HTTP API and event stream | [docs/api.md](docs/api.md) |
| Daemon, projects, sessions, turns, approvals | [docs/daemon.md](docs/daemon.md) |
| The Manager (voice agent above all sessions): hidden sessions, tools, authority, cards, navigation | [docs/manager.md](docs/manager.md) |
