# Plan 01: Unified sessions, session trees, and context handoffs

**Current completion status (latest; read this first):** P0–P6 are **complete
under an explicit maintainer waiver** of the real-usage soak and the
real-provider quality/completion gate (working tree, uncommitted). Neither was
run: there is no real-usage or provider-quality evidence that handoff is as good
as compaction, and the waiver covers only that soak/quality gate, nothing
broader. `context.on_overflow` defaults to `handoff` for schema-v2 configs by the
maintainer's decision ("Waive soak; make handoff the default now"); a legacy
config with no v2 layer (v1) still resolves to `compact`, and explicit `compact`
remains a supported opt-out. P7 (bounded assembly reads) is **deferred and
optional**, not part of this completion. Details: "Default flip under soak
waiver" and "Final completion status" at the end of this file.

Many headings and ledgers below say "not complete", "NOT yet run", or "default
stays `compact`". They are **historical chronology, superseded** by the status
above and kept so the history is not lost; where they conflict, the final
sections at the end, then `docs/`, are the source of truth. The design text is
the original proposal (advisor revision 2, 2026-10-17).

Intentional departures from the original design (tracked, not defects):

- No `deduplicated: bool` result field (§5.7): a request-id retry returns the
  original receipt/identities (`fresh=False`); docs do not promise the flag.
- Default is `handoff` only for v2 configs; legacy no-config/v1 stays `compact`.
- A retried `SessionStart` returns its historical `turn_id`, but an interrupted
  turn is not resumable; queued work runs on a fresh explicit turn.
- Crash coverage does not include killing an in-flight external shell/tool; the
  T3 scenario is a completed `bash` result that is not re-executed.

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
  (which handoffs encourage) make every turn O(log). *(2026-10-18: one read-only
  snapshot of the maintainer database now exists; see the consolidated ledger.
  It is not soak evidence.)*

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

### 5.1 Identity and ownership (schema 6; allocation reservations schema 7)

Ownership changes are additive columns on `sessions`, so `SessionSummary`
builds from one row and child promotion stays one transaction. Schema 7 adds a
separate `child_allocations` reservation table (unique project/parent/ordinal,
logical id and stored id), allocated under `BEGIN IMMEDIATE`. Reservations
survive abandoned drafts and are not ownership or runnable session rows:

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

- New fields are optional with defaults. P2 inspection found no
  `deny_unknown_fields` in the native clients' bridge/session structs; additive
  SessionList fields are accepted without a protocol bump. P3 must still wire
  the new tree command into both clients together with their UI parity tests.
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

### P1 implementation progress

- **Operational convention:** Update this progress ledger after each completed
  slice with changes, test verification (distinguishing worker-reported from
  parent/independent runs), and next steps; keep entries concise to avoid
  bloating the plan.
- **P1a done (scoped verification): parent-log recovery.**
  - *Implementation:* In `nexus/session/session.py`, recovery under the exclusive
    lease tracks open `agent.spawned` by ID, preserves external-worker recovery,
    and closes remaining native/nested children in reverse spawn sequence before
    `turn.failed`. Metadata (including `parent_agent_id`), interruption errors,
    and unknown usage are preserved; already-completed children are not
    duplicated. Tests added in `tests/test_session_manager.py` covering
    pre-promotion crashes, nested-first replay routing, mixed native/external
    children, repeat reopen, and live `begin_turn` lease held across a second
    manager open. Docs updated in `docs/sessions.md` and `docs/agents.md`.
  - *Independent verification:* Parent ran targeted suite `.venv/bin/python -m pytest -q tests/test_session_manager.py tests/test_view_external.py tests/test_ratatui_projection.py tests/test_docs.py` (172 passed in 1.10s); `ruff check nexus/session/session.py tests/test_session_manager.py` passed (global ruff, not `.venv/bin/ruff`); `git diff --check` passed.
  - *Full suite status:* Full suite `.venv/bin/python -m pytest -q` redirected to `/tmp/nexus-unified-p1a-pytest.log` hit an explicit 200s timeout (truncated at 58% in log tail); full suite is NOT verified by parent. Log tail shows execution stopped at 58% with two earlier failures (including known baseline frozen `AgentSection` schema failure on dirty worktree).
- **P1b done (worker-verified): owned child-log recovery.** Parent recovery
  traverses project-scoped proven ownership rows, repairing descendant turns
  and dangling tool uses under each child's non-blocking exclusive lease.
  A live owner or child prevents writes to its protected subtree; missing and
  unknown legacy logs are not fabricated. A visited set and 256-log safety
  bound prevent malformed graphs from unbounded recursion. Tests cover nested
  repair, repeat recovery, unknown logs, cycles, and live parent/child leases.
  This uses the P2 storage prerequisite below; it does not claim that every
  unknown legacy log can be recovered.
- **P1c done (worker-verified): surface child-log persistence failures.**
  Child-log append failures still attempt the owner relay and a stable,
  non-sensitive `child_log_persist_failed` diagnostic. `todo.updated` failures
  propagate through `_ChildEventSink`, `_ChildRelay` and `_forward`, including
  nested relays; optional telemetry remains best-effort. Docs travel with this
  distinction. Verification: `pytest -q tests/test_child_persistence.py
  tests/test_builtin_todo.py tests/test_subagent_runner.py` — **103 passed**;
  `ruff check nexus/runtime.py nexus/agents/runner.py
  tests/test_child_persistence.py` — passed. D4 is implemented.

**P1 complete (worker-verified)** for the recovery/write scope above. Combined
verification: `.venv/bin/python -m pytest -q tests/test_session_ownership.py
tests/test_session_db.py tests/test_session_manager.py tests/test_runtime.py
tests/test_child_persistence.py tests/test_subagent_runner.py tests/test_builtin_todo.py
tests/test_view_external.py tests/test_ratatui_projection.py tests/test_docs.py
tests/test_agents_handoff.py tests/test_agent_attention.py tests/test_host_agent_cancel.py
tests/test_host_agent_metadata.py tests/test_subagent_worktrees.py
tests/test_claude_code_routing.py -k 'not test_external_child_gets_no_wall_clock_even_when_parent_has_one'`
— **410 passed, 1 deselected (18.05s)**. Relevant eight-file `ruff check` and
`git diff --check` passed. The deselected known baseline failure was separately
reproduced: mutation of immutable `AgentSection` before child execution.

