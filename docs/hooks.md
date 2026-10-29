# Hooks

Command hooks run a program at fixed points of a turn. They are configured in
`config.toml` as `[[hooks.<Event>]]` (PLAN.md H). Code: `nexus-core/nexus/hooks.py`,
used by `nexus/loop.py`. Tests: `tests/test_hooks.py`.

```toml
[[hooks.PreToolUse]]
name = "no-rm"
matcher = "bash(rm *)"          # same grammar as permission rules; omit to match every tool
command = ["python3", "guard.py"]   # argv, no shell
on_nonzero = "block"            # warn (default) | block | ignore
timeout_s = 10                  # default 10, at most 600
```

`shell = true` makes `command` one string run by the shell.

## Events

| Event | When | Can |
| --- | --- | --- |
| `SessionStart` | before a session's first turn | add context |
| `UserPromptSubmit` | after the user message is stored | add context; `block` ends the turn with stop `blocked`, no model call |
| `PreToolUse` | before a known tool call runs | `block` (error result), `modify` (new input), add context |
| `PostToolUse` | after the call, with `tool_result` and `is_error` | add context |
| `TurnEnd` | when the model gives a final answer | add context |
| `SessionEnd` | when a session is deleted | side effects only |

`matcher` works for `PreToolUse` and `PostToolUse` only.

## Protocol

stdin is one JSON object (at most 256 KiB): `event`, `session_id`, `turn_id`,
`cwd`, and for tool events `tool_name`, `tool_input`, `call_id`. Exit 0 allows.
stdout (at most 64 KiB) may be one JSON object:

```json
{"decision": "allow | warn | block | modify", "message": "text", "tool_input": {}, "context": "text"}
```

A non-zero exit, a timeout, output that is too large or a crash follows
`on_nonzero`. The environment is `PATH HOME LANG LC_ALL TMPDIR TZ USER SHELL`
plus `NEXUS_HOOK_EVENT`, `NEXUS_WORKSPACE`, `NEXUS_PROJECT_ROOT`,
`NEXUS_SESSION_ID`, `NEXUS_TURN_ID`, `NEXUS_TOOL_NAME`. A hook's process group is
killed on timeout. Hooks run in order; a `modify` feeds the next hook and a
`block` stops the rest.

## What the model sees

Every run publishes a `hook_fired` event (`hook`, `event`, `decision`,
`message`, `error`, `seconds`, `tool`, `call_id`). That is an execution detail.
Only these reach the context, as text labelled `[hook <name>]`:

- a block: the tool result `blocked by hook <name>: <message>`, or a user message for a blocked prompt;
- a `warn` message and any `context`, as a user message before the next model call;
- a `modify`: a notice with the new input. The assistant's original call is not edited (the context is append-only); the tool's own validation and approval run on the new input.

A hook is policy, not permission.

## Errors

Entries that do not parse are skipped and listed in `GET /api/config` `errors`;
the others still apply. Hooks are read when each turn starts. There is no
Settings editor yet: edit the file.
