# Events and the view

Events are the spine of Nexus. The loop and managers emit them, the session
persists them, and a pure reducer turns them into the tree every UI renders.

> Every state change a UI could draw is an event. No UI polls a manager.

## The envelope (`events.py`)

`Event(type, data, seq, ts, session, turn, id)`, a frozen `msgspec` struct.

- `seq` is monotonic per session and assigned by the session store when the event
  is persisted (the loop leaves it `0`). It makes subscriptions resumable,
  records replayable, and SSE `Last-Event-ID` map one-to-one onto the log.
- `data` is JSON-serializable and bounded. UIs **must tolerate unknown types**.
- Emitters persist first, then fan out (`EventSink` contract in `core/loop.py`).
- The session summary's `last_seq` is the cursor for all durable log records;
  its `completion_seq` is a separate durable watermark for terminal turn events
  (`turn.completed`, `turn.failed`, `turn.cancelled`). Use the latter for unread
  completion indicators, since presence and detach events also consume sequences.

## Catalogue

Groups live in `EVENT_GROUPS`; `EVENT_TYPES` is their union plus legacy/compat
names and `agent.selected`.

A `turn.cancelled` whose `reason` is `nexus.events.SHUTDOWN_CANCEL_REASON`
(`supervisor closing`, emitted when the supervisor cancels every turn at
shutdown) folds to `phase="cancelled"` with no `TurnView.error`, so views show no
`Error:` row for the expected shutdown. Other cancel reasons and every
`turn.failed` keep their error text.