**P2 complete (worker-verified).** Schema 6 ownership and schema 7 durable
allocation reservations now cover native/external promotion and nested physical
owner/root/owner-local turn+spawn metadata. Allocation is serialized under
`BEGIN IMMEDIATE`, skips existing/trash identities, and survives abandoned
reservations/restart. Three spawned processes on separate SQLite connections
reserve 36 distinct ids concurrently in the acceptance test. Legacy inference
examines a bounded evidence set from spawn/completion/request/turn records;
conflicts remain explicitly unknown. Logical/stored identity resolution replaces
remaining question/cancellation/resume/worktree slug routing. Fork ancestry is
not task ownership; no execution authority is granted by metadata.

As specifically requested for this P2 batch, the **read-only P3 host foundation**
is also implemented: bounded `SessionTree`, root-default extended `SessionList`,
redacted identity/detail projection, lock-derived liveness, explicit unattached
children and Doctor count. No terminal/desktop UI, reducer or web expansion was
made. Tree reads do not open/recover handles, create locks or touch project
activity. Root lookup/recursion and response sizes are capped; truncation and
unknowns are explicit. Summary running counts are conservative over the newest
500 child rows; tree tails read at most 64 records per node.

**Verification (worker):** `PYTHONPATH=. uv run pytest -q
 tests/test_session_ownership.py tests/test_child_persistence.py
 tests/test_session_tree.py tests/test_session_manager.py tests/test_runtime.py
 tests/test_subagent_runner.py tests/test_host_facade.py tests/test_host_diagnostics.py
 tests/test_host_agent_cancel.py tests/test_tools_manager.py tests/test_session_db.py
 tests/test_docs.py tests/test_claude_code_routing.py tests/test_subagent_worktrees.py
 tests/test_host_worktrees.py tests/test_core_loop.py tests/test_question_tool.py
 tests/test_questions_broker.py
 -k 'not test_external_child_gets_no_wall_clock_even_when_parent_has_one'`
— **504 passed, 1 deselected (31.97s)**. Relevant implementation/test `ruff check`
and `git diff --check` passed. The explicitly excluded frozen `AgentSection`
test was reproduced unchanged during the earlier focused external run. Tests
need `PYTHONPATH=.` for the existing `tests.live_fakes` imports; no import fix was
made. Parent independent P1/prerequisite verification remains recorded above.

