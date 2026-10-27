# AGENTS.md

This branch (`nexus10`, Nexus 0.10) rebuilds Nexus from the provider wire up. **Read [PLAN.md](PLAN.md)
first**: principles, layout, the current step, and the ledger of deliberate
changes from old Nexus.

- Old Nexus is reference only: branch `nexus01`, checkout `~/repos/nexus`. Copy
  code from it deliberately, and add a ledger row in PLAN.md when you change a
  behavior. Its AGENTS.md and docs do not apply here.
- Keep the context visible: wire events are never dropped, and clipping is announced.
- One timeline: the "timeline" (or "context line"), which is what the user sees
  as a session in the UIs, reflects the agent's context. Everything the model
  receives is shown in order, and the UI shows nothing the model does not have.
  Presenting context well is a core idea of Nexus.
- Task tools (`nexus-core/nexus/tools/`) act on the workspace. Changes to Nexus state
  (session, workspace, worktree, model) belong to the control plane, never a tool.
- State lives in `~/.nexus10` (`$NEXUS10_HOME`). Never touch `~/.nexus`.
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
| read, bash, edit, write | [docs/tools.md](docs/tools.md) |
| The agent loop and approval | [docs/loop.md](docs/loop.md) |
| uv, tests, ruff, repo layout | [docs/development.md](docs/development.md) |
| Agent files (frontmatter + prompt) | [docs/agents.md](docs/agents.md) |
| Session inspector web page | [docs/inspector.md](docs/inspector.md) |
| HTTP API and event stream | [docs/api.md](docs/api.md) |
| Daemon, projects, sessions, turns, approvals | [docs/daemon.md](docs/daemon.md) |
