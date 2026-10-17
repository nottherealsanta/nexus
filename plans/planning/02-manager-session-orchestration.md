# Plan 02: Manager session above roots

Status: proposal (advisor draft, 2026-10-17). Not implemented. Doc updates are
future implementation tasks.

Phase 2 of 3. **Requires plan 01** milestones M2–M4 (logical sessions,
supervision edges, `SessionTree`). Plan 03 adds names the manager uses for
addressing.

---

## 1. Executive intent

Add a **manager**: one durable session per workspace (see U1) that sits above
the user's root sessions. The human talks to it in plain language ("start a
session to fix the flaky sync test", "what finished in the last hour?"). It
can only:

1. **discover** live and recent (last 2 h) sessions, bounded;
2. **launch** a root session with an instruction **from the human**;
3. **monitor** lifecycle and completion of the sessions it launched or the
   human points it at;
4. **bring completed results into its own context** (bounded, redacted final
   report, not the transcript).

It has **no read, write, bash, web, MCP, skill or subagent tools**. It always
runs on the low-tier GitHub Copilot model **`github-copilot/gpt-6-luna`**, with
**no silent fallback or upgrade**: if that route cannot run, the manager fails
visibly.

### Non-goals

- The manager does not do the work, read files, or review diffs.
- It does not grant permissions. Roots it launches get the same authority as a
  root the human starts from the dashboard, never more.
- It does not see full child contexts (plan 03 adds a narrow, authorised
  context tool for roots; the manager does not get it by default).
- No cross-workspace management in this phase (U2).
- No web UI.

---

## 2. Current state (evidence)

- Model ID. `github-copilot/gpt-6-luna` is the first `low` route in
  `DEFAULT_TIER_MODELS` (`nexus/model/tiers.py:36-41`) and is in the vendored
  catalogue under `github-copilot` (`nexus/model/data/models.min.json:287-330`,
  entry at `:305`). Live `/models` and Luna inference on Copilot were verified
  by the project (`docs/models.md:211-212`); Luna on Copilot uses `/responses`,
  learned after `/chat/completions` is rejected (`docs/models.md:210-211`).
  **Drift found:** `docs/models.md:296` names the fallback
  `openai-codex/gpt-6-luna` while code uses `codex/gpt-6-luna`
  (`nexus/model/tiers.py:39`). Fix the doc in the implementation task.
- Tiers are subagent-only; main models are concrete IDs
  (`docs/models.md:174-176`). So the manager must name the **concrete**
  `github-copilot/gpt-6-luna`, not the tier `low`; "low tier" here describes
  the cost class, not a router lookup. Resolving via tier would allow fallback
  to `codex/gpt-6-luna` or DeepSeek, which the requirement forbids.
- Fallbacks are listed by `ModelRouter.fallbacks` (agent, tier, global
  `model.fallback`) and the loop decides when to use them
  (`docs/models.md:115-117`). The manager must disable all three.
- Roles: `orchestrator` root already exists and "never edits code itself"
  (`docs/agents.md:297-311`) but it still has tools; prompt-only rules are not
  enforcement (`docs/agents.md:96-104`, `:319-323`: read-only is prompt
  guidance only). The manager needs a **structural** tool catalog, not a prompt.
- Host commands already cover list/start/enqueue/cancel/subscribe/state
  (`docs/host.md:35`), cross-project lists (`docs/sessions.md:75-83`), and
  `Supervisor` caps of 4 concurrent turns (`docs/host.md:163-167`).
- Attention travels child → root → human (`docs/agents.md:446-474`).
- Deadlines: the dirty worktree makes root `max_turn_seconds` default 0
  (unlimited) and removes subagent wall clocks (`docs/loop.md:155-160`). The
  manager's "wait" therefore cannot depend on a child timeout; it needs
  explicit, bounded waits (below).
- One-way layering: a UI uses only the host API (`docs/README.md:47`,
  `docs/host.md:267-273`). The manager is **not** a UI; it is a session whose
  tools call a narrow in-process facade port, never the DB.

---

