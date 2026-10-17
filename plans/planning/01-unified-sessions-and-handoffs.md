# Plan 01: Unified sessions, session trees, and context handoffs

Status: proposal (advisor revision 2, 2026-10-17). Nothing here is implemented.
Docs under `docs/` stay the source of truth; every doc change listed below is a
future implementation task, not part of this plan.

Phase 1 of 3. Plan 02 (manager) and plan 03 (names and recursive context)
depend on the identity and ownership model defined here. §10 maps the
identifiers those plans use onto this revision.

### What changed from revision 1

Revision 1 (same date) made each handoff create a new **segment** session (a
new `sessions` row), with a `logical_sessions` table above it, segment sealing,
head-following subscriptions, and a five-record cross-session transaction.
Checking the code shows that is more machinery than the goal needs, and its
crash story depends on a write primitive Nexus does not have:

- Every append is its own `BEGIN IMMEDIATE` transaction
  (`nexus/session/db.py:790-830`). The only multi-record write is
  `create_from_records`, used for draft promotion and fork
  (`db.py:815`, `nexus/session/manager.py:714-740`).
- A summary record already acts as a context head: assembly sends the summary
  plus the suffix it does not cover, and reuses it by digest
  (`nexus/context/manager.py:1886-1941`).
- Pi's durable harness, the reference the user asked for, implements reset and
  handoff as a **head marker inside the same conversation**, not as a new
  conversation (`references/pi/packages/durable/docs/spec.md:257-306`, `:913-917`,
  `:3378-3386`).

This revision therefore makes a handoff an **atomic context-head write inside
the same session**. Session identity never changes, so subscriptions, `from_seq`,
fork, export, trash, grants, queue, todos and live children need no migration.
Segments are deferred (§9). It also adds the work the first draft missed:
recovery gaps for native children, submission idempotency, bounded reads,
reducer behavior for unknown events, and a failure-injection matrix.

---

## 1. Intent and scope

Every unit of agent work is a **durable session** with an immutable ID: user
roots, subagents a root creates, and external workers (Claude Code,
Antigravity). The dashboard shows **roots with their children** as one tree.
When a session's projected context passes **85 %** of its input budget, Nexus
writes a **handoff** inside that session: a structured brief that replaces
older history in the model context, while all history stays in the log. Long
work then continues instead of silently losing its oldest messages.

### Must-have (this plan delivers)

| # | Deliverable |
| --- | --- |
| A | Durable **ownership** for children: who spawned whom, stored on the session row, written when the child row is created, backfilled for old rows |
| B | **Session tree** reads (`SessionTree`, extended `SessionList`) for Ratatui and desktop; status roll-up |
| C | **Recovery** that closes every child a crash left open, native and external, in the parent and the child |
| D | **Handoff**: a deterministic brief written as one atomic head record plus event, idempotent, at a safe boundary, behind a flag |
| E | `append_batch`: one-transaction multi-record append (the primitive D and F need) |
| F | Optional `client_request_id` on `SessionStart`/`SessionEnqueue` so a retried submission is not run twice |
| G | Reducer and both native clients render handoffs and tree rows; unknown kinds do not spam diagnostics |

### Deferred (explicitly not in this plan; §9 says what would trigger each)

- A general durable task engine (Pi-style task checkpoints, effect phases,
  resumable generation).
- Resuming an interrupted turn or a model stream after a crash. Today a crashed
  turn is closed `interrupted` and nothing reruns (`docs/sessions.md:109-125`).
  This stays.
- Tool replay of any kind. Tools are never re-executed after a crash
  (`nexus/session/session.py:1635-1658`). There is no `replay: safe` marker.
- Background or persistent subagents that outlive the parent turn, and
  restart-safe child reuse.
- Physical segments (new log per handoff), branching beyond today's
  `SessionFork`, typed durable documents with fork policies.
- Background (non-blocking) compaction, a model-written brief by default, and
  live extension replacement semantics.
- Provider-overflow retry (an "error: context too long" response triggers a
  handoff and one retry) is a stretch item in P5, not required.

### Non-goals (unchanged from revision 1)

- The manager (plan 02); names and recursive context (plan 03).
- Permission model changes: children still get `parent ∩ role ∩ requested`
  (`docs/agents.md:348-366`).
- Web client changes. Web is deprecated (`docs/surfaces.md:371`); it must keep
  working on old projections.
- Subagent deadlines. The dirty worktree removes them
  (`docs/agents.md:105-108`, `docs/loop.md:155-160`); this plan adds none.

---

## 2. Vocabulary: five things that must stay separate

The user's request mixes these. Each has a different owner, lifetime and table.

| Concept | Question it answers | Nexus today | This plan (v1) | Pi analogue (verified in spec) |
| --- | --- | --- | --- | --- |
| **Identity** | Which conversation is this? | `sessions` PK `(project_id, namespace, id)` (`db.py:95-110`) | unchanged; a session ID never changes, also across handoffs | `ConversationId` (`spec.md:93`, `:108-118`) |
| **Context lineage** | Which records does the model see? | whole log minus compaction; fork copies a prefix (`manager.py:705-742`) | newest **context head** in the log, then records after its `kept_from_seq`; fork inherits heads at or before `at_seq` | `head` entry plus `parent.at` for forks (`spec.md:257-306`) |
| **Ownership** (supervision) | Who started this, who can cancel it, whose turn waits for it? | implicit in the `<parent>/sub/<n>` string and in parent-log relays (`runtime.py:1804-1849`, `:2988-2989`, `:6048-6049`) | explicit `owner_*` columns on the child row (§5.1) | `owner: {conversationId, taskId}`, "not an access-control capability" (`spec.md:247-255`) |
| **Handoff** | How does a full context continue? | none; `hybrid` drops oldest silently (`docs/context.md:101-105`) | a head record of strategy `handoff` in the same session (§5.2) | `pi.reset` with `head: "self"` and a handoff note (`spec.md:3378-3386`) |
| **Fork** | Branch a copy to explore | `SessionFork(at_seq)`: new row, `parent_id`, `fork_seq`, copied prefix, `session.forked` (`manager.py:705-742`) | unchanged; a fork is a new root and owns no children | `fork(entryId)`, fresh identity (`README.md:531-538`) |

