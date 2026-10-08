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
| input | `input.started` `input.queued` `input.moved` `input.consumed` `input.dropped` |
| shell | `shell.started` `shell.completed`: a composer `!` run, turn-less; reduced to a `TurnView(kind="shell")` with one `bash` row ([tools.md](tools.md#shell-mode--in-the-composer)) |
| presence | `presence.joined` `presence.left` (count, not identity) |
| daemon | `daemon.started` `daemon.stopping` `daemon.session_scheduled` `daemon.session_queued` |
| misc | `provider.raw` `error` `registry.refreshed` `registry.stale` `registry.failed` `registry.mismatch` |
| legacy / compat | `started` `message` `provider` `completed`; `presence.changed` `daemon.stopped` `daemon.client_connected` `daemon.client_disconnected` |

Notes: `input.started` marks a direct (non-queued) start so it never appears as a
pending queue item. `text` and `thinking` are the finalized aggregates after the
`*.delta` stream; the reducer dedupes them (`BlockView.streamed`). Tool events
carry only bounded, presentation-safe views: raw inputs are not duplicated into
the log beyond `tool.input`, which passes through `_safe_tool_input`.

## The reducer (`view/`)

`view/reduce.py` is pure and synchronous: `apply(state, event) → state`,
`apply_many`, `initial_state`; `view/fold.py` has `fold(events)` for a whole log
and delta accumulation. It imports only `nexus.events`.

- **Idempotent replay:** an already-applied `seq` is ignored.
- **Unknown events** are kept as `DiagnosticView`s, never dropped silently.
- **Child events:** a relayed event carrying an `agent` block reduces into that
  agent's nested conversation (`AgentView`), so a subagent tree rebuilds from the
  parent log alone.
- Text is bounded (`MAX_TEXT = 8192` per field, with an ellipsis). Message
  text and thinking blocks (user prompts, pastes, assistant prose) use the much
  larger `MAX_MESSAGE_TEXT` (8 MiB), so a long prompt is never clipped in the
  transcript; the model always receives the full prompt either way.

`view/model.py` defines `ConversationView`: `session_id`, `last_seq`, `phase`
(`idle|running|awaiting_input|awaiting_permission|closed`), `turns`
(`TurnView` → `MessageView`/`BlockView`, `ToolCallView`, approvals, `ContextView`,
`RetryView`, usage), `permissions`, `presence`, `input_queue`, `agents`,
`extensions`, `mcp`, `context`, `model`, `registry`, `errors`, `hooks`,
`diagnostics`; derived `messages`, `tools`, `usage`, `pending_permissions`,
`active_turn`, `root_agents`.

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
