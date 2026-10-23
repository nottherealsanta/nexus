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
| session | `session.opened` `session.closed` `session.forked` `session.linked` (a supervision edge in the **manager's** log: `{kind: "managed", child, turn, origin_message_id, instruction_digest, agent}`; a root the manager launched also gets `session.opened {id, launched_by: {manager, origin_message_id}}`) `session.root_set` (per-session working root; `{kind, path_display, branch, base_ref, base_sha, worktree_id}`) |
| worktree | `session.worktree_set {enabled, base_ref}` records the draft preference; `worktree.created {branch, base_ref, base_sha, dirty_main: {modified, untracked}, path_display}`; `worktree.setup_ran {reason}`; `worktree.setup_output`; `worktree.setup_trust`; `worktree.identity_fallback`; `worktree.file_written`; `worktree.provenance_clipped`; `worktree.turn_started {head, seq, worktree_id}` is an internal commit boundary; `worktree.promoted`; `worktree.promotion_requested`; `worktree.promotion_failed {request_id, detail}` replays as a bounded status warning; `worktree.committed {sha, seq, by, agent_id, subject, files, clipped, insertions, deletions}`; `worktree.commit_skipped {reason, detail}`; `worktree.detached`; `checkpoint.started {seq, head}` records the turn's starting HEAD; `checkpoint.captured {seq, ref, commit, head, head_start, head_moved, base_ref, files, files_clipped, foreign, insertions, deletions, duration_ms}`; `checkpoint.skipped {seq, reason, detail}` |
| turn | `turn.started` `turn.completed` `turn.failed` `turn.cancelled` |
| context | `context.assembled` `context.compacted` `context.degraded` |
| model | `model.selected` `reasoning_effort.selected` `model.started` `text.delta` `text` `thinking.delta` `thinking.end` `thinking` `model.usage` `model.stopped` `model.retrying` |
| tool | `tool.requested` `tool.started` `tool.progress` `tool.completed` `tool.failed` `tool.input` `tool.result` |
| permission | `permission.requested` `permission.resolved` |
| question | `question.requested` `question.resolved` (answers are data, never grants, so they stay outside `permission`) |
| ext | `ext.loaded` `ext.unloaded` `ext.failed` `ext.tool_shadowed` `ext.manifest_changed` |
| mcp | `mcp.connected` `mcp.disconnected` `mcp.failed` `mcp.tools_changed` |
| skill | `skill.invoked` `skill.completed` |
| agent | `agent.spawned` `agent.completed` `agent.clamped` `agent.routed` `agent.resumed` `agent.stopped` (and `agent.selected`); see [Persistent children](#persistent-children) |
| hook | `hook.fired` `hook.blocked` |
| external | `external.started` `external.tool` `external.finished` ([below](#external-worker-events)) |
| input | `input.started` `input.queued` `input.moved` `input.mode_changed` (`{queued_id, mode, order}`: a promotion, queue→steer→interrupt; the reducer sets the item's mode and applies `order`) `input.consumed` `input.dropped` |
| shell | `shell.started` `shell.completed`: a composer `!` run, turn-less; reduced to a `TurnView(kind="shell")` with one `shell` row ([tools.md](tools.md#shell-mode--in-the-composer)). `shell.job_started` `shell.job_yielded` `shell.job_exited`: the lifecycle of a `shell` tool job, turn-less, ids/status/counts only (`job_exited` also records `delivery`: `tool_result`, `queued`, `paused` or `none`; `queued`/`paused` exits are written in the same batch as their `input.queued` notification, origin `shell_job`; a job lost with its daemon is closed on the next open as `status: interrupted`, `delivery: none`, `recovered: true`, with no notification); reduced to `ConversationView.jobs` ([tools.md](tools.md#shell-jobs)) |
| live | `live.started` `live.transcript` `live.transcript.delta` `live.delegated` `live.appended` `live.phase` `live.levels` `live.error` `live.ended`: a GPT Live voice call, turn-less and durable; reduced into `ConversationView.live_calls` ([live.md](live.md#events-and-view)) |
| manager | `manager.launch_refused {reason, turn}` `manager.result_read {session, bytes, clipped, turn}` `manager.wait {ids, outcome, waited_seconds, turn}` `manager.cancelled {session, cancelled, turn}`: audit records in the workspace manager's own log, turn-less and durable, ids/counts/reason codes only, never instruction or result text ([manager.md](manager.md#events)) |
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
| `external.started` | `provider`, `model`, `session`, `workspace`, `permission_mode`, `tools`, `allowed_tools`, `disallowed_tools`, `restricted`, `setting_sources`, `mcp` (`explicit`/`none`), `read_only`, `provenance`, `authority` |
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
unchanged. The subagent task row's `external_tool_calls` (separate from native
`tool_calls`, which stays unreported for external workers) is derived from the
child's unique `external.tool` rows, raised to `external.finished.tool_calls`
when that is larger, so live and replayed sessions agree. The native Ratatui client renders them ([ratatui-parity.md](ratatui-parity.md#external-claude-code-workers));
a web port is not planned (the web client is being deprecated).

### Code mode (`code.*`, plan 03 section 3.12)

`code.started`, `code.call`, `code.store` and `code.finished` describe one `code` tool call
(`call_id` is the outer call); nested calls are not message blocks and never enter the model
context. The reducer folds them into the outer `ToolCallView` (`view/reduce.py:_on_code`):

- `code_mode`: `code.started` and `code.finished` data merged (purpose, digest, timeout_s,
  exposure, callable, targets, limits, then status, limit, diagnostic, totals).
- `nested_calls`: one dict per `nested_id`, in start order. `code.call` is emitted `running`
  and again final; the later event merges over the earlier one, so rows appear live and replay
  equals live. When `code.finished` arrives, rows still `running` become `cancelled` (the
  program was cut off by a cancel, limit or crash), so a replayed log never shows a spinner.
  The loop must persist `call_id`, `nested_id` and `program_digest` of `code.*` events verbatim
  (`_safe_code_event`): the long-token redaction once turned the 44-character call id into
  `***`, so the events opened a phantom `code` row that never finished (an endless spinner and
  a `Working` header that kept animating). Replaying such old logs, a `code.finished` closes a
  row that no `tool.started` ever named.
- `ConversationView.code_store`: the reduced store (`set` / `delete`, only on success). The outer
  tool's `metrics["code.store"]` holds `entries` (whole store), `set` and `delete` of that call;
  `tool.completed` keeps it when it replaces the result metrics.

Rendering: `ui_support/tool_details.py` lays a code call out as sections `code · <purpose>`,
`Program status`, `Targets`, `Code` (line-numbered program), `Call list`, one `Nested call N` per
call (Tool, Kind, Status, Duration (ms), Permission, Matched rule, Bytes, Model / Input tokens /
Output tokens for `llm`, `Arguments.*`, then the result through the tool's own renderer; clipped
results are announced), `Result`, `Diagnostic`, `Store writes`, `Stored entries`.
`code_signal_rows` gives the compact timeline body (numbered program, one glyph row per nested call
`✓ ✗ ⊘ … –`, outcome). Golden log: `tests/fixtures/view/code_mode.*`.

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
`active_turn`, `root_agents`. A `PermissionView.ts` is the `permission.requested` event
time while the request is pending (a prompt counts "waiting" from it) and the resolution
time once resolved.

### Persistent children

A child run ending is not the child dying (plans/done/NONBLOCKING_SUBAGENTS_PLAN.md
3.4; the root-side tools land in later phases, the events and fields exist now).
`agent.completed` means "a run ended" and gains `run` (1-based) and `continuable`;
`agent.spawned` gains `background` (the tool call returned before the child ended)
and `continuable`. New events, both in the **root** log:

| Event | Data |
| --- | --- |
| `agent.resumed` | `{agent, run, from: "root", message, message_chars, background, model, tier, call_id, worktree}`; written by the runner for `subagent(resume=…)` (the earlier `subagent_send` tool wrote the same shape; old logs replay it). `run` is previous runs + 1. `message` is the redacted follow-up clipped to 3900 characters (ellipsis-marked); the full text is the user message of the child's new turn in its own log. `background` is always false |
| `agent.stopped` | `{agent, by: "root"/"user"/"limit", reason, run}`; written through the root event port by host `AgentClose` (`by: "user"`; historical logs also hold `by: "root"` from the retired `subagent_stop`); terminal, the child log is kept. A running child's cancelled `agent.completed` precedes it |

`AgentView.status` keeps its meaning (`spawned` = a run is in flight; `/tasks` and
crash recovery key off it). It gains `runs` (spawn is run 1; `agent.resumed` and
`agent.completed` take the larger value), `continuable`, `stopped` (+ `stopped_by`,
`stopped_reason`), `last_run_ts` (latest spawn/resume/completion) and `background`.
Derived display state: running = `status == "spawned"`; idle = ended and
`continuable` and not `stopped`; stopped; interrupted (`status == "interrupted"`).
The reducer handles `agent.resumed` by setting `status` back to `spawned`, clearing
the last result and appending a new active turn to `AgentView.body` whose user row
carries `origin = {"kind": "root_followup", "agent", "run"}`; the child's own relayed
events then land in that turn. Agent events carry no root turn id, so they never open
a phantom root turn (tested). An `agent.resumed`/`agent.stopped` for an unknown agent
is a diagnostic, as for `agent.completed`.

Foreground continuation ([agents.md](agents.md#continuing-a-finished-subagent)) writes
these fields again: every native child's `agent.completed` carries `run` and `continuable`
(true only for a completed, non-worktree, non-external child with a stored record), and its
`agent.spawned` carries `config_sha` (a digest, never the record). `agent.resumed` and
`agent.stopped` set the view's `continuable` to false (a run in flight, or closed); the next
`agent.completed` says again. `usage` on the view is the latest run's. The child's own log
holds one more event, `agent.config` (the immutable configuration record, including the
redacted role prompt). It exists only in the child log, is never relayed to the parent log
or to a UI, is not part of the `AGENT_EVENTS` catalogue, and is not model context; consumers
that read raw child logs must treat it as sensitive and skip it. The reducer recognises it
and ignores it, so a child's `ConversationView` (and the inspect UI built on it) never shows
it as a diagnostic.

A user `MessageView` and a `QueuedInputView` carry an optional `origin`
(`{"kind": "live", "delegation_id": ...}`) when the message was not typed; it is
recorded on `input.queued` only, so the prompt text and the turn are unchanged
(omitted from the wire when absent). A root launched by the manager carries
`{"kind": "manager", "manager": <manager id>, "origin_message_id": ...}` (the reducer keeps those two
extra keys for that kind only), so a client can say "launched by manager". Two more kinds carry
`agent`, `run`, `status` and `agents` (a bounded list of at most 32 agent ids, each at most 200
characters; `run` is a non-negative integer, anything else is dropped): `subagent_report` (a
finished child's report delivered to the root through its input queue; `agents` lists `"<agent id>:<run>"` for every report in the batch, and the queue keeps ints and string lists in an origin instead of stringifying them; the native Ratatui client draws it as a compact `agent_report` block from this origin and the child's `type`/`description`, never as a user card) and `root_followup` (the
root's follow-up shown on a child's page). Unknown kinds keep only `kind`. `session.linked` and every
`manager.*` event fold to nothing in the view: they are audit metadata, not conversation, so they
add no diagnostic row. A durable `context.notice` with `kind: "subagent_wake_paused"` (`message`, `pending`, `wakes`) records that the auto-wake cap paused report delivery; the reducer does not draw it yet. `LogsRead` projects each with a fixed summary and no event data.

A user `MessageView` and a `QueuedInputView` also carry `sent_ts`: the `input.queued`
event time (a consumed queued item keeps it, not the later consume time), or the
`input.started` time for a direct send. It is optional (`None`, omitted from the wire,
for older recordings; the first card then falls back to `TurnView.user_ts`) and is
durable: replay and live fold to the same value, including a consume seen before its
queued event.

`ToolCallView.diff` (bounded, durable) is the only source for an Edit/Patch diff
in any UI; a surface never reads files to render one.

`session.root_set` is the durable root change event. Its `path_display` is a
bounded path relative to the project's parent, which lets replay reconstruct
the absolute root without storing an absolute path in the log or adding a
separate root table. `kind` is `worktree` or `external`; main roots are implicit
and remain the default when no event exists. `branch`, `base_ref`, `base_sha`,
and `worktree_id` are bounded metadata. `ConversationView.root` reduces this to
a `RootView` with the same metadata and an empty display path for the main root.
The frozen `SessionRoot` value object in `nexus/session/root.py` owns validation
and the relative `to_data`/`from_data` conversion.

`ConversationView.worktree` is the replayable session worktree projection. It
keeps the selected/base branch and lifecycle state, bounded uncommitted counts,
the last commit skip, and the newest 200 `WorktreeCommitView` records. Each
commit carries a turn sequence and SHA; when the event envelope has a turn id,
the reducer also adds its SHA to that `TurnView.commit_shas` row. Setup events
and malformed lifecycle payloads cannot expand the view without bounds. Setup
output and internal file-write receipts are intentionally not projected into
the view; `worktree.file_written` carries promotion provenance only.

Worktree tool events are audit summaries: commit file lists are capped at 200
paths and subjects at 200 characters; skipped details are redacted/bounded by
the emitter. Status polling is a tool result rather than a durable event, so
ahead/behind and live dirty counts remain unknown in replay until a future
event contract records a bounded snapshot.

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

`context.handoff` is a known context event. Its stable marker includes the
handoff ID, head/summary seq, kept boundary, read-through seq, brief version,
method, bounded redacted preview, approximate before/after counts and fallback
flag. The reducer retains it as `ContextView(kind="handoff")`; transcript
content before it is never deleted. Child handoff markers are suppressed from
parent relay (the child log retains them). Native projection shows a stable
“Context handed off” divider with `/handoff read` guidance.

`TurnView.completed_ts` is the terminal event timestamp (`turn.completed`,
`turn.failed`, or `turn.cancelled`), preserved by replay and snapshots. It is
omitted when unknown; views must not substitute a last-update or current clock.

The native projection (`nexus/ui/ratatui`, not the reducer) adds three block fields,
all derived from the view: `markdown.streaming` (true while the assistant message is
not `done` and its turn is not terminal; absent otherwise), `tool_group.hidden`
(members left out of `members` while the group runs folded, which then carries only
the newest five), and `tool_group.running_label` (the literal `Working` while a member
runs). A group's `text` drops its nouns above five members.
