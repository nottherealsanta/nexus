# Plan: turn summaries instead of loading event history

Status: proposed 2026-10-28, revised 2026-10-29. Not started.

Clients stop loading the event log when they open a session. Core keeps a small
durable summary of every turn (tokens, cache, model, tool timings, thinking
time, errors, retries) and a per-session spend ledger, and clients read those.
A streaming response is kept as a throttled partial on its request row, as
pi-durable does, so deltas are published live and never saved. The state
tables become the only source of truth for what clients show. The event log
keeps the raw wire traffic, the reconnect cursor and the `message.added`
outbox.

## Contents

1. Why
2. What the event log is for
3. What exists
4. Design: schema, store, runtime, live partial, bus, API, recovery,
   retention, backfill
5. Clients: TUI, NSH
6. Old Nexus and pi-durable
7. Decisions and ledger rows
8. Steps (each with files, tests and a done-when)
9. Risks, out of scope

## 1. Why

- Opening a session pages the whole event log (5000 per page) and keeps the
  newest 20 000 (`HISTORY_EVENT_CAP`, `nexus-tui/src/sync.rs:547`). Most of
  those are per-token `text_delta` and `reasoning_delta` events that only the
  live view and the inspector use. A large session shows "N older diagnostics
  are not loaded" (`timeline.rs:608`), and the TUI walks every kept event on
  every frame.
- The event log is pruned at 14 days or 256 MiB (`retention.py`), so an old
  session already loses its footer, tokens, models and tool timings. A turn row
  is pruned only when it is older than 30 days **and** outside the session's
  newest 200 (`store.prune_turns`). Today's behaviour is already "partly gone
  after two weeks"; this plan makes it deliberate and complete.
- Storage and load time: a long session should open from a few kilobytes of
  summaries, not megabytes of events.

## 2. What the event log is for

Old Nexus stored only events (messages included) and derived every view from
them. nexus10 stores state tables (`message`, `turn`, `turn_request`,
`turn_attempt`, `turn_tool`) and an event log beside them (`store.py` module
docstring). Today the log does these jobs:

| Job | Reader | After this plan |
| --- | --- | --- |
| Raw wire traffic (`wire_request`, `wire_response`, `wire_event`) | inspector, export, `nexus chat --wire` | Kept under the existing retention |
| Reconnect from a cursor | `GET /api/session/{id}/event?after=`, SSE `Last-Event-ID` | Kept. Streamed text no longer needs it (the live partial) |
| `message.added` outbox: link a saved message to its announcement, publish a missed one exactly once | `recovery.py` (`event_for_message`, `messages_missing_events`) | Kept. The event *is* the announcement; only the newest events are read |
| Did the loop end the turn before the crash? | `recovery._durable_turn_end` reads `turn_end` | Moved to the turn row (step 8) |
| What an interrupted stream received | `store.stream_summary` counts saved deltas | Read from `turn_request.partial` (step 9) |
| Footer, stats, context %, model, timings, retries | TUI `timeline.rs`, `render.rs`, `stats.rs` | Moved to turn summaries and the ledger (steps 1–4) |
| Directory change lookup | NSH `store.ts:262` | Session view (step 6) |

Deltas (`text_delta`, `reasoning_delta`, `tool_args_delta`,
`tool_call_started`) are a large share of the volume. Their final content is
in messages and the raw SSE they were parsed from is already stored as
`wire_event`. After this plan they are published live only. `wire_event` has
about as many rows as the deltas, so this roughly halves the streaming rows;
wire retention still bounds the total.

## 3. What exists

Core already stores, independent of the event log (`store.py` schema):

| Table | Holds |
| --- | --- |
| `turn` | status, started, finished, steps, `start_seq` (first message seq of the turn), error, recovery, notice id, data |
| `turn_request` | one row per provider call: step, status, started, finished, `event_after` (log head when sent), `message_id`, stop, response id, error, `parts` (completed parts after `Done`, for recovery), data |
| `turn_attempt` | one row per attempt, retries included: attempt number, status, delay, started, finished, error |
| `turn_tool` | per tool call: name, arguments, state (saved → started → finished → inserted), started, finished, output, `is_error` |