| Group | Types |
| --- | --- |
| session | `session.opened` `session.closed` `session.forked` |
| turn | `turn.started` `turn.completed` `turn.failed` `turn.cancelled` |
| context | `context.assembled` `context.compacted` `context.degraded` |
| model | `model.selected` `reasoning_effort.selected` `model.started` `text.delta` `text` `thinking.delta` `thinking.end` `thinking` `model.usage` `model.stopped` `model.retrying` |
| tool | `tool.requested` `tool.started` `tool.progress` `tool.completed` `tool.failed` `tool.input` `tool.result` |
| permission | `permission.requested` `permission.resolved` |
| question | `question.requested` `question.resolved` (answers are data, never grants, so they stay outside `permission`) |
| ext | `ext.loaded` `ext.unloaded` `ext.failed` `ext.tool_shadowed` `ext.manifest_changed` |
| mcp | `mcp.connected` `mcp.disconnected` `mcp.failed` `mcp.tools_changed` |
| skill | `skill.invoked` `skill.completed` |
| agent | `agent.spawned` `agent.completed` `agent.clamped` (and `agent.selected`) |
| hook | `hook.fired` `hook.blocked` |
| external | `external.started` `external.tool` `external.finished` ([below](#external-worker-events)) |
| input | `input.started` `input.queued` `input.moved` `input.consumed` `input.dropped` |
| shell | `shell.started` `shell.completed`: a composer `!` run, turn-less; reduced to a `TurnView(kind="shell")` with one `bash` row ([tools.md](tools.md#shell-mode--in-the-composer)) |
| live | `live.started` `live.transcript` `live.transcript.delta` `live.delegated` `live.appended` `live.phase` `live.levels` `live.error` `live.ended`: a GPT Live voice call, turn-less and durable; reduced into `ConversationView.live_calls` ([live.md](live.md#events-and-view)) |
| presence | `presence.joined` `presence.left` (count, not identity) |
| daemon | `daemon.started` `daemon.stopping` `daemon.session_scheduled` `daemon.session_queued` |
| misc | `provider.raw` `error` `registry.refreshed` `registry.stale` `registry.failed` `registry.mismatch` |
| legacy / compat | `started` `message` `provider` `completed`; `presence.changed` `daemon.stopped` `daemon.client_connected` `daemon.client_disconnected` |

Notes: `input.started` marks a direct (non-queued) start so it never appears as a
pending queue item. `text` and `thinking` are the finalized aggregates after the
`*.delta` stream; the reducer dedupes them (`BlockView.streamed`). Tool events
carry only bounded, presentation-safe views: raw inputs are not duplicated into
the log beyond `tool.input`, which passes through `_safe_tool_input`.

### External worker events

Emitted by `ClaudeCodeWorker` for an explicit `claude-code/<model>` child
([agents.md](agents.md#external-claude-code-workers)); they go in the child's own
log and are relayed upward like other child events.

| Event | Payload |
| --- | --- |
| `external.started` | `provider`, `model`, `session`, `workspace`, `permission_mode`, `tools`, `allowed_tools`, `disallowed_tools`, `restricted`, `setting_sources`, `mcp` (`explicit`/`none`), `read_only`, `timeout_seconds`, `provenance`, `authority` |
| `external.tool` | `phase` `requested` (bounded `input`) or `completed` (`content`, `is_error`), `call_id`, `tool`, optional `clip_note`, `provenance` (`Claude Code (own tools)`), `provider`, `session` |
| `external.finished` | `status` (`completed`/`failed`/`interrupted`/`cancelled`/`needs_attention`), `stop_event`, `transcript_path`, `tool_calls`, `usage` (`"unknown"`), `error`, `attention`, `clip_notes`, `elapsed_seconds`, `provenance`, `session`, `model` |

Claude's assistant text arrives as ordinary `text` and `model.stopped` events
(provider `claude-code`, `source` `claude-code-transcript` or `claude-code-hook`).
Claude's tool calls are never `tool.*` events: Nexus did not dispatch them.
The child's `turn.completed` carries `usage_unknown: true`, and `agent.completed`
carries it too.

View fields: the reducer turns `external.tool` into a `ToolCallView` with
`provenance` set and `executed = false` (idempotent by `call_id`, tolerant of a
result arriving before its request); the tool-details panel shows a
`Provenance` row in place of `Executed`. `external.started`/`external.finished`
merge into `ConversationView.external` (a dict, `finished: true` after the end).
`AgentView.usage_unknown` marks usage that is unknown rather than zero. All three
fields are omitted from the wire shape when unset, so ordinary sessions are
unchanged. The native Ratatui client renders them ([ratatui-parity.md](ratatui-parity.md#external-claude-code-workers));
a web port is not planned (the web client is being deprecated).

## The reducer (`view/`)

`view/reduce.py` is pure and synchronous: `apply(state, event) → state`,
`apply_many`, `initial_state`; `view/fold.py` has `fold(events)` for a whole log
and delta accumulation. It imports only `nexus.events`.

- **Idempotent replay:** an already-applied `seq` is ignored.
- **Unknown events** are kept as `DiagnosticView`s, never dropped silently.
- **Child events:** a relayed event carrying an `agent` block reduces into that
  agent's nested conversation (`AgentView`), so a subagent tree rebuilds from the
  parent log alone. `AgentView.model` is the spawn reference (a tier or requested
  model); `AgentView.resolved_model` is the actual `provider/model` from the
  latest child `model.started`/`external.started` (omitted until one is seen;
  `agent.completed.resolved_model` backfills it).
- Text is bounded (`MAX_TEXT = 8192` per field, with an ellipsis). Message
  text and thinking blocks (user prompts, pastes, assistant prose) use the much
  larger `MAX_MESSAGE_TEXT` (8 MiB), so a long prompt is never clipped in the
  transcript; the model always receives the full prompt either way.

`view/model.py` defines `ConversationView`: `session_id`, `last_seq`, `phase`
(`idle|running|awaiting_input|awaiting_permission|closed`), `turns`
(`TurnView` → `MessageView`/`BlockView`, `ToolCallView`, approvals, `ContextView`,
`RetryView`, usage), `permissions`, `presence`, `input_queue`, `agents`,
`extensions`, `mcp`, `context`, `model`, `registry`, `errors`, `hooks`,
`diagnostics`, `live_calls` (`LiveCallView`s, oldest first, omitted from the wire when empty); derived `live` (newest call), `messages`, `tools`, `usage`, `pending_permissions`,
`active_turn`, `root_agents`.

A user `MessageView` and a `QueuedInputView` carry an optional `origin`
(`{"kind": "live", "delegation_id": ...}`) when the message was not typed; it is
recorded on `input.queued` only, so the prompt text and the turn are unchanged
(omitted from the wire when absent).

`ToolCallView.diff` (bounded, durable) is the only source for an Edit/Patch diff
in any UI; a surface never reads files to render one.

## Why this shape

- **Replay is the regression harness.** `nexus replay` / `nexus sessions replay`
  run the same reducer the live UIs use.
- **Reconnect is free.** A client folds the log to `seq`, then subscribes from
  `seq + 1` (`HostFacade.state` + `subscribe`).
- **Browser patches.** `host_support/browser_view.py` projects the view to JSON
  and diffs it into JSON-Pointer ops (`add`/`replace`/`remove`/`append`) with a
  `resync` escape hatch ([web.md](web.md)).

## Adding an event or view field

1. Emit it from the owning layer and add the name to its group in `events.py`.
2. Teach `view/reduce.py` to fold it and extend `view/model.py` if the UI must
   draw it. Keep the reducer pure and bounded.
3. If the browser needs it, it rides the existing snapshot/patch path; add a
   port in `ui/web/js/` and keep the TUI and web rows matching
   ([surfaces.md](surfaces.md)).
4. Tests: `tests/test_view_reduce.py`, the fixtures under `tests/fixtures/view/`,
   and `tests/test_events*.py` where the catalogue is pinned.

Overload retries record `model.retrying` with `reason: provider_overloaded`,
`attempt`, and `delay_seconds`. The reducer closes the interrupted assistant
message; the next `model.started` opens a separate attempt, retaining partial
text for live display and replay. Failed/cancelled turns mark unfinished tool
rows failed, so a task cannot remain at “Starting…” after its turn has ended.

`agent.completed.selected_model` optionally discloses a concrete route selected
before a child-start refusal (e.g. an Antigravity read-only policy mismatch).
The reducer retains it as `AgentView.selected_model`; it is never treated as
`resolved_model` or counted as a model start. Terminal links label it selected /
not started and display the redacted configuration error. Replay reproduces
this disclosure.

## Compact question rendering and expected shutdown cancellation

The Ratatui projection renders a Question Tool call as the prompt plus one line per option,
marking the recorded answer. Free-text answers render as text, and an unanswered call is
labelled awaiting answer; full labelled details remain available through the inspector or
verbose mode. This is a view-only presentation and does not change how the pending-question
prompt accepts an answer. The projection also suppresses the duplicate generic tool summary
from a completed compact question card.

A `turn.cancelled` with the supervisor's expected `supervisor closing` reason has
`phase="cancelled"` but no view error, avoiding a misleading `Error:` row during shutdown.
Other cancellation reasons and all `turn.failed` errors are retained. See the catalogue
above and `ratatui-parity.md` for focused test and visual-verification caveats.
