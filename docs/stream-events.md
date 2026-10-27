# Stream events and types

Everything a provider returns is normalized into the types in
`nexus/llmhub/types.py`. The agent loop adds its own events ([loop.md](loop.md)).
All types are frozen dataclasses with `slots`.

## Durable event identity

Every event the daemon publishes is stored in the `event` table **before** any
subscriber sees it, and its row `id` is flushed into the event as the durable
`id`. `nexus/bus.py` does that; `nexus/eventlog.py` owns the cursor and replay
contract:

| Field | Meaning |
| --- | --- |
| `id` | Durable row id in this database's event log; never reused, stable across restarts. |
| `seq` | The publishing daemon's counter (`1, 2, 3…`). Restarts with the daemon and is **not** a cursor. |
| `time` | Wall-clock publish time. |
| `type` | Event name (`text_delta`, `message.added`, `session.created`, …). |
| `session_id` | The owning session, or `null` for a global event. |
| `data` | The event payload. |
| `cursor` | On the SSE stream only: `<log generation>:<id>`. |

The `log` table holds one generation (`log_<hex>`) for the database. A new,
deleted or replaced database gets a new generation; a copy keeps the same one,
because it is the same log. A client resumes with `cursor`, and the server
rejects a cursor from another generation, ahead of the head, malformed, or
older than the log floor with an actionable resync response. Filtered streams
skip unrelated ids, so a numeric gap is normal and is not evidence of loss.

Session events are deleted with their session. Global lifecycle events are
stored with `session_id: null` and survive: `project.created`,
`project.updated`, `project.deleted`, and a minimal `session.deleted` tombstone
(`{"id": "ses_…"}`). A deleted session's messages, events, snapshots and
observations are gone; the tombstone is only what a client needs to drop it.
`bus.overflow` is stream control, is never stored, and has no durable id.
Details and response codes: [api.md](api.md#event-stream).

## Conversation

A conversation is a list of `Message`s made of ordered parts.

| Type | Fields | Meaning |
| --- | --- | --- |
| `Text` | `text`, `origin=None`, `raw=None` | Plain text |
| `Reasoning` | `summary`, `origin=None`, `raw=None` | Visible reasoning summary; `raw` carries the opaque state to send back |
| `ToolCall` | `id`, `name`, `arguments` (JSON text exactly as the model produced it), `origin=None`, `raw=None` | A tool call |
| `ToolResult` | `call_id`, `output`, `is_error=False` | The result of a call; no `origin` or `raw` |
| `Message` | `role` (`"user"`, `"assistant"` or `"tool"`), `parts` (tuple) | |

`Part = Text | Reasoning | ToolCall | ToolResult`.

`origin` is the provider that produced the part (`"codex"`, ...). `raw` is the
exact wire item: the Responses output item for codex and grok, and for chat
providers `{"field": "<reasoning field name>"}` on `Reasoning` (other chat parts
have `raw=None`). User text and tool results have `origin=None`. Replay rules are
in [llm-providers.md](llm-providers.md).

Roles in practice: the user message holds one `Text`; the assistant message holds
the parts of one model step in the order they finished; the `tool` message holds
one `ToolResult` per call of that step.

### Request

| Field | Meaning |
| --- | --- |
| `model` | Bare model id (no provider prefix) |
| `instructions` | System prompt |
| `messages` | Tuple of `Message` |
| `tools` | Tuple of `ToolSpec(name, description, parameters)` (JSON schema) |
| `reasoning_effort` | e.g. `low`, `medium`, `high`, or `None` |
| `conversation_id` | Stable per conversation; used for prompt caching and session headers |

## Provider stream events

`provider.stream(request)` is an async iterator of `Event`:

| Event | Fields | Emitted when |
| --- | --- | --- |
| `WireRequest` | `method`, `url`, `headers` (credentials redacted), `body` | Once, before the HTTP call. Auth failures happen before it |
| `WireResponse` | `status`, `headers` (redacted) | Once, when the response head arrives (also for HTTP errors, just before the `ProviderError`) |
| `WireEvent` | `name`, `data` | For every SSE event, as received, before the events derived from it. `data` is parsed JSON, or the raw string if it is not JSON (`"[DONE]"` for chat) |
| `TextDelta` | `text` | A chunk of assistant text |
| `ReasoningDelta` | `text` | A chunk of reasoning summary |
| `ToolCallStarted` | `id`, `name` | A tool call begins |
| `ToolArgsDelta` | `id`, `delta` | A fragment of the call's JSON arguments |
| `PartDone` | `part` | A finished part, ready to append to the assistant message |
| `Usage` | `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_tokens` (all default 0) | Once per response, when the host reports usage |
| `Done` | `stop`, `response_id=None`, `detail=None` | Last event of a response |

`Done.stop` is one of `end_turn`, `tool_calls`, `max_tokens`, `other`.
`detail` carries the provider's reason for `other` (and the incomplete reason
for Responses).