No API exposes these rows to clients.

Missing:

- **Token usage.** `Usage` (`llmhub/types.py:144`: input, cached input,
  output, reasoning) is yielded by the providers and published as a `usage`
  event, but the runtime's event branch (`runtime.py`, the `isinstance` chain
  after `StepStart`) does not handle it, so it reaches only the event log.
- **Model and effort per request.** Only `wire_request.body.model` has it, as
  the bare provider model id, inside a large body. The model can change
  between steps: `refresh` re-binds the running agent (`runtime.py`, the
  `setattr` loop over `provider`, `model`, `reasoning_effort`).
- **Reasoning duration.** Clients measure it from delta timestamps.
- **Session totals that survive turn pruning.**
- **An endpoint** that returns all of the above compactly.

## 4. Design

### 4.1 Schema

New columns and one new table. Added in `Store._migrate` with the existing
`pragma table_info` pattern (`store.py:275–320`).

```sql
-- turn_request: what the request ran with, what it cost, and its live partial
alter table turn_request add column model text;              -- "provider/model" ref
alter table turn_request add column effort text;             -- reasoning effort, null if none
alter table turn_request add column input_tokens integer;    -- null = unknown, never 0 for unknown
alter table turn_request add column cached_input_tokens integer;
alter table turn_request add column output_tokens integer;
alter table turn_request add column reasoning_tokens integer;
alter table turn_request add column reasoning_seconds real;
alter table turn_request add column partial text;            -- JSON, see 4.4; null when none

-- turn_attempt: each attempt's own usage, so a retried attempt's spend is kept
alter table turn_attempt add column input_tokens integer;
alter table turn_attempt add column cached_input_tokens integer;
alter table turn_attempt add column output_tokens integer;
alter table turn_attempt add column reasoning_tokens integer;

-- turn: the loop ended the turn (replaces reading `turn_end` from the log)
alter table turn add column ended text;                      -- JSON {"stop": ..., "steps": n}, null until then

-- per-session spend, kept when turns are pruned
create table if not exists session_usage (
  session_id text not null references session(id) on delete cascade,
  model text not null,                                       -- "provider/model"
  requests integer not null default 0,
  input_tokens integer not null default 0,
  cached_input_tokens integer not null default 0,
  output_tokens integer not null default 0,
  reasoning_tokens integer not null default 0,
  since real not null,                                       -- first time this row counted
  primary key (session_id, model)
);
```

Columns, not JSON in `turn_request.data`: `data` already holds the
interrupted-request payload (`interrupt_request`), and columns let totals be
summed in SQL. `partial` is JSON because its shape follows the conversation
types.

### 4.2 Store

New and changed `Store` methods (`store.py`):

- `start_request(turn_id, session_id, step, *, model, effort)`: also writes
  `model` and `effort`. Called at `StepStart`, after `refresh` has run, so it
  records the model this step actually uses.
- `record_usage(request_id, attempt_id, usage)`: one transaction that
  1. replaces the attempt's usage columns with `usage` (a provider reports one
     cumulative `Usage` per response; the last one for an attempt wins),
  2. sets the request's usage columns to the sum over its attempts that have
     usage (null if none do),
  3. adds the difference between the new and old attempt usage to the
     `session_usage` row for the request's `model`, inserting the row with
     `since = now` if missing; `requests` is incremented the first time an
     attempt of a new request reports usage.

  Idempotent for a repeated identical `Usage`.
- `save_partial(request_id, partial)`: writes `turn_request.partial` and
  `reasoning_seconds`. Throttled by the runtime, not the store.
- `finish_request(...)`, `interrupt_request(...)`, `fail_request(...)`: also
  take `partial` and `reasoning_seconds` and write them in the same update, so
  the final state is never more than the last delta behind.
