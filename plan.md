# Plan: core and host work for the unified interface

Status: draft plan, 2026-10-27. Nothing here is built. This file holds the core, host, tool and
storage changes that [INTERFACE_PLAN.md](INTERFACE_PLAN.md) needs. Everything the TUI and NSH show
is in that file, which cites these sections as **→ core §N**.

Sources read: `docs/context.md` (Compaction, Same-session handoff), `docs/sessions.md`
(same-session handoff, immutable names), `nexus/tools/builtin/question.py` (`ANSWER_WINDOW_S` = 15
min), `nexus/tools/builtin/worktree.py` (`worktree_create/move/list/commit`),
`nexus/host/protocol.py` (`PROTOCOL_VERSION = 7`), `nexus/view/model.py`.

The rule that drives most of this, now in `AGENTS.md`: **the timeline the user sees and the
context the agent sees are the same.**

## Contents

1. [One-turn sessions](#1-one-turn-sessions)
2. [Context handoff to a new session (replaces compaction)](#2-context-handoff-to-a-new-session)
3. [The `ask` tool: questions with no timeout](#3-the-ask-tool-questions-with-no-timeout)
4. [Permissions and questions stay separate in core](#4-permissions-and-questions-stay-separate-in-core)
5. [Session search, archive, delete](#5-session-search-archive-delete)
6. [Per-session context overrides](#6-per-session-context-overrides)
7. [Reading full tool results](#7-reading-full-tool-results)
8. [Files, providers, settings pages](#8-files-providers-settings-pages)
9. [Worktrees: how an agent decides to isolate (discussion)](#9-worktrees-how-an-agent-decides-to-isolate)
10. [Client and daemon version handshake](#10-client-and-daemon-version-handshake)
11. [Mid-session agent, model and effort changes](#11-mid-session-agent-model-and-effort-changes)
12. [Summary of host contract changes](#12-summary-of-host-contract-changes)

---

## 1. One-turn sessions

A **one-turn session** is a real, durable session with exactly one request and one answer, no
tools, and a parent link. It is not a hidden side call. Its request and answer are recorded like
any other session, so the user can open them and see what was sent (the context rule).

- **Shape:** `SessionSummary.kind = "oneshot"`, plus `purpose` (`title` | `btw`), `parent_id`, and
  `fork_seq` (the parent seq its context was taken from). It's never listed in Sessions, it's
  reachable from the parent, and it's deleted with the parent.
- **Bounds:** no tools. One provider call with a timeout (60 s for title, 300 s for btw) and a
  bounded output (title 64 tokens, btw 4K default). It can be cancelled.
- **Title.** Replaces today's title call. Input: the first user message (capped at 4K characters) and
  a fixed title prompt. Model: Settings → Models → title model. It writes the session title when done; a
  failure leaves `New session` and records the error in the one-turn session.
- **`/btw <text>`:**
  - Input: the **current agent's** context up to the **last completed tool call**, cut at a pair-safe
    boundary so no unanswered tool call is included, plus the user's text as the final user message.
    It's the same assembly the next model step would use, so the provider's prompt cache is reused
    when the model is the same.
  - The answer is never appended to the parent's context. The UI can turn it into a steer explicitly
    (that is a normal steer message).
  - It works mid-turn and needs no turn lease (read-only on the parent). It records `fork_seq` so
    the exact context it used is known.
- **Host:** `OneTurnStart(session, purpose, text, agent?)` → the one-turn session id. It streams like any
  session (`session-poll`). `OneTurnList(session)` lists them for the parent.
- **Open:** if the parent's agent is a subagent, `/btw` on a child page uses the child's context.

## 2. Context handoff to a new session

**Today:** compaction strategies drop or evict history inside the same session, and the default
`on_overflow = "handoff"` does a **same-session** context-head cutover. Either way the user's timeline
still shows turns the agent no longer sees. That breaks the rule.

**Proposed:** when the context is full, the session **hands off to a new root session**:

- **Trigger:** projected usage crosses `context.handoff_at_fraction` (proposed 0.90), with a warning
  event at 0.85. Also manually (`SessionHandoff`, which exists today; repurposed).
- **The new session:**
  - It starts with: the same agent, model, effort, workspace and worktree; the same
    skills/tools/MCP selection and context overrides (§6); the **brief** as a labelled, pinned
    context block (the existing version-1 deterministic brief, or summarized); and the kept
    user/tool tail (`handoff_keep_fraction`), copied in as real records so they appear in its
    timeline.
  - It records `continued_from = <old id>` and `continued_seq`.
- **The old session:** gets a durable `session.continued` event (`to = <new id>`, the reason, the
  sizes), becomes read-only for new turns, and keeps its whole log.
- **What moves to the new session:** the input queue, pending waiting items, and running children's
  ownership (re-parented with an `ownership.moved` event so the move is visible). The existing rule that
  "pending approvals and children are not moved" must change here, and the change is recorded in
  `docs/decisions.md`. Running shell jobs stay owned by the old session and finish there, and the new
  session's brief lists them.
- **Chain:** `SessionSummary.chain_id` (the first session's id) and `chain_index` let lists show a
  chain as one row.
- **In-turn:** if the threshold is hit mid-turn, the handoff happens at the next step boundary, and the
  turn continues in the new session as its first turn (`origin.kind = "handoff"`).
- **Compaction strategies** (`drop_oldest`, `evict_tool_results`, `summarize`) are no longer applied
  silently. `evict_tool_results` may stay only if each eviction is a visible event that the timeline
  renders as `result evicted from context`. Proposed: keep eviction (visible), remove dropping.
- **Replay** reproduces both sessions exactly. The `docs/context.md` and `docs/sessions.md` sections
  are rewritten.
- **Risk:** this changes a default that exists under a maintainer waiver (`docs/decisions.md`).
  Ship it behind `context.on_overflow = "new_session"` first, then flip the default after real use.

## 3. The `ask` tool: questions with no timeout

**Today:** `question` waits at most 15 minutes (`ANSWER_WINDOW_S`), then closes with `timeout`.

**Proposed:** a new tool, `ask`, replaces `question` (the old name stays as an alias for one release):

- **Schema:** `question` (≤ 2,000 characters), `options[]` (≤ 10 labels, each ≤ 200 characters, with an
  optional description), `multiple: bool`, `allow_text: bool` (default true), `context` (≤ 4,000
  characters of why it asks).
- **No timeout.** The call waits until it is answered, the turn is cancelled, or the session hands off
  (§2, where it moves). It survives client disconnects and a **daemon restart**: the pending ask is
  durable (`ask.pending`) and on restart the turn is resumable at that call. If the restart can't
  resume the turn, the ask closes with `daemon restarted` and the turn says so.
- The answer is a durable `ask.answered` event (`by`: the client and surface, the chosen options, the
  text). The tool result the model gets is exactly what the UI shows.
- In headless runs (`nexus run`, JSONL) with no one to answer, an ask fails at once with `no
  operator` unless `--wait-for-input` is given.
- A subagent's `ask` goes to the user (shown on the child's card and in the parent), never to the
  parent agent.
- **Host:** `AskAnswer(session, call_id, options, text, client_request_id)`. The first answer wins, and
  later ones get `already answered by …`.

## 4. Permissions and questions stay separate in core

The interface combines permissions and questions into "waiting for your input" (INTERFACE_PLAN C1).
Core keeps them apart: permission requests are owned by `tools/permissions.py` and the permission
events, and questions by `ask`. Each keeps its own events, rules and results.

- The view keeps `PermissionView` and a new `AskView` as separate lists.
- The combining happens only in `ui_support/waiting.py` (presentation), which reads both lists and
  emits one ordered list of waiting items. No core type knows about "waiting".
- The proposed worktree request (§9) and sign-in-needed are also separate core events, combined only
  in `ui_support`.
- `SessionSummary.state` already has `awaiting_input` / `awaiting_permission`. Add
  `waiting_count` so lists can show `●` without loading the view.

## 5. Session search, archive, delete

- **Search:** `SessionSearch(query, project?, limit=50)` matches **only titles and user message text**.
  Backed by an SQLite FTS5 table (`session_search(session_id, kind, seq, text)`) in `nexus.db`, filled
  when a title is set and when a user message is appended. Tool output, assistant text and
  attachments are not indexed. Results carry `session`, `seq` (to jump to the turn), a snippet (≤ 200
  characters, highlighted spans as offsets, not markup), `project` and `last_activity`. Bounded: query
  ≤ 200 characters, 50 results, 200 ms budget, with the cut announced. A one-time backfill for existing
  sessions is bounded and resumable.
- **Rename:** `SessionTitleSet(session, title)` (≤ 200 characters). The immutable name is unchanged.
- **Archive:** reversible and whole (exists). Make sure `SessionArchive` / `SessionUnarchive` cover
  children and chains (§2): archiving a chain archives every part.
- **Delete:** `SessionDelete` moves the session to trash with its children and one-turn sessions.
  `SessionRestore` brings it back. `SessionPurge` deletes forever (names are never reused, as today).
  Automatic purge after `sessions.trash_days` (proposed 30). A running session or one with live
  children is refused with `stop the turn first`. `SessionTrashList` lists the trash for Settings.
- All mutations are idempotent by `client_request_id` so the UI's Undo is safe.

## 6. Per-session context overrides

- `ContextOverrideSet(session, block, text | None)` with `block` in `system_prompt | agents_md:<path> |
  memory_md:<path>`. It writes `session.context_override`, and `None` clears it. Each block is ≤ 64
  KiB. It is refused after the first turn (prompt cache), and the refusal says why.
- The reducer exposes overrides in `ConversationView.context`. Prompt assembly (`docs/context.md`) uses
  the override in place of the file and marks the block `edited` in the request context record, so
  the header can label it.
- `ContextFileToggle(session, path, enabled)` turns one AGENTS.md / MEMORY.md off for the session (same
  rules).
- A handoff (§2) carries overrides to the new session.

## 7. Reading full tool results

- `ToolResultRead(session, call_id, offset, limit)` returns bytes `offset..offset+limit` of the
  call's **durable** result text (the log keeps more than the 8,192-character view clip), plus
  `total`, `encoding`, and `truncated_at_source` (when the tool itself capped its output, as
  `_output_limit.py` does). `limit` ≤ 64 KiB. Redacted like every result.
- For running shell jobs, the UI uses the existing `SessionJobLog`.

## 8. Files, providers, settings pages

- **`FileWrite(path, text, base_hash, project?)`** for the built-in editor: workspace-relative, the
  same path rules as `DirectoryList` and `tools/permissions.py` write checks. It refuses when the file's
  current hash differs from `base_hash` (and returns the current hash), when the file is over 1 MiB,
  and when the file is binary. Writes are atomic (temp file plus rename). A user edit is recorded as a
  durable `workspace.user_edit` event in the current session (path, size, hash), so the agent can be
  told and the timeline can show it.
- **`ProviderAdd(name, base_url, dialect, api_key?, headers?)`, `ProviderTest(...)`,
  `ProviderRemove(name)`** for OpenAI-compatible providers (`kind = "openai_compatible"`):
  - The URL is validated (http(s), no credentials in it). Loopback URLs are allowed for local servers.
  - The key is stored with the existing credential store and **never returned**.
  - `ProviderTest` lists models with a 10 s timeout and returns the count and names only.
  - Written to the user config layer, then hot reloaded.
- **`SettingsPage(area)` / `SettingsPageOp(area, op, value, …)`**, only if INTERFACE_PLAN J1 chooses to
  serve pages from the daemon. That means moving the page builders from
  `nexus/ui/ratatui/settings_pages/` into `nexus/host_support/`, which must pass `tests/test_layering.py`.
- **`GitStatus`** gains the session's checkout (worktree when the session is in one) via `session`,
  which it already accepts. Check that it resolves the session's root and not the project root.

## 9. Worktrees: how an agent decides to isolate

This is a discussion with a recommendation; it is not decided. The interface side is **TBD**
(INTERFACE_PLAN E3).

### Where it stands
Every session starts in the main checkout. The agent has four tools: `worktree_create(reason, name?,
base?)`, `worktree_move(to, reason)`, `worktree_list`, `worktree_commit`. Each move is a durable,
reason-labelled row. The move takes effect after the current step. **What's missing is any signal
telling the agent when to isolate.** The tools say *how*, but nothing says *when*, so the agent either
never isolates or does it for no reason.

### The problem, as questions
1. **What facts does the agent need?** Whether anyone else is working in this checkout, whether there
   are uncommitted changes that aren't its own, and how big and risky the task is.
2. **Who decides?** The agent (judgment), the harness (rules), or the user (approval).
3. **When?** Isolating before the first write is cheap. After writes, the session's changes must move
   with it.
4. **How does it end?** Integrate, keep, or discard, and back to the main checkout.

### Recommendation: one `checkout` tool + harness facts + a policy

**a. One tool instead of four:** `checkout(action, …)`:

| Action | Does |
| --- | --- |
| `status` | where this session is; base branch and commit; **who else is here** (live sessions in this checkout, and whether they're writing); uncommitted files, split into *mine* (from this session's tool-derived changed paths) and *not mine*; ahead/behind |
| `isolate(reason, name?)` | create a worktree from the current HEAD and move into it at the step boundary; *mine* uncommitted changes move with the session (below) |
| `switch(to, reason)` | move to an existing worktree or `main` |
| `commit(message)` | commit in the current worktree (today's `worktree_commit`) |
| `integrate(reason)` | merge into the base branch (today's integrate flow), with the preview |
| `discard(reason)` | leave and remove the worktree; the branch stays, as today |

One schema costs fewer tokens than four and gives one mental model. Every action is a durable,
reason-labelled event, shown in the timeline.

**b. Facts the harness puts in front of the agent** (visible in the timeline, per the context rule):
- A short **checkout line** in the environment block: `Checkout: main checkout · 1 other live session
  here (writing) · 3 uncommitted files not yours`.
- When this changes mid-session (another session starts writing here), a **checkout notice** is
  appended as a visible context row before the next step. It is not injected silently.

**c. Guidance in the tool description (when to isolate):**
- **Isolate** when another live session is writing in this checkout; when the task is a multi-file
  change the user may want to review or throw away as a unit (refactors, dependency upgrades,
  migrations, experiments); when the user asks for parallel or separate work; or before running
  something that rewrites many files (formatters, codegen).
- **Don't isolate** for questions, reading, reviews, or small single-file edits; or when the user is
  working in the main checkout and expects to see the changes there (for example, they asked for a quick
  fix to what they're looking at).
- Isolate **before the first write**. If unsure, call `status`.

**d. A policy the user chooses** (`worktree.policy`, Settings → Tools & permissions):

| Policy | Behaviour |
| --- | --- |
| `off` | the tool has no `isolate`; everything happens in the main checkout |
| `agent` (proposed default) | the agent decides using (b) and (c) |
| `ask` | `isolate` becomes a proposal the user approves: a waiting item (§4, INTERFACE_PLAN C1) with the reason and *Move to a worktree / Stay* |
| `auto` | the harness isolates on the session's **first write** when another live session is writing in this checkout; otherwise, as `agent` |

**e. A write guard (the safety net, in every policy except `off`):** on a session's first write tool
call, if another live session wrote to this checkout in the last 10 minutes, the call is not run. It
returns `This checkout is in use by "<title>" (writing). Call checkout(action="isolate") first, or
call again with shared=true to write here anyway.` This turns a silent conflict into an explicit
decision without blocking the agent.

**f. Moving after writes.** `isolate` after the session has written: the session's *mine* changes
(known from tool events) are moved by creating the worktree from HEAD, applying the session's diff of
those paths there, and restoring those paths in the main checkout, all-or-nothing. A path that is
also changed by someone else (a *not mine* overlap) refuses the move and lists the paths. **Not
verified as safe; needs a design review before building.**

**g. Children.** Writing subagents launched in parallel already get child checkouts. Align them with
the same `checkout` vocabulary, and have a parent's `status` list its children's worktrees.

**h. Ending.** When a root session in a worktree goes idle with unmerged commits, `status` says so, and
the next `checkout` call or the end of the turn reminds the agent once. The user-facing controls are
TBD.

Open: should `auto` also isolate when the task *looks* large, which needs a model judgment? Proposed
no: `auto` is only for the conflict fact, and judgment stays with the agent.

## 10. Client and daemon version handshake

- Every client calls `Health` first. It returns `protocol_version` (today 7), `min_client_protocol`,
  and `daemon_version`.
- **Breaking mismatch** (the client's protocol is under `min_client_protocol`, or the daemon's is under
  the client's minimum): the client **halts** and shows the blocking banner (INTERFACE_PLAN F1). The
  daemon also refuses commands from a client that declares an incompatible protocol
  (`X-Nexus-Protocol` header for web, a field in the UDS hello), answering `ErrorResult(kind =
  "protocol_mismatch")` with both versions.
- **Compatible difference:** a warning only. New optional fields are ignored by old clients
  (msgspec defaults), which is already the rule.
- `docs/host.md` documents when to bump `PROTOCOL_VERSION` vs `min_client_protocol`, and the release
  checklist (`docs/release.md`) gains the step.
- Restart and reconnect: the daemon's `daemon_instance_id` (new, in `Health`) changes on restart, so a
  client knows to resync everything, not just the open session.

## 11. Mid-session agent, model and effort changes

- `SessionConfigure(session, agent?, model?, effort?)` is allowed at any time. Mid-turn, it takes
  effect at the next model step. It writes `session.configured` (old, new, the step it applies from),
  so the timeline shows it.
- The UI's cache warning needs an estimate: `ContextInspect` returns `cached_prefix_tokens` (the
  provider's last reported cache read) and the price, so the warning can say `re-sends ~48K tokens (≈
  $0.14)`. When unknown, it says `the prompt cache will be rebuilt`.
- An agent change mid-turn changes the system prompt and tools for the remaining steps. The request
  context record of the next step shows the new header.

## 12. Summary of host contract changes

| Command / event | § | Notes |
| --- | --- | --- |
| `OneTurnStart`, `OneTurnList`; `kind = "oneshot"` | 1 | title, `/btw` |
| `session.continued`, `continued_from`, `chain_id`, `ownership.moved` | 2 | handoff to a new session |
| `ask` tool, `AskAnswer`, `ask.pending/answered`, `AskView` | 3 | no timeout |
| `SessionSummary.waiting_count` | 4 | |
| `SessionSearch` (FTS5), `SessionTitleSet`, `SessionDelete/Restore/Purge/TrashList` | 5 | |
| `ContextOverrideSet`, `ContextFileToggle` | 6 | |
| `ToolResultRead` | 7 | |
| `FileWrite`, `workspace.user_edit`, `ProviderAdd/Test/Remove`, `SettingsPage/Op` (maybe) | 8 | |
| `checkout` tool, `worktree.policy`, the write guard | 9 | discussion |
| `Health.min_client_protocol`, `daemon_instance_id`, `protocol_mismatch` | 10 | |
| `SessionConfigure`, `session.configured`, `cached_prefix_tokens` | 11 | |

Each needs: a struct in `nexus/host/protocol.py`, a handler in `facade.py`, redacted errors, bounds, a
reducer change where it adds an event (replay reproduces it), a test next to its peers, and the
matching `docs/` page in the same change.