## 3. Target contracts

### 3.1 Manager session

| Property | Value |
| --- | --- |
| Logical kind | `manager` (plan 01 `logical_sessions.kind`) |
| Namespace | `manager` (new; excluded from `project_sessions` like `agents`) |
| Model | `github-copilot/gpt-6-luna`, pinned; `fallback = []`; tier and global fallback disabled |
| Effort | provider default (U4) |
| Tools | exactly the manager tool set below; `subagent`, `escalate`, all file/shell/web/MCP/skill tools absent from the catalog |
| Root agent | packaged role `manager` with `contexts: [manager]` (new context value) so it cannot be selected as an ordinary root or subagent |
| Compaction | plan 01 handoff, deterministic brief only (no summary call, so no second model) |

**No silent fallback.** Add a `model_policy = "pinned"` session property:
`ModelRouter.fallbacks` returns `()`, `EndpointFallback` (protocol dialect
retry, same model) **stays allowed** because it does not change the model.
If Copilot is not signed in, rate-limited or the model is missing from
`/models`, the turn fails with a `ManagerModelUnavailable` error naming the
exact model and the sign-in command; the UI shows it. Model selection commands
(`select_model`) are rejected for manager sessions.

### 3.2 Manager tools (the whole catalog)

| Tool | Input | Output | Bounds |
| --- | --- | --- | --- |
| `sessions_find` | `scope` (`live`/`recent`), optional `query` (≤ 256 chars, literal substring), `limit` (≤ 50) | rows: id, name (plan 03), title, state, kind, `order_time`, `completion_seq`, child counts, resolved model | `recent` = completed or active within **2 h** of now; never archived/trashed; never `agents` namespace rows unless they are children of a listed root |
| `session_launch` | `instruction` (≤ 16 KiB), optional `agent` (root role), optional `model` (concrete, from the human's allow-list), optional `title`, `origin_message_id` | new logical id, name, status `queued` | see §3.3 |
| `session_status` | `ids` (≤ 20) | state, latest attempt, attention flags, `completion_seq` | only sessions visible via `sessions_find` |
| `session_wait` | `ids` (≤ 20), `until` (`any`/`all` complete), `max_wait_seconds` (≤ 600 per call) | which completed, which still running | the tool returns; the manager re-calls. Cancellation aborts the wait, never the target |
| `session_result` | `id` | final assistant message + files changed + status + attention, redacted, ≤ 16 KiB, clipping announced | only terminal sessions; only `managed` edges or explicitly named by the human (§3.4) |
| `session_cancel` | `id`, `reason` | `cancelled: bool` | only sessions the manager launched (`managed` edge); requires human confirmation (approval prompt) |

There is no `session_send` for arbitrary steering in v1 (U5). The manager's
only way to give a session work is `session_launch` with an instruction that
came from the human.

### 3.3 "Instructions from the human" and privilege rules

The risk: a model writing its own instructions, or relaying text from a
completed session's result into a new launch (prompt injection laundering).

Rules (enforced in the tool, not the prompt):

1. `session_launch` requires `origin_message_id` = the id of a **user** message
   in the manager's own log in the current turn. The launch event records it.
   A launch in a turn with no new user message is refused.
2. Launch is a **permission-gated** tool (`SessionLaunch(*)` key), default
   `ask`. The approval shows the exact instruction, agent, model, and
   workspace. With no viewer attached, the unattended default
   (`deny_once`, `docs/sessions.md:126-128`) applies, so an unattended manager
   cannot launch.
3. The launched root gets **the workspace's normal root permissions** (config
   + rules), exactly as `SessionStart` from the dashboard. The manager's own
   (empty) tool set and any manager-session grants are **not** inherited and
   cannot be added. No `allow`-all flag exists on the tool.
4. The model of a launched root is: the human-stated model, else the workspace
   default root model. A model change is a new `model.selected` event in the
   root, visible. The manager can never request a "higher" model than the
   human chose; it can only pass through a model the human named in the
   originating message (checked by literal match against the message text, U6).
