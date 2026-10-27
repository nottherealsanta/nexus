# Plan 02: Manager session above roots

Status: **implemented and verified offline (2026-10-19); feature off by default; not soaked
against the real services.** The backend (M1-M9), the Ratatui pinned manager row and chat, the
desktop manager card and the `nexus manager` CLI are written and covered by the offline Python
suite, the Rust TUI/desktop tests and the manager PTY flow. What remains is live-only: a real
Codex/GPT Live run and the soak that flips the default (M10, §7 acceptance). Behavior is
documented in [docs/manager.md](../../docs/manager.md); where this plan and the code differ,
the code wins. The high advisor review changed several first-draft decisions; this text states
the settled ones (they are listed in §8).

> **Proposed rethink (not implemented, not verified):** §10 records a new intended design in which
> GPT Live talks only to the manager and the manager routes messages to other sessions. It reopens
> several contracts stated below (`session_send`, launch-only, exact-ID result visibility, per-session
> Live attach). Until §10 is accepted and built, the text above is the current behavior.

Phase 2 of 3. **Requires plan 01** milestones M2–M4 (logical sessions, supervision edges,
`SessionTree`). Plan 03 adds names the manager uses for addressing.

---

## 1. Executive intent

Add a **manager**: one durable session per workspace (see U1) that sits above the user's root
sessions. The human talks to it in plain language ("start a session to fix the flaky sync test",
"what finished in the last hour?"). It can only:

1. **discover** live and recent (last 2 h) sessions, bounded;
2. **launch** a root session with an instruction **from the human**;
3. **monitor** lifecycle and completion of the sessions it launched or the human points it at;
4. **bring completed results into its own context** (bounded, redacted final report, not the
   transcript).

It has **no read, write, bash, web, MCP, skill or subagent tools**, and no hook runs in it. It always
runs on the low-tier Codex (ChatGPT) model **`codex/gpt-6-luna`**, with **no silent fallback
or upgrade**: if that route cannot run, the manager fails visibly. (Decision, superseding the original
`github-copilot/gpt-6-luna` pin: Codex is the project default provider and the one GPT Live already needs,
so one sign-in covers both. GitHub Copilot is not required and is not a fallback. The route is stamped
from packaged constants each turn, so the existing `nexus-manager` session needs no data migration; a
running daemon keeps the old pin until restarted.)

### Non-goals

- The manager does not do the work, read files, or review diffs.
- It does not grant permissions. Roots it launches get the same authority as a root the human starts
  from the dashboard, never more.
- It does not see full child contexts (plan 03 adds a narrow, authorised context tool for roots; the
  manager does not get it by default).
- No cross-workspace management in this phase (U2).
- No web UI.

---

## 2. Current state (evidence)

- Model ID. `codex/gpt-6-luna` is the workspace default (`nexus.toml` `models.default`), is a `low`
  route in `DEFAULT_TIER_MODELS` (`nexus/model/tiers.py`) and resolves through the model registry
  (`tests/test_model_registry.py`). The plan originally pinned `github-copilot/gpt-6-luna`
  (first `low` route; Luna on Copilot uses `/responses`); that pin was replaced by Codex.
- Tiers are subagent-only; main models are concrete IDs. So the manager names the **concrete**
  `codex/gpt-6-luna`, not the tier `low`; "low tier" describes the cost class, not a router
  lookup. Resolving via tier would allow fallback to `github-copilot/gpt-6-luna` or DeepSeek, which the
  requirement forbids.
- Fallbacks are listed by `ModelRouter.fallbacks` (agent, tier, global `model.fallback`) and the loop
  decides when to use them. The manager disables all three.
- Roles: `orchestrator` root already exists and "never edits code itself" (`docs/agents.md`) but it
  still has tools; prompt-only rules are not enforcement. The manager needs a **structural** tool
  catalog, not a prompt.