- `save_assistant_message(...)`: the existing
  `update turn_request set message_id = ?, parts = '[]'` (`store.py:1328`) also
  sets `partial = null`. The message replaces the partial atomically.
- `mark_turn_ended(turn_id, stop, steps)`: sets `turn.ended`.
- `turn_summaries(session_id, *, after=None, before=None, limit=50)`: the API
  shape in 4.6, oldest to newest, with requests, attempts and tools joined in
  three queries (no per-turn queries).
- `session_usage(session_id)`: the ledger rows.
- `_request_row`, `_tool_row`: carry the new columns.
- `stream_summary(session_id, after)` becomes `partial_summary(request)`: the
  same counts (text, reasoning and argument characters, started tool calls,
  completed parts) computed from `turn_request.partial`.

### 4.3 Runtime

In the turn task's event branch (`runtime.py`, the loop over
`agent.turn(user_text)`):

- `StepStart`: resolve the model ref (`resolve_model(session, spec)[0]`, the
  value `_agent` uses) and `agent.reasoning_effort`, pass both to
  `start_request`, and create a `LivePartial` (4.4) for the request.
- `Usage`: `store.record_usage(active_request, active_attempt, usage)`. New
  branch; today `Usage` only falls through to `bus.publish`.
- `TextDelta`, `ReasoningDelta`, `ToolCallStarted`, `ToolArgsDelta`: fold into
  the `LivePartial`, which returns `(part, seq)`; publish with `request_id`,
  `part` and `seq` added and `durable=False` (4.5). Persist with
  `save_partial` when at least `PARTIAL_PERSIST_SECONDS = 1.0` passed since the
  last save.
- `PartDone`: `LivePartial.done(part)` replaces the part with its final
  encoding (`to_json(event.part)`); a provider may change text at a part's
  end, so the final part wins over the folded deltas.
- `Done`, the `CancelledError` path and the `ProviderError` path: pass the
  partial and `reasoning_seconds` to `finish_request`, `interrupt_request` or
  `fail_request`.
- `MessageAdded` (assistant): drop the `LivePartial`; `save_assistant_message`
  clears the column.
- `TurnEnd`: `store.mark_turn_ended(turn_id, stop, steps)` before publishing
  `turn_end`.
- After every `finish_turn` (completed, failed, interrupted) and after
  recovery closes a turn: publish a durable `turn.summary` event whose data is
  that turn's entry from `turn_summaries`. Failed and interrupted turns never
  yield `TurnEnd`, so `turn_end` cannot carry the summary.
- `self._live[session_id]`: the running request's `LivePartial`, read by the
  API so a joining client sees text up to the latest delta, not the last
  persisted second.

Reasoning time: `LivePartial` records the time of a reasoning part's first
delta and its end (its `PartDone`, or the first delta of another part).
`reasoning_seconds` is the sum over the request's reasoning parts.

### 4.4 Live partial

After pi-durable's `pi.live.generation.message`. New module `nexus/live.py`:

```python
class LivePartial:
    """The response of one running request so far, folded from its deltas."""

    request_id: str
    seq: int                   # last delta folded in; 0 before the first
    parts: list[dict]          # see shape below
    reasoning_seconds: float

    def text(self, delta: str) -> tuple[int, int]: ...       # -> (part index, seq)
    def reasoning(self, delta: str) -> tuple[int, int]: ...
    def tool_call(self, id: str, name: str) -> tuple[int, int]: ...
    def tool_args(self, id: str, delta: str) -> tuple[int, int]: ...
    def done(self, part: dict) -> None: ...                  # final encoding of the oldest open part
    def to_json(self) -> dict: ...
```

Folding rules: a text or reasoning delta appends to the last part if it is an
open part of the same kind, otherwise opens a new part. `tool_call` opens a
part; `tool_args` appends to the open tool call with that id. `done` closes
parts in order.

Stored and served shape:

```json
{
  "request_id": "req_…", "seq": 412,
  "parts": [
    {"type": "reasoning", "text": "…", "done": true},
    {"type": "text", "text": "…", "done": false},
    {"type": "tool_call", "id": "call_…", "name": "bash", "arguments": "{\"cmd\": \"ls", "done": false}
  ],
  "reasoning_seconds": 2.1
}
```