5. Results returned by `session_result` are wrapped as data
   (`<session_result id=...>…</session_result>`), like auto-title input
   (`docs/sessions.md:188-189`), and are never auto-forwarded into a launch.

### 3.4 Visibility (what the manager may see)

- `sessions_find` sees root sessions **of this workspace** (U2). It sees
  metadata only (titles, snippets already shown on the dashboard,
  `docs/sessions.md:51-54`), which the human can already see.
- `session_result` requires a `managed` edge (manager launched it) **or** the
  current user message names the session (id or plan 03 name). This prevents
  the manager from reading every session's output on its own initiative.
- No access to child sessions' results directly; a root's final report already
  includes its children's summaries.

### 3.5 Events and schema

| Event | Log | Data |
| --- | --- | --- |
| `session.linked` (kind `managed`) | manager | `child`, `origin_message_id`, `instruction_digest`, `agent`, `model` |
| `manager.launch_refused` | manager | `reason` (`no_user_message`, `denied`, `model_not_named`, `cap`) |
| `manager.result_read` | manager | `session`, `bytes`, `clipped` |
| `manager.wait` | manager | `ids`, `outcome`, `waited_seconds` |

The launched root's first records: `session.opened` with `launched_by:
{manager: <id>, origin_message_id}`, then the instruction as the first **user**
message, marked `source: "manager"` so the dashboard shows "launched by
manager". `title_source` stays `first_message`, so auto-title still works.

### 3.6 Host API

| Command | Purpose |
| --- | --- |
| `ManagerOpen()` | open or create the workspace's manager session (idempotent) |
| `ManagerState()` | manager id, model, sign-in status of `github-copilot`, managed sessions |
| existing `SessionEnqueue`, `SessionSubscribe`, `SessionCancel` | talk to it like any session |

Facade port for tools: `ManagerPort` (L4 host-side protocol injected into the
runtime's tool context) with `find`, `launch`, `status`, `wait`, `result`,
`cancel`. The tools depend on the protocol only, keeping one-way layering
(tools never import `host/`).

### 3.7 Data flow

```
Human ──msg──▶ Manager session (gpt-6-luna, tools: find/launch/status/wait/result/cancel)
                  │ session_launch (approval) ─────▶ HostFacade.start(root, instruction)
                  │                                    └─ Supervisor (cap 4 turns)
                  │ session_wait ◀── completion_seq / turn.completed of root
                  └ session_result ◀── bounded final report (redacted)