An **attempt** (one run of a turn, retry, fallback) stays implicit in
`turn.started`/terminal events and `model.started` (`docs/loop.md:20-42`). The
revision-1 `attempts` table is deferred: nothing in A–G needs it.

Rules:

1. **Ownership is not context.** A child never sees its parent's conversation
   (`docs/agents.md:339-341`), and a parent sees only the child's report.
2. **Ownership is not authority.** Owner columns record facts for display,
   recovery and cancellation. They grant nothing. Plan 03 may *use* them as one
   input to authorization, but authorization is plan 03's rule, not a property
   of the column (same stance as Pi, `spec.md:250`).
3. **Fork lineage is not ownership.** `sessions.parent_id`/`fork_seq` keep
   meaning "forked from". Revision 1 put forks in the edge table; that
   overloaded `parent`. Forks stay in their existing columns.
4. **A handoff is not a new identity.** It is a context change in one log.

---

## 3. Current state (evidence, re-checked 2026-10-17)

### 3.1 Storage and writes

- One DB, `~/.nexus/nexus.db`, `SCHEMA_VERSION = 5` (`nexus/session/db.py:78`).
  `project_id` is `TEXT` (`db.py:96`); revision 1's SQL used `INTEGER`, which
  was wrong.
- WAL, `synchronous=FULL`, `busy_timeout=5000` (`db.py:200-208`); several
  daemons (one per workspace, `docs/host.md:187`) share the file.
- **Each append is one transaction with one record** (`_commit_append`,
  `db.py:790-830`). Multi-record atomicity exists only in `create_from_records`
  (draft promotion at `db.py:815`, fork at `manager.py:738-740`).
- A new chat is an in-memory **draft** until the first user message or
  `input.queued` (`db.py:809-816`, `docs/sessions.md:3-12`).
- `Session.read()` loads and caches **the whole log** (`session/session.py:923-926`,
  `db.py:611-628`). `summaries`, `latest_summary`, `summary_for` and
  `_events_after` scan all records (`session.py:1006-1037`, `:1158-1164`). Only
  diagnostics use the bounded `read_tail` (`db.py:630-655`). Long-lived sessions
  (which handoffs encourage) make every turn O(log). **Not measured**: real log
  sizes in user databases.

### 3.2 Children

- Children are real logs in namespace `agents`. Logical id `<parent>/sub/<n>`
  becomes a sanitized id with a 10-hex SHA-1 suffix (`runtime.py:1804-1816`).
  Allocation is an in-process counter plus an `exists` probe (`runtime.py:1839-1849`).
- `agent.spawned` is written to the **parent** log before the child runtime
  exists (`agents/runner.py:1654-1672`, factory at `:1915`). The child row
  becomes durable only at its first user message (draft rule above).
- Child events are appended to the child log and relayed to the parent with an
  `agent` block. Lifecycle, context, permission, input and presence kinds are
  not relayed (`_CHILD_RELAY_SUPPRESS`, `runtime.py:1882-1904`). **Child-log
  append failures are swallowed** (`runtime.py:1936-1939`), so a child log can
  miss events that the parent relay has.
- **Recovery gap (verified by reading, not by test):**
  `_recover_interrupted_locked` writes `agent.completed{status: interrupted}`
  only for children that had an `external.started` (`session.py:1585-1612`).
  A native child whose parent crashed keeps an open `agent.spawned` in the
  parent log. Its own log keeps an open `turn.started` until someone reopens it.
  `docs/sessions.md:120-121` says unclosed child-task rows are closed; that is
  only true for external workers.
- **Doc drift:** `docs/agents.md:152-154` says recovery of in-flight external
  runs is "not implemented", but `session.py:1585-1612` implements it.
  `docs/agents.md:478-479` still mentions a wall-clock budget for children.
- A failed child returns a deterministic handoff with no model call
  (`agents/handoff.py`, `runtime.py:2506-2507`, `docs/agents.md:476-491`).

### 3.3 Compaction

- Trigger at `context.compact_at_fraction = 0.85` (`nexus/config/schema.py:603`),
  applied by shrinking the history budget to the trigger
  (`context/manager.py:1558-1590`). After compacting, the context sits right at
  85 %, so the next turn compacts again. There is no hysteresis.
- No summarizer is injected, so `hybrid` = evict tool results, then drop oldest
  (`docs/context.md:101-105`). The summary path exists and is durable:
  `append_summary` (`context/manager.py:1803-1880`), reuse when a record covers
  the dropped prefix, extension otherwise, digest reuse (`:1886-1941`).
- The loop assembles at the top of each iteration, after the previous tool
  results are persisted (`docs/loop.md:20-42`). That point has no unpaired
  `ToolUse`.
- No provider-overflow retry: `docs/loop.md:91` covers only the estimate-based
  path.

### 3.4 Submissions, recovery, replay

- `SessionStart`/`SessionEnqueue` have no request or idempotency key
  (`host/protocol.py:252-266`). A client that retries after a lost reply sends
  the prompt twice.
