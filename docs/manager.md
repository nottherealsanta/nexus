# Manager session

Plan: [`plans/planning/02-manager-session-orchestration.md`](../plans/planning/02-manager-session-orchestration.md).
**Status: implemented but not yet verified end to end; no completion is claimed. The
feature is off by default (`[manager] enabled = false`).** The backend has offline tests
and the native presentation (`nexus/ui_support/manager.py`, `nexus/ui/ratatui/manager.py`,
the Rust TUI and desktop panels) is implemented, but the full Python suite, the Rust
builds and the PTY tests have not been run green against this change, and nothing has
been soaked against the real Copilot or GPT Live services. With the flag on, a client can
`ManagerOpen`, talk to the manager like any session, and connect GPT Live to it; nothing
else in Nexus changes when it is off.

The manager is one durable session per workspace that sits above the user's root
sessions. The human talks to it in plain language ("start a session to fix the flaky sync
test", "what finished in the last hour?"). It can only list sessions, launch a root with
an instruction the human gave, wait for roots, read a finished root's bounded report, and
cancel a root it launched. It has no file, shell, web, MCP, skill, extension or subagent
tool and no hook runs in it.

## What exists

| Piece | File | Contract |
| --- | --- | --- |
| Config | `nexus/config/schema.py` (`ManagerSection`) | `[manager]` `enabled = false`, `max_launches_per_turn = 3`, `max_live_managed = 8` |
| Port protocol | `nexus/tools/manager_port.py` | `ManagerPort`: `find`, `status`, `result`, `launch`, `wait`, `cancel`; frozen bounded value types; `LaunchRefused` carries a reason code |
| Tools | `nexus/tools/manager_tools.py` | The six `ManagerToolDef`s and `build_manager_catalog(port)`, the manager's whole catalog |
| Host port | `nexus/host_support/manager_port.py` | `HostManagerPort`: visibility, provenance, caps, `session.linked`, wait, cancel; `FacadeManagerHost` starts and cancels roots through `HostFacade` |
| Facade | `nexus/host/facade.py` | `open_manager`, `manager_state`, the `manager_port_factory` injection, reserved id, refusals |
| Wire | `nexus/host/protocol.py`, `nexus/client/protocol.py` | `ManagerOpen`/`ManagerOpenResult`, `ManagerState`/`ManagerStateResult`, `LiveConsent.scope` |
| Storage | `nexus/session/db.py`, `manager.py`, `session.py` | `kind = "manager"` row (no schema change), `Session.kind`, list exclusion, archive/delete/fork refusal, no hooks |
| Runtime | `nexus/runtime.py`, `nexus/agents/manager_prompt.py` | Structural six-tool turn, pinned model, packaged prompt (see [tools.md](tools.md), [models.md](models.md), [agents.md](agents.md)) |
| Live | `nexus/host_support/live.py`, `live_consent.py` | `LiveStart` on the manager needs it enabled and the cross-session consent |
| Tests | `tests/test_manager_*.py`, `tests/manager_helpers.py` | catalog, pinned model, read side, host/storage/Live/events, launch/wait/cancel |

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
  pinned model (`github-copilot/gpt-6-luna`), whether GitHub Copilot is signed in (and the exact
  fix, `sign in to GitHub Copilot under Settings > Providers`, when not), whether Codex is signed in,
  the two GPT Live consents, `live_ready`, the managed root ids and the caps. A client uses it to say
  what is missing instead of failing a turn.
* The manager is excluded by kind from `SessionManager.list`, the project index and archive lists, and is
  never an auto-archive candidate. `archive`, `delete` and `fork` refuse it; so do `SessionShell`,
  `ModelSelect`, `ReasoningEffortSelect`, `AgentSelect`/`AgentReset`, `SessionHandoff` and the skill/MCP
  context selectors (the runtime refuses model and role changes as well).
* No hook runs (`Session._hook_runner` returns `None`: no `SessionStart`, `UserPromptSubmit` or
  `SessionEnd`), and no automatic title or summary side call is made (`AutoTitler.candidate` returns
  `None`; the context has no summarizer). The title stays the derived first message.

## Port, provenance and caps

The runtime builds the manager turn's catalog from `Runtime.manager_port_factory(session)`;
`HostFacade` fills that in with a `HostManagerPort` bound to the manager `Session` and a
`FacadeManagerHost`. A runtime that already has a factory keeps it.

| Call | Rule |
| --- | --- |
| `find(scope, query, limit)` | Visible roots only (below); `recent` = live or active within 2 h; limit ≤ 50 |
| `status(ids)` | 1–20 visible ids; any invisible id fails the call (one message, no probing) |
| `result(id)` | Finished turn needed; id must be **managed** or occur as a whole token in the user's latest message; redacted, ≤ 16 KiB, clipping announced; records `manager.result_read` |
| `launch(instruction, origin_message_id, agent=None, title=None)` | See below |
| `wait(ids, until, max_wait_seconds)` | `until` any/all, ≤ 600 s; returns `completed`, `running`, `interrupted`, `waited_seconds`; records `manager.wait` |
| `cancel(id, reason)` | Only a session this manager launched; records `manager.cancelled` |

**Visible** = main namespace, `kind == "root"`, not archived, not trashed, not the manager itself, with
real user input. Everything else raises the same `SessionNotVisible`.

**Launch.** Refused (`manager.launch_refused` with a reason code, never the instruction) unless:

1. the manager is **attended** (a viewer is attached). The runtime already asks that viewer to approve every
   launch, fresh each time, and denies when nobody watches; the port re-checks as a second line
   (`denied`);
2. the current turn contains a **real user prompt**, found in the manager's own log by turn id (a tool reply
   or the assistant's own record is not one), and `origin_message_id` names one of them (`no_user_message`).
   Message ids are `m<seq>`; the refusal text names the latest valid id, so a model that guessed wrongly
   corrects itself on the next call;
3. fewer than `max_launches_per_turn` launches (counted from `session.linked` records of this turn) and
   fewer than `max_live_managed` managed roots still running or waiting (`cap`);
4. the optional `agent` is a root role of this workspace (`bad_agent`).

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

**Cancel** only reaches managed ids and asks the supervisor to stop the root and drop its queue; the runtime
asks the human to approve each cancel.

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
message for launching, subject to the same on-screen approval.

## GPT Live

Connect with `ManagerOpen`, then `LiveStart(session = manager id)`; the delegation and final-answer bridge is
unchanged ([live.md](live.md)). The host adds, with no UI:

* `LiveStart` on a manager session fails with `manager_disabled` when the flag is off and with
  `live_consent_required` until the **cross-session consent** (`LiveConsent(accept=True, scope="manager")`)
  is recorded: the call then carries what the manager reads of other sessions to OpenAI, which the base notice
  does not cover. It is versioned on its own (`MANAGER_NOTICE_VERSION`), so existing base consent stays valid for
  ordinary calls; recording it records the base too, revoking it keeps the base, revoking the base removes both.
* The voice persona is named "Manager".

Both sign-ins are needed: Copilot for the manager model, Codex for Live. `ManagerState` reports each.

## Events

| Event | Log | Data |
| --- | --- | --- |
| `session.linked` | manager | `kind: "managed"`, `child`, `turn`, `origin_message_id`, `instruction_digest`, `agent` |
| `session.opened` | launched root | `id`, `launched_by: {manager, origin_message_id}` |
| `manager.launch_refused` | manager | `reason` (`no_user_message`, `denied`, `cap`, `bad_agent`, `error`), `turn`. A cancel of a session that is not managed is refused to the model (`not_managed`) with no event |
| `manager.result_read` | manager | `session`, `bytes`, `clipped`, `turn` |
| `manager.wait` | manager | `ids`, `outcome` (`completed`, `timeout`, `interrupted`), `waited_seconds`, `turn` |
| `manager.cancelled` | manager | `session`, `cancelled`, `turn` |

They carry ids, counts and reason codes only. The view reducer folds them as session metadata (no
diagnostics) and labels a manager-launched root's first message with `origin.kind = "manager"`
([events-and-view.md](events-and-view.md)); `LogsRead` projects each with a fixed summary.

## Bounds

| Item | Bound |
| --- | --- |
| `sessions_find` | `scope` live/recent, `query` ≤ 256 chars literal case-insensitive over title and id, `limit` ≤ 50 (default 20) |
| Titles | control-free, credential-redacted, ≤ 160 chars |
| `session_status` / `session_wait` | 1–20 ids, de-duplicated |
| `session_wait` | ≤ 600 s per call |
| `session_launch` | instruction ≤ 16 KiB, no model argument |
| `session_result` | ≤ 16 KiB UTF-8 after redaction, wrapped as `<session_result …>` data with the closing tag defanged |
| Launches | 3 per manager turn, 8 managed roots running at once (configurable) |

## Design decisions (settled)

- Kind `manager` in the main namespace (not a new namespace); managed links are `session.linked` records in the
  manager log (not owner columns on the child); no launch model override (the Live transcript contains
  model-authored lines, so "the human named the model" cannot be checked); launch and cancel confirmation is
  unconditional and unattended means deny; waits end on new user input; no auto-title or summary side call; the
  model is pinned with no fallback of any kind; the catalog is structural and bypasses MCP, skills and extensions at
  both runtime builders; on-screen approval stays mandatory. See [decisions.md](decisions.md).

## Not done

- **Native UI is implemented but unverified**: the pinned row, manager chat affordance and Live route exist in the
  Ratatui and desktop clients. The desktop `bridge::Prompt` test initializer was updated for the expanded shared
  `Prompt` (`title`, `since`, `what`, `why`, `what_rows`), but `cargo test` for `rust/desktop` and `rust/tui`, the PTY
  tests and a visual pass have **not** been run since: that change was made in an environment without a shell.
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
- The plan's open questions resolve as: U1 one manager per workspace, U2 no cross-workspace, U3 no pre-approval
  (confirmation is unconditional), U4 provider-default effort (effort selection is refused), U5 no `session_send`,
  U6 moot (no model override).
