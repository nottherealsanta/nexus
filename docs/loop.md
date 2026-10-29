# The agent loop

`nexus/loop.py`: model, tool calls, results, model, until the model stops calling
tools. The loop owns no storage and no UI. It keeps the conversation in memory and
yields events ([stream-events.md](stream-events.md)).

## `Agent`

```python
Agent(provider, *, model, workspace, tools=DEFAULT_TOOLS, instructions=None,
      reasoning_effort=None, approve=None, max_steps=50, messages=None,
      conversation_id=None, files=None, cwd_changed=False)
```

| Argument | Meaning |
| --- | --- |
| `provider` | Anything with `name`, `body(Request)`, `stream(Request)` |
| `model` | Bare model id |
| `workspace` | Directory the tools act in ([tools.md](tools.md)) |
| `tools` | Tuple of `Tool`; the model sees their specs. Names must be unique (a dict by name) |
| `instructions` | System prompt; default is `default_instructions(workspace)` |
| `reasoning_effort` | Passed in every `Request` |
| `approve` | Approval callback, or `None` for no questions |
| `max_steps` | Step limit per turn, default `MAX_STEPS = 50` |
| `messages` | Earlier history (copied) |
| `conversation_id` | Default is a new `uuid4().hex`; used for prompt caching and session headers |
| `files` | The session's read observations and mutation lock, passed to tools |
| `cwd_changed` | The live directory differs from the one in the frozen prompt; tools report absolute paths |

`default_instructions` states the workspace path, today's date and the tools,
and tells the model to read before it edits and keep answers short.

