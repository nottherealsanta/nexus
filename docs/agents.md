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
| `tools` | list of tool names | The only tools the model is offered. Omit it for every tool. An unknown name makes session creation fail with a clear error. |
| `control` | list | Control tools: only `session` today, which starts child sessions. Never implied: omitting `tools` does not grant them. Ignored in child sessions. |
| `steps` | 1–500 | Max model calls per turn. |

Any other key is an error, so a typo is never silently ignored.

## Where agents come from

The file name is the agent name (`review.md` → `review`: lowercase letters,
digits, `-` and `_`). A later source replaces an earlier one with the same name:

1. **built-in:** `nexus-core/nexus/builtin_agents/*.md`
2. **global:** `~/.nexus10/agents/*.md`
3. **project:** `<project>/.agents/agents/*.md`

Files are read when a session is created. An edit, addition or deletion applies
to new sessions (roots and children), not to a session that already exists: its
resolved configuration is stored as a snapshot (below). A file that fails to
parse is listed under `problems` (`GET /api/agent`, `nexus agent`) with the
reason; it is never skipped silently.

## Session snapshots

Every session freezes what it needs to reproduce its requests when it is
created (root or child). The daemon loads the current agent files, resolves the
selected agent, its granted tools and the child-agent catalog, renders the
system prompt once, reads the session's instruction file, and stores it all as
JSON in the `session_snapshot` table (`nexus/snapshot.py`, `nexus/runtime.py`,
`nexus/store.py`).

The snapshot contains:

- the agent configuration (name, description, mode, model/effort preferences,
  steps, declared tools and control tools), its source (`builtin`, `global`,
  `project`), absolute path, file fingerprint (`sha256:…`) and prompt body;
- the ordered tool list, with each enabled tool's request description, JSON
  parameters and `guidance`; a switched-off tool has none of these;
- the child-agent catalog (name, description, mode, source, path and
  fingerprint) used by the `session` tool's enum and guidance;
- the rendered agent prompt and the final `system_prompt` sent as the provider's
  `instructions`, including the linked `AGENTS.md` section;
- the instruction-file record (path, status, content, bytes and hash) and the
  snapshot's origin (`created`, `agent-switch` or `reconstructed`).

Tool records are plain data. Execution is rebound by name through the tool
registry (`nexus/tools/`) on every turn, so the daemon never stores Python
callables. If a snapshot names a tool this build cannot execute, the turn fails
with a clear error rather than silently dropping the tool.

Ordinary turns, `GET …/context`, daemon restarts and the inspector all use the
stored snapshot. Editing or deleting the agent file, changing another agent's
description, editing `AGENTS.md` or letting the date roll over does not rewrite
an existing session's prompt. New sessions pick up file changes.
`GET /api/session/{id}/snapshot` returns the full stored snapshot; the session
object carries a bounded summary.

A session created before snapshots existed has no stored original prompt. On
first use, the daemon reconstructs one from the current agent files and
`AGENTS.md`, records `origin: "reconstructed"`, and puts a labelled
`# Snapshot note` in the system prompt saying this is not a historical record.
The reconstruction is stored once and is stable from then on. It also emits a
`session.snapshot` event.

## The instruction file

The starting context also includes exactly one instruction file:
`<session.directory>/AGENTS.md`. Ancestor and nested directories are not
searched. Its path, status and actual content are a labelled `# Instructions
file` section in `instructions`, separate from the agent's own prompt, and both
are visible in the inspector's Requests level.

Absence is explicit: the section says `Status: not found` and that no
instructions were loaded. An unreadable file reports its error, and a file over
256 KiB reports `Status: too large`, the byte count and the limit without
including the content. Nothing is omitted silently.

Editing (or deleting) `AGENTS.md` affects new sessions only. Switching agents
replaces the agent prompt and tool snapshot deliberately; the instruction file
loaded for the session is retained. A directory change (a later control-plane
step) will add the destination's instructions as forward context instead of
rewriting this prompt.

## How a session uses its agent

A session has one `agent`. The default is `build` when its mode allows a root;
otherwise the first loaded agent allowed as a root is used (normally
`orchestrate`). At creation:

- **instructions** = the agent's body with `{{tools}}` replaced by guidance for
  its actual tools, plus an environment block (workspace, date), then the
  instruction-file section. This is rendered once and frozen.
  `GET /api/session/{id}/context` shows the full text.
- **tools** = the agent's `tools`, or all task tools, minus the session's
  `disabled_tools`; explicitly granted control tools follow the same switch.
  Disabled tools are absent from the stored definitions, the request schemas and
  the system prompt's `# Tools` section.