- Host commands already cover list/start/enqueue/cancel/subscribe/state (`docs/host.md`), cross-project
  lists (`docs/sessions.md`), and `Supervisor` caps of 4 concurrent turns.
- Attention travels child → root → human (`docs/agents.md`).
- Deadlines: the dirty worktree makes root `max_turn_seconds` default 0 (unlimited) and removes
  subagent wall clocks (`docs/loop.md`). The manager's "wait" therefore cannot depend on a child
  timeout; it needs explicit, bounded waits (below).
- One-way layering: a UI uses only the host API. The manager is **not** a UI; it is a session whose
  tools call a narrow in-process facade port, never the DB.

---

## 3. Target contracts

### 3.1 Manager session

| Property | Value |
| --- | --- |
| Kind | `manager` (the existing `sessions.kind` column; no schema change) |
| Namespace | `main`, fixed id `nexus-manager`; excluded from lists, the project index and the stale-archive sweep **by kind**, so `LiveStart` and `SessionEnqueue` reach it like any session |
| Model | `codex/gpt-6-luna`, pinned; `fallback = []`; tier and global fallback disabled |
| Effort | provider default (U4); effort selection is refused |
| Tools | exactly the manager tool set below; `subagent`, `escalate`, all file/shell/web/MCP/skill tools absent from the catalog. Built structurally at **both** runtime builders, bypassing MCP, skills and extensions |
| Prompt | packaged constant (`agents/manager_prompt.py`), not a role file; `contexts: [manager]`, `bundles: [manager]` and `profile: manager` are rejected by the role grammar, so it cannot be selected as an ordinary root or subagent |
| Compaction | plan 01 handoff, deterministic brief only (no summary call, so no second model) |
| Hooks, titles | no hook of any kind (`SessionStart`, `UserPromptSubmit`, `SessionEnd`); no automatic title |
| Refused | archive, trash, fork, `!` shell, model selection, role selection, effort selection, manual handoff, skill/MCP selectors |

**No silent fallback.** The pinned policy is request metadata (`model_policy = "pinned"`):
`ModelRouter.fallbacks` returns `()`, `EndpointFallback` (protocol dialect retry, same model) **stays
allowed** because it does not change the model. If Codex is not signed in, rate-limited or the model
is missing from `/models`, the turn fails with `ManagerModelUnavailable` naming the exact model and the
sign-in command; the UI shows it. `ManagerState` reports the sign-in state ahead of time.

### 3.2 Manager tools (the whole catalog)

| Tool | Input | Output | Bounds |
| --- | --- | --- | --- |
| `sessions_find` | `scope` (`live`/`recent`), optional `query` (≤ 256 chars, literal substring), `limit` (≤ 50) | rows: id, title, state, kind, `order_time`, `completion_seq`, child counts | `recent` = live or active within **2 h** of now; never archived/trashed; never `agents` namespace rows |
| `session_launch` | `instruction` (≤ 16 KiB), `origin_message_id`, optional `agent` (root role), optional `title` | new id, title hint, state `queued` | see §3.3; **no model argument** |
| `session_status` | `ids` (≤ 20) | state, latest outcome, attention flags, `completion_seq` | only sessions visible via `sessions_find` |
| `session_wait` | `ids` (≤ 20), `until` (`any`/`all` complete), `max_wait_seconds` (≤ 600 per call) | completed, running, `interrupted`, `waited_seconds` | the tool returns; the manager re-calls. New user input ends the wait early; cancelling the turn abandons the wait and never cancels a target |
| `session_result` | `id` | final assistant message, status, redacted, ≤ 16 KiB, clipping announced | only terminal sessions; only `managed` links or sessions the human named in their latest message (§3.4) |
| `session_cancel` | `id`, `reason` | `cancelled: bool` | only sessions the manager launched (`managed` link); requires human confirmation (approval prompt) |

There is no `session_send` for arbitrary steering in v1 (U5). The manager's only way to give a session
work is `session_launch` with an instruction that came from the human. (Files-changed and resolved-model
fields on rows and results are not implemented.)