Ordering differs by dialect. Responses emits `PartDone` as each output item
finishes, so text and tool calls interleave with deltas. Chat emits every
`PartDone` after the stream ends: reasoning, text, then tool calls by index,
followed by `Usage` and `Done`. A sample sequence per dialect is in
[llm-providers.md](llm-providers.md#streaming).

Failures raise `ProviderError(message, status=None, body=None)` from the
iterator; they are not events.

## Loop events

`Agent.turn(text)` yields every provider event unchanged plus these
(`nexus/loop.py`). `LoopEvent = Event | StepStart | ToolStart | ToolEnd | MessageAdded | TurnEnd`.

| Event | Fields | Emitted when |
| --- | --- | --- |
| `MessageAdded` | `message` | Each time the loop appends to history: the user message (first event of a turn), each assistant message (after its provider stream ends), each tool message (after all its results) |
| `StepStart` | `step` (from 1) | Before each provider request |
| `ToolStart` | `call`, `args` | Just before a tool call is checked, approved and run. `args` is `None` when the arguments are not a JSON object |
| `ToolEnd` | `call`, `result` | After the call finished, was declined, or failed; `result.is_error` tells which |
| `TurnEnd` | `stop`, `steps` | Last event of a turn. `stop` is the final `Done.stop`, or `"max_steps"` |

One turn with one tool call:

```
MessageAdded(user)
StepStart(1)
WireRequest, WireResponse, WireEvent..., deltas, PartDone..., Usage, Done(tool_calls)
MessageAdded(assistant)
ToolStart(call, args)
ToolEnd(call, result)
MessageAdded(tool)
StepStart(2)
WireRequest, WireResponse, WireEvent..., TextDelta..., PartDone(Text), Usage, Done(end_turn)
MessageAdded(assistant)
TurnEnd("end_turn", 2)
```

## JSON encoding (`nexus/codec.py`)

Storage and the HTTP API use this encoding.

`to_json(value)`:

* A dataclass becomes a dict `{"type": <name>, <field>: ...}` with fields encoded
  recursively, in field order.
* Lists and tuples become lists; dicts are encoded value by value; anything else
  passes through unchanged.
* The `type` name is `text`, `reasoning`, `tool_call` or `tool_result` for the four
  parts, and otherwise the class name in snake case (`WireRequest` becomes
  `wire_request`, `ToolCallStarted` becomes `tool_call_started`, `StepStart`
  becomes `step_start`, `Message` becomes `message`).

```json
{"type": "tool_call", "id": "call_1", "name": "read", "arguments": "{\"path\":\"a.txt\"}", "origin": "codex", "raw": {"type": "function_call", "call_id": "call_1"}}
{"type": "tool_end", "call": {"type": "tool_call", "...": "..."}, "result": {"type": "tool_result", "call_id": "call_1", "output": "...", "is_error": false}}
{"type": "done", "stop": "end_turn", "response_id": "resp_1", "detail": null}
```

Messages and parts round-trip exactly:

* `message_to_json(m)` returns `{"role": ..., "parts": [...]}` (no `type` key).
* `message_from_json(data)` rebuilds the `Message`.
* `part_from_json(data)` builds a part from its `type` tag; only the four part
  types decode. Each part's `raw` survives, so replay stays lossless after a
  round trip. Events encode one way only.

## Bash jobs

A background bash job (`bash` with `background: true`) publishes `bash.started`
with its metadata, then `bash.finished` with status, exit code, duration, counts
and a clipped output tail. Both are stored in the starting session's event log.
The full retained output is available from `GET /api/bash/{id}` until the job is
evicted. A finish also enters the session as a notice message, so the model sees
it without polling. Killed jobs also finish; a job killed for session/project
deletion or daemon shutdown is silent.

## Questions

`question.asked` carries one pending request: id, session id, tool call id, the
validated questions (ids, text, options with labels/previews, `multiple` and
`allow_free_text`), and its creation time. It is stored in the asking session's
event log; a question asked from inside `code` uses the nested call id.

`question.replied` is the single resolution event. With `outcome: "answered"` it
carries the validated answers in question order. With `outcome: "cancelled"` it
carries an empty `answers` list and a `reason` (interrupted turn, deleted
session/project, daemon shutdown, or cancelled code cell). Startup recovery adds
a cancellation for a question a daemon crash left pending. There is never a
second reply event: a duplicate or late API reply gets 409. The tool's result
then enters the conversation once, as an ordinary `tool` message. Schema and
limits: [tools.md](tools.md#ask); endpoints: [api.md](api.md#endpoints).

## Approvals

`permission.asked` carries the pending request. `permission.replied` carries
`id`, `allow` and, for every new reply, `outcome` (`allowed`, `denied` or
`cancelled`) with `session_id`; a cancellation also has `reason`. Startup
recovery publishes a cancellation for an approval the crash lost, with
`allow: false`, so a client can close its waiting card. The old approval id no
longer exists and a reply gets 404.

## Code cells

The runtime publishes `code.call` for each finished nested task call: kernel id,
cell, index, parent code call id, tool, arguments, output and error (null on
success). A call stopped by a timeout or interrupt has an error and empty
output. `code.cell` records source, caller, stdout, value repr, duration and
error for a finished cell (including an aborted one). Both are stored in the
caller's session event log. `kernel.deleted` announces deletion or forced state
loss.


## Workspace control events

`session.workspace_tools` records that the on-demand workspace tools were
activated: `{active: true, entry, tools}`. It is stored in the session's event
log; the tool result (or the API notice) holds the definitions and guidance.

`session.directory_changed` records an applied move: `from`, `to`, `branch`,
`repository`, `worktree`, `source` (`tool` or `api`), `project_id`, the
`frozen_directory` (the directory baked into the system prompt) and a bounded
`instructions` record (path, status, bytes, sha256). The forward announcement is
an ordinary stored message: the tool result for a model-driven move, or a user
message added immediately (idle) or before the next model request (busy) for an
API move. The event is published only when the move is applied, so a pending API
move is visible first in session JSON's `directory_change`.

`worktree.listed`, `worktree.created` and `worktree.deleted` are stored in the
session that issued the control operation. They carry the repository, main
checkout, operation result and (for listing) every registered worktree's path,
branch, flags and working-tree state. Worktree operations never move the
session, so clients update worktree/git views without changing the session
directory.

## Archive events

`session.archived` and `session.unarchived` mark a whole root tree's transition.
Data: `{root, sessions: [ids], archived}`. The event is stored in the root
session's event log; `sessions` includes the root and every descendant. An
unchanged repeat publishes nothing. Session JSON carries `archived` separately
from `status` (`busy`/`idle`).
Automatic notices respect archive state. A background job finishing publishes
`bash.finished` as usual. If the target session is archived, its notice is
saved as an ordinary user message (`message.added`) instead of starting a turn;
the provider is not called. The stored notice is part of the context the model
receives when the user unarchives and resumes. A `wait: false` child cannot run
under an archived tree (archive refuses a busy descendant), but the same notice
path applies to any automatic notification.

## Session snapshots

`session.snapshot` records one-time reconstruction of an older session's
configuration, with the snapshot summary and its explicit historical limitation.
New sessions carry their snapshot summary in `session.created`; explicit updates
appear in `session.updated` and model-visible forward notices. Notices received
while a call is running are saved after its tool results, before the next model
request, as ordinary `message.added` items.

## Crash recovery

A daemon restart reconciles turns it left unfinished ([daemon.md](daemon.md#crash-recovery)).
Recovery publishes only durable, replayable facts:

- `turn.recovered` once per turn: `outcome` (`interrupted`, or `completed`
  when a durable `turn_end` proved the loop finished), the request state,
  restored `assistant_message` and `tool_message` ids, each tool call's
  classification, cancelled waits, lost runtime state, the diagnostic-only
  partial-output counts and the notice message id.
- `message.added` for a restored assistant message (parts that were completed
  before the crash, with `origin` and `raw`), for the single tool message that
  closes the calls, and for the new `[Nexus recovery: interrupted turn]` user
  notice. Earlier messages are never rewritten.
- `session.status` `/` idle with `reason` and `turn_id`, `question.replied`
  cancellations and `permission.replied` cancellations.

**Partial output.** Deltas, `part_done` items and `wire_event` records of an
interrupted stream remain durable diagnostic history. They are not inserted as
an assistant message: a cut-off response cannot be replayed without presenting
partial text, arguments or reasoning as a complete answer, and recovery never
completes or re-executes a tool call. Completed parts are restored only when the
provider's `Done` was durable. An interrupted request's `turn_request` row is
marked `interrupted`, which is distinct from a `completed` response.