A done part holds its final `to_json(part)` encoding plus `"done": true`. Open
tool-call arguments are the raw text so far, not parsed JSON.

The partial is a diagnostic of an unfinished response, not model context. The
timeline shows it as streaming while the request runs; after an interruption
it shows the recovery notice and the partial marked as not in context, as
recovery's `diagnostic_only` flag already says. The UI must not present it as
an assistant message (AGENTS.md: the UI shows nothing the model does not
have).

Joining or reconnecting: read `/turn` (or `/turn?after=`), take the running
request's `partial`, then apply live deltas for that `request_id` with a
greater `seq` and drop the rest. Deltas for another request start a new
scratch part list.

### 4.5 Bus

`Bus.publish(type_, data, *, session_id, durable=True)` (`bus.py:82`):

- `durable=False`: no `append_event`, `event["id"] = None`, delivered to
  subscribers as usual.
- `_deliver` sets `self._subscribers[queue]` (the last delivered id, used by
  `bus.overflow`'s `dropped_after`) only for durable events, so an ephemeral
  event never moves a client's resume point.
- The SSE writer in `server.py` omits the `id:` line for events without an id,
  so `Last-Event-ID` stays at the last durable event. Check the writer and the
  `stored_events` route for any assumption that every event has an id.
- Ephemeral types: `text_delta`, `reasoning_delta`, `tool_call_started`,
  `tool_args_delta`. Every other type stays durable, `wire_event` included.

### 4.6 API

`GET /api/session/{id}/turn` (singular, matching `/message` and `/event`).

Query:

- none: the newest `limit` turns.
- `after=<turn id>`: turns started after that turn, oldest first (reconnect).
- `before=<turn id>`: the `limit` turns before that turn (load earlier).
- `limit`: default 50, max 200.

Response:

```json
{
  "turns": [{
    "id": "trn_…", "status": "completed", "started": 0.0, "finished": 0.0,
    "start_seq": 41, "steps": 3, "stop": "end_turn", "error": null,
    "recovery": "none",
    "requests": [{
      "id": "req_…", "step": 1, "status": "completed",
      "model": "provider/model", "effort": "medium",
      "started": 0.0, "finished": 0.0, "stop": "tool_calls", "error": null,
      "message_id": "msg_…",
      "usage": {"input": 0, "cached": 0, "output": 0, "reasoning": 0},
      "reasoning_seconds": 2.1,
      "attempts": [{"n": 1, "status": "failed", "delay": 0.0, "error": "…",
                    "usage": null}],
      "partial": null
    }],
    "tools": [{"call_id": "…", "name": "bash", "state": "inserted",
               "started": 0.0, "finished": 0.0, "is_error": false}]
  }],
  "more_before": false,
  "floor": {"seq": 12},
  "usage": {
    "since": 0.0,
    "models": {"provider/model": {"requests": 0, "input": 0, "cached": 0,
                                  "output": 0, "reasoning": 0}}
  }
}
```

- `stop`: the turn's `ended.stop`, else the last request's stop.
- `usage` on a request or attempt: `null` when unknown, never zeros.
- `partial`: present only on the running request; served from
  `runtime._live`, falling back to the stored column.
- `message_id` and `start_seq` anchor summaries to messages: a request belongs
  to the assistant message `message_id`; a turn starts at message `start_seq`.
- `floor.seq`: messages below this seq belong to pruned turns and have no
  summary. `null` when no turn was pruned.
- `usage`: the session ledger, covering pruned turns too. `since` is the
  earliest `session_usage.since`; for a session older than the ledger, totals
  start there.
- Tool output and arguments stay out; they are in the messages.
- 404 for an unknown session, 400 for an unknown `after`/`before` turn.
- `API_VERSION` (`server.py:86`) goes to minor 2.

Events:

- `turn.summary` (durable): one turn entry, published as in 4.3.
- Delta events gain `request_id`, `part`, `seq` and are ephemeral.
- `usage` stays as it is.
- Documented in `docs/api.md` and `docs/stream-events.md`.

Not embedded in the snapshot: clients fetch `/turn` in parallel with
messages.

### 4.7 Recovery

`recovery.py`:

- `_durable_turn_end`: read `turn.ended` instead of searching the log for
  `turn_end`. A turn that crashed before this migration has `ended = null`;
  for those, keep the log lookup as a fallback until no such turn is open (or
  for one release).
- `partial = {"diagnostic_only": True, **self.store.stream_summary(...)}` and
  the notice text in `_notice`: use `partial_summary(request)`. Change "That
  output remains in the session's event log" to say it remains on the request
  as its partial output (and the raw SSE in the event log).
- `event_for_message` / `messages_missing_events`: unchanged; the event log is
  the outbox for `message.added`.
- Publish `turn.summary` for every turn recovery closes.

### 4.8 Retention

- `prune_turns`: unchanged. Request and attempt rows cascade with the turn;
  `session_usage` does not reference turns and is kept until the session is
  deleted.
- `prune_events`: unchanged. With deltas gone it reclaims less often.
- `docs/daemon.md` retention section: describe what survives which pruning.

### 4.9 Backfill

Run once inside the migration that adds the columns (it runs only when the
columns are missing, which makes it once).

- For each request with `input_tokens is null`, its events are those after
  its `event_after` and up to the next request's `event_after` in the same
  session (or the turn's end). Read them with `store.events(session_id,
  after=..., types=["usage", "wire_request"])`.
- Exactly one `usage` event in the range: fill the request's usage and its
  last attempt's usage. Zero or more than one: leave null.
- `wire_request.body.model`: fill `model` with the bare id (the provider part
  is not in the body); `effort` from the body's reasoning effort field. Leave
  null when absent.
- `session_usage`: seeded from the backfilled requests, `since` = the oldest
  backfilled request's start.
- Sessions whose events expired stay empty. At most 14 days of history is
  recovered. If the matching proves fragile in testing, drop the backfill and
  ship with unknown values.

## 5. Clients

### 5.1 TUI

Types (`nexus-tui/src/api.rs`): `TurnSummary`, `RequestSummary`,
`AttemptSummary`, `ToolSummary`, `LivePartial`, `UsageLedger`,
`TurnPage { turns, more_before, floor, usage }`. `CoreApi::turns(sid, after,
before, limit)`. `CLIENT_API` goes to 1.2; the TUI requires Core 1.2 through
the existing `check_compat`.

`History` (`api.rs:330`): replace `events` and `events_gap` with `turns`,
`floor`, `usage`, `more_before`.

`load_history` (`sync.rs:549`): fetch messages, `snapshot_json` and `/turn`
concurrently. Delete the event page loop, `fold_event_pages`,
`HISTORY_EVENT_CAP`, `page_last_id`.

Live state (`reduce.rs`): the selected view keeps the summaries and a scratch
list of the running turn's events only. On `turn.summary`, replace or insert
the summary and clear the scratch. On reconnect (`bus.overflow`, a dropped
stream), fetch `/turn?after=<newest known>`; on a cursor below the retention
floor, reload history.

Deltas: fold into the scratch part list by `request_id`, `part` and `seq`
(seeded from `partial`); never keep raw delta events.

Each event scan moves:

| What | Where today | Reads instead |
| --- | --- | --- |
| Turn footer (tokens, cache %, steps, stop, models) | `timeline.rs:1021` `turn_meta` | the turn's requests: summed usage, `steps`, `stop`, distinct `model` |
| Tool timing and error flag | `timeline.rs:538` `traces` | `tools[]`; scratch for the running turn |
| Retry and error rows | `timeline.rs:241` | `requests[].attempts`, `turn.error` |
| "older diagnostics" row | `timeline.rs:608` `events_gap` | removed; a row at `floor.seq` says older turns have no details |
| Composer context % | `render.rs:4318` `composer_usage` | newest request with usage: `usage.input` |
| Header model label | `render.rs:4554` (`wire_request.body.model`) | newest request's `model` |
| Sidebar stats and cost | `stats.rs:81` `compute` (scans `usage` and `wire_request`) | the `usage` ledger; label totals "since <date>" when `since` is later than the session's creation |
| "Thought for 2.1s" | delta timestamps | `requests[].reasoning_seconds`; live from the scratch timer |
| "enter continue" hint | scan for `turn_end` | the newest turn's `status` and `ended` |
| Notifications (`app.rs:948`, `app.rs:995`) | live `turn_end` | unchanged (live events) |

Unknown usage renders as "unknown", never zero.

Tests (`nexus-tui/tests/timeline.rs`, `behaviour.rs`, `shell.rs`): fixtures
that seed events get summaries instead; new cases for the floor row, unknown
usage, joining a running turn from `partial`, and a `turn.summary` replacing
the scratch.

### 5.2 NSH

- Find the event-history loader that fills `model.selected.events` and
  replace it with `/turn` (same shapes as the TUI, typed in `src/store.ts`).
- `store.ts:262` (newest `session.directory_changed`): use the session view's
  `directory_changed` flag (`runtime.py:303`) and `session.directory`; add a
  field only if those don't cover it.
- Same live rules as the TUI: deltas folded, `turn.summary` replaces scratch.
- Tests: `App.test.tsx`, `i6.test.tsx`.

## 6. Old Nexus and pi-durable

### Old Nexus (`~/repos/nexus`, read 2026-10-28)

Old Nexus never sends raw events to the terminal client. Events and messages
share the `records` table; "Events are the spine; views are pure reductions"
(`docs/decisions.md`). The host folds the log into a `ConversationView`
(`view/reduce.py`) whose `TurnView` carries usage, models, retries and times;
`facade.state()` replays a session once and caches the last 8 views; a
derived snapshot is written every 20 turns. Nothing is pruned.

| | Old Nexus | nexus10 today | This plan |
| --- | --- | --- | --- |
| Source of truth | The event log | State tables, with clients reading events | State tables only |
| Where events are folded | Python host, once, cached | Rust TUI, every frame | Not folded; Core stores the result |
| What the client receives | Folded view | Raw events, up to 20 000 | Turn summaries and a ledger |
| Streaming for a late joiner | Replay | Replay saved deltas | Live partial, then live deltas |
| Event retention | None | 14 days or 256 MiB | Same; deltas not saved |
| Old turn footers | Always available | Vanish when events expire | Kept as long as the turn row |
| Session totals | From the log | From retained events | Ledger, survives pruning |

Taken: the client reads a derived per-turn view, never events. Different: the
derived data is stored when it happens, so events are disposable.

Alternative considered: fold at read time in Core with a cache of recent
sessions. No backfill, but old turns lose their footer once retention prunes
events. Rejected.

### pi-durable (`earendil-works/pi`, `packages/durable`, read 2026-10-29)

pi-durable commits everything a UI shows as state before showing it; clients
watch a view, not a log.

- **Spend ledger** (`pi.usage`): per conversation, totals per
  `provider/model`, updated in the commit that records the response, failed
  and aborted attempts included. Taken: `session_usage`, per-attempt usage.
- **Live document** (`pi.live`): the in-flight partial answer, tool output,
  retry backoff and why the last run failed. The partial is committed at most
  every 100 ms and persisted at most every 1 s; a late joiner reads it and
  follows live, "nothing is replayed". Its `PartialChanges` appends new text
  instead of rewriting strings. Taken: 4.4.
- **Windowed transcript**: a watch starts with the newest 50 entries, caps at
  100, with "load earlier" and gap markers. Out of scope here; see 9.

## 7. Decisions and ledger rows

Decisions:

- **Source of truth:** the state tables. Nothing a client shows depends on the
  event log.
- **Usage:** columns on `turn_request` and `turn_attempt`; every attempt
  counts; a `session_usage` ledger survives turn pruning.
- **Model:** the `provider/model` ref and effort, written at request start.
  Never the request body.
- **Thinking time:** `reasoning_seconds` per request.
- **Running response:** a `LivePartial` served from memory and persisted
  every second; deltas are ephemeral. The partial is diagnostic, never shown
  as an assistant message.
- **Summary delivery:** `GET /api/session/{id}/turn` with paging, and a
  durable `turn.summary` event after every turn closes. Not in the snapshot.
- **Running-turn memory in clients:** deltas folded, no event cap.
- **Older sessions:** backfilled where unambiguous; unknown otherwise.
- **Compatibility:** additive API, `api_version` 1.2. Older clients still read
  events, but can no longer replay a running response from the log.

Ledger rows for PLAN.md:

| Before | Now | Why |
| --- | --- | --- |
| Clients page the event log on session open and show diagnostics from it | Clients read `/api/session/{id}/turn` and `turn.summary`; the event log is not loaded | Bounded storage and load time; details survive event retention |
| Footer, stats, context %, model and thinking time derived from events | Derived from `turn_request` columns and `session_usage` | Same; totals survive turn pruning |
| A retried attempt's usage only in the event log | Every attempt's usage stored and counted | A failed attempt is still billed |
| Recovery reads `turn_end` and counts deltas from the log | Recovery reads `turn.ended` and `turn_request.partial` | State tables are the source of truth |
| Every event saved, deltas included; a joining client replays deltas | Deltas ephemeral; the running response is a throttled partial; raw SSE kept as `wire_event` | Deltas duplicate the message and `wire_event` (after pi-durable's `pi.live`) |

## 8. Steps

Each step lands green (`uv run pytest -q`, `uv run ruff check nexus tests`,
`nexus check`) with its docs.

**Before step 0:** land or park the uncommitted TUI rework in `timeline.rs`
and `render.rs` and fix the failing tests, so measurements and step 4 start
from a green tree.

0. **Baseline.** Seed a session with 30 000 events (a script under
   `nexus-core/tests/fixtures/` or `scripts/`), open it in a release TUI build,
   and record: time from open to first full frame, RSS after load, mean and
   p95 frame time while scrolling, and the session's event-log bytes. Record
   the numbers and the command in PLAN.md.
   *Done when* the numbers are in PLAN.md.

1. **Core store and runtime: usage, model, ledger.** Schema (4.1, without
   `partial`), `start_request` with model and effort, `record_usage`, the
   `Usage` branch, `reasoning_seconds` measured from deltas, `session_usage`,
   `mark_turn_ended`, backfill (4.9).
   Tests (new `tests/test_turn_summary.py`): usage saved for a completed,
   failed and interrupted request; a retried request sums its attempts and the
   ledger counts both; a model change between steps is recorded per request; a
   request that fails before output has its model; unknown usage stays null;
   the ledger survives `prune_turns`; migration and backfill are idempotent;
   an ambiguous backfill range stays null.
   Docs: `docs/daemon.md` (tables), `docs/loop.md` if the loop changes.
   *Done when* a real session (`nexus chat`) shows usage and model on every
   `turn_request` row.

2. **Core API.** `turn_summaries`, `GET /api/session/{id}/turn` with `after`,
   `before`, `limit`, `floor`, `usage`; `turn.summary` after every
   `finish_turn` and recovery close; `API_VERSION` 1.2.
   Tests (`tests/test_server.py`, `tests/test_recovery.py`): shape and
   ordering; paging both ways; a 500-turn session returns 50 by default and
   pages to the end; `floor` after pruning; 404 and 400 cases; `turn.summary`
   equals the `/turn` entry for completed, failed, interrupted and recovered
   turns; three queries per page, not per turn.
   Docs: `docs/api.md`, `docs/stream-events.md`, `docs/daemon.md`.
   *Done when* `curl` on a real session returns the documented shape.

3. **TUI data.** Types and `CoreApi::turns` in `api.rs`; `load_history`
   fetches `/turn`; `turn.summary` handling in `reduce.rs`; `CLIENT_API` 1.2.
   Keep the event path in parallel so the two can be compared on the same
   session.
   *Done when* a debug view or test shows summaries and event-derived values
   agree for a real session.

4. **TUI views.** Every row of the table in 5.1. Remove event paging,
   `HISTORY_EVENT_CAP`, `fold_event_pages`, `events_gap` and its row; add the
   floor row. Update the tests listed in 5.1.
   Docs: `nexus-tui/README.md`.
   *Done when* `grep -n '"usage"\|wire_request\|events_gap' nexus-tui/src`
   finds nothing outside live handling, and all TUI tests pass.

5. **Measure.** Repeat step 0 on the same seeded session. Record in PLAN.md
   next to the baseline.
   *Done when* both sets of numbers are in PLAN.md.

6. **NSH.** 5.2.
   Docs: `nexus-nsh/README.md`.
   *Done when* NSH loads no event history and its tests pass.

7. **Docs and ledger.** Ledger rows (7) and the step entry in PLAN.md;
   `docs/development.md` where it mentions event history;
   `interface/design-language.md` if the floor row is a new pattern.

8. **Recovery off `turn_end`.** 4.7: `turn.ended` with the log fallback for
   turns open before the migration.
   Tests (`tests/test_recovery.py`): a crash after `TurnEnd` and before
   `finish_turn` recovers as completed with the event log emptied; an old open
   turn still recovers through the fallback.
   *Done when* recovery tests pass with `event` rows of the turn deleted
   (except `message.added`).

9. **Live partial; stop saving deltas.** `nexus/live.py`, the runtime fold and
   throttled `save_partial`, `partial` on the running request in `/turn`,
   `Bus.publish(durable=False)` and the SSE writer (4.5), `partial_summary`
   in recovery, delta fields `request_id`, `part`, `seq`; TUI and NSH seed
   from `partial`.
   Tests: folding rules (interleaved reasoning and text, two tool calls,
   `PartDone` replacing folded text); a client opening mid-stream sees the
   text so far with no duplicated or missing text, across part start, part
   end, retry and message saved; a crash mid-stream leaves a stored partial at
   most a second old; the saved message clears the partial; an interrupted
   request keeps it and recovery's counts match it; no delta rows in `event`;
   `Last-Event-ID` never points at an ephemeral event; `bus.overflow` reports
   the last durable id.
   Docs: `docs/stream-events.md`, `docs/api.md`, `docs/inspector.md` (streaming
   detail now from `wire_event`), `docs/daemon.md`.
   *Done when* a second TUI opened during a long response shows the same text
   as the first, and the session's event log holds no delta rows.

## 9. Risks and out of scope

Risks:

- **Backfill mismatch** would show a wrong token count on an old turn. Leave
  it empty rather than guess; drop the backfill if tests find it fragile.
- **Pruned turns** have no summary. The floor row announces it, as messages
  announce clipping.
- **Unknown usage** (crash before the provider reported it) shows as unknown,
  never zero.
- **Partial/stream hand-off:** a wrong `seq` shows text twice or skips some.
  Covered by step 9's boundary tests.
- **Partial write cost:** rewriting a growing JSON blob every second on a long
  response. Measure in step 5 and after step 9; if it matters, store parts in
  their own rows and append to the last one (as pi-durable's `PartialChanges`
  appends rather than reassigns).
- **Ephemeral events and cursors:** any code that assumes every delivered
  event has an id (SSE writer, `bus.overflow`, client cursor parsing) must be
  found in step 9.
- **Old clients** reconnecting mid-response lose the deltas they missed; the
  message arrives when the response finishes.
- **Showing a partial after interruption** must not look like model context;
  check the timeline treatment against the one-timeline rule.

Out of scope:

- A windowed message transcript (pi-durable's watch window). Opening a long
  session still loads every message.
- Server-side views with patches, as old Nexus did.
- Changing wire-event retention.