- Crash recovery closes a turn `interrupted` and appends error results for
  unresolved tool calls. It executes nothing (`session.py:1504-1658`).
- Queued input is durable and rehydrated (`docs/sessions.md:96-108`).
- The reducer turns **unknown event kinds into `DiagnosticView` rows** with the
  full data (`view/reduce.py:1783-1806`, `:228-236`). Revision 1 assumed unknown
  kinds are ignored; they are not. Every new kind needs a handler or an explicit
  no-op.
- Subscriptions catch up from `from_seq` and then follow, gap-free
  (`session.py:1178-1190`). `PROTOCOL_VERSION = 3`; a mismatched client is
  rejected (`host/protocol.py:42`, `docs/host.md:193-194`).

### 3.5 Dirty worktree (not authored by this plan)

The uncommitted changes (`git status`: `docs/agents.md`, `docs/config.md`,
`docs/decisions.md`, `docs/loop.md`, `nexus/runtime.py`, both worker modules,
three tests) remove subagent and external-worker deadlines. Root
`max_turn_seconds` defaults to 0 (unlimited) (`docs/loop.md:155-160`). Leftover
deadline text remains (`docs/agents.md:85-86`, `:117-118`, `:125`, `:191-192`,
`:478-479`; `external.started.timeout_seconds`, `docs/events-and-view.md:70`).
**Not verified** whether those are stale or a residual per-process bound. This
plan assumes there is no deadline. Liveness relies on cancellation, process exit,
and the visible `idle_seconds` signal (§5.6).

---

## 4. What we take from Pi durable, and what we do not

Source: `references/pi` (clone of `https://github.com/badlogic/pi-mono`, commit
`c268154ca1171c2f9eefe75d3d7372a68aada2d5`; the root agent verified that
`earendil-works/pi` HEAD is the same commit), package `packages/durable`
(`@earendil-works/pi-durable` 1.3.0). The README marks it **"Experimental. The
API changes without notice"** (`packages/durable/README.md:3`).

**Article not read directly.** This advisor had no web-fetch tool. The
`earendil.com/posts/pi-durable/` content comes from the root agent's summary,
cross-checked against the repo's README and normative spec. Claims below cite the
repo, not the article.

| Pi idea | Where (verified in repo) | Nexus decision |
| --- | --- | --- |
| Reset/handoff = head marker in the same conversation, older entries kept and scannable | `spec.md:257-306`, `:913-917`; `README.md:443-452` | **Adopt** (§5.2) |
| Compaction cut never splits a call from its results; keep recent tokens verbatim | `spec.md:4277-4294` | **Adopt** the cut rule and a keep-recent window (hysteresis) |
| Block only when the context will not fit; background compaction otherwise | `spec.md:3985-4011`, `:4260-4270` | Defer background; v1 is blocking at the 85 % check |
| Context-overflow error → compact and retry once; a second overflow fails | `spec.md:4045-4054`, `README.md:488` | Stretch item, P5 |
| `requestId` dedupes submissions per conversation | `README.md:115-126`, `spec.md:2358-2361`, `src/harness/submissions.ts:179-183` | **Adopt** as `client_request_id` (§5.4) |
| Intent committed before an effect; on reopen, rerun only if `replay: "safe"`, else an `interrupted` result | `spec.md:1948-1964`, `:3393-3399`, `:4122-4136`; `src/harness/tool.ts:180-202` | Keep Nexus's stricter rule: **never** rerun. No replay flag in v1 |
| Owner (task) vs parent (history) are separate; owner is not access control | `spec.md:247-255` | **Adopt** the separation (§2) |
| Foreground children die with the owner; `background: true` is a boundary | `README.md:576-580`, examples `test/examples/22-subagent-foreground.ts`, `23-subagent-background.ts` | v1: foreground only. Background deferred |
| Subagent restart-safe reuse via owner-task scan plus stable `requestId` | `README.md:544-574` | Deferred: needs tool replay, which Nexus does not do |
| Watch: snapshot, then deltas; a consumer more than 100 frames behind gets a fresh snapshot | `README.md:404`, `:508` | Nexus already has catch-up from `from_seq`. No change |
| Steering lands after the current tool round | `README.md:419`, `spec.md:2379-2382` | Already true in Nexus (`docs/loop.md:99-106`) |
| One process owns a storage; **no cross-process locking** | `README.md:688` | **Do not adopt.** Nexus has many daemons on one DB and per-session `flock` leases (`docs/sessions.md:89-91`). Pi's single mutation line does not transfer |
| Typed documents with `asOf/current/fresh`-style fork policies | `README.md:630-652`, `spec.md:1552-1600` | Deferred. Nexus state is events folded by the reducer |
| Non-goals: no forced termination of non-cooperative code, no CRDT | `spec.md:5355-5369` | Same limits apply to Nexus |

Unverified Pi claims: crash behavior was read from the spec and two source
spots, not run. The example scripts were not executed.

---

## 5. Design

### 5.1 Identity and ownership (schema 6)

All changes are additive columns on `sessions`, so `SessionSummary` builds from
one row and child creation stays one transaction:

