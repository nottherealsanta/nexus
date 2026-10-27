# Agents

An agent is a Markdown file. The frontmatter says how a session should run,
and the body is the system prompt. The idea comes from OpenCode's agent files,
cut down. Code: `nexus-core/nexus/agents.py`; built-ins:
`nexus-core/nexus/builtin_agents/`.

```markdown
---
description: Reviews code without changing it   # required
mode: subagent                                   # primary | subagent | all (default all)
model: copilot/claude-sonnet-5                   # preferred model
effort: high                                     # preferred reasoning effort
tools: [read, bash]                              # allowed task tools; omit for all
control: [session]                               # control tools; none unless listed
steps: 20                                        # max loop steps per turn (default 50)
---
You review code. Point out bugs and risks; do not edit files.
```

| Key | Type | Meaning |
| --- | --- | --- |
| `description` | string, required | What the agent is for. Shown in lists, and later used to pick an agent for a child session. |
| `mode` | `primary`, `subagent`, `all` | Where the agent may run: `primary` only as a root session, `subagent` only as a child session, `all` both. Enforced when a session is created or its agent is changed. Default `all`. |
| `model` | `provider/model` | Used when the session has no model of its own. |
| `effort` | string | Used when the session has no effort of its own. |
| `tools` | list of tool names | The only tools the model is offered. Omit it for every tool. An unknown name fails the turn with a clear error. |
| `control` | list | Control tools: only `session` today, which starts child sessions. Never implied: omitting `tools` does not grant them. Ignored in child sessions. |
| `steps` | 1–500 | Max model calls per turn. |

Any other key is an error, so a typo is never silently ignored.

## Where agents come from

The file name is the agent name (`review.md` → `review`: lowercase letters,
digits, `-` and `_`). A later source replaces an earlier one with the same name:

1. **built-in:** `nexus-core/nexus/builtin_agents/*.md`
2. **global:** `~/.nexus10/agents/*.md`
3. **project:** `<project>/.agents/agents/*.md`

Files are read again on every turn, so an edit applies to the next turn. A file
that fails to parse is listed under `problems` (`GET /api/agent`,
`nexus agent`) with the reason; it is never skipped silently.

## Built-in agents

| Agent | Mode | Tools | For |
| --- | --- | --- | --- |
| `orchestrate` | primary | read, bash + `session` | Breaks a task down, delegates it to child sessions, checks the results, reports. The only agent with the `session` tool. Its prompt says not to edit files. |
| `build` (default) | all | all | The grunt work: writes and edits code, runs and checks it. Prompt adapted from OpenCode's GPT system prompt (MIT; see `builtin_agents/NOTICE`). |
| `quick` | subagent | all (effort low, 20 steps) | Small self-contained tasks whose details the parent should not carry in its context. Returns only what the parent needs. |
| `scout` | subagent | read, bash | Reads the material and returns a context brief: answer, relevant files with line ranges and quotes, how they fit, gotchas, what was checked, open questions. |
| `plan` | subagent | all | Designs a change. Its prompt allows writing Markdown plan files only, never code. This is advice in the prompt, not enforced by the harness. |

So a root session runs `orchestrate` or `build`, and a child session runs
`build`, `quick`, `scout` or `plan`. Prompts are advice: bash is not sandboxed,
so "read-only" (`scout`) and "Markdown only" (`plan`) rest on the prompt.

## How a session uses its agent

A session has one `agent` (default `build`). On each turn:

- **instructions** = the agent's body + an environment block (workspace, date,
  tool names). `GET /api/session/{id}/context` shows the full text.
- **tools** = the agent's `tools`, or all of them.
- **model** = the session's model if the user set one, else the agent's
  `model`, else the daemon's default. **effort** works the same way, ending
  with the provider's default.
- **steps** = the agent's `steps`, or 50.

The session JSON reports the result and where it came from: `model` and
`model_source` (`session`, `agent` or `default`), and `effort` and
`effort_source` (`session`, `agent` or `provider`). Setting the model to `""`
(`/model default` in chat) returns to the agent's choice.

Approval is a session setting (`permission`), not an agent setting: read never
asks, and bash, edit and write ask unless the session is `allow`.

## Commands

```sh
nexus agent                     # agents for this project, with source, tools and preferences
nexus agent plan                # one agent's settings and full prompt
nexus run --agent plan "…"      # new session with that agent
nexus chat                      # then: /agent plan, /agent (list), /model default
```

API: `GET /api/agent?directory=…`, `GET /api/agent/{name}`,
`POST /api/session {agent}`, `POST /api/session/{id}/agent {agent}`
([api.md](api.md)).

## Not yet

- Per-tool permission rules (OpenCode's `permission` map).
- `temperature`/`top_p`.
- Hidden or disabled agents.