### 3.3 "Instructions from the human" and privilege rules

The risk: a model writing its own instructions, or relaying text from a completed session's result into
a new launch (prompt injection laundering).

Rules (enforced in the port and the runtime, not the prompt):

1. `session_launch` requires `origin_message_id` = the id (`m<seq>`) of a **real user prompt** in the
   manager's own log in the **current turn**; a tool reply or the assistant's own record is not one. The
   launch records it. A launch in a turn with no user message is refused. A GPT Live delegation is a real
   user message for this purpose. The refusal text names the latest valid id.
2. Launch and cancel are **permission-gated unconditionally**: the runtime's per-turn manager engine asks
   the human every time (the exact instruction and role are the key), ahead of any grant or rule; there is
   no `allow` rule or pre-approval (U3). With no viewer attached the unattended default (`deny`) applies,
   and the port also refuses an unattended launch (`denied`). Launch is capped at
   `[manager] max_launches_per_turn = 3` per turn and `max_live_managed = 8` running managed roots.
3. The launched root gets **the workspace's normal root permissions** (config + rules), exactly as
   `SessionStart` from the dashboard. The manager's own tool set and grants are **not** inherited and
   cannot be added. No `allow`-all flag exists on the tool.
4. **There is no model override.** A launched root uses the workspace default model. (A literal "the human
   named this model" check is unsound: the Live transcript carries model-authored `Voice:` lines.)
5. Results returned by `session_result` are wrapped as data (`<session_result id=...>…</session_result>`),
   like auto-title input, and are never auto-forwarded into a launch.

### 3.4 Visibility (what the manager may see)

- `sessions_find` sees root sessions **of this workspace** (U2). It sees metadata only (titles, state),
  which the human can already see on the dashboard.
- `session_result` requires a `managed` link (the manager launched it) **or** the human's latest message
  names the session by exact id. This prevents the manager from reading every session's output on its own
  initiative. For GPT Live only the **delegated request** counts; the `Voice:` lines of the transcript a
  delegation carries never do.
- No access to child sessions' results directly; a root's final report already includes its children's
  summaries.

### 3.5 Events and schema

No schema change. Records in the manager log are durable, turn-less events carrying ids, counts and reason
codes only:

| Event | Log | Data |
| --- | --- | --- |
| `session.linked` | manager | `kind: "managed"`, `child`, `turn`, `origin_message_id`, `instruction_digest` (16 hex of SHA-256), `agent` |
| `manager.launch_refused` | manager | `reason` (`no_user_message`, `denied`, `cap`, `bad_agent`, `error`), `turn` |
| `manager.result_read` | manager | `session`, `bytes`, `clipped` |
| `manager.wait` | manager | `ids`, `outcome` (`completed`, `timeout`, `interrupted`), `waited_seconds` |
| `manager.cancelled` | manager | `session`, `cancelled` |

The launched root's first records: `session.opened` with `launched_by: {manager, origin_message_id}`,
then the instruction as the first **user** message whose `input.queued` carries `origin = {kind:
"manager", manager, origin_message_id}` (the dashboard shows "launched by manager"). `title_source` stays
`first_message`, so auto-title still works (the manager's optional `title` is only a hint for it).

### 3.6 Host API

| Command | Purpose |
| --- | --- |
| `ManagerOpen()` | open or create the workspace's singleton manager session (idempotent; refused unless `[manager] enabled`) |
| `ManagerState()` | enabled, session id and state, pinned model, Codex sign-in (manager model and Live) with the exact fix, the two Live consents, managed roots, caps (never an error for "off") |
| `LiveConsent(scope="manager")` | the separate cross-session GPT Live notice |
| existing `SessionEnqueue`, `SessionSubscribe`, `SessionCancel`, `LiveStart` | talk to it like any session |

Facade port for tools: `ManagerPort` (L4 protocol in `nexus/tools/manager_port.py`, injected into the
runtime through `Runtime.manager_port_factory`) with `find`, `launch`, `status`, `wait`, `result`,
`cancel`. The tools depend on the protocol only, keeping one-way layering (tools never import `host/`).

### 3.7 Data flow

```
Human ──msg──▶ Manager session (gpt-6-luna, tools: find/launch/status/wait/result/cancel)
                  │ session_launch (approval) ─────▶ HostManagerPort ─▶ HostFacade.enqueue(new root)
                  │                                    └─ Supervisor (cap 4 turns)
                  │ session_wait ◀── durable state of the roots, polled; ends on new user input
                  └ session_result ◀── bounded final report (redacted)
Dashboard (Ratatui/desktop): Manager pinned on top ▸ managed roots ▸ their children
GPT Live ──LiveStart(manager id)──▶ the same session over the delegation bridge
```

---

## 4. Milestones

| # | Work package | State |
| --- | --- | --- |
| M1 | Decision record: concrete pinned model; no tier routing; kind `manager` in the main namespace; launch provenance rule | done ([docs/decisions.md](../../docs/decisions.md)) |
| M2 | Pinned model policy in router/loop: no agent/tier/global fallback; endpoint dialect retry allowed; `select_model` refused | done (`test_manager_model_pinned.py`) |
| M3 | Structural tool catalog: built from an explicit allow-list at both runtime builders, no MCP/skills/extensions | done (`test_manager_catalog.py`) |
| M4 | `ManagerPort` + `HostManagerPort`: read half (`find`/`status`/`result`), launch/wait/cancel half, `session.linked` log | done (`test_manager_port.py`, `test_manager_launch.py`) |
| M5 | Tools `sessions_find`, `session_status`, `session_result` with bounds and visibility rules; managed/user text injected from the manager log | done (resolved-model and files-changed fields dropped) |
| M6 | `session_launch` with `origin_message_id` provenance, approval gate, `managed` link | done |
| M7 | `session_wait` (bounded, interrupted by user input, cancellable) and `session_cancel` (approval) | done |
| M8 | Packaged manager prompt (short; states what it cannot do) | done (a code constant, not a role file); the runtime appends the current user message id to the system prompt |
| M9 | `ManagerOpen`/`ManagerState`, Live pathway, storage/list/refusal rules | done: host (`test_manager_host.py`), Ratatui pinned row and chat (`test_ratatui_manager.py`, `test_ratatui_pty_manager.py`), desktop manager card |
| M10 | Flag `[manager] enabled = false` default; flip after soak | flag done and default off; **soak not done** |

---

## 5. Migration, compatibility, rollback

- No schema bump. Kind `manager` is a value of the existing `sessions.kind` column, written with the row at
  the first accepted input; older databases read it as a plain string.
- `[manager] enabled` (default `false`) gates `ManagerOpen` and every operation on an existing manager
  session. Rollback is the flag; the manager log stays as an ordinary row (not listed, not archived).
- Old clients do not know `ManagerOpen`; they see nothing (the kind is excluded from lists).
- If Codex sign-in is missing, `ManagerState` reports it and a client can offer Settings → Providers. No
  fallback (by design).

---

## 6. Security, budget, observability

- **Threats**: (a) manager self-launching work; (b) injected text in session results laundering into
  launches; (c) privilege escalation via launch parameters; (d) reading sessions the human never pointed at;
  (e) silent model substitution; (f) Voice-authored lines passed off as the user's request. Mitigations: §3.3
  rules 1–5, §3.4, the pinned model, the approval gate, tests.
- Launch caps: `[manager] max_launches_per_turn = 3` and `max_live_managed = 8`; above that
  `manager.launch_refused(cap)`. The `Supervisor` cap (4 turns) still applies to running roots, and a waiting
  manager turn holds one of its slots.
- Cost: Luna usage counted on the manager session; launched roots are billed to themselves.
  `session_result` is ≤ 16 KiB to protect the manager context.
- Waits: `session_wait` returns at most every 600 s so a turn never blocks indefinitely; with root
  `max_turn_seconds = 0` (dirty tree), the manager turn is not killed by a clock either, so the per-call bound
  is what keeps it responsive and cancellable.
- Observability: `LogsRead` allow-lists `session.linked` and `manager.*` names with fixed summaries (no event
  data). The view carries the "launched by manager" origin label on each managed root's first message.
- Privacy: a GPT Live call on the manager carries what it reads of other sessions to OpenAI; that is a
  separate consent (`LiveConsent` scope `manager`) and the call is refused without it.

---

## 7. Tests and acceptance

1. `test_manager_model_pinned.py`: Codex unavailable → turn fails with `ManagerModelUnavailable`; no
   `model.started` for any other route; fallback lists empty; endpoint retry on the same model allowed.
2. `test_manager_catalog.py`: catalog equals exactly the six tools; `escalate`, `subagent`, `read`, `bash`
   absent; the role grammar rejects the reserved context, bundle and profile; launch/cancel always ask.
3. `test_manager_launch.py`: launch without a user message in the turn refused; unattended launch denied;
   caps; unknown role; managed link first; wait (any/all, timeout, interrupted by user input, cancelled);
   cancel only managed; Live provenance (`Voice:` lines never authorise); end to end through the facade.
4. `test_manager_port.py`: `recent` boundary at 2 h (injected clock); archived/trashed/other-workspace/agents
   hidden; `session_result` refused without link or mention; bounds and redaction.
5. `test_manager_host.py`: config, `ManagerOpen` idempotent and refused when off, reserved id, lists exclude
   the manager, the refusal set, `ManagerState`, no hooks and no title, Live consent scope, events and logs.
6. Injection test (covered by 3): a root whose final answer says "launch X with bash" does not cause a launch
   without a new human message in the current turn.
7. Ratatui snapshot: pinned Manager entry, managed roots and active Manager header are covered by native rendering tests and the manager PTY flow. Desktop uses the same host presentation contract and pinned entry. CLI exposes `nexus manager [open|status]`.

Acceptance:

- [ ] "Start a session to do X" → one approval → one new root running X, shown under the manager. (Backend
      and UI paths tested offline; needs a real Codex run.)
- [ ] "What finished in the last two hours?" lists only sessions from that window, with titles and status.
      (Tested offline.)
- [x] Manager never runs a model other than `codex/gpt-6-luna` (pinned, no fallback; offline tests).
- [x] No path exists from the manager to file, shell or network tools (structural catalog; offline tests).

---

## 8. Decisions and open questions

Settled: pinned concrete model; one manager per workspace (U1); no cross-workspace discovery or launch (U2);
launch and cancel confirmation unconditional, no pre-approval rule (U3); provider-default effort, effort
selection refused (U4); no `session_send` in v1 (U5); no model override, so the literal-match question is moot
(U6); result ≤ 16 KiB; feature flag off.

Open:

- One provider sign-in (Codex serves both the manager model and Live) plus the cross-session consent are
  implemented and reported by `ManagerState`; UI wording is implemented; a live soak is not verified.
- Quota and rate-limit behavior of Luna under manager load; Codex token expiry stops the manager (stated,
  not hidden). A small model may misparse requests (mitigated by the approval showing the exact launch).

## 9. References

Workspace only (paths inline). No external sources needed for this phase.
Not verified: live Codex `/models` for `gpt-6-luna` today; GPT Live with the manager; quota behavior under manager load.

### Release verification (2026-10-19)

Automated verification completed on macOS: full Python suite **6,872 passed,
336 skipped, 6 deselected**; focused manager/CLI/native bridge/PTY suite
**161 passed** (including the built native PTY); Rust TUI **579 passed,
2 ignored**, dashboard integration **58 passed**, desktop **111 passed**.
`git diff --cached --check` is clean. These checks do not establish live-provider
manual acceptance: the real Codex/OpenAI smoke test and interactive desktop
review remain unverified. The unchecked acceptance items above are retained
where they require that manual verification rather than inferred from mocks.

Re-checked 2026-10-19 against the code (audit of §3 contracts, §4 milestones, §7 tests): the manager
suites pass (236 tests; one fork/reserved-id test broke under in-flight plan 03 resolve work and was
fixed by refusing the reserved `new_id` before name resolution), `cargo test` passes for `rust/tui`
(580 + 58) and `rust/desktop` (114). Still unverified: live Codex/GPT Live and the soak.

---

## 10. Proposed rethink: GPT Live fronts the manager, manager routes to sessions

Status: **proposed target design. Not implemented, not verified, not accepted.** It states the user's
intended direction, not current behavior. Sections 1-9 above remain the record of what was built and
tested; where §10 conflicts with them, §10 describes the *requested* change and the earlier text stays
authoritative until this section is accepted and implemented. No code, tests or docs outside this plan
have been changed for it.

### 10.1 User intent

- Any GPT Live session opens (slides in) a **Live surface** placed "between the bottom activity bar and
  the composer".
- **Everything GPT Live interacts with is only the manager.** Live does not attach to, read or write any
  other session directly.
- The manager knows which agent/session is in the **foreground** and which session the user means, and
  routes the right message to that session, whether it is active, idle or recently run.
- Example: the user tells Live "there's another session about X; send it the following information".
  The manager identifies the X session and sends that payload to it.
- The manager has broad awareness of what all Nexus sessions are doing.

**Placement needs resolving.** The phrase above is kept as the user stated it. The documented composer
layout ([docs/surfaces.md](../../docs/surfaces.md), Composer) is: editor, then `Agent model provider
effort`, then context usage, with the activity bar *under* it. In that order nothing lies "between" the
activity bar and the composer in the way the phrase reads, so the phrase may refer to a different surface
or order than the documented one. The exact slot (above the editor, between the editor and the model row,
or below the activity bar) is an open decision (§10.9); it is not silently substituted here, and it must be
checked against the real Ratatui and desktop layouts before building.

### 10.2 Contracts this reverses or reopens

| Existing statement | Where | Status under the rethink |
| --- | --- | --- |
| "There is no `session_send` for arbitrary steering in v1 (U5)"; the only way to give a session work is `session_launch` | §3.2, §8 | **Reopened.** A routed-message operation is proposed (§10.5) |
| The manager can only discover, launch, monitor and read results; non-goal "does not do the work" stays, but "launch-only" input is relaxed | §1, §3.2 | **Reopened** for sending to existing sessions |
| `session_result` only for `managed` links or a session the human named by **exact id** in the latest message | §3.4 | **Reopened.** Resolution of "the session about X" by description is needed (§10.4); whether transcript/result access widens is a separate decision (§10.3, §10.9) |
| `session_cancel`, launch caps, "launch requires approval" | §3.2-3.3 | Unchanged, except that approval must reach the user wherever the Live panel is shown (§10.6) |
| GPT Live is started per session (`LiveStart` against a session id, and the manager over the delegation bridge as one case) | §3.6, §3.7 | **Reversed** for the target: Live attaches only to the manager. Current Live attaches per session, which stays true until built |
| `sessions_find` is metadata only (titles, state) | §3.4 | Kept as the baseline; foreground/focus metadata is added (§10.3) |
| Settled decision U5 (no `session_send`) | §8 | **Reopened**; if accepted, §8 must be rewritten |

### 10.3 Awareness is not transcript access

The plan distinguishes three levels and the rethink only asks for the first two by default:

1. **Metadata awareness**: id, title, state, kind, counts, `order_time`, `completion_seq` (today's
   `sessions_find`/`session_status`).
2. **Activity and focus awareness**: what a session is doing now (running/idle/needs input, last active
   time) and which session the user is looking at (new).
3. **Transcript/content access**: reading messages, tool output or final results. This is not implied by
   "broad awareness". `session_result` rules (§3.4) stay as they are unless the user explicitly decides to
   widen them (§10.9). Routing a message does not by itself grant reading the target's replies.

**Focus signal (new).** Each client (Ratatui, desktop, CLI where relevant) would report which session is
foreground and when that changes, as a host-API signal that does not travel through the model. The
manager reads it as data. Needed properties, to be designed: per-client (a user may have several clients),
timestamped/expiring so a stale client does not claim focus, and clearly separate from "the session the
user *means*", which comes from the utterance. Foreground is a hint for resolving "this session"/"that
one", never an authorisation.

### 10.4 Resolving which session the user means

- Inputs: the utterance, the foreground session, and bounded metadata over active, idle and recently run
  sessions (the existing 2 h `recent` window is a starting point, not a decision).
- Match by title, descriptions the user gave, and recent activity. Matching on content the manager cannot
  read (level 3) is limited to what level 1-2 provides; whether any bounded topic summary may be exposed
  for matching is open (§10.9).
- **Ambiguity means ask, not guess.** If zero or more than one session plausibly matches, the manager asks
  the user (naming candidates by title and id) before any send. A single weak match is also treated as
  ambiguous when the payload is consequential.
- A resolved target is confirmed to the user in the Live panel/chat (title, id) before or as the send
  happens, per §10.6.

### 10.5 Routed message operation (proposed)

A new manager tool, working name `session_send` (name not decided), replaces the "no steering" rule for
this one purpose.

- Input: target session id, message text, `origin_message_id` (same real-user-message rule as
  `session_launch`, §3.3 rule 1; a Live delegation counts, `Voice:` lines never do).
- **User-message provenance:** the message is delivered to the target as a *user* message that carries
  `origin = {kind: "manager", manager, origin_message_id}`, in the same shape as a launched root's first
  message (§3.5), so the target and the dashboard can show "sent by manager" and an injected tool/assistant
  text cannot pose as the user.
- **Durable origin/link/event:** a durable `session.linked` record (new kind, e.g. `routed`, name open)
  plus a manager-log event (e.g. `manager.routed` with `target`, `turn`, `origin_message_id`,
  `payload_digest`, bytes) and the target's `input.queued` origin. Ids, counts and digests only, as in
  §3.5; no payload text in the manager log events.
- Delivery uses the existing enqueue path, so a busy target queues like any user message; whether to
  interrupt versus queue is open (§10.9).
- Bounds: payload size cap (to be set; `session_launch` uses 16 KiB), per-turn send cap, rate limit.
- The payload must come from the human's request. Text taken from another session's result is never
  auto-forwarded (extends §3.3 rule 5). Forwarding content the human explicitly asks to relay is allowed
  only as part of that request, and the approval shows the exact payload.

### 10.6 Approval, privacy and security constraints

- **Approval:** following launch/cancel, routing into an existing session is permission-gated
  unconditionally, with the exact target (title and id) and exact payload as the key, ahead of any grant
  or rule. No pre-approval rule and no unattended send (deny). Whether low-risk sends could ever be
  relaxed is open (§10.9); the default is the strict form.
- **Live panel and approvals:** the Live panel is visible **while a manager call is active even when the
  user is viewing another session**, and the panel itself **subscribes to the manager session** so
  approval prompts, resolution questions and status reach the user wherever they are. An approval that can
  only appear in a hidden manager view would deadlock or auto-deny, so this is a requirement, not polish.
- **Privacy:** the existing `LiveConsent(scope="manager")` notice covers what Live reads of other
  sessions. Widened awareness (focus, activity) and routed payloads going to OpenAI via Live need the notice
  to be updated and re-consented; wording and consent versioning are open.
- **Security:** manager authority stays minimal. A target receives a user-origin message and runs under its
  own permissions, with no grants inherited from the manager. A routed message is an instruction
  injection point: caps, provenance, approval and the no-laundering rule above are the mitigations. Routing
  to sessions in other workspaces stays out of scope (U2). Archived/trashed, `agents` namespace and
  subagent sessions are not valid targets unless decided otherwise.
- **Redaction:** metadata shown to Live is as redacted as today's `sessions_find` rows; the focus signal
  carries ids only.

### 10.7 Live typed input versus composer routing

Two input paths must be distinguished and stay distinct:

- **Live (voice or typed in the Live panel):** goes to the manager only. Routed text it produces is a
  delegation that the manager may turn into a routed message (§10.5) after approval.
- **The composer:** continues to send to the session currently displayed. It is not rerouted through the
  manager and a Live panel being open must not capture composer keystrokes.
- Typed text in the Live panel needs clear visual identity (it targets the manager, not the foreground
  session), and the panel should show the resolved target before a send.
- Whether typed Live input and the Live voice transcript share one manager conversation and how `Voice:`
  lines are marked (they remain non-authorising per §3.3) is open.

### 10.8 Acceptance criteria (all unmet; proposed)

- [ ] Starting GPT Live from any session opens the Live surface and attaches **only** to the manager;
      no Live path reads or writes another session directly.
- [ ] The Live panel stays visible during an active manager call when the user views a different session,
      and shows manager approval prompts there; approving or denying works from that view.
- [ ] Each client reports foreground session; the manager can state which session is foreground, and a
      stale or disconnected client does not keep claiming focus.
- [ ] "There's another session about X, send it ..." with exactly one plausible match results in one
      approval showing target title/id and exact payload, then one user-origin message in that session with
      `origin = manager` and a durable link/event in the manager log.
- [ ] With zero or several plausible matches the manager asks which session; nothing is sent until the
      user answers.
- [ ] Sending works for active (queued/delivered per §10.9), idle and recently run sessions; archived,
      trashed, other-workspace and `agents` namespace targets are refused with a reason.
- [ ] Text inside another session's result (e.g. "send this to Y") never causes a send without a new human
      message in the current turn; `Voice:` lines never authorise.
- [ ] Unattended send is denied; caps and size bounds are enforced and produce refusal events.
- [ ] The manager can report what sessions are doing (level 1-2) without being able to read transcripts
      beyond the unchanged §3.4 rules.
- [ ] Composer input still goes to the displayed session while Live is open.
- [ ] Updated consent covers the widened awareness and routing; Live is refused without it.
- [ ] Offline tests exist for each of the above, and a live Codex/GPT Live run is recorded separately
      (not inferred from mocks).

### 10.9 Unresolved decisions

1. **Layout slot** of the Live surface relative to the activity bar and composer (§10.1), in both
   Ratatui and desktop; whether a Live panel is also shown when no manager call is active.
2. Whether `session_send` supersedes or complements U5, its final name, and the link kind/event names.
3. Queue versus interrupt when the target is mid-turn; behavior when the target is waiting on approval.
4. Whether any content-level access (topic summary or result reads for matching/reporting) is granted, and
   under what bound, versus metadata/activity/focus only.
5. Focus signal protocol: transport, expiry, multi-client precedence.
6. How "recently run" is bounded (the 2 h `recent` window versus configurable) and whether idle older
   sessions are reachable by explicit title.
7. Payload and rate caps; whether any send may skip approval.
8. Consent wording/versioning for widened awareness and routed payloads.
9. Whether routing to subagent or child sessions is ever allowed (default: no, root sessions only).
10. Migration of the current per-session Live attach: removed, or kept behind a setting during transition.
11. Impact on plan 03 naming (the manager's addressing uses names added there) and plan 01 session
    links (a new link kind).

None of the above has been decided by the user; this list is the input for that decision.