**Next task brief — P3 remaining UI/reducer:** read the native parity docs and
load the nexus-ratatui skill before terminal edits. Wire the existing host tree
contract to Ratatui first, then desktop; add reducer no-ops and tree refresh on
existing list polls/agent events, selected-row transcript/request/context actions,
explicit unknown/read-only rows, and parity snapshots. Keep ownership separate
from fork ancestry and do not add web features. The next full phases remain
P4 atomic append/submission dedup, P5 handoff context cutover, P6 soak/default
flip, and conditional P7 bounded assembly. Submissions and handoff watermarks
remain schema-only (apart from migration's existing summary watermark); no
handoff execution or admission-dedup invariant is claimed.

| Phase | Work | Depends on | Invariants introduced | Tests (offline, fakes; `docs/testing.md`) | Out of scope |
| --- | --- | --- | --- | --- | --- |
| **P0** Decisions and measurement | Answer D1–D6 (§8). Settle the residual-deadline question (§3.5). Add a `doctor` line with the record count / bytes of the 10 largest logs (**implemented**, `host_support/doctor.py`). Fix doc drift (§3.2) as a doc task | dirty tree merged | — | — | code changes |
| **P1** Recovery fixes (C) | close native open `agent.spawned`. Recover owned children under their locks. Surface child-log write failures | P0 | **I1** no `agent.spawned` without a terminal after recovery. **I2** recovery never writes to a locked session | `test_session_recovery_children.py`: crash after `agent.spawned` before child row; after child turn start; child live in a second process (lock held) → untouched; idempotent second recovery | tree, schema |
| **P2** Schema 6/7 + ownership (A), complete | ALTERs, `submissions` table, backfill, owner write path, `/sub/` split removal | P1 | **I3** every durable child row has owner columns in the same transaction as its creation. **I4** no cross-project owner | `test_session_schema6_migration.py`: v5 fixture with roots, forks, nested children, externals, trash, archive → v6; migration twice = no-op; v5 binary refuses v6; unmatched children stay `kind=''`. `test_session_owner_write.py`: crash between `agent.spawned` and promotion leaves no ownerless row | UI |
| **P3** Tree reads + UI (B, G), implemented (native visual check blocked; see P3 log below) | `SessionTree`, `SessionList` fields, reducer no-ops, Ratatui then desktop tree rows, roll-up | P2 | **I5** a dead child is never `running`. **I6** tree reads are bounded | `test_session_tree_host.py`: bounds, truncation, roll-up, cross-namespace, cross-project impossible, lock-held child shows running. Ratatui snapshot tests; desktop parity checklist | handoff |
| **P4** `append_batch` + idempotent submissions (E, F), complete | primitive; request-id path in facade; clients generate ids | P2 (table) | **I7** one admission per `(session, request_id)`. **I8** a batch is all-or-nothing | `test_session_append_batch.py`: injected failure mid-batch → nothing visible; draft promotion. `test_host_submission_idempotency.py`: duplicate → same `turn_id`/`queued_id`, one user message; conflicting digest → error; two processes racing the same id → one row | child request ids |
| **P5** Handoff (D, G) | brief builder, head record, assembly from head, hysteresis, `context.handoff`, divider, `SessionHandoffRead`, `on_overflow` flag **default `compact`** | P4 | **I9** at most one head written per iteration. **I10** context is a pure function of the log. **I11** no `ToolResult` without its `ToolUse` in assembled context | `test_session_handoff.py` + failure matrix §7. Stretch: summarized brief; provider-overflow → handoff → one retry | segments, background compaction |
| **P6** Soak and default flip | run with `on_overflow = "handoff"` on maintainers' machines. Measure handoff count, brief size, re-handoff interval, assembly latency vs log size | P5, P3 | — | replay benchmark: same tasks with and without handoff (completion rate). **Synthetic latency/brief/interval measured; completion rate and real-session evidence not measured (see consolidated ledger)** | — |
| **P7** Bounded assembly reads (conditional; **deferred**, see ledger) | only if P0/P6 show slow assembly: read records from `context_head_seq`/`kept_from_seq` by seq range instead of `read()` | P5 | **I12** assembly reads O(context), not O(log) | latency test on a synthetic 100k-record log | segments |

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


### P3 implementation and verification (2026-10-17)

- Reducer slice: `todo.updated` and `handoff.created/accepted/dismissed` are
  metadata no-ops, including agent-mirrored payloads; no blank turn/diagnostic.
- Host slice: P2 `SessionTree` now carries bounded nullable task/description,
  model/tier, current tool, elapsed running time, latest model usage, optional
  cost, worktree/branch, result/error. `SessionInspect`/result and client methods
  read a bounded durable tail and last persisted request without opening handles
  or recovering/locking a child. Parent `AgentTranscript` deliberately keeps its
  fork-bounded semantics; tree inspection is the separate durable endpoint.
- Ratatui slice: Sessions `t` opens ownership (not ancestry); node facts,
  transcript/request pages, Refresh, Back and root opening all use host operations.
  Visible trees refresh on existing list polls and agent lifecycle events.
  Children are read-only and have no send/resume/fork. Refresh does not grow the
  Back stack. Running descendant/handoff counts are carried in root rows.
- Desktop slice: sidebar Tree and Sessions root actions use the same shared
  ownership menus/detail panels and host contract. GPUI Sessions uses typed menus
  because its wire does not consume the Ratatui-only dashboard field. Native
  Rust entry point and Python navigation are implemented and tested; no web work.
- Docs/module map updated with both new Python modules and host/UI contracts.
- Verification: focused Python ownership/session/reducer/host/docs suite: **369
  passed**. Targeted PTY/session-dashboard/desktop-launch/wire suite excluding
  known unrelated sidebar baseline assertion: **49 passed**. New PTY covers `t`
  and indented child selection; restart tests verify durable transcript/request,
  no handles, read-only child navigation, model/task/usage bounds, truncation.
- Rust Ratatui: `cargo check`, `cargo build`, `cargo test` pass (**405 tests**).
  Desktop `cargo check` attempted: fails on pre-existing missing
  `Snapshot.sessions_sidebar` and `Snapshot.details_sidebar` in main.rs; no
  unrelated baseline edit made. Native app launch/screenshot review therefore
  **not completed**; no screenshot or visual-polish claim.
- Broader diagnostics: desktop wire baseline test
  `test_layout_preferences_do_not_hide_the_desktop_sidebar_lists` fails on the
  existing hidden-sidebar snapshot. Full generic Ratatui PTY run also has three
  unrelated fixed-coordinate disclosure/copy failures (53 passed / 3 failed on
  rerun); ownership-specific PTYs pass. Runtime/phase6 integration: **97 passed,
  1 failed**, existing nested logical-vs-physical child-id assertion
  (`test_nested_agents_keep_immediate_parent_and_root_turn`), outside P3.
  Frozen AgentSection baseline left untouched.
- These are verification limitations, not omitted P3 implementation. P4 is next;
  do not treat desktop visual review as performed.

**Next P4 brief:** implement atomic `append_batch` and request-id admission
idempotency using the existing submissions table. Add rollback/draft-promotion
and duplicate/conflicting-digest/two-process race tests; clients generate ids.
Preserve P1–P3 changes. Handoff context-head cutover belongs to P5, not P4.
No P4 code was started here.

### P4 in progress

- Storage sub-slice: added a bounded `append_batch`, common per-record bookkeeping,
  transaction-local draft promotion, submission conflict lookup, and rollback on
  failed COMMIT. Existing SQLite and supervisor tests passed (initial focused run).
- Admission/client integration and dedicated rollback/concurrency verification are
  in progress; P4 is not yet declared complete.

### P4 verified admission/client sub-slice

- Optional request ids wired through protocol, direct facade start/enqueue,
  supervisor preassigned turn ids, Python client/shared stream helper,
  native/desktop shared composer, existing web composer, and gateway validation.
- `input.queued` + original ids/digest commit atomically; retries run lookup before
  resolving/releasing attachments and do not schedule/cancel again. Result depth
  is read from the original admission event. Empty ids preserve legacy behavior.
- Dedicated tests cover independent connections and spawned processes racing the
  same id, conflicting digests, consecutive batches, draft/index/record/COMMIT
  rollback, after-commit visibility, restart/lost attachment reply, concurrent
  protocol clients, invalid ids and native composer retry ids.
- Focused integrated suite: **436 passed** before final small validation/docs
  additions; dedicated storage/host additions after those changes: **73 passed**.
  Targeted Ruff: **all checks passed**. Final verification follows below.


### P4 complete (worker-verified)

- Final integrated focused command (`.venv/bin/python -m pytest -q`) over
  submission-idempotency, SQLite, supervisor/facade, concurrency/send/surface,
  controller parking, native actions/dashboard/workflows/integration, client,
  UDS/web/gateway transports, web composer, docs, snapshots/summaries/locks,
  manager/ownership/tree: **607 passed (6.71s)**. Targeted 15-file Ruff passed;
  `node --check nexus/ui/web/js/app.js` and `git diff --check` passed.
- Supplemental wider run initially found a missing module-map row (fixed and
  docs suite rerun green) plus `test_session_summary_is_transport_neutral`'s
  pre-P4 exact-field assertion. Reproduced that same extra ownership-field failure
  in an isolated pre-P4 storage/session copy retaining P1–P3 manager/schema;
  left unrelated assertion unchanged. Initial `uv run pytest` had five
  `tests.live_fakes` import failures; `python -m pytest` resolves repository
  imports and the full facade suite passes. New signature assertions in native
  tests were updated for request ids; no implementation regressions hidden.
- No generic engine, tool replay, schema bump, web feature expansion, commit or
  push. Frozen AgentSection, desktop missing-sidebar compile fields and PTY
  coordinate baselines untouched. Exactly-once external effects are not claimed.

**Next P5 brief:** implement deterministic bounded handoff brief building,
`SummaryRecord` head plus `context.handoff` and row head/count in one batch;
assemble from that head with tool-pair-safe cuts and hysteresis. Add handoff
read/divider and overflow configuration defaulting to `compact`, relay
suppression and failure-matrix tests. Keep session identity, queue, permission
and ownership semantics unchanged; no physical segments, background compaction,
provider retry expansion or P6 default flip in this slice.

### P5 verified core/native slice (worker verification)

- Added versioned deterministic/redacted briefs, optional foreground session-model
  recall with bounded output/no tools/fallback, pair-safe active-context reads,
  atomic summary+marker+head/count+optional continuation/request identity, and
  indexed current-head reads. Full history/export stays readable; fork inherits
  the copied head without changing handoff identity semantics. Read-COMMIT
  failure now rolls back as write-COMMIT already did.
- Host/client codec, read-only brief endpoint, known reducer event, child relay
  suppression, constant LogsRead entry and shared native `/handoff [read]` plus
  stable transcript divider are wired; desktop uses the same NativeShell bridge.
  No web features, generic engine, background compactor or commit/push added.
- Integrated focused test command over handoff, host, logs, context/budget/cache/
  refinement, runtime, native actions/projection/integration, submission identity,
  SQLite/snapshot/summary/manager/ownership/tree and docs: **558 passed**.
  Targeted 25-file Ruff and `git diff --check` passed. Additional final read-by-ID
  contract tests/verification follow; do not interpret this as P6 acceptance.
- `context.on_overflow` remains **compact**. P6 remains the broad acceptance,
  ownership exact-match/nested-ID expectation cleanup and deliberate default flip.
  P7 remains conditional migration/performance work, not implied by P5. Known
  original frozen AgentSection failure remains outside this slice.

### P5 complete (worker-verified)

- Final integrated focused run: **558 passed (5.55s)**. Final supplemental
  handoff/read-by-ID/docs run: **56 passed (0.72s)**. Targeted Ruff: all checks
  passed; `git diff --check` passed. Protocol exhaustive command/result tests
  and native divider tests were updated, not dismissed as baseline failures.
- Next P6 brief: independently rerun the focused matrix and broader acceptance,
  reconcile P3 ownership exact-field/nested-ID assertions if wider suites still
  expose them, verify native end-to-end/desktop contracts on available tooling,
  then deliberately flip `context.on_overflow` from compact only when the P6
  checklist is satisfied. P7 remains conditional; no migration/default flip or
  provider-overflow retry stretch implementation is claimed by P5.

### P6 verification in progress (not accepted)

- Broad repository run: **6584 passed, 336 skipped, 7 failed, 6 deselected**
  (9m03s). Four failures were reproduced as pre-existing/baseline failures by
  the worker: frozen `AgentSection` mutation, exact context-extension ownership
  expectation, desktop shared-shortcut parity, and first-line ellipsis wording.
  Three native PTY failures remain unresolved; the independent PTY run also had
  3 failures and 6 passes. Do not count them as baseline without reproducing on
  baseline or a clear unrelated cause.
- Implemented P6-scope fixes already present in the working tree include native
  nested logical-id compatibility, post-commit publisher exception isolation,
  and explicit dropping (rather than rerunning) stranded startup admissions,
  preserving original retry receipts. Desktop (123 tests) and TUI (264 tests)
  passed; bridge missing-Snapshot-field baseline fix was verified. Full-suite
  evidence does not establish the P6 replay/completion-rate and latency/size
  measurements in the phase table. Startup drop behavior and retry receipts
  require independent acceptance review.
- `context.on_overflow` remains `compact`. P6 is **not complete**: PTY failures,
  acceptance measurements, and policy review are outstanding. P7 is conditional
  on actual slow-assembly evidence; no bounded-read migration is claimed.
- Next: resolve/reproduce all seven failures against baseline, run the targeted
  Ruff checks successfully (last recorded lint run found undefined `SessionBusy`
  in `nexus/host/supervisor.py`), complete PTY review and P6 measurements, and
  only then decide whether the default flip is justified. No commit or push.

### P6 latest verified status (ledger update)

- `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_session_handoff.py tests/test_session_tree.py tests/test_submission_idempotency.py tests/test_ratatui_pty_sessions.py`: **42 passed**. Targeted Ruff on context/session/host/host_support/runtime/view plus the new tests passed; undefined `SessionBusy` is no longer present. A subsequent standalone `tests/test_ratatui_pty_sessions.py` run exited 0.
- `tests/test_ratatui_pty.py`: **3 failed, 4 passed (6.41s)**. Failures: `test_local_disclosure_without_python_reply`, `test_tool_single_click_selects_double_click_toggles_locally`, and `test_hover_copy_buttons_on_user_cards_and_code_fences`. Baseline attribution for these failures is **not established**; do not characterize them as regressions or unrelated.
- P6 remains **uncompleted**: required 100k-record assembly measurement/hysteresis and mixed-tree acceptance evidence, plus startup receipt review, remain outstanding. P7 remains conditional and unresolved. `context.on_overflow` default remains **compact**; no completion or default flip is claimed.
- Requested medium `github-copilot/gpt-6.1-sol` exhausted quota (HTTP 429); all further low work is `codex/gpt-6-luna` as requested. No code/default changes or commit.

### P6 advisor-finding fixes (edited without a shell; NOT yet run)

This pass had file tools only (no Bash), so **nothing below was executed**; run
the targeted matrix before relying on it.

- **Startup admission recovery (blocker):** `pending_submission_sessions` now
  matches `input.consumed`/`input.dropped` (the old `input.dequeued`/`input.steered`
  never occur) and skips archived/trashed rows. `Supervisor.recover_admissions` is
  read-only: no lease, no `manager.open` (no unarchive), no `input.dropped`; it
  returns per-session diagnostics (an unreadable session does not block others)
  and the daemon logs `daemon.admissions_pending`. This reverses the earlier
  "drop stranded admissions" behavior to keep the documented durable queue:
  stranded and legacy queued input stays queued and runs at the next turn start;
  no interrupted turn is replayed. Policy caveat for the maintainer: such input
  does not auto-run on restart; a user action starts the turn.
- **Token accounting:** handoff brief budget cost uses the counted history cost
  (environment counter), not `len(text)`. `context.handoff` `tokens_before/after`
  come from assembler counts (kept suffix + default-tokenizer brief); direct calls
  use the default tokenizer over canonical text.
- **Brief overflow:** `fit_brief` shortens every line to a shared cap and keeps
  all sections; intent-only is a last resort for tiny caps.
- **New tests:** consumed/dropped not pending, archived stays archived, legacy
  queue survives, bad session isolated; cross-turn hysteresis; fork before/after
  head; unfit required context → `ContextOverflow`, no loop; 4-node tree with an
  external child; structured brief truncation; marker token sanity.
- **Docs:** `docs/config.md` now lists `on_overflow`, `handoff_keep_fraction`,
  `handoff_brief`; `docs/sessions.md`, `docs/host.md`, `docs/context.md` explain
  the read-only recovery, truncation and token semantics.
- **First targeted run (parent-verified):** 428 passed, 2 failed in 19s. Both
  failures were fixture defects, not product bugs: the `populate` 500-token budget
  cannot hold one kept group plus a brief, so a follow-up turn legitimately
  re-trips the threshold (`test_small_turn_after_handoff_does_not_rehandoff`); and
  an unbroken 900-char run is redacted as an opaque token, shrinking the brief
  below the limit (`test_oversized_brief_keeps_every_section_instead_of_intent_only`).
  Fixtures were rewritten (larger budget; word-separated prose) in a pass with no
  shell; *since re-run and verified fixed (see "P6 finalized status" below).*
- **Benchmark script:** `artifacts/p6_benchmark.py` (git-ignored) measures
  `active_messages`/`assemble` at 1k/10k/100k, brief size and re-handoff interval.
  *(Superseded: results now recorded in "P6 finalized status" below.)*
- **Still outstanding (as of this pass; see finalized status below):** PTY
  baseline attribution against `.baseline-p6`, the maintainer soak, and the
  default flip. `on_overflow` stays `compact`. P7 is not started.

### P6 finalized status (parent-verified; plan NOT complete)

- **Targeted tests (parent-run):** submission_idempotency, session_handoff,
  host_handoff, session_tree, session_ownership, child_persistence,
  session_manager, session_operations, host_facade, host_daemon,
  host_supervisor, runtime, phase6_integration and docs (15 files):
  **430 passed in 18.48s**. The two earlier fixture failures are verified fixed.
- **Benchmark** (`artifacts/p6_benchmark.py`, git-ignored; median per call;
  no-head / head):

  | active records | active_messages | assemble | brief chars |
  |---|---|---|---|
  | 1k | 2.04 / 1.07 ms | 7.01 / 3.78 ms | 1757 |
  | 10k | 21.50 / 9.99 ms | 76.31 / 39.13 ms | 1763 |
  | 100k | 248.34 / 115.32 ms | 837.62 / 410.80 ms | 1769 |

  Brief size is effectively flat (~1.76k chars). Caveat: the benchmark retains
  half the history after the head, so it is **not** a bounded-head scalability
  claim; latency still grows roughly linearly with log size (~0.4 s assemble at
  100k with a head), which is evidence for, not against, considering P7
  (decision still open).
- **Re-handoff simulation:** `max_tokens=4000`, 400 turns, 39 handoffs; first at
  turn 17, then 27, and so on; interval 10 turns (min/median/max all 10), i.e.
  no thrash.
- **Lint:** `.venv/bin/ruff` is absent, but parent global
  `ruff check nexus/session/db.py nexus/session/handoff.py nexus/context/manager.py nexus/context/handoff.py nexus/host/supervisor.py nexus/host/facade.py nexus/host/daemon.py tests/test_submission_idempotency.py tests/test_session_handoff.py tests/test_session_tree.py`
  succeeded (all checks passed).
- **Layering & docs:** `.venv/bin/python -m pytest -q tests/test_layering.py tests/test_ui_layering.py tests/test_docs.py`: **245 passed in 0.89s**.
- **Not verified in this continuation:** maintainer soak, replay/completion-rate
  comparison, the `on_overflow` default flip, the full repository suite, native
  UI/PTY baseline attribution. `context.on_overflow` remains `compact`. P6 is
  **not** declared complete and the overall plan is **not** declared complete.

### P6 current status (latest continuation; supersedes "Not verified" above)

Historical entries above are kept as-is; this is the concise current state.

- **New runtime coverage:** `tests/test_runtime_handoff.py` drives a real offline
  `Runtime` (scripted provider, temp `NEXUS_HOME`, small `context.max_tokens`,
  `on_overflow = "handoff"`). Three tests:
  1. automatic handoff sends the brief first, then a contiguous pair-safe
     newest suffix; one handoff marker/divider; `handoff_count == 1`; a queued
     submission is delivered after the turn settles (`session.wait_idle()`,
     event-driven, no sleeps); the todo state survives with its revision; a small
     follow-up turn does not re-handoff (hysteresis: same `context_head_seq`,
     same brief);
  2. summarized brief uses the runtime summarizer and marks the recall
     untrusted;
  3. compact vs handoff run the same fixed tool-loop script intact. This is a
     deterministic loop-integrity comparison, **not** completion-rate evidence.
- **Test fix (no runtime code change):** the old slash-inference fake in
  `tests/test_context_extension_selection.py` now uses an explicit `root_id`
  plus an unowned negative control.
- **Focused results:** context_extension_selection, runtime_handoff,
  child_persistence, session_ownership, session_tree: **33 passed in 1.58s**;
  ruff on both changed tests passes. Earlier focused run (runtime_handoff,
  session_handoff, host_handoff, submission_idempotency, docs): **222 passed in
  0.77s**.
- **Full suite (before the final two test fixes, ~9m):** 3024 passed, 8 failed,
  17 skipped. Six failures were reproduced on the baseline with the parent
  interpreter (`../.venv/bin/python`; the baseline has no `.venv` python):
  `tests/test_claude_code_routing.py::test_external_child_gets_no_wall_clock_even_when_parent_has_one`,
  desktop keymap parity `shared_shortcuts`, ratatui PTY `local_disclosure`,
  `tool_single_click...`, `hover_copy...`, and ratatui verb-groups
  `member_summaries`. The other two (the new runtime queue-wait tests and the
  context_extension_selection fake) were fixed afterwards and pass in the
  focused runs. **The full suite was not rerun after those fixes**, so no green
  full run is claimed; the expected remainder is the six pre-existing failures.
  *(Superseded: the full suite has since been rerun; see "P6 crash-durability
  continuation" below.)*
- **Native/PTY:** `cargo build --locked` succeeded for current and baseline.
  The dedicated PTY sessions file passes 4 on both (current 11.51s, baseline
  11.24s), but the baseline file contains only the legacy tests while the current
  file's updated tests pre-existed, so suite semantics are not claimed identical
  beyond test names. The three non-session native PTY failures reproduce on the
  baseline. `.baseline-p6` source is untouched (build artifacts only).
- **Still outstanding:** real maintainer soak, the real-provider completion-rate
  comparison, and the default gate. `context.on_overflow` stays `compact`; no
  default flip. P6 and the plan remain **not** complete.
  *(The "full suite not rerun" gate is now resolved; see below.)*

### P6 crash-durability continuation (supersedes the "full rerun outstanding" gate)

Documentation-only update; no production code changed in this continuation.

- **New tests:** `tests/test_session_crash_recovery.py`, 8 tests. Each case runs a
  real child *process* over the real SQLite session store, ownership rows,
  `fcntl` turn locks, `Session` and (F4/F6) `ContextManager` handoff code, parks
  it at a deterministic stdout-sentinel barrier (bounded wait, no sleeps),
  `SIGKILL`s it, and reopens the on-disk state in the test process. Injection
  uses no production hooks (SQLite trace callback, a `db.transaction` wrapper,
  a `Session._publish` wrapper, a replaced `build_brief`). Parent verified the
  file independently: **8 passed in ~1s**, ruff clean, after strengthening the
  lease order, full record counts and the exact F2 test name.
- **Coverage:**
  - F4: kill during brief build leaves no head; the next turn writes exactly one
    head and a further turn writes no second one.
  - F5: kill before COMMIT and mid-batch (before the submissions INSERT) leaves
    neither record nor event visible; a retry is fresh, then deduped.
  - F6: kill after commit before the request: one head, queue kept; the next
    small turn starts at the brief and keeps the same head. Direct-handoff
    variant: retry is deduped, queue kept.
  - F1: kill after `agent.spawned` before the child is promoted: parent
    recovery appends one `agent.completed` (`interrupted`) and one
    `turn.failed`; no child row or handle appears; idempotent on reopen.
  - F2: kill during an active streamed child (persisted state only: lease held,
    `turn.started`/`model.started`, no terminal event): recovery appends only
    `turn.failed` to the owned child log and adds no tool events. This pins the
    log; it is **not** proof that an executor would not rerun a tool.
  - F3: a second holder keeps the child `running` and untouched while its lock
    is held; after it dies, recovery appends only `turn.failed`.
- **Limits (review verdict):** the SIGKILL storage boundaries are meaningful but
  this is **not** actual daemon/socket recovery. No Nexus daemon/host process,
  `Runtime`, provider/model stream, network or credentials are involved; F2's
  "stream" is only the state one leaves behind.
- **Full suite (parent, `.venv/bin/python -m pytest -q -rf`):** 6 failed, 6607
  passed, 336 skipped, 6 deselected in 550.44s. This is **not** wholly green. The
  six failures are exactly those reproduced on the baseline in the prior
  continuation: `test_claude_code_routing::test_external_child_gets_no_wall_clock_even_when_parent_has_one`;
  desktop keymap parity `shared_shortcuts_have_matching_desktop_routes`; ratatui
  PTY `local_disclosure_without_python_reply`,
  `tool_single_click_selects_double_click_toggles_locally`,
  `hover_copy_buttons_on_user_cards_and_code_fences`; ratatui verb-groups
  `member_summaries_are_concise_first_line_with_an_ellipsis`. The crash tests
  were strengthened after the suite started and were rerun independently
  (8 passed). Collection counts differ from earlier runs; no explanation is
  claimed.
- **Still outstanding:** true daemon/socket kill-and-restart recovery if the gate
  demands more than the storage-level subprocess tests; the maintainer session
  soak; a real-provider paired quality comparison; the actual threshold/turns
  dossier. The default flip is **NO**: `context.on_overflow` stays `compact`.
  A suggested gate (10 real sessions each with a handoff plus 5 representative
  paired tasks) was a proposal only, is not approved, and is not a contract.
  P6 and the plan remain **not** complete.
  *(The daemon/socket item is now materially covered for the scenarios below.)*

### P6 daemon/socket crash continuation

Documentation-only update; no production code changed in this continuation.

- **New tests:** `tests/test_host_daemon_crash.py`, 3 tests. A test-owned driver
  runs a real `nexus.host.daemon.Daemon` *subprocess* (offline `Runtime` with a
  `ScriptedProvider`, no credentials, isolated home and Unix socket, real SQLite
  store and turn locks). The test `SIGKILL`s it, starts a second daemon over the
  same home and socket through `ensure_daemon` (new pid, stale pid/lease
  reclaimed) and reconnects with a real client over the socket. Injection is
  test-owned (a `Session._publish` wrapper in the driver for T1, provider barrier
  steps for T2/T3); no production hooks. Reported: 3 passed in ~7.01 s, lint
  clean; the parent reran the strengthened tests independently; high-effort
  review found no blockers.
- **T1, handoff committed, killed before continuation scheduling:** after
  restart the single head and the continuation's `input.queued` are visible; the
  continuation is not auto-run (no provider call in a bounded quiet window).
  `SessionHandoffRead` returns the brief/head. A request-id retry returns the
  same `queued_id`/`turn_id` with `fresh=False` and adds no head or input. One
  fresh `SessionStart` runs a single turn that starts at the head and consumes
  the continuation exactly once; later retries add nothing.
- **T2, interrupted model turn with a queued admission:** the dead turn shows as
  `interrupted` in the tree; the queued admission is preserved and not run.
  Opening the session (`SessionSubscribe`) closes the abandoned turn with one
  `turn.failed{interrupted}`, idempotently. Request-id retries of the enqueue
  and of the original `SessionStart` return the original identities and add
  nothing. One fresh turn consumes the queued input exactly once.
- **T3, native child model stream interrupted after `bash` completed:** the
  durable `bash` result and its side effect (one line in the effect file) are
  not re-executed after restart, including after the parent is opened (which
  closes the orphaned child once, `agent.completed{interrupted}`, idempotently)
  and after a fresh turn. `SessionInspect` of the child shows that bash call
  `completed` with no running tool. This is **not** an in-flight tool kill.
- **Read-only contract:** the first `SessionTree` after restart sets a baseline;
  after it, `SessionHandoffRead`, `SessionTree` (T2, T3) and `SessionInspect`
  (T3) leave every node's `last_seq` unchanged and make no provider call. Startup
  admission recovery runs before any client read and is not asserted to write
  nothing, so no claim is made that the read-only commands, or the tree itself,
  never recover; only post-baseline behaviour is pinned. Public child inspect
  uses the stored (physical) id; the tree exposes the logical `root/sub/N` id as
  metadata (`logical_session_id`).
- **Known existing policy limitation, unchanged:** a retried `SessionStart`
  returns its historical `turn_id`, but that turn stays failed and is never
  resumed. The retained queued entry runs on a fresh explicit turn under a new
  turn id. Clients must not interpret a retry as restarted execution. No UX
  behavior is defined here. The default stays `compact`; there is no default flip.
- **Gate status:** the daemon/socket kill-and-restart gate is materially covered
  for these three scenarios only; this is not a claim about all crash points.
  Not tested: recovery of an in-flight external shell/tool at kill time, and
  concurrent second daemons (F3 stays in the storage suite). Full suite: last
  result is the prior 6607 passed, 6 baseline failures, 336 skipped, 6
  deselected; it has **not** been rerun for this continuation. Parent focused
  verification: `.venv/bin/python -m pytest -q tests/test_host_daemon_crash.py tests/test_session_crash_recovery.py tests/test_runtime_handoff.py tests/test_host_daemon.py tests/test_host_supervisor.py tests/test_submission_idempotency.py tests/test_host_handoff.py tests/test_docs.py tests/test_layering.py tests/test_ui_layering.py` (343 passed in 21.11s); ruff new daemonfile clean.
- **Still outstanding:** maintainer real-session soak; real-provider paired
  quality comparison; the observed actual threshold/turns dossier. The earlier
  10-session/5-task gate suggestion remains an unapproved proposal and is not
  codified. P6 and the plan remain **not** complete.

### Implementation readiness ledger (consolidated, 2026-10-18)

This section is the current state. It supersedes earlier statements in this file
that the largest-log diagnostic was a gap, or that actual-database metrics were
unavailable; those lines are history. Earlier ledgers stay as written.

**Implemented and verified (engineering)**

- P1–P5 behavior and tests as recorded in their ledgers (recovery, ownership,
  tree, `append_batch`/request-id admission, handoff head and brief), plus the
  storage-level SIGKILL suite (`tests/test_session_crash_recovery.py`, 8 tests)
  and the daemon/socket suite (`tests/test_host_daemon_crash.py`, 3 tests) for
  the scenarios listed above only.
- **P0 diagnostic (was a gap, now done):** `nexus/host_support/doctor.py` reports
  the 10 largest logs for the doctor's project: `records` count plus
  `sum(length(body))`, computed in SQL; no bodies, titles or paths. The output
  is bounded (10 rows) but the SQL aggregates over all of the project's records,
  so the work is not bounded; there is no hard cost ceiling. The result is
  cached for `HEAVY_TTL` (10 min) across namespaces, so a large store may pay
  that scan once per 10 minutes. CLI tests cover it.
- **Dossier script (new, tracked):** `scripts/p6_dossier.py` is a read-only,
  metadata-only report over a real database. It opens SQLite with `mode=ro` and
  `query_only`, writes nothing, and never emits message/brief text, titles or
  roots. It reports counts and byte sizes, the largest logs, per-project metrics
  (project ids hashed), and, between successive handoffs of a session,
  `turn_interval` (true `turn.started` events, read via
  `json_extract(body, '$.event.type')`) separate from `message_interval` (message
  records, a proxy), with compact counts and mixed intervals exposed. It excludes
  records inherited from a fork and trashed rows under the current schema. The
  actual turn interval is the number of turns strictly between one handoff's
  summary seq and the next trigger (turn counts, exclusive of the summary seq).
  Only the output size is bounded: the script scans the whole database offline
  and has no hard cost ceiling, so run it on a large database knowingly. It
  measures no latency. It honors `NEXUS_HOME` for the default path; pass
  `--database "$NEXUS_HOME/nexus.db"` explicitly anyway. Tests: the new dossier
  tests pass and are lint clean (parent-reported).
- **Verification (parent):** focused doctor + dossier tests 43 passed, 62
  deselected (0.44 s); `ruff check` clean on the touched files.
- **Synthetic evidence (unchanged):** the 1k/10k/100k curve and the 400-turn
  re-handoff simulation above. It retains half the history after the head, so it
  is not a bounded-head scalability claim.

**Real-data observation (one point in time)**

A read-only dossier run on the maintainer database found 36 sessions, 8,573
records and about 10,088,094 body bytes, and **zero handoffs**. This is a size
snapshot only. It supports no claim about real handoff frequency, brief size in
use, re-handoff interval, quality or soak. No content or paths are recorded here.
The counts above are a dated snapshot, kept as recorded; the real database
evolves, so a rerun will differ (the dossier now also filters fork-inherited and
trashed rows, so counts are not directly comparable).

**Decision: P7 deferred.** There is no assembly hot-path evidence from real use,
and the synthetic benchmark does not bound the post-head read, so the question
stays explicit rather than closed. Revisit if a real dossier or soak shows slow
assembly (§9, P7 row).

**Full suite (historical; superseded by the final run in "Final completion status").** `6 failed, 6620 passed, 336 skipped,
6 deselected` in 540.28 s; not green. The six failures are the same exact
baseline list as before, unchanged. This supersedes the earlier blanket "latest
full suite not rerun" statement **in this current section only**; the older
sections above stay as historical record. Caveat: that full run began **before**
the final fork/trash/default-env dossier test modifications. Separately, the
final focused doctor/dossier/host-facade/docs/layering run was 355 passed in
2.99 s, and `ruff check` is clean on all changed Python files. The one-field
config command above was verified to print compact output. No full run after
the final dossier test edits is claimed.

**Latest read-only dossier snapshot (supersedes the 36-session point above).**
Excluding fork-inherited records and trashed sessions (`trash_id`): 41 sessions,
9,257 records, 11,206,081 body bytes, **zero handoffs**. These totals evolve
and are not meant as published exact figures; the exact snapshot date/time was
not recorded and is not invented here (ledger date 2026-10-18). The snapshot
likely includes this orchestration conversation, which has no handoff, so
handoff count stays 0. It is a size observation only, not soak evidence.

**Scope boundaries.** P6 automated/diagnostic code is complete. The maintainer
real-session soak and the quality/completion gate are hard-blocked: there are no
actual samples. P6 is **not** declared complete and the default is **not**
flipped. The original P0 largest-logs item is done, and the docs that called it
a gap are corrected (tracked script: `scripts/p6_dossier.py`). No user settings
are changed automatically; evaluation needs the maintainer's explicit opt-in and
a real-provider comparison. The "10 sessions / 5 tasks" figures are an
unapproved suggestion, not a requirement. Current P6 work that remains open is
distinct from the deferred P7 (bounded assembly reads) and from the accepted
timer changes (worker wall-clock deadline removal), neither of which is part of
the P6 gate.

**Blocked: rollout (needs the maintainer; not an engineering task)**

- Maintainer real-session soak with `on_overflow = "handoff"`.
- Real-provider quality/completion-rate comparison on representative long tasks.
- Default flip. Per the original plan (§8, P6), `context.on_overflow` moves from
  `compact` to `handoff` **only** after the maintainer soak and a comparison
  showing completion rate does not regress. Until then the plan cannot be marked
  complete. The earlier "10 sessions / 5 tasks" figures were an invented
  proposal and are **not** a gate; no numeric threshold is defined here.

**Running the dossier**

```
python scripts/p6_dossier.py [--database PATH] [--top N] [--projects N]
```

Default database `$NEXUS_HOME/nexus.db` (`~/.nexus/nexus.db` when unset). Pass
`--database "$NEXUS_HOME/nexus.db"` explicitly. Output is one JSON document.
Safe to run against a live database (read-only); it writes nothing. It scans the
whole database, so the output is bounded but the cost is not.

**Real evaluation protocol (no model selection, no network from this plan)**

1. Isolation. `NEXUS_HOME` does **not** isolate config: `Config.load` also reads
   `Path.home()/.nexus/config.toml`, so the user's real config and extensions
   still apply. Use a throwaway workspace whose project layer
   `.agents/nexus.toml` explicitly sets `context.on_overflow` for the arm
   (`"handoff"` or `"compact"`; never rely on the default). Use a separate
   `NEXUS_HOME` only for the DB, socket and auth state. A temporary `HOME` would
   isolate config fully but external CLIs lose their auth, so prefer the project
   override plus `NEXUS_HOME`. Do not modify the user's real config. **Before
   each arm**, verify the effective config from the same environment (same
   `NEXUS_*` variables, which also override file layers) prints the intended
   value, using a one-field inspection that does not dump the config or secrets:
   `python -c 'import sys; from pathlib import Path; from nexus.config import Config; c=Config.load(Path(sys.argv[1])); print(c.v2.context.on_overflow if c.v2 is not None else "compact (legacy)")' "$WORKSPACE"`
   (`.v2` is `None` for a legacy v1 config, which means `compact`). Also note any
   user-level extensions that remain active. Leave `handoff_keep_fraction` and
   `handoff_brief` at defaults unless a separate variation is the point.
2. Choose representative long tasks the maintainer already does, large enough to
   cross the 85 % threshold. Use the same task, workspace snapshot and model for
   both arms; the maintainer picks the model and credentials.
3. Run each task once with `on_overflow = "compact"` and once with `"handoff"`.
   Repeat per the maintainer's judgment; single runs are anecdotes.
4. Record per run: task outcome (done / partly / failed, judged against criteria
   written before the run), errors (including `ContextOverflow`), whether the
   continuation lost the task, todo or queued input, handoff count, brief size,
   turns between handoffs, and wall time. Take sizes and intervals from
   `scripts/p6_dossier.py` run on the isolated database.
5. Measurable pass gate: on the same tasks, `handoff` completion is no worse than
   `compact`, with no new error class, no lost task state, and no repeated
   handoff thrash. The maintainer sets sample size and any numeric bar before
   running; none is invented here.
6. Only a maintainer decision on that evidence changes the default. Keep rollback
   as a config flip.

**Needs the user/maintainer:** real usage over time and a genuine external
quality judgment. Neither can be produced by tests or by this plan.

### Default flip under soak waiver (historical; supersedes "default stays compact" above)

- **Decision (maintainer, explicit):** "Waive soak; make handoff the default now",
  given after being told the code was automated-complete and the soak gate was
  open. The soak, the real-provider completion-rate comparison and the
  evaluation protocol above were **not run**. This is a waiver, not a pass: there
  is no real-usage or provider-quality evidence that handoff is as good as
  compaction.
- **Change:** `ContextSection.on_overflow` defaults to `handoff`. Explicit
  `compact` is unchanged. Legacy v1 configs (`.v2 is None`) and config objects
  without a `v2.context` still resolve to `compact` in the assembler.
- **Status:** P0–P6 implementation complete under that waiver. P7 (bounded
  assembly reads) remains **deferred** and is not part of this completion; revisit
  if real use shows slow assembly. Known caveat: the six baseline full-suite
  failures recorded above are unchanged by this entry and were not re-attributed.
- **Rollback:** `context.on_overflow = "compact"` (or
  `NEXUS_CONTEXT__ON_OVERFLOW=compact`). Release notes must warn about the default
  change (docs/release.md).

### Final completion status (latest; supersedes all "not complete" statements above)

- **P0–P6: complete under the maintainer's explicit waiver** of the real-session
  soak and the real-provider quality/completion comparison. This is a waiver, not
  a pass; no quality evidence exists and none is claimed. No approval broader than
  that soak/quality waiver is recorded or implied.
- **P7: deferred, optional.** Revisit only if real use shows slow assembly.
- **Default:** `context.on_overflow = "handoff"` for schema-v2 configs; legacy
  no-config/v1 configs stay `compact`; explicit `compact` is the rollback.
- **Intentional departures** (also listed at the top): no `deduplicated` flag
  (a retry returns the canonical receipt); legacy default `compact`; a historical
  retried turn is not resumable; no in-flight shell/tool kill coverage.
- **Final test evidence (most recent; after all code fixes).**
  `.venv/bin/python -m pytest -q -rf`: **6 failed, 6636 passed, 336 skipped,
  6 deselected in 542.29 s**. Not green. The six failures are the same baseline
  list as before: `test_external_child_gets_no_wall_clock_even_when_parent_has_one`
  (claude_code_routing); `shared_shortcuts_have_matching_desktop_routes`
  (desktop_keymap_parity); ratatui PTY `local_disclosure_without_python_reply`,
  `tool_single_click_selects_double_click_toggles_locally` and
  `hover_copy_buttons_on_user_cards_and_code_fences`; and
  `member_summaries_are_concise_first_line_with_an_ellipsis` (ratatui_verb_groups).
  No failures were introduced by the schema-v2 default flip, and four old test
  assumptions were updated. Ruff is clean on all changed Python files. Final docs
  tests: 34 passed in 2.75 s. No claim is made that the suite is green. The
  earlier full-run figures above are historical record only.
- This is the final closure: there is no pending verification, and any earlier
  "latest" or "pending" paragraph is stale.
- Plans 02 and 03 still need the follow-up edit described in §10.