`agent.messages` is the history (a list of `Message`). `agent.request()` builds the
`Request` the next step would send; `provider.body(agent.request())` is the exact
wire body (the CLI's `/context` prints it).

## `turn(user_text)`

An async generator of `LoopEvent`.

`user_text` (`TurnInput = str | Message | NoUserMessage`) is the new user text,
a complete user `Message`, or the explicit `NO_USER_MESSAGE` marker
(`nexus.loop.NoUserMessage`). Text becomes a user message with one `Text`; a
`Message` is appended as given, which is how a prompt with images arrives (text,
then a `[Image A]` label and an `Image` part per attachment;
[llm-providers.md](llm-providers.md#user-images)). With the marker the loop
appends no user message and sends the stored context as it is; that is the only difference a
manual continuation has from an ordinary prompt. The daemon uses the marker for
`POST /api/session/{id}/continue` and text (or a `Message` when images are
attached) for `POST …/prompt`, through the same turn lifecycle.

1. Append the user message unless `user_text` is `NO_USER_MESSAGE`, yield `MessageAdded`.
2. For `step` in `1..max_steps`:
   1. Yield `StepStart(step)`.
   2. Stream `provider.stream(request)` through the bounded retry wrapper
      (`nexus/retry.py`). Every event is yielded unchanged. A transient
      pre-stream failure yields `RetryScheduled`, waits, then `RetryStarted`
      and tries the frozen request again; settings changes wait for the next
      logical request. No response stream event may have been
      received yet. `RetryExhausted` says why no further attempt fits the
      budget. `PartDone` parts are collected; the last `Done.stop` is kept
      (default `"other"`).
   3. Append the assistant message made of the collected parts, yield `MessageAdded`.
   4. If there are no `ToolCall` parts, yield `TurnEnd(stop, step)` and return.
   5. Otherwise run the calls one by one, in order: yield `ToolStart(call, args)`,
      run it, yield `ToolEnd(call, result)`.
   6. Append one `tool` message with all the results, yield `MessageAdded`.
3. After `max_steps` steps without a final answer, yield `TurnEnd("max_steps", max_steps)`.
   The history then ends with a tool message and no assistant reply.

Calls run sequentially, even when a step has several (`parallel_tool_calls` is
sent as `false` on the wire). The next `turn()` call continues the same history.

## Explicit configuration updates

An optional synchronous `refresh(agent)` callback runs before each model and
tool dispatch. The daemon rebinds current stored configuration there without
reloading agent files or instruction files. An already-running call is unchanged.
A directory change requested through the API is applied there, between calls,
never inside one. Pending notices (the `inbox` callback: notifications, and a
parent's `message_session` steer) are drained before model requests, after
previous tool results, so stored context stays in the same order as the model's
context. Active `code` cells retain their caller's task-tool bindings until that
outer call finishes; control tools are never available inside cells.

## Tool execution

`_parse_args` parses `call.arguments` as JSON (empty text counts as `{}`). A
result that is not a JSON object, or invalid JSON, gives `args=None`.

`_run` returns a `ToolResult` and never raises for these cases (`is_error=True`):

| Case | Result text |
| --- | --- |
| Tool name not in `tools` | `unknown tool '<name>'` |
| `args is None` | `arguments were not a JSON object` |
| `approve` returns false | `the user declined this tool call` |
| Tool raises `ToolError` | the error message |
| Tool raises `OSError`, `ValueError`, `TypeError`, `KeyError` or `UnicodeError` (e.g. a list where an integer was expected) | `<ExceptionName>: <message>` |

The checks run in this order. `ToolStart` is yielded before any of them, so a
client shows even rejected calls. Other exceptions from a tool propagate out of
`turn()`. Errors go back to the model as
results, so it can correct itself.

Provider errors (`ProviderError`, `AuthError`, `CredentialError`) propagate out of
`turn()`. The caller catches them (the CLI prints them). Messages already added
stay in `agent.messages`.

## Approval

`Approve = Callable[[ToolCall, dict], Awaitable[bool]]`. It is called once per
call that has a known tool and valid arguments, including `read`. The loop has no
policy of its own: `None` approves everything.

The daemon's callback (`Runtime._approve`, see [daemon.md](daemon.md#turns))
approves `read`, `bash_output`, `bash_kill`, `session`, `message_session`,
`ask`, `edit`, `write`, the outer `code` call and the workspace control tools
silently; `edit` and `write` are
approval-free inside the session directory by fixed policy (path confinement and
read freshness still apply). Nested code calls go through the same approval
callback. For the other tools (including background `bash`) it approves when the
session's permission is `allow` (the default for new sessions); otherwise it
publishes `permission.asked` and waits for a client's reply. The CLI answers with `allow <tool>? [y/N]`; `--yes`
sets the session to `allow`. `ask` has its own `question.asked` wait, not an
approval.

## Stop reasons

`TurnEnd.stop` is the last `Done.stop` (`end_turn`, `max_tokens`, `other`; a final
step has no tool calls) or `max_steps`. A `max_tokens` stop that also has tool
calls continues the loop, since the tool-call check is what decides.

## Not covered (PLAN.md, known gaps)

No context limit or compaction. Provider retries are bounded and owned by the
loop's wrapper (`nexus/retry.py`), not by storage: the daemon records attempts
and publishes their events. Saving and cancelling are the
caller's job: the daemon saves each `MessageAdded` and cancels the turn task on
interrupt ([daemon.md](daemon.md)). The daemon also records the turn, each
provider request and each tool call's lifecycle durably around this loop, so a
crash can be reconciled from evidence rather than from memory
([daemon.md](daemon.md#crash-recovery)); the loop itself stays storage-free.
From the events it yields the daemon also keeps each turn's summary: the model
and effort at `StepStart`, token counts from `Usage`, and `{stop, steps}` from
`TurnEnd` (`Store.mark_turn_ended`) before the turn closes
([stream-events.md](stream-events.md#what-the-daemon-stores-from-them),
[daemon.md](daemon.md#turn-summaries-and-the-spend-ledger)).
Manual continuation reuses the same loop with `NO_USER_MESSAGE`; the daemon
validates and reconciles the stored context before calling it
([api.md](api.md#manual-continuation)).

## Steered and queued input

The runtime's durable input queue is separate from the loop's message history.
Before a model request, the loop takes pending steers after preceding tool
results. Each delivered input becomes one user message. Storage commits the
message and delivery state together; clients show it at that point in the
context line. If a steer arrives while a final answer streams, the loop takes
another step before ending the turn.

Queued inputs start separate turns after completion or failure. Interruption
holds the queue; sending the next item explicitly releases it. Recovery keeps
pending input, converts undelivered steers to queued input and holds it, without
starting model work automatically. See [Pending inputs](api.md#pending-inputs-steer-and-queue).