- **model** = the session's model if the user set one, else the agent's frozen
  `model`, else the daemon's default. **effort** works the same way, ending
  with the provider's default.
- **steps** = the agent's frozen `steps`, or 50.

## Built-in agents

| Agent | Mode | Tools | For |
| --- | --- | --- | --- |
| `orchestrate` | primary | read, bash, code + `session` | Breaks a task down, delegates it to child sessions, checks the results, reports. The only agent with the `session` tool. Its prompt says not to edit files. |
| `build` (default) | all | all | The grunt work: writes and edits code, runs and checks it. Prompt adapted from OpenCode's GPT system prompt (MIT; see `builtin_agents/NOTICE`). |
| `quick` | subagent | all (effort low, 20 steps) | Small self-contained tasks whose details the parent should not carry in its context. Returns only what the parent needs. |
| `scout` | subagent | read, bash | Reads the material and returns a context brief: answer, relevant files with line ranges and quotes, how they fit, gotchas, what was checked, open questions. |
| `plan` | subagent | all | Designs a change. Its prompt allows writing Markdown plan files only, never code. This is advice in the prompt, not enforced by the harness. |

So a root session runs `orchestrate` or `build`, and a child session runs
`build`, `quick`, `scout` or `plan`. Prompts are advice: bash is not sandboxed,
so "read-only" (`scout`) and "Markdown only" (`plan`) rest on the prompt.
Granting `bash` also grants the `bash_output` and `bash_kill` companions for
background jobs (the table lists only the frontmatter tools).

The session JSON reports the result and where it came from: `model` and
`model_source` (`session`, `agent` or `default`), and `effort` and
`effort_source` (`session`, `agent` or `provider`). Setting the model to `""`
(`/model default` in chat) returns to the agent's choice.

Approval is a session setting (`permission`), not an agent setting: read,
`bash_output`, `bash_kill`, `ask`, `edit`, `write` and the outer `code` call
never ask. In-directory `edit` and `write` are approval-free by fixed policy,
with path confinement and read freshness still enforced. bash (including
background) still asks unless the session is `allow`. Nested code calls follow
the same rules.

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

The default task tools include `ask` and `code`. An explicit `tools` list must
include them to enable questions or cells. Inside code, only that caller's other
task tools are exposed, with the caller's directory and approval policy; control
tools are excluded. Session trees share a default kernel. See
[tools.md](tools.md#ask) and [tools.md](tools.md#code). A question is not an
approval and never grants a tool.

## Tool guidance and session switches

Put `{{tools}}` in the body where the generated `# Tools` section should appear.
Without a placeholder, the section is appended. With no tools, it explicitly
says none are available. Keep agent bodies tool-agnostic: naming tools elsewhere
would leave stale advice when those tools are disabled. Custom bodies are not
rewritten beyond the placeholder.

The `session` tool's guidance and `agent` enum are part of the snapshot: they
come from the child-capable agents loaded when that parent session was created.
A new parent picks up added agents, description changes and mode changes; an
existing parent keeps its catalog. If an existing parent names a child agent
that has since been deleted (or is no longer allowed as a child), the call
fails with a clear error and never substitutes another agent.

Use `nexus run|chat --disable bash,write` to save switches on a session;
`--disable ''` clears them. In chat, `/tools` shows available and disabled tools.
A switch applies to subsequent calls; an already-running call keeps the
configuration it started with. Disabling a tool removes it from the next request's
schemas, from the nested functions inside code cells, and from the stored tool
definitions. The frozen system prompt is not rewritten: the daemon appends a
`[Nexus notice: tool availability changed]` message to the conversation. It
names the tools now available (with their guidance), the tools switched off, and
says the notice is authoritative where the frozen prompt mentions an old tool.
Clearing a switch adds the tool's definition back and uses the same notice.

Switching agents (`POST /api/session/{id}/agent`) is the deliberate prompt
replacement: the agent prompt, tool definitions and child catalog are rebuilt
from the current files, with a `[Nexus notice: agent changed]` message. The
loaded `AGENTS.md` section is retained. Model and effort changes apply from the
next model call and never mutate a running call. No filesystem hot reload is performed
for existing sessions.

`bash` brings `bash_output` and `bash_kill` with it, unless they are switched
off individually, so a background command can always be read and stopped. They
are task tools, not control tools; a child session sees its tree's jobs.

## Not yet

- Per-tool permission rules (OpenCode's `permission` map).
- `temperature`/`top_p`.
- Hidden or disabled agents.