Dashboard (Ratatui/desktop): Manager pinned on top ▸ managed roots ▸ their children
```

---

## 4. Milestones

| # | Work package | Depends on |
| --- | --- | --- |
| M1 | Decision record: concrete pinned model; no tier routing; namespace `manager`; launch provenance rule | plan 01 M1 |
| M2 | `model_policy = "pinned"` in router/loop: no agent/tier/global fallback; endpoint dialect retry allowed; `select_model` refused; tests | — |
| M3 | Structural tool catalog for `contexts: [manager]`: the manager catalog is built from an explicit allow-list, not by intersection with defaults; assert in tests that no other tool name exists | — |
| M4 | `ManagerPort` + facade implementation over `SessionList`/`SessionStart`/`SessionState`/`completion_seq` | plan 01 M4 |
| M5 | Tools `sessions_find`, `session_status`, `session_result` (read side) with bounds and visibility rules | M3, M4 |
| M6 | `session_launch` with `origin_message_id` provenance, approval gate, `managed` edge | M5, plan 01 M3 |
| M7 | `session_wait` (bounded, cancellable) and `session_cancel` (approval) | M6 |
| M8 | Packaged `manager` role prompt (short; states what it cannot do) | M3 |
| M9 | `ManagerOpen`/`ManagerState`; Ratatui pinned manager row and chat; desktop parity | M7 |
| M10 | Flag `[manager] enabled = false` default; flip after soak | M9 |

---

## 5. Migration, compatibility, rollback

- New namespace and logical kind need plan 01 schema 6. No further bump if the
  namespace is a value, not a column.
- `[manager] enabled` (default `false`) hides `ManagerOpen` and the dashboard
  row. Rollback is the flag; manager logs stay as ordinary archived sessions.
- Old clients do not know `ManagerOpen`; they see nothing (the namespace is
  excluded from lists).
- If Copilot sign-in is missing, `ManagerState` reports it and the UI offers
  Settings → Providers. No fallback (by design).

---

## 6. Security, budget, observability

- **Threats**: (a) manager self-launching work; (b) injected text in session
  results laundering into launches; (c) privilege escalation via launch
  parameters; (d) reading sessions the human never pointed at; (e) silent model
  substitution. Mitigations: §3.3 rules 1–5, §3.4, M2, approval gate, tests.
- Launch cap: at most `[manager] max_launches_per_turn = 3` and
  `max_live_managed = 8`; above that `manager.launch_refused(cap)`.
  The `Supervisor` cap (4 turns) still applies to running roots.
- Cost: Luna usage counted on the manager session; launched roots are billed
  to themselves. `session_result` is ≤ 16 KiB to protect the manager context.
- Waits: `session_wait` returns at most every 600 s so a turn never blocks
  indefinitely; with root `max_turn_seconds = 0` (dirty tree), the manager turn
  is not killed by a clock either, so the per-call bound is what keeps it
  responsive and cancellable.
- Observability: `LogsRead` allow-lists `manager.*` names. The dashboard shows
  launched-by provenance on each managed root.

---

## 7. Tests and acceptance

1. `test_manager_model_pinned.py`: Copilot unavailable → turn fails with
   `ManagerModelUnavailable`; no `model.started` for any other route; fallback
   lists empty; endpoint retry on the same model allowed.
2. `test_manager_catalog.py`: catalog equals exactly the six tools; `escalate`,
   `subagent`, `read`, `bash` absent; adding a tool to the role file is
   rejected by the grammar for `contexts: [manager]`.
3. `test_manager_launch.py`: launch without user message refused; unattended
   launch denied; launched root's tool set and permission snapshot equal a
   dashboard-started root; model not named by the human refused.
4. `test_manager_visibility.py`: `recent` boundary at 2 h (freeze clock);
   archived/trashed/other-workspace hidden; `session_result` refused without
   edge or mention.
5. `test_manager_wait.py`: completion detection via `completion_seq`, cancel
   during wait leaves target running.
6. Injection test: a root whose final answer says "launch X with bash" does not
   cause a launch without a new human message.
7. Ratatui snapshot: manager row pinned, managed roots grouped.

Acceptance:

- [ ] "Start a session to do X" → one approval → one new root running X, shown
      under the manager.
- [ ] "What finished in the last two hours?" lists only sessions from that
      window, with titles and status.
- [ ] Manager never runs a model other than `github-copilot/gpt-6-luna`.
- [ ] No path exists from the manager to file, shell or network tools.

---

## 8. Decisions and open questions

Recommended defaults: pinned concrete model; manager per workspace; launch
approval `ask`; no steering tool; result ≤ 16 KiB; feature flag off.

Unresolved user decisions:

- **U1** One manager per workspace (recommended, matches one-daemon-per-
  workspace, `docs/host.md:187`) or one global manager?
- **U2** Cross-workspace discovery/launch? Recommended no for v1; it would
  need the cross-project index plus a daemon per target workspace.
- **U3** May the human pre-approve launches (`allow` rule for
  `SessionLaunch(*)`)? Recommended: allowed only in user config, not project.
- **U4** Reasoning effort for Luna: provider default (recommended) or fixed.
- **U5** Add `session_send` (follow-up to a managed root) later? It needs the
  same provenance rule.
- **U6** How strictly to match a human-named model (literal ID vs fuzzy
  alias)? Recommended literal registry ref.

Risks: Copilot token expiry stops the manager (stated, not hidden); a small
model may misparse requests (mitigated by approval showing the exact launch).

## 9. References

Workspace only (paths inline). No external sources needed for this phase.
Not verified: live Copilot `/models` today; quota behavior under manager load.
