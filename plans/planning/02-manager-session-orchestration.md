# Plan 02: Manager session above roots

Status: **implemented and verified offline (2026-10-19); feature off by default; not soaked
against the real services.** The backend (M1-M9), the Ratatui pinned manager row and chat, the
desktop manager card and the `nexus manager` CLI are written and covered by the offline Python
suite, the Rust TUI/desktop tests and the manager PTY flow. What remains is live-only: a real
Copilot/GPT Live run and the soak that flips the default (M10, §7 acceptance). Behavior is
documented in [docs/manager.md](../../docs/manager.md); where this plan and the code differ,
the code wins. The high advisor review changed several first-draft decisions; this text states
the settled ones (they are listed in §8).

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
runs on the low-tier GitHub Copilot model **`github-copilot/gpt-6-luna`**, with **no silent fallback
or upgrade**: if that route cannot run, the manager fails visibly.

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

- Model ID. `github-copilot/gpt-6-luna` is the first `low` route in `DEFAULT_TIER_MODELS`
  (`nexus/model/tiers.py:36-41`) and is in the vendored catalogue under `github-copilot`
  (`nexus/model/data/models.min.json`). Live `/models` and Luna inference on Copilot were verified by
  the project (`docs/models.md`); Luna on Copilot uses `/responses`, learned after `/chat/completions`
  is rejected. The `codex/gpt-6-luna` fallback name in `docs/models.md` was corrected to match code.
- Tiers are subagent-only; main models are concrete IDs. So the manager names the **concrete**
  `github-copilot/gpt-6-luna`, not the tier `low`; "low tier" describes the cost class, not a router
  lookup. Resolving via tier would allow fallback to `codex/gpt-6-luna` or DeepSeek, which the
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
| Model | `github-copilot/gpt-6-luna`, pinned; `fallback = []`; tier and global fallback disabled |
| Effort | provider default (U4); effort selection is refused |
| Tools | exactly the manager tool set below; `subagent`, `escalate`, all file/shell/web/MCP/skill tools absent from the catalog. Built structurally at **both** runtime builders, bypassing MCP, skills and extensions |
| Prompt | packaged constant (`agents/manager_prompt.py`), not a role file; `contexts: [manager]`, `bundles: [manager]` and `profile: manager` are rejected by the role grammar, so it cannot be selected as an ordinary root or subagent |
| Compaction | plan 01 handoff, deterministic brief only (no summary call, so no second model) |
| Hooks, titles | no hook of any kind (`SessionStart`, `UserPromptSubmit`, `SessionEnd`); no automatic title |
| Refused | archive, trash, fork, `!` shell, model selection, role selection, effort selection, manual handoff, skill/MCP selectors |

**No silent fallback.** The pinned policy is request metadata (`model_policy = "pinned"`):
`ModelRouter.fallbacks` returns `()`, `EndpointFallback` (protocol dialect retry, same model) **stays
allowed** because it does not change the model. If Copilot is not signed in, rate-limited or the model
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
| `ManagerState()` | enabled, session id and state, pinned model, Copilot and Codex sign-in with the exact fix, the two Live consents, managed roots, caps (never an error for "off") |
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
- If Copilot sign-in is missing, `ManagerState` reports it and a client can offer Settings → Providers. No
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

1. `test_manager_model_pinned.py`: Copilot unavailable → turn fails with `ManagerModelUnavailable`; no
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
      and UI paths tested offline; needs a real Copilot run.)
- [ ] "What finished in the last two hours?" lists only sessions from that window, with titles and status.
      (Tested offline.)
- [x] Manager never runs a model other than `github-copilot/gpt-6-luna` (pinned, no fallback; offline tests).
- [x] No path exists from the manager to file, shell or network tools (structural catalog; offline tests).

---

## 8. Decisions and open questions

Settled: pinned concrete model; one manager per workspace (U1); no cross-workspace discovery or launch (U2);
launch and cancel confirmation unconditional, no pre-approval rule (U3); provider-default effort, effort
selection refused (U4); no `session_send` in v1 (U5); no model override, so the literal-match question is moot
(U6); result ≤ 16 KiB; feature flag off.

Open:

- Two provider sign-ins (Copilot for the manager, Codex for Live) and the cross-session consent are
  implemented and reported by `ManagerState`; UI wording is implemented; a live soak is not verified.
- Quota and rate-limit behavior of Luna under manager load; Copilot token expiry stops the manager (stated,
  not hidden). A small model may misparse requests (mitigated by the approval showing the exact launch).

## 9. References

Workspace only (paths inline). No external sources needed for this phase.
Not verified: live Copilot `/models` today; GPT Live with the manager; quota behavior under manager load.

### Release verification (2026-10-19)

Automated verification completed on macOS: full Python suite **6,872 passed,
336 skipped, 6 deselected**; focused manager/CLI/native bridge/PTY suite
**161 passed** (including the built native PTY); Rust TUI **579 passed,
2 ignored**, dashboard integration **58 passed**, desktop **111 passed**.
`git diff --cached --check` is clean. These checks do not establish live-provider
manual acceptance: the real Copilot/OpenAI smoke test and interactive desktop
review remain unverified. The unchecked acceptance items above are retained
where they require that manual verification rather than inferred from mocks.

Re-checked 2026-10-19 against the code (audit of §3 contracts, §4 milestones, §7 tests): the manager
suites pass (236 tests; one fork/reserved-id test broke under in-flight plan 03 resolve work and was
fixed by refusing the reserved `new_id` before name resolution), `cargo test` passes for `rust/tui`
(580 + 58) and `rust/desktop` (114). Still unverified: live Copilot/GPT Live and the soak.