```sql
-- schema 6 (Python backfill after the ALTERs, like v4/v5)
ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT '';        -- 'root' | 'child' | 'external'; '' = not backfilled
ALTER TABLE sessions ADD COLUMN root_id TEXT NOT NULL DEFAULT '';     -- top of the ownership tree ('' = self)
ALTER TABLE sessions ADD COLUMN owner_namespace TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN owner_id TEXT NOT NULL DEFAULT '';    -- owning session's stored id
ALTER TABLE sessions ADD COLUMN owner_agent_id TEXT NOT NULL DEFAULT '';  -- logical '<parent>/sub/<n>'
ALTER TABLE sessions ADD COLUMN owner_turn_id TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN owner_spawn_seq INTEGER NOT NULL DEFAULT 0; -- seq of agent.spawned in the owner log
ALTER TABLE sessions ADD COLUMN context_head_seq INTEGER NOT NULL DEFAULT 0; -- seq of the newest head record (§5.2)
ALTER TABLE sessions ADD COLUMN handoff_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX sessions_by_root  ON sessions(project_id, root_id);
CREATE INDEX sessions_by_owner ON sessions(project_id, owner_namespace, owner_id);
```

Why columns, not revision 1's `logical_sessions`/`session_edges` tables:

- A child has exactly one owner, so 1:1 data belongs on the row. Without
  segments there is no logical/physical split.
- `create_from_records` already inserts the row and first records in one
  transaction (`db.py:815`). Owner columns go into that insert, so a durable
  child row without its owner cannot exist.
- `project_id` stays in every key and index. Cross-project ownership cannot be
  stored.

Write path:

1. The runner already forwards `agent.spawned` to the parent before creating
   the child (`runner.py:1654-1672`). Keep that order. Take the parent seq
   returned by the append and pass `(owner ns, owner id, agent id, turn id,
   spawn seq, root id)` into the child's `ChildSpec`.
2. The child facade sets those values on its draft. Promotion
   (`create_from_records`) writes them with the row.
3. External workers get `kind = 'external'`. They have no Nexus handoff (their
   context belongs to Claude/agy).
4. `/sub/` string splits (`runtime.py:2988-2989`, `:6048-6049`) read `root_id`
   from the row. The split stays only as a fallback for rows not backfilled.

Backfill (migration step, Python, batched 500 rows per transaction, idempotent):

- Every `main` row: `kind='root'`, `root_id=id`.
- For `agents` rows: one SQL scan over `records` for
  `json_extract(body,'$.event.type')='agent.spawned'` events per project. Map
  `agent.id` (logical) through `_child_session_id` to the stored child id
  (`runtime.py:1804-1816`), then fill the owner columns. Nested children resolve
  their parent's logical id the same way. `root_id` follows the chain upward,
  bounded by depth `max_depth + 1`.
- Children with no match keep `kind=''`, show under an "Unattached" group, and
  are counted in a `doctor` line. They are never guessed.
- `context_head_seq` backfill = max seq of any `summary` record (0 if none).

### 5.2 Context heads and the handoff

**The record.** A handoff is a `SummaryRecord` (existing kind,
`session/records.py`) with `strategy = "handoff"` plus three fields:
`kept_from_seq` (the first record kept verbatim), `handoff_id`, and
`brief_version`. Adding fields to a tagged msgspec record with defaults keeps
old records decodable. **Not verified**: that `SummaryRecord` decoding tolerates
new optional fields on older Nexus. If it does not, use a new record kind and
bump `SESSION_LOG_VERSION`. Old Nexus is already refused by schema 6, so the
risk is only for exports read by old builds.

**Context derivation (pure function of the log, as in Pi `spec.md:267-302`):**

1. Let `H` be the newest summary record with `seq <= cutoff`. Read it directly
   via `sessions.context_head_seq`, not by scanning.
2. If `H.strategy == "handoff"`, the model history is `[brief(H)]` followed by
   the message records with `seq >= H.kept_from_seq`, in order. Records after
   `H` (steering, queued input, `!` shell context) are always included.
3. Otherwise, the existing summary/compaction rules apply unchanged
   (`context/manager.py:1886-1941`).
