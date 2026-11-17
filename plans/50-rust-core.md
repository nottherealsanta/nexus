# 50. Core performance: Python first, Rust only for proven hot spots

User request (2026-11-17): "create a detailed plan to turn more and more of the core into rust, and better design, so
we don't face things like this". *This* is a session that felt slow to open, and the worry that Python is the cause.
After discussion the user chose (2026-11-17, [decisions](decisions.md)): **get the performance in Python by fixing the
design; use PyO3 only where a profile shows CPU-bound work that Python cannot do fast enough.** Moving the server,
state machine or agent into Rust is deferred, not planned (see [Deferred](#deferred-a-rust-core)).

## What we know (2026-11-17)

Timing landed today (`nexus perf`, [docs/api.md § Performance](../docs/api.md#performance)). From the TUI's
`~/.nexus/logs/tui-perf.jsonl` the same afternoon (the daemon was not yet restarted, so Core-side numbers are missing):

| Observation | Value | Reading |
| --- | --- | --- |
| Session opens (7) | 19–139 ms, no older turn pages | Opening was fast today; the slow open was not reproduced |
| `GET /api/bash` and `GET /api/kernel`, always together | 100–1,333 ms | Both only serialize an in-memory list (~1 ms of work). The rest is **waiting for the event loop** |
| `GET /api/session/:id/context` | 522 KB per call | A large payload read on open |
| `GET …/worktrees` | 50–150 ms | Git subprocesses (async); probably also queued |
| `nexus.db` | 1.6 GB | Every query and retention pass works against this |

The problem is not Python's speed at a request; it is that **Core is one event loop and work that does not yield
runs on it**: every SQLite call (175 `self.store.*` calls in `runtime.py`, 37 in `server.py`, none off the loop),
JSON encoding of large responses with the stdlib `json`, retention, Python file walks. Those are design problems and
are fixed the same way in Python.

Why Python is enough for most of it, and where it is not:

- `sqlite3` releases the GIL while SQLite runs, so a writer thread and a read pool take queries off the loop.
- Bounded reads and read models cut the work itself, in any language.
- `orjson`/`msgspec` (Rust/C extensions) encode JSON several times faster than `json`.
- **Limit:** pure-Python CPU work (big row → dict conversion, file walks, `@` ranking, tokenizing) still holds the GIL
  in a thread and competes with the loop. That is where PyO3 helps: the call drops the GIL while Rust works.
  Free-threaded Python 3.14 could lift this later; not relied on (native dependencies).

## Design rules (the "never again" part)

| # | Rule | Enforced by |
| --- | --- | --- |
| R1 | **Nothing blocks the loop.** Storage, encoding of large bodies, file walks and subprocess waits run off it. | A loop-stall watchdog in the test suite: a stall ≥ 100 ms during a test fails it |
| R2 | **One writer, many readers.** One writer thread (queue, batched commits), a pool of read connections (WAL). | `Store` has no synchronous entry points used from async code; a check forbids `store.db` outside the store |
| R3 | **Read models are written, not computed on read.** Turn summaries, session rows and usage totals are maintained when they change; a read is one indexed lookup. | Per-route query-count tests (≤ 2 queries for open/list routes) |
| R4 | **Every client read is bounded.** A session opens with one request returning a fixed-size bundle; older history is paged on scroll. | Response-size budgets per route on a large fixture |
| R5 | **Budgets are tests.** p95 per route on a large fixture database while a scripted turn streams. | `nexus bench`; numbers recorded here |
| R6 | **A slow consumer never slows the producer.** SSE fan-out queues are bounded; a slow client is dropped to resync. | A slow-SSE-client test; queue depth in `/api/debug/perf` |
| R7 | **Native code only with evidence.** A PyO3 module needs a profile showing CPU-bound Python on a route that misses its budget after R1–R4. | The workstream's report, recorded here |

## Phases

### Phase 0 — Measure (workstream A)

- [ ] Restart the daemon on the instrumented build and collect a week of `nexus perf` (user; not from an agent session).
- [ ] When a stall appears, `py-spy dump`/`record` it and name the code. Record the causes here.
- [ ] Fixture generator: a large synthetic `nexus.db` with the real schema (thousands of sessions, one with 5,000
  turns, large tool outputs, children). Never copied from the user's data.
- [ ] `nexus bench`: opens sessions, lists, searches and polls `/api/bash` while a mock-provider turn streams; reports
  p50/p95 per route and stalls. Baseline recorded in this file.
- [ ] Why `nexus.db` is 1.6 GB (read-only queries on a copy): which tables, whether retention keeps up. Recommend fixes.

### Phase 1 — Stop blocking the loop

1a. **Async store (workstream B).**

*Design (2026-11-17).* The store has about 175 call sites in `runtime.py`, 37 in `server.py` and more in the Manager,
terminals and usage services, interleaved with `with store.db:` blocks, so a big-bang conversion to `await` is not
safe in one change. Instead the store becomes thread-correct first and call sites migrate behind it:

- One write connection, serialized by a re-entrant lock held for the whole of every `Store` method and every
  `with store.db:` block (`store.db` is a `LockedConnection` that fetches rows under the lock). A synchronous caller on
  the loop and the writer thread never interleave a transaction. A transaction spanning several statements is one
  method, or one `await store.w.run(fn)` (one lock hold on the writer thread).
- One writer thread (single-worker executor, FIFO): `await store.w.<method>()`. Each method commits before it returns,
  so an awaited write is durable on return and idempotency/recovery ordering is the call order, unchanged. Commits are
  not batched: WAL with `synchronous = normal` does not sync per commit, and batching would break "durable when the
  API says done". The await is shielded, so a cancelled caller cannot drop a queued write.
- Four reader threads, each with its own read-only (`query_only`) connection, one snapshot per call:
  `await store.r.<method>()` for whitelisted reads, `Runtime.read(fn)` for whole route bodies (`runtime.store` and
  `runtime.inputs.store` resolve to the reader inside it). Read-your-writes holds because a write's commit precedes
  its await returning and a reader's snapshot starts after that.
- Event log and bus: `Bus.publish` stays synchronous (persist, then deliver, one lock). `Bus.apublish` commits on the
  writer and delivers on the loop; thread-committed events queue in `_pending` and every loop-side delivery drains it
  first, so delivery order is id order and `open()` cannot hand a subscriber an event its replay covers.
- Migration order: hottest reads first (messages, turns, stored events, snapshot, session lists, usage, replay,
  retention), then the turn loop's writes.

- [x] A writer thread owning the only write connection and a read pool; durable-on-return, FIFO, shielded.
- [x] Async facade (`store.w`, `store.r`, `Runtime.read`, `Bus.apublish`) with tests (`tests/test_store_threads.py`):
  a slow read or write does not hold other requests; a 12 MB read does not stall the loop; read-your-writes;
  snapshot consistency; ordering.
- [x] Hot reads and the retention pass off the loop (list in docs/daemon.md § Storage threading).
- [ ] Remaining synchronous on the loop: the turn loop's `store.*` writes and `bus.publish` commits in `runtime.py`,
  session create/update/delete, the Manager service, `inputs` writes, terminals and usage services, `runtime.session()`
  lookups on non-migrated routes. Convert with `apublish`/`store.w` route by route; then add the R2 check that
  forbids `store.db` outside the store.

1b. **Fast encoding, off-loop CPU, watchdog (workstream C).**
- [ ] JSON responses and SSE frames encode with `orjson` (or `msgspec`), output byte-compatible where clients care.
- [ ] Large responses (`/context`, `/snapshot`, inspector, export) encode off the loop.
- [ ] Remaining synchronous file walks and subprocess calls on request paths move off the loop (`tools/walk.py`,
  `files.search_files`, the few `subprocess.run`).
- [ ] The stall watchdog joins the test suite (R1).
- [ ] The timing-flaky tests seen 11-17 (`test_children`, `test_manager_service`, kernel-start tests in
  `test_server`) made deterministic.

**Gate:** a week of use with no stall ≥ 250 ms; trivial routes p95 < 20 ms during a streaming turn on the fixture.

### Phase 2 — Read paths by design (workstream D; API addition, both clients)

- [ ] `GET /api/session/{id}/open`: one bounded bundle (session row, starting context, newest page of messages and the
  turn summaries those messages reference, usage totals, pending waits). Replaces the TUI's four parallel requests and
  up to nine sequential turn pages; NSH changes in the same change (AGENTS.md parity) and gets the TUI's timings.
- [ ] Turn summaries as a read model, written when a turn step closes (R3).
- [ ] Per-route payload budgets (R4); `/context` sends a summary on open and details on demand.
- [ ] Visible changes (what loads when) get a porting-ledger row.

**Gate:** open p95 < 100 ms on the fixture, independent of history length; no route over budget.

### Phase 3 — PyO3 for proven hot spots (workstream E)

- [ ] First candidate: the `@` file index and search (ARCHITECTURE.md § Speed): a Python walk of up to 50,000
  entries per keystroke. Benchmark it on a large repo first (after Phase 1b moves it off the loop). Only if it misses
  its budget (p95 < 20 ms warm on 100k files): a `nexus_native` module (PyO3 + maturin in the uv project) with a warm
  index (`ignore` + `notify`), same ranking and confinement rules, GIL released.
- [ ] Each further candidate (row conversion, token counting, diffing) needs the R7 evidence first.
- [ ] If a module lands: per-platform wheels in the release, `install.sh` and `nexus doctor` updated.

## Deferred: a Rust Core

Moving the HTTP edge, the state machine, the agent loop and providers to Rust (the earlier draft's phases 4–7) is
not planned. Revisit only if, after Phase 2, measurements show Python itself (not blocking, not payload size) is the
limit, for example under several clients or long streams. The earlier analysis is in this file's history.

## Work split (2026-11-17)

Each workstream runs in its own git worktree and branch, by one agent, and reports back; merges are reviewed in the
main checkout. Overlaps: B and D both touch `store.py`/`server.py`; D builds on the current synchronous store and
is rebased onto B. C touches `server.py` only for the JSON response class.

| Workstream | Branch | Scope |
| --- | --- | --- |
| A | `perf/a-measure` | Phase 0: fixture generator, `nexus bench`, DB size report |
| B | `perf/b-async-store` | Phase 1a |
| C | `perf/c-loop-hygiene` | Phase 1b |
| D | `perf/d-open-bundle` | Phase 2 |
| E | `perf/e-file-index` | Phase 3, benchmark first; native module only past the gate |
