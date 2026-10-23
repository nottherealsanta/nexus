# Manager session

Plan: [`plans/in-progress/02-manager-session-orchestration.md`](../plans/in-progress/02-manager-session-orchestration.md).
**Status: implemented and verified offline; off by default (`[manager] enabled = false`).**
The backend, the native presentation (`nexus/ui_support/manager.py`, `nexus/ui/ratatui/manager.py`,
the Rust TUI and desktop panels) and the manager PTY flow pass offline; `cargo test` passes for
`rust/tui` and `rust/desktop`. Nothing has been soaked against the real Codex or GPT Live
services. With the flag on, a client can
`ManagerOpen`, talk to the manager like any session, and connect GPT Live to it; nothing
else in Nexus changes when it is off.

The manager is one durable session per workspace that sits above the user's root
sessions. The human talks to it in plain language ("start a session to fix the flaky sync
test", "what finished in the last hour?"). It can only list sessions, launch a root with
an instruction the human gave, wait for roots, read a finished root's bounded report,
cancel a root it launched, relay a message to a root, and ask the native client to switch to
a root. It has no file, shell, web, MCP, skill, extension or subagent
tool and no hook runs in it.

## What exists

| Piece | File | Contract |
| --- | --- | --- |
| Config | `nexus/config/schema.py` (`ManagerSection`) | `[manager]` `enabled = false`, `max_launches_per_turn = 3`, `max_live_managed = 8`, `auto_compaction = true`, `idle_compaction_minutes = 55`, `state_snapshot = true` |
| Compaction and memory | `nexus/session/manager_compaction.py`, `nexus/context/manager_memory.py` | Lazy idle compaction at turn admission; deterministic machine memory in `kv`; see [below](#automatic-compaction-and-machine-memory) |
| State snapshot | `nexus/host_support/manager_state.py`, `HostManagerPort.state_snapshot` | Per-request ephemeral `## Nexus State` block; see [below](#nexus-state-snapshot) |
| Port protocol | `nexus/tools/manager_port.py` | `ManagerPort`: `find`, `status`, `result`, `launch`, `wait`, `cancel`, `send`, `show`; frozen bounded value types; `LaunchRefused` carries a reason code |
| Tools | `nexus/tools/manager_tools.py` | The eight `ManagerToolDef`s and `build_manager_catalog(port)`, the manager's whole catalog |
| Host port | `nexus/host_support/manager_port.py` | `HostManagerPort`: visibility, provenance, caps, `session.linked`, wait, cancel, send, show (`manager.show`); `FacadeManagerHost` starts, cancels and messages roots through `HostFacade` and reads `focused_clients` |
| Client | `nexus/ui/ratatui/manager.py`, `manager_follow.py` | `Manager.on_event` turns a fresh `manager.show` into a switch (terminal and desktop share it; see [Show](#show-a-session-on-the-users-screen)) |
| Facade | `nexus/host/facade.py` | `open_manager`, `manager_state`, the `manager_port_factory` injection, reserved id, refusals |
| Wire | `nexus/host/protocol.py`, `nexus/client/protocol.py` | `ManagerOpen`/`ManagerOpenResult`, `ManagerState`/`ManagerStateResult`, `LiveConsent.scope` |
| Storage | `nexus/session/db.py`, `manager.py`, `session.py` | `kind = "manager"` row (no schema change), `Session.kind`, list exclusion, archive/delete/fork refusal, no hooks |
| Runtime | `nexus/runtime.py`, `nexus/agents/manager_prompt.py` | Structural eight-tool turn, pinned model, packaged prompt (see [tools.md](tools.md), [models.md](models.md), [agents.md](agents.md)) |
| Live | `nexus/host_support/live.py`, `live_consent.py` | `LiveStart` on the manager needs it enabled and the cross-session consent |
| Tests | `tests/test_manager_*.py`, `tests/manager_helpers.py` | catalog, pinned model, read side, host/storage/Live/events, launch/wait/cancel, send, show (`test_manager_show.py`) |

## The session

* Kind `manager` in the **main** namespace, fixed id `nexus-manager`. The kind is written
  with the row when the first input is accepted (`SessionManager.open(..., ownership={"kind":
  "manager"})`), so it needs no schema change and `LiveStart`/`SessionEnqueue`/`SessionSubscribe`
  reach it like any session. `Session.kind` reads it once and caches it.
* `ManagerOpen` creates it or returns it; it is idempotent and a singleton
  (`SessionManager.manager_summary` finds the draft or the row). It is refused with
  `[manager] enabled = false`. The id is reserved: `SessionOpen`/`SessionStart`/`SessionFork` cannot
  create a regular session under it, and an existing regular session with that id blocks `ManagerOpen`
  with a clear error.
* With the flag off, **every** operation that opens an existing manager session
  (`SessionState`, `SessionEnqueue`, `Live*`, …) fails with "The manager is turned off";
  rollback is the flag, and the log stays as an ordinary row.
* `ManagerState` never fails for "off": it reports `enabled`, the session id and state, the
  pinned model (`codex/gpt-6-luna`), whether Codex (ChatGPT) is signed in for it (`signed_in`, and the exact
  fix, `sign in to ChatGPT (Codex) under Settings > Providers`, when not), whether Codex is signed in for Live
  (`codex_signed_in`; the same provider, so the two now agree), the two GPT Live consents, `live_ready`, the managed root ids and the caps. A client uses it to say
  what is missing instead of failing a turn.
* The manager is excluded by kind from `SessionManager.list`, the project index and archive lists, and is
  never an auto-archive candidate. `archive`, `delete` and `fork` refuse it; so do `SessionShell`,
  `ModelSelect`, `ReasoningEffortSelect`, `AgentSelect`/`AgentReset`, `SessionHandoff` and the skill/MCP
  context selectors (the runtime refuses model and role changes as well).
* No hook runs (`Session._hook_runner` returns `None`: no `SessionStart`, `UserPromptSubmit` or
  `SessionEnd`), and no automatic title or summary side call is made (`AutoTitler.candidate` returns
  `None`; the context has no summarizer). The title stays the derived first message.

## Native presentation: a view, not a tab

The native clients (Ratatui, desktop) never list the manager in the tab strip. A top-bar button immediately
before the Sessions button, and `Ctrl+X A`, toggle it: the first press opens the manager view (`ManagerOpen`,
nothing new on the wire) and remembers the session in front; the second restores that session. The button reads
pressed while the manager is in front. Closing is only a client switch: the singleton is never archived, deleted
or cancelled, and a Live call (always attached to the manager) is unaffected. Details:
[ratatui-parity.md](ratatui-parity.md#manager-entry-plan-02-native-ui), [desktop.md](desktop.md). Web is unchanged.

## Port, provenance and caps

The runtime builds the manager turn's catalog from `Runtime.manager_port_factory(session)`;
`HostFacade` fills that in with a `HostManagerPort` bound to the manager `Session` and a
`FacadeManagerHost`. A runtime that already has a factory keeps it.

| Call | Rule |
| --- | --- |
| `find(scope, query, limit)` | Visible roots only (below); `recent` = live or active within 2 h; limit ≤ 50 |
| `status(ids)` | 1–20 visible ids; any invisible id fails the call (one message, no probing) |
| `result(id)` | Finished turn needed; id must be **managed** or occur as a whole token in the user's latest message; redacted, ≤ 16 KiB, clipping announced; records `manager.result_read` |
| `launch(instruction, origin_message_id, agent=None, title=None, worktree=False)` | See below |
| `wait(ids, until, max_wait_seconds)` | `until` any/all, ≤ 600 s; returns `completed`, `running`, `interrupted`, `waited_seconds`; records `manager.wait` |
| `cancel(id, reason)` | Only a session this manager launched; records `manager.cancelled` |
| `show(id, origin_message_id)` | Backend of `session_show`: a visible root of this workspace (no managed link needed); same current-turn real-user provenance as `send`; ≤ 3 per turn; no approval prompt. Records `manager.show` (below) and returns `requested` or `no_viewer`, never "shown". Grants no read and no cancel |
| `send(id, message, origin_message_id)` | Backend of `session_send` (plan 02 §10.5): a visible root that is live or active within 2 h; same current-turn real-user provenance as `launch` (a Live delegation counts by its request text, never `Voice:` lines); message ≤ 16 KiB, ≤ 3 sends per turn, ≤ 6 per 60 s; no approval prompt, attended or not. Writes `session.linked` (`kind: "routed"`) first, then queues the message as a user message with `origin = {kind: "manager", manager, origin_message_id}`. A routed link is not a managed link: it grants no `result` read and no `cancel` |

**Visible** = main namespace, `kind == "root"`, not archived, not trashed, not the manager itself, with
real user input. Everything else raises the same `SessionNotVisible`.

**Launch.** Refused (`manager.launch_refused` with a reason code, never the instruction) unless:

1. (no approval step: the runtime's manager permission engine allows the whole manager catalog by name, so a
   launch, cancel, send or show runs with or without a viewer attached; the checks below are the guard);
2. the current turn contains a **real user prompt**, found in the manager's own log by turn id (a tool reply
   or the assistant's own record is not one), and `origin_message_id` names one of them (`no_user_message`).
   Message ids are `m<seq>`; the refusal text names the latest valid id, so a model that guessed wrongly
   corrects itself on the next call;
3. fewer than `max_launches_per_turn` launches (counted from `session.linked` records of this turn) and
   fewer than `max_live_managed` managed roots still running or waiting (`cap`);
4. the optional `agent` is a root role of this workspace (`bad_agent`).

**Task, not launch framing.** The manager owns `session_launch`, so the packaged prompt tells it to launch
itself and pass the child only the underlying task ("Can you launch a new session to find out how /restart
works?" becomes "Find out how /restart works."). Only wording addressed to the manager is dropped; scope,
constraints and agent requests stay as written. Otherwise the child would read "launch a session" as its own
task and could launch another through any enabled session tool. This is a prompt contract, not text
preprocessing, and it changes no relay or authorization rule; tests pin the wording, not model obedience.

Then, in this order: a `session.linked` record (`kind: "managed"`, `child`, `turn`, `origin_message_id`,
`instruction_digest` = 16 hex of SHA-256, `agent`) is written to the **manager log**, a `session.opened`
(`launched_by: {manager, origin_message_id}`) goes into the new root, and the instruction is enqueued as its
first user message with `origin = {kind: "manager", manager, origin_message_id}`. Link first, so a started
root can never lack its edge; a start failure is reported as `error` (redacted). The new root is a normal
root: the workspace default model (**no model override exists**), the normal root permissions and rules,
`SessionEnqueue`-path scheduling under the supervisor cap, automatic titling (`title` is only a hint for the
title call; `title_source` stays `first_message`). The manager's own tool set and grants are never inherited.

**Wait** polls the targets' durable state, so it also sees roots driven from another client. It is
**interrupted** when the user sends the manager new input after the wait began (a queued id that was not
pending at the start; input already pending then does not re-interrupt). Cancelling the turn abandons the
wait and never cancels a target. While it waits, the manager's turn **lends its supervisor slot**
(`Supervisor.lent_slot`), so a launched root can start even at `max_concurrent_turns = 1`. When the wait
ends the manager takes its slot back without queueing: it is never stuck behind roots that may need the
user, so the cap can be exceeded by that one turn until another turn ends, and no new turn starts while
the slots in use are at or over the cap. The default cap is 4.

**Cancel** only reaches managed ids and asks the supervisor to stop the root and drop its queue; no approval
prompt, attended or not.

## Show a session on the user's screen

`session_show(id, origin_message_id)` cannot move anything itself: the host has no handle on a client's screen.
It writes one durable `manager.show` record in the manager log and a native client may act on it.

* **Record**: `target`, `origin_message_id`, `turn`, `clients` (= `focused_clients(manager)`: the client ids that
  reported the manager in the foreground in the last five minutes, via `SessionFocus`), `live` (the origin message
  was a Live delegation) and `expires_at` (now + 30 s). No user text.
* **Result**: `requested` when `clients` is non-empty or the request is from Live, `no_viewer` otherwise. The record
  is written either way; the tool and the prompt never claim the session is on screen. `focused_clients` is a hint
  that is not exposed to the model.
* **Client** (`Manager.on_event`, called from `prototype.update` before ingest and from `ManagerFollow` for every
  event of the manager log). It switches only when all hold: not a replay (the event is past the reduced cursor, and
  for a Live follow newer than the follow's start; bootstrap and reconnect replays never reach `update`), within
  30 s of its timestamp and before `expires_at`, not acted on before (bounded memory of 64), and it targets this
  client: its id is in `clients` **and** the manager is still the session in front, or `live` is set and this
  client's own Live call follows that manager. Other clients, and clients elsewhere, ignore it. A modal, form,
  dashboard, voice recording or pending approval/question is never replaced: a toast says the Manager asked.
  The switch (`switch_project`) runs in its own retained task, because a switch cancels the stream task that
  delivered the event, and then resumes the stream; draft and tab behaviour are the ordinary `switch`.
* **Launch autofocus**: a fresh `session.linked` (`kind: "managed"`) in the manager log is handled by the same
  `Manager.on_event` path with the same guards (not a replay, within 30 s, not acted on, manager in front or
  followed by this client's Live call, nothing modal replaces) and switches to its `child`. It carries no
  `clients`/`expires_at`, so the "manager in front on this client" check is the targeting rule. The link is written
  before the child starts, so the child's first events arrive over the ordinary stream.
* **Focus ordering**: `switch` reports the foreground session before replaying it, so the host knows the client is
  on the manager before the user can type. A client without a claim (old host, expired claim) gets `no_viewer`;
  there is deliberately no wider fallback.
* The desktop and web clients get no new behaviour here. The debug MCP lists the tool and always refuses it.

## Visibility is not authorisation to read

`status` and `find` give metadata the dashboard already shows. `result` gives a report, so it needs a
reason: the manager launched the session, or the human named it in their **latest message**. Both are
derived from the manager's log, never from model-supplied text.

**GPT Live provenance.** A Live delegation arrives as a user message whose `input.queued` origin is
`{kind: "live"}` and whose text is `Voice request`, the delegated request, a transcript of **both** speakers
(`You:` / `Voice:`) and a speech note. Only the **delegated request** (or, when the request was taken from the
transcript, the `You:` lines) counts as something the user asked for. `Voice:` lines are model-authored and
never authorise a result read; a message that merely looks like a delegation is reduced the same way
(`delegated_request`, `scan_manager_log` in `host_support/manager_port.py`). A delegation is a real user
message for launching, under the same checks (and, like every manager action, no approval prompt).

## GPT Live

Connect with `ManagerOpen`, then `LiveStart(session = manager id)`; the delegation and final-answer bridge is
unchanged ([live.md](live.md)). The host adds, with no UI:

* `LiveStart` on a manager session fails with `manager_disabled` when the flag is off and with
  `live_consent_required` until the **cross-session consent** (`LiveConsent(accept=True, scope="manager")`)
  is recorded: the call then carries what the manager reads of other sessions to OpenAI, which the base notice
  does not cover. It is versioned on its own (`MANAGER_NOTICE_VERSION`), so existing base consent stays valid for
  ordinary calls; recording it records the base too, revoking it keeps the base, revoking the base removes both.
* The voice persona is named "Manager".

One sign-in covers both: Codex (ChatGPT) runs the manager model and GPT Live. GitHub Copilot is **not**
required and is not a fallback; a Copilot-only setup fails the manager turn with `ManagerModelUnavailable`
naming `codex/gpt-6-luna`. `ManagerState` reports `signed_in` and `codex_signed_in` separately for clients.

### Migrating from the Copilot pin

Earlier builds pinned the manager to `github-copilot/gpt-6-luna`. The route is never stored in the manager
session: every turn is stamped from the packaged constants (`manager_prompt.py`), so the existing singleton
`nexus-manager` session and its log need no migration and keep their history. A **running daemon keeps the old
constants** until it is restarted; restart it (and any client that caches `ManagerState`) to move the manager to
Codex. Until then it still talks to Copilot.

## Events

| Event | Log | Data |
| --- | --- | --- |
| `session.linked` | manager | `kind: "managed"`, `child`, `turn`, `origin_message_id`, `instruction_digest`, `agent` |
| `session.opened` | launched root | `id`, `launched_by: {manager, origin_message_id}` |
| `manager.launch_refused` | manager | `reason` (`no_user_message`, `denied`, `cap`, `bad_agent`, `error`), `turn`. A cancel of a session that is not managed is refused to the model (`not_managed`) with no event |
| `manager.result_read` | manager | `session`, `bytes`, `clipped`, `turn` |
| `manager.wait` | manager | `ids`, `outcome` (`completed`, `timeout`, `interrupted`), `waited_seconds`, `turn` |
| `manager.cancelled` | manager | `session`, `cancelled`, `turn` |
| `session.linked` (send) | manager | `kind: "routed"`, `target`, `turn`, `origin_message_id`, `payload_digest`, `bytes`, `at` |
| `manager.routed` | manager | `target`, `origin_message_id`, `payload_digest`, `bytes`, `turn` |
| `manager.send_refused` | manager | `reason` (`no_user_message`, `denied`, `cap`, `rate`, `not_sendable`, `error`), `target`, `turn` |
| `manager.show` | manager | `target`, `origin_message_id`, `turn`, `clients`, `live`, `expires_at` |
| `manager.show_refused` | manager | `reason` (`no_user_message`, `cap`, `error`), `target`, `turn` |
| `manager.memory_updated` | manager | `chars`, `digest` (16 hex of SHA-256), `from_seq`, `through_seq` (the retired range), `reason: "idle_timeout"`. Written after the memory note and the handoff are durable |

They carry ids, counts and reason codes only. The view reducer folds them as session metadata (no
diagnostics) and labels a manager-launched root's first message with `origin.kind = "manager"`
([events-and-view.md](events-and-view.md)); `LogsRead` projects each with a fixed summary.

## Automatic compaction and machine memory

Plan: [`plans/planning/manager-automatic-compaction.md`](../plans/planning/manager-automatic-compaction.md).
The manager is one log for the life of the workspace, so it compacts itself, lazily and durably, with no
timer and no model-callable tool (a `<session_result>` could otherwise talk it into erasing its context).

* **Trigger.** At turn admission (`Session.start_turn` / `send`, before the input is resolved and before
  any request is stamped) `nexus/session/manager_compaction.py` compares now with the newest *activity*
  record of the manager's own log: a message, a summary, or a `turn.*`, `manager.*` (except
  `manager.memory_updated`), `session.linked` or `context.handoff` event. Presence, `input.*` and the audit
  record do not count, or opening the manager screen and queueing the very message being admitted would reset
  the clock. A gap of `[manager] idle_compaction_minutes` (default 55, 5-1440) with retirable context
  triggers it. The clock is in SQLite, so a daemon restart needs no state; a busy session never compacts.
  Config: `auto_compaction` (default true), `idle_compaction_minutes`; both off-switches leave the log
  untouched.
* **Order (fail-safe).** 1. `extract_manager_memory` folds the log into a note. 2. The note is written to the
  `kv` table (`manager_machine_memory`, `main` namespace) **first**; if that fails nothing is retired.
  3. An ordinary pair-safe same-session handoff (`reason = "idle_timeout"`, brief from `build_manager_brief`)
  retires everything before the incoming turn; a refused handoff (for example an unresolved tool call) keeps
  the note and the context and the next admission retries. 4. `manager.memory_updated` is recorded.
  Any exception is logged and the turn runs on the uncompacted context.
* **Memory** (`nexus/context/manager_memory.py`, deterministic, redacted, ≤ 4,000 chars) holds what the manager
  did and was told: launched root ids with role, launch message id and time, and an outcome only as *last
  observed* from `manager.wait` / `manager.result_read`; relay counts per target; refusal counts by reason;
  up to 8 directives (sentences from real user prompts with always/never/prefer/must/… wording; Live
  delegations are skipped because they carry model-written lines). Sources are structured events and real user
  prompts only: assistant prose is never read and titles are never stored. It never holds a session's current
  state, a count or an attention flag. When it must shrink it drops the oldest entries and says so. It lives
  in the `kv` table and is **never** `<workspace>/MEMORY.md`, which the manager does not read or write.
* **Injection.** The manager's system text is `MANAGER_PROMPT` (a system override bypasses the normal memory
  part), so `_ManagerAssembler._stamp` appends, in this order: the `## Manager Memory (machine notes)` block
  (changes only at compaction), the `Current user message id` line, then the `## Nexus State` block (changes
  every request): stable prefix first. The handoff brief is the first context message and carries history
  (`Earlier history: this session, seq 1..N`, the human's recent asks, past events phrased as past), plus
  one fixed line saying current state is supplied fresh in the `## Nexus State` block.
* Not verified against real Codex. No second model is ever called.

## Nexus State snapshot

Every manager model request ends with a compact, **ephemeral** `## Nexus State` block built by
`ManagerPort.state_snapshot()` (`HostManagerPort`, rendered by `host_support/manager_state.py`) from the same
visible roots the tools see, so it never names a session the tools would refuse. It is computed per request
(not per turn, so a `session_wait` return gets fresh data), lives only in that request's system text and is
never written to `kv`, the log, `manager.memory_updated`, a brief or a trace the product keeps; the extractor
never reads it. `manager.state_snapshot = false` is the kill switch; a port without the method yields no block
and a failing one yields `Nexus State: unavailable`, never a failed turn.

| Bucket | Derived from |
| --- | --- |
| `working` | the root's `state == "running"` (running children only add `+Nc`) |
| `review` | `awaiting_permission` or `awaiting_input` (a pending question or queued input): blocked on the human, the same meaning as `session_status.attention` |
| `done` | idle, a terminal outcome (`completed`/`failed`/`cancelled` shown as the tag) and activity inside the 2 h recent window |
| `idle` | idle with no outcome yet, or finished longer ago |
| `archived` | not computable (archived roots are invisible): rendered `archived n/a`, never listed |

The block has the project name and 8-char id, exact bucket counts, the attention count, at most 12 rows
(`[bucket] id "title" · tag · age`; review first, then working, done, idle) and a hard 1,800-character cap; a
review row is never dropped for a lower-priority one and the hidden remainder is counted
("N more not listed … use sessions_find"). Titles are sanitized, quote-free, ≤ 60 chars and labelled
untrusted. No message text, result or path appears. The block also tells the model it is true only at `as_of`
for this request and that it should call `session_status` before asserting a state after a wait or a gap.
Outcomes are read (`_tail`) only for idle roots in the recent window, at most 30 per request, so beyond that
a finished root counts as `idle`. **It grants no authority**: result, cancel, launch, send and show keep their
rules and do not read it. Not verified: cost of the changing tail on Codex prompt caching; whether
`MANAGER_NOTICE_VERSION` consent wording must mention it (GPT Live goes through the manager).

## Bounds

| Item | Bound |
| --- | --- |
| `sessions_find` | `scope` live/recent, `query` ≤ 256 chars literal case-insensitive over title and id, `limit` ≤ 50 (default 20) |
| Titles | control-free, credential-redacted, ≤ 160 chars |
| `session_status` / `session_wait` | 1–20 ids, de-duplicated |
| `session_wait` | ≤ 600 s per call |
| `session_launch` | instruction ≤ 16 KiB, no model argument; `worktree` boolean (default false); `title` ≤ 160 chars, or a worktree name (see below) when `worktree` is true |
| `session_result` | ≤ 16 KiB UTF-8 after redaction, wrapped as `<session_result …>` data with the closing tag defanged |
| Launches | 3 per manager turn, 8 managed roots running at once (configurable) |
| `session_send` | message ≤ 16 KiB, 3 per turn, 6 per 60 s |
| `session_show` | 3 per turn; a request is actionable for 30 s |

The manager prompt tells the model to launch every session with `worktree=true` and a worktree name as `title`
(short, descriptive, e.g. `release-0-7-1`), unless the user explicitly asks for the main checkout; only then does it
pass `worktree=false` with a free-form title of a few words.

### Launching into a named worktree

`session_launch` takes an optional `worktree: boolean`. With `worktree=true`, `title` is required and is
the worktree name: 1-60 characters of letters, digits, `-` or `_`, starting with a letter or digit (no
spaces). The tool checks this before any side effect. The name is the launched session's title, its
worktree folder and the branch `nexus/session/<name>`. `HostManagerPort` makes the selection while the
session is still a draft: `ManagerHost.start_root(..., worktree_name=name)` opens the session, runs the
host's `set_session_worktree(name=...)` (the same path as `SessionWorktreeSet`; it refuses a taken branch
or folder and a non-Git workspace) and only then enqueues the instruction. The worktree itself is created
by the first model turn. The project's Ask mode does not block this unattended launch. If the selection is
refused, the tool returns an error with the redacted reason and the opened session's id; the instruction
was never enqueued. With `worktree` false or omitted, `title` stays a free-form hint. The schema default stays `false` (the tool
is also used by the debug MCP); the default-to-worktree rule lives in the manager prompt.

Prompt rules: the manager launches into a worktree by default so the user's main checkout is untouched. If the
refusal says "Worktrees need a Git repository" it launches again with `worktree=false` and tells the user the
session ran in the main checkout (the refused draft session stays opened but not started); for any other refusal
(name taken, ...) it retries once with another name or reports it.

The result adds `worktree_name` and `worktree_branch` when a worktree was requested. The manager never
merges the work: the user lands it with `/worktrees`.

## Debug MCP server (debug only)

`nexus/devtools/manager_debug_mcp.py` exposes the manager's eight tools (`sessions_find`,
`session_launch`, `session_status`, `session_wait`, `session_result`, `session_cancel`,
`session_send`, `session_show`) to an MCP client a developer uses to debug Nexus. **Debug only**: not used by
the product path, and every tool description starts with
`[DEBUG ONLY]`. It reuses the manager's own tool functions (validation, bounds, result
wrapping) against `DaemonManagerPort` (`devtools/debug_mcp_port.py`), which is a plain
host-protocol client (`SessionList`, `SessionState`, `SessionEnqueue`, `SessionCancel`,
`AgentSelect`, `ManagerState`). It needs `[manager] enabled` for nothing: there is no manager
session in this path.

| | |
| --- | --- |
| Transport | newline-delimited JSON-RPC on stdio; no listener. Requests ≤ 1 MB, ≤ 8 in flight, reply ≤ 64 KiB |
| Daemon | connects to the workspace's **running** daemon (`--workspace`, default cwd); never starts or restarts one. No daemon → an error reply per call |
| Run | `uv run python -m nexus.devtools.manager_debug_mcp`; `--list-tools` prints the names |
| Project setup | `.agents/mcp.json` entry `nexus-manager-debug`, shipped enabled like any other MCP and needs no environment variable (a session lists it under MCP search); turn it off in Settings > MCP or with `"enabled": false` |
| Other clients | e.g. `claude mcp add nexus-manager-debug -- uv run python -m nexus.devtools.manager_debug_mcp` from the repo |

Differences from the in-app manager, all because there is no manager log and no on-screen
approval here (the MCP client's tool approval is the human gate):

* `origin_message_id` is not a parameter (and is rejected if passed); the server fills in
  `debug-mcp`. Host commands carry no message origin, so launched roots and sent messages
  carry no `origin.kind = "manager"`; a `send` text starts with
  `[Sent through the Nexus debug MCP, not typed by the user]`, and launched ids start with `dbg-`.
* `session_launch`: 3 per minute, and refused when this server's launched roots still running
  reach the daemon's `max_live_managed`; `agent` is applied with `AgentSelect`; `title` is
  applied with `SessionRename` right after the enqueue, so it becomes a durable user title
  (differs from the manager, where it is only an auto-title hint); if the rename fails the
  session still runs and the reply's `title` is empty. `worktree=true` (title required, same name rule
  as above) sends `SessionWorktreeSet(enabled=True, name=title)` before `SessionEnqueue`; the host sets
  the title, so no `SessionRename` follows, and a refusal (taken name, not a Git repo) starts nothing.
  The id keeps its `dbg-` prefix; the name is the user-visible one. The new root is a
  normal root: default model, normal permissions.
* `session_send`: 6 per minute, 16 KiB, visible roots that are live or active within 2 h.
* `session_cancel`: only roots this server process launched (kept in memory, forgotten on restart).
* `session_result`: **any** visible finished root, not only launched or user-named ones,
  because the caller is the developer, not a model steered by session text. Still redacted,
  ≤ 16 KiB, wrapped as `<session_result …>` data.
* `session_wait` has no "user wrote again" interruption (`interrupted` is always false); end a wait early with MCP `notifications/cancelled`. Many
  clients time out requests sooner than the 600 s maximum, so prefer short waits.
* `session_show` is listed (the catalog is the manager's) but always refused as unsupported: there is no UI and no
  manager log to carry the request.
* No `manager.*` audit events are written (there is no manager log).

Tests: `tests/test_debug_mcp.py` (fake daemon client; no daemon or session is touched) plus one
real stdio subprocess test of the handshake, catalog and the no-daemon error reply.
Not verified against a live daemon or a real MCP client.

## Design decisions (settled)

- Kind `manager` in the main namespace (not a new namespace); managed links are `session.linked` records in the
  manager log (not owner columns on the child); no launch model override (the Live transcript contains
  model-authored lines, so "the human named the model" cannot be checked); launch, cancel, send and show need
  no approval and work unattended (provenance, ownership and caps are the guard); waits end on new user input; no auto-title or summary side call; the
  model is pinned with no fallback of any kind; the catalog is structural and bypasses MCP, skills and extensions at
  both runtime builders; the launched root keeps its normal permissions. See [decisions.md](decisions.md).

## Not done

- **Desktop visual pass not done**: the desktop manager card has Rust tests but no interactive or screenshot review.
- **PTY tests need the debug binary**: `tests/test_ratatui_pty*.py` read a fixed
  `rust/tui/target/debug/nexus-ratatui` (no `NEXUS_RATATUI_BIN` override exists) and are `skipif` when it is missing, so
  a green Python run without `cargo build --manifest-path rust/tui/Cargo.toml` skips them rather than passing them.
- **Python suite**: the parent reported 6861 passed, 336 skipped, 6 deselected after the backend fixes; the skips
  include those PTY tests unless the binary was built.
- **No real-provider or real-Live soak.** All tests are offline; the default stays off until a soak.
- The runtime appends one line, `Current user message id (pass as origin_message_id): m<seq>`, to the manager's
  system prompt on every request (the newest real user prompt in its log; the id only, no message text), so the
  first launch of a turn names a valid id. The port still validates it and its refusal names the latest valid id.
- A manager turn started with no port configured (`manager_port_factory` unset or returning `None`) fails closed
  with a `ConfigError`; through the supervisor that is a persisted `turn.failed`, never a crash.
- The plan's open questions resolve as: U1 one manager per workspace, U2 no cross-workspace, U3 no approval
  step for launch, cancel, send or show (superseded from "unconditional confirmation"), U4 provider-default effort (effort selection is refused), U5
  `session_send` exists (see Port, provenance and caps; superseded from "no `session_send`"), U6 moot (no model override).
