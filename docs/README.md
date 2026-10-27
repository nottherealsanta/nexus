# Nexus 0.10 docs

These pages describe the code as it is. [PLAN.md](../PLAN.md) is the plan and the
ledger of decisions; [AGENTS.md](../AGENTS.md) holds the working rules. Where a
doc and the code disagree, the code wins: fix the doc.

## Index

| Doc | About |
| --- | --- |
| [llm-providers.md](llm-providers.md) | The four providers, the table, both dialects, quirks, replay, redaction, errors, examples |
| [auth.md](auth.md) | Sign-in flows, `auth.json`, locking, refresh-token rotation |
| [models.md](models.md) | models.dev catalog, cache, filters, `provider/model` refs, default model |
| [stream-events.md](stream-events.md) | Conversation types, provider stream events, loop events, JSON codec |
| [tools.md](tools.md) | read, bash (background jobs too), edit, write, ask (questions), code and kernels; workspace controls (expose, directory moves, worktrees); parameters, bounds, confinement, approval, read freshness |
| [loop.md](loop.md) | The agent loop: steps, tool execution, errors, approval |
| [development.md](development.md) | uv, tests, ruff, repo layout, docs rules |
| [agents.md](agents.md) | Agent files: frontmatter, where they load from, session snapshots and `AGENTS.md` |
| [inspector.md](inspector.md) | The session inspector web page: four levels of detail down to raw events |
| [api.md](api.md) | The HTTP API every surface speaks, and its event stream |
| [daemon.md](daemon.md) | The daemon, projects, sessions, turns, approvals, questions, session archiving, the CLI as a client |

## Find it by task

| You are working on... | Read |
| --- | --- |
| Adding or changing a provider, its endpoint, default model or quirks | [llm-providers.md](llm-providers.md), [models.md](models.md) |
| The request body a provider sends, or replay of earlier turns | [llm-providers.md](llm-providers.md) |
| Parsing a provider's stream, or a new event type | [llm-providers.md](llm-providers.md), [stream-events.md](stream-events.md) |
| Header redaction or provider error handling | [llm-providers.md](llm-providers.md) |
| `nexus login`, tokens, `auth.json`, refresh-token rotation | [auth.md](auth.md) |
| The grok client version requirement | [auth.md](auth.md) |
| `nexus models`, the model list, `--model`, `$NEXUS_MODEL` | [models.md](models.md) |
| Message and part types, `raw` and `origin` | [stream-events.md](stream-events.md) |
| JSON encoding of messages and events | [stream-events.md](stream-events.md) |
| A tool's parameters, limits or path rules | [tools.md](tools.md) |
| The patch format | [tools.md](tools.md) |
| Read-before-edit observations, stale or partial reads, edit/overwrite coverage | [tools.md](tools.md#read-observations-and-freshness) |
| On-demand workspace tools, moving a session's directory, Git worktrees | [tools.md](tools.md#workspace-control-tools-on-demand), [api.md](api.md#workspace-controls) |
| Steps, max steps, approval callback, tool errors | [loop.md](loop.md) |
| Running tests, lint, uv, repo layout | [development.md](development.md) |
| Agents, their prompts, tools and preferred models | [agents.md](agents.md) |
| Session snapshots, frozen prompts, `AGENTS.md` | [agents.md](agents.md#session-snapshots), [agents.md](agents.md#the-instruction-file) |
| Seeing what happened in a session, down to the wire | [inspector.md](inspector.md) |
| Child sessions, who launched whom | [daemon.md](daemon.md#session-trees-who-launched-whom) |
| Archiving a session tree, list filters, notices while archived | [daemon.md](daemon.md#session-archiving), [api.md](api.md#archive-and-session-lists) |
| An endpoint, the event stream, a new client | [api.md](api.md) |
| Starting or finding the daemon, projects, sessions, storage, interrupts | [daemon.md](daemon.md) |