4. Pairing: `kept_from_seq` is always the seq of a user message that is not a
   `ToolResult` carrier, or of an assistant message whose results all come after
   it (Pi's candidate rule, `spec.md:4277-4281`). A kept `ToolResult` never loses
   its `ToolUse`.

**Trigger and hysteresis.** Projected tokens are computed at assembly, after
tool-result eviction (`context/manager.py:1558-1590`). When they are still at
or above `compact_at_fraction` (0.85) of the input budget and
`context.on_overflow = "handoff"`:

- choose the newest valid cut so that verbatim kept history ≤
  `handoff_keep_fraction` (default **0.25**) of the input budget. After a
  handoff the context is about 25 % + brief, not 85 %, so the next handoff is
  far away. Revision 1 had no hysteresis.
- at most **one handoff per iteration**. If brief + kept history + required
  parts still do not fit, the existing `ContextOverflow` failure applies
  (`docs/context.md:81-83`). It never loops.

**Brief.** The brief is deterministic, versioned (`brief_version = 1`), and
built from the log and session state, with no model call. It generalizes
`agents/handoff.py`. It is capped at 24 KiB, with the cut announced, and passes
through `redact_secrets`:

- the session's first user message (bounded) and the latest unconsumed user
  input;
- previous brief, if any, carried forward (bounded, oldest sections trimmed
  first);
- todo state (`todowrite`), open shell jobs, live children (owner index), and
  pending queue IDs, so the model knows about them. They are *not* moved,
  because the session does not change;
- files changed since session start (≤ 100), worktree id if any;
- the last N tool calls with errors flagged, and the last assistant message;
- a pointer: "earlier history: this session, seq 1..`kept_from_seq-1`".

A **model-written brief** (`handoff_brief = "summarized"`) is P5. It uses the
session's own model and a 10 % reserve. On failure it falls back to the
deterministic brief and says so in the event. This follows the Anthropic
guidance revision 1 cited (recall first; compaction alone does not pass clear
instructions), which is why the deterministic part stays mandatory.

**Transaction (the only write).** One `append_batch` (§5.3) under one
`BEGIN IMMEDIATE`:

```
records: [ SummaryRecord(strategy=handoff, kept_from_seq, handoff_id, text=brief),
           Event context.handoff {handoff_id, summary_seq, kept_from_seq, through_seq,
                                  tokens_before, tokens_after, brief_chars, method, preview<=2 KiB} ]
row:     context_head_seq = summary seq, handoff_count += 1
```

**Idempotency.** `handoff_id = sha256(session_id, through_seq, kept_from_seq,
brief_version)[:16]`, where `through_seq` is the last record the brief read.
Before writing, assembly checks the current head. If it is a handoff with
`kept_from_seq >=` the chosen cut, it reuses that head and writes nothing (same
pattern as `summary_for`, `session.py:1022-1037`). Only the turn-lease holder
writes heads (assembly runs inside the turn).

**Crash windows** (D = daemon dies):

| Window | Durable state after reopen | Outcome |
| --- | --- | --- |
| D before brief build | no head | the turn is closed `interrupted` by existing recovery. The next turn re-evaluates and writes the head once |
| D during brief build / (P5) summarizer call | no head; summarizer tokens possibly spent | same as above. Spent tokens are lost and not charged (as in Pi, `spec.md:4378-4379`) |
| D during `append_batch` | either both records and the row update, or none (one transaction) | no half-handoff is possible |
| D after commit, before the model request | head present | the next turn assembles from the head. No second handoff (idempotency check) |
| D mid-stream after the handoff | head present; assistant partial not persisted (loop persists the assistant message after streaming, `docs/loop.md:32`) | turn `interrupted`. **Not verified**: whether partial `text` events are persisted per delta |

**What a handoff does not touch:** session id, title, `order_time`, model,
effort, root-agent and skill/MCP selections (all keep working because they are
the same session's events, `docs/sessions.md:129-133`), the queue, permission
grants, live children, todo and shell stores. Revision 1's open question U3
("do grants carry across segments?") disappears.

**Prompt cache.** A handoff invalidates the `history` boundary once. The
`system_tools` prefix is unchanged (`docs/context.md:126-131`). Hysteresis keeps
this rare.

**Children.** A native child at 85 % uses the same mechanism in its own log.
`context.handoff` joins `_CHILD_RELAY_SUPPRESS` (like `context.compacted`,
`runtime.py:1891`). The parent sees the count through the tree
(`handoff_count`), not as a parent event. This resolves revision 1's U1.

### 5.3 `append_batch` (new storage primitive)

`SqliteSessionStore.append_batch(session, builders, *, row_updates=None,
index_rows=())`: assigns consecutive seqs, inserts all records, applies the same
row bookkeeping `_commit_append` does (`last_seq`, `last_activity`, snippets,
`completion_seq`), and applies `row_updates` and `index_rows`, all in one
`BEGIN IMMEDIATE`. On a draft session it promotes through `create_from_records`
(`db.py:809-816`). Single-record `append_*` stays as it is. Users: handoff (§5.2)
and idempotent submissions (§5.4). It never spans two sessions.

### 5.4 Submission idempotency vs side-effect safety

- `SessionStart` and `SessionEnqueue` gain `client_request_id: str = ""`
  (≤ 64 chars, `[A-Za-z0-9_-]`). Clients generate one per composer submit and
  reuse it on retry after a lost reply or reconnect.
- New table (schema 6):

  ```sql
  CREATE TABLE submissions (
    project_id TEXT NOT NULL, namespace TEXT NOT NULL, session_id TEXT NOT NULL,
    request_id TEXT NOT NULL, content_digest TEXT NOT NULL,
    seq INTEGER NOT NULL, turn_id TEXT NOT NULL DEFAULT '', queued_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (project_id, namespace, session_id, request_id),
    FOREIGN KEY (project_id, namespace, session_id)
      REFERENCES sessions(project_id, namespace, id) ON DELETE CASCADE
  );
  ```

  The row is written in the same `append_batch` as the user message or
  `input.queued`. If the same id comes with the same digest, the original
  `SessionStartResult`/`SessionEnqueueResult` is returned and nothing is
  appended. If the digest differs, `RequestIdConflict` is returned. Rows are
  purged with the session. Rows older than 7 days may be swept.
- **Scope of the guarantee:** one admission per `(session, request_id)`. It
  does **not** make effects exactly-once. A turn that crashed after `bash` ran
  is closed `interrupted`. If the user resubmits with a *new* request id, the
  work reruns, side effects included. This matches Pi's own caveat
  (`README.md:115-126`; the root agent's article summary says the same).
- **Tool side effects:** unchanged and stricter than Pi. Nothing reruns after a
  crash. Unresolved calls get error results (`session.py:1635-1658`), and the
  model sees "interrupted; may have partially run". The handoff brief lists
  interrupted calls so the continuation does not assume they succeeded.
- Children use no request ids in v1 (they do not survive restart, §5.5).

### 5.5 Child lifecycle and recovery (foreground only)

Lifetime rule (v1): a child is owned by the parent's tool call. It ends when
the call ends, when the parent turn is cancelled, or on process exit. Nothing
resumes it. The root re-delegates (`docs/agents.md:486-491`, unchanged).

Crash windows:

| Window | Today | Fix (must-have C) |
| --- | --- | --- |
| `agent.spawned` written, child row never durable | parent keeps an open agent row (native) | parent recovery closes **every** open `agent.spawned` (native and external) with `agent.completed{status: interrupted, usage_unknown}` |
| child row durable, child turn open, parent crashed | child log keeps an open `turn.started` forever unless reopened | parent recovery, after closing its rows, opens each owned child via `sessions_by_owner` and runs `_recover_interrupted_locked` under that child's lock. Bounded: ≤ `max_fanout × max_depth` sessions, non-blocking lock; a held lock is skipped (that child is live in another process) |
| child alive in another daemon (shared DB) | n/a | the non-blocking `flock` probe is the liveness truth (`docs/sessions.md:89-91`, `:115-117`). Recovery never writes to a locked session |
| child log append failed but relay succeeded | swallowed (`runtime.py:1936-1939`) | count failures and emit one `error` relay (`child_log_write_failed`). The child log is authoritative for the child transcript, the parent relay for the parent view. **Decision D4** if the user prefers failing the child |

Tree status derivation (host, per node): `running` if the session has a live
lease in this daemon or its lock is held elsewhere. Otherwise the latest
terminal event (`completion_seq`), with `interrupted` when the latest
`turn.started` is newer than `completion_seq` and no lease is held. This never
reports a dead child as running.

Cancellation: `AgentCancel` (exists, `host/protocol.py:322-331`) stays the only
child cancel. A cascade on root cancel already happens through the parent turn's
cancel token (`runner.py:1910-1937`).

### 5.6 Determinism, replay, and multi-surface visibility

- **Replay:** the model context at any seq is a pure function of the log: the
  head at or before that seq, plus the kept records. `context.assembled` already
  records what was sent. A test compares a recomputed context with the logged
  one.
- **Fork:** `SessionFork(at_seq)` copies records ≤ `at_seq` in one transaction
  (`manager.py:714-740`). A head after `at_seq` is not copied, so the fork sees
  the pre-handoff context, and a head before `at_seq` carries over. Fork must
  also copy `context_head_seq`/`handoff_count` (computed from the copied
  prefix). The fork's owner columns stay empty: a fork is a root.
- **Reducer:** register handlers for `context.handoff` (a `HandoffView` divider
  in the transcript: method, tokens before/after, preview) and an explicit no-op
  for any other new kind. Unknown kinds otherwise become diagnostics
  (`view/reduce.py:1806`).
- **Ratatui and desktop:** a one-line divider "Context handed off (≈ X → Y
  tokens) · brief" that expands to the bounded brief (`SessionHandoffRead`).
  Tree rows on the sessions page (§5.7). Both clients in the same phase
  (`docs/ratatui-parity.md`).
- **Reconnect:** no new mechanism. Handoff and tree state come from the log
  plus host commands, which follows the "durable first" rule (`docs/host.md:273-274`).
  The tree has no live push in v1. Clients refresh it on the existing list poll
  and on `agent.*` events of the open session.
- **Observability:** `LogsRead` allow-list gains `context.handoff` (names and
  counts only, never brief text). Tree nodes carry `last_activity` and
  `idle_seconds`, so a stuck child is visible without a deadline. Nothing kills
  it automatically.

### 5.7 Host contract

| Command | Returns | Bounds |
| --- | --- | --- |
| `SessionTree(session)` (new) | nodes `{id, namespace, kind, title, status, depth, owner_agent_id, resolved_model, tier, handoff_count, last_activity, idle_seconds, children[]}` for the root of `session` | ≤ 500 nodes, depth ≤ `max_depth + 1`, `truncated` flag |
| `SessionList` (extended) | adds `kind`, `root_id`, `child_count`, `running_children` | existing; children hidden unless `include_children=true` |
| `SessionHandoffRead(session, handoff_id)` (new) | redacted brief and metadata | ≤ 64 KiB |
| `SessionStart`/`SessionEnqueue` | `client_request_id` (optional); result adds `deduplicated: bool` | — |

- New fields are optional with defaults. **Not verified**: whether the Rust
  clients' decoders reject unknown fields. Check the serde attributes in
  `rust/` before deciding whether `PROTOCOL_VERSION` must bump to 4. If a bump
  is needed, it ships with both clients in one release (the handshake rejects
  mismatches, `docs/host.md:193-194`).
- Hosts and surfaces never read the DB (`docs/host.md:269-270`). The tree query
  lives in `StateDatabase`/`SessionManager` because it crosses the `main` and
  `agents` namespaces of one project.
- Status roll-up for a root's dashboard group: the most urgent of itself and
  its live descendants (Needs input > Working > Idle > Done), with the root's
  own state shown beside it. Roots stay ordered by `order_time`
  (`docs/sessions.md:55-61`); children never reorder roots.

### 5.8 Security and permissions

- No authority change. Owner columns and edges grant nothing (§2 rule 2).
  Handoffs do not touch grants.
- Briefs, previews and `SessionHandoffRead` output pass `redact_secrets` before
  persistence or return, like other handoffs (`docs/agents.md:69-71`).
- Tree reads are workspace-scoped: only this daemon's `project_id`. The
  cross-project index still excludes child namespaces (`docs/sessions.md:75-78`).
- `client_request_id` is not a secret. It is validated for shape and never
  logged with content.
- Budgets: a handoff is not a child and consumes no depth or fan-out. A P5
  summarized brief charges the session's own usage and shows it in the event.

---

## 6. Rollout phases

Each phase can ship and be reverted alone. "Invariant" means a property that
tests enforce from that phase on.

| Phase | Work | Depends on | Invariants introduced | Tests (offline, fakes; `docs/testing.md`) | Out of scope |
| --- | --- | --- | --- | --- | --- |
| **P0** Decisions and measurement | Answer D1–D6 (§8). Settle the residual-deadline question (§3.5). Add a `doctor` line with the record count / bytes of the 10 largest logs. Fix doc drift (§3.2) as a doc task | dirty tree merged | — | — | code changes |
| **P1** Recovery fixes (C) | close native open `agent.spawned`. Recover owned children under their locks. Surface child-log write failures | P0 | **I1** no `agent.spawned` without a terminal after recovery. **I2** recovery never writes to a locked session | `test_session_recovery_children.py`: crash after `agent.spawned` before child row; after child turn start; child live in a second process (lock held) → untouched; idempotent second recovery | tree, schema |
| **P2** Schema 6 + ownership (A) | ALTERs, `submissions` table, backfill, owner write path, `/sub/` split removal | P1 | **I3** every durable child row has owner columns in the same transaction as its creation. **I4** no cross-project owner | `test_session_schema6_migration.py`: v5 fixture with roots, forks, nested children, externals, trash, archive → v6; migration twice = no-op; v5 binary refuses v6; unmatched children stay `kind=''`. `test_session_owner_write.py`: crash between `agent.spawned` and promotion leaves no ownerless row | UI |
| **P3** Tree reads + UI (B, G) | `SessionTree`, `SessionList` fields, reducer no-ops, Ratatui then desktop tree rows, roll-up | P2 | **I5** a dead child is never `running`. **I6** tree reads are bounded | `test_session_tree_host.py`: bounds, truncation, roll-up, cross-namespace, cross-project impossible, lock-held child shows running. Ratatui snapshot tests; desktop parity checklist | handoff |
| **P4** `append_batch` + idempotent submissions (E, F) | primitive; request-id path in facade; clients generate ids | P2 (table) | **I7** one admission per `(session, request_id)`. **I8** a batch is all-or-nothing | `test_session_append_batch.py`: injected failure mid-batch → nothing visible; draft promotion. `test_host_submission_idempotency.py`: duplicate → same `turn_id`/`queued_id`, one user message; conflicting digest → error; two processes racing the same id → one row | child request ids |
| **P5** Handoff (D, G) | brief builder, head record, assembly from head, hysteresis, `context.handoff`, divider, `SessionHandoffRead`, `on_overflow` flag **default `compact`** | P4 | **I9** at most one head written per iteration. **I10** context is a pure function of the log. **I11** no `ToolResult` without its `ToolUse` in assembled context | `test_session_handoff.py` + failure matrix §7. Stretch: summarized brief; provider-overflow → handoff → one retry | segments, background compaction |
| **P6** Soak and default flip | run with `on_overflow = "handoff"` on maintainers' machines. Measure handoff count, brief size, re-handoff interval, assembly latency vs log size | P5, P3 | — | replay benchmark: same tasks with and without handoff (completion rate). **Not measured yet** | — |
| **P7** Bounded assembly reads (conditional) | only if P0/P6 show slow assembly: read records from `context_head_seq`/`kept_from_seq` by seq range instead of `read()` | P5 | **I12** assembly reads O(context), not O(log) | latency test on a synthetic 100k-record log | segments |

Cross-plan dependencies: plan 02 needs P2 (ownership) and P3 (tree reads).
Plan 03 needs P2 (stable ids and owners) and P5 (heads, so "earlier history" is
addressable by seq).

---

## 7. Acceptance tests and failure injection

Failure injection uses a fake store that raises at a named point
(`before_commit`, `mid_batch`, `after_commit`), plus a subprocess daemon killed
with `SIGKILL` for the end-to-end cases.

| # | Injected failure | Expected |
| --- | --- | --- |
| F1 | kill after `agent.spawned`, before child promotion | reopen: parent has `agent.completed{interrupted}`; no child row; tree shows nothing running |
| F2 | kill during a native child's model stream | reopen: parent row closed; child log has `turn.failed{interrupted}` after parent recovery; no tool reran |
| F3 | second daemon holds the child's lock | first daemon's recovery skips that child; tree shows `running` |
| F4 | kill during handoff brief build | no head; next turn writes exactly one head |
| F5 | raise in `append_batch` between the summary record and the event | neither visible; `context_head_seq` unchanged |
| F6 | kill after head commit, before the request | reopen + next turn: no second head; context starts at the brief |
| F7 | handoff while steering input and a `!` shell context arrive | both appear after the head and reach the next request |
| F8 | handoff with a pending tool pair at the chosen cut | cut moves to the previous valid candidate; no orphan `ToolResult` |
| F9 | brief + kept history still over budget | one `ContextOverflow`, no handoff loop |
| F10 | client retries `SessionStart` with the same `client_request_id` after a dropped reply | one user message, one turn, same `turn_id` returned |
| F11 | fork at a seq before / after a head | before: pre-handoff context; after: the fork assembles from the head |
| F12 | old log with unknown future event kind | reducer folds without unbounded diagnostics growth (explicit no-op list) |
| F13 | v5 DB opened by v6, then by v5 | v6 migrates once; v5 refuses with the "newer than this Nexus" message (`db.py:225-229`) |

Acceptance checklist:

- [ ] A root that spawns 3 children (one external) shows one tree of 4 nodes in
      Ratatui and desktop, with tier and resolved model on each child.
- [ ] Killing the daemon at any point in F1–F6 never shows a dead child as
      running and never yields two heads.
- [ ] A session pushed past 85 % continues with the task, todo list, queued
      input and live children intact, and the transcript shows one divider.
- [ ] After a handoff the context is ≤ ~25 % + brief, and the next handoff is
      not triggered on the next turn.
- [ ] No child gains a tool or permission through ownership or handoff.
- [ ] Retried submissions never run twice; a new request id after a crash
      reruns, and the transcript says the earlier attempt was interrupted.
- [ ] Old protocol-compatible clients work; web is unaffected.

---

## 8. Decisions

Recommended (adopt unless the user objects):

| Decision | Recommendation | Deciding reason |
| --- | --- | --- |
| Handoff representation | head record in the same session | atomic in one transaction, no identity change, reuses summary machinery; Pi does the same |
| Ownership storage | columns on `sessions` | 1:1 relation, written in the existing creation transaction |
| Fork lineage | keep `parent_id`/`fork_seq`; not an owner | avoids overloading "parent" |
| Brief | deterministic always; summarized optional in P5 | works when the model call fails; matches `agents/handoff.py` |
| Tool replay | none | Nexus has no idempotency for external effects; an `interrupted` result is honest |
| Children across restart | interrupted, root re-delegates | unchanged product rule; durable children need replay first |
| `on_overflow` default | `compact` until P6, then `handoff` | rollback is a config flip |

**Open user decisions:**

- **D1** Same-session handoff (recommended) vs a new linked dashboard row per
  continuation (revision 1). Choose the latter only if users need each context
  window as its own row. Cost: segments, head-following subscriptions,
  cross-session transactions (§9).
- **D2** `handoff_keep_fraction`: 0.25 recommended. Higher keeps more verbatim
  context but hands off more often.
- **D3** Trash a root with children: cascade (recommended; children are hidden
  with the root and restored with it) or orphan. Trash still never cancels a
  running turn (`docs/sessions.md:154`).
- **D4** Child-log write failure: surface and continue (recommended) or fail
  the child.
- **D5** Should `client_request_id` also cover `SessionShell` and queue edits?
  Recommended: not in v1.
- **D6** Is the provider-overflow retry (P5 stretch) wanted in v1?

Resolved by this revision: revision 1's U1 (child handoff stays in the child),
U2 (external workers are tree nodes with no Nexus handoff), U3 (grants are
unaffected; same session), U4 (now D3).

---

## 9. Deferred work and what would trigger it

| Deferred item | Trigger to revisit | Prerequisite |
| --- | --- | --- |
| Physical segments | P7 bounded reads still too slow, or D1 chosen the other way | `append_batch` across sessions, head-following subscribe |
| Background compaction (non-blocking brief) | P5 summarized brief makes turns visibly stall | a summary task that places its result at the next boundary, with a stale rule for older cuts (Pi `spec.md:2389-2406`) |
| Turn/stream resume after crash | users lose long turns to restarts | persisted partials (Pi commits every 100 ms, `README.md:406`) and a per-call intent record |
| Tool replay (`replay: safe`) | a concrete tool with real idempotency (e.g. read-only) needs resume | per-call intent record, a policy check at reopen time (Pi `spec.md:3393-3396`) |
| Durable/background subagents | plan 02 manager wants long-lived workers | tool replay, stable child request ids (Pi `README.md:544-585`) |
| General task engine | two or more of the above are needed together | — |
| Typed documents with fork policies | state that does not fit events | — |
| `attempts` table | analytics on retries/fallbacks | — |

---

## 10. Impact on plans 02 and 03 (not edited here)

Plans 02 and 03 were written against revision 1. Mapping:

| Revision-1 term | This revision |
| --- | --- |
| logical session id | the session id (`sessions` PK) |
| `logical_sessions.kind` | `sessions.kind` (plan 02 adds `'manager'`) |
| `session_edges` kind `spawned` | owner columns on the child row |
| `session_edges` kind `managed` (plan 02) | not provided here. Plan 02 should add `launched_by_*` columns or its own table; flagged for plan 02 |
| `session_edges` kind `forked` | `sessions.parent_id`/`fork_seq` (unchanged) |
| segments / `SessionSegments` (plan 03 reads) | heads within one log; plan 03 reads by seq range |
| plan 01 M2 / M3 / M4 / M7 | P2 / P2 / P3 / P5 |

Plan 03's `authority.context_read` on edges has no column here. Plan 03 must
define where it lives. A follow-up edit to plans 02 and 03 is needed. It was not
done because this task was limited to plan 01.

---

## 11. References

Pi, commit `c268154ca1171c2f9eefe75d3d7372a68aada2d5`, under
`references/pi/packages/durable/` (read 2026-10-17):

- `README.md:3` (experimental), `:115-126` (requestId), `:181` (tool replay),
  `:404-406` (watch, partial cadence), `:443-452` (reset/handoff), `:454-492`
  (compaction), `:540-585` (abort, subagents), `:688` (one process owns storage).
- `docs/spec.md:41-78` (invariants), `:247-255` (parent vs owner), `:257-306`
  (context derivation via head), `:913-917` (reset), `:1948-1964` (effect
  sandwich), `:2285-2433` (submissions and inbox), `:3378-3399` (handoff control,
  replay policy), `:3927-4096` (generation, thresholds, overflow retry),
  `:4098-4156` (tool task), `:4227-4398` (compaction), `:5355-5369` (non-goals).
- `src/harness/submissions.ts:179-183`, `src/harness/tool.ts:180-202`
  (implementation spot-checks).
- Examples (not run): `test/examples/22-subagent-foreground.ts`,
  `23-subagent-background.ts`, `25-compaction.ts`.
- Article `https://earendil.com/posts/pi-durable/`: **not read by this advisor**
  (no fetch tool). Used only through the root agent's summary.

Kept from revision 1 (search-result pointers, not re-read):

- Anthropic, "Effective context engineering for AI agents":
  https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic, "Effective harnesses for long-running agents":
  https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- arXiv 2605.08580, 2606.23525, 2607.05378 (compaction papers; pointers only).

Nexus evidence: paths cited inline, read 2026-10-17 on the dirty worktree. Not
run: no tests, no live daemon, no database measurements.
