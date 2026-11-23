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

- [x] For the v0.15 Core gate, move blocking Git, file seeding and terminal store initialization out of async tests,
  and block live models.dev downloads in the offline suite. The 100 ms watchdog is unchanged; hosted results and
  remaining stall evidence are tracked in the [release handoff](../reports/release-0.15.0-validation.md).
- [x] The watchdog samples the blocked stack and excludes garbage-collection time. A local survey showed full
  collections of up to 50 ms landing in random steps late in the run; hosted errors moved between tests each run.
- [ ] Restart the daemon on the instrumented build and collect a week of `nexus perf` (user; not from an agent session).
- [ ] When a stall appears, `py-spy dump`/`record` it and name the code. Record the causes here.
- [x] Fixture generator: a large synthetic `nexus.db` with the real schema (thousands of sessions, one with 5,000
  turns, large tool outputs, children). Never copied from the user's data.
- [x] `nexus bench`: opens sessions, lists, searches and polls `/api/bash` while a mock-provider turn streams; reports
  p50/p95 per route and stalls. Baseline recorded in this file.
- [x] Why `nexus.db` is 1.6 GB (read-only queries on a copy): which tables, whether retention keeps up. Recommend fixes.

**Phase 0 results (workstream A, 2026-11-17).** Tools: `nexus bench fixture|run` (docs/development.md). Aggregates
only; no row contents were read. The large-scale baseline is still to run (see below).

*Small-scale baseline* (51 sessions, 216 turns, 1,500 files; `--seconds 6 --no-maintenance`; mock turns streaming).
Idle: `/api/bash` p95 0.5 ms, `/api/kernel` 0.2 ms, loop lateness p95 1.1 ms. Under load: loop lateness p50 0.6 /
p95 66 / p99 140 / max 160 ms (10 ms probe), 10 samples over 100 ms, Perf stalls of 103-148 ms with no request in
flight (background work). `@` search p50 71 / p95 154 ms (Core handler time equals it, so it runs on the loop).
Session list p50 22 ms (197 KB). message?limit=200 p95 92 ms client-side against 3.8 ms in the handler, and
turn?limit=200 p95 87 ms against 5 ms (both waiting on the loop). Mock turn create+stream p50 0.6 s, p95 3.1 s.

**Finding: `GET /api/session/{id}/context` took 2.6-4.6 s on a 120-turn session and returned 1.35 MB.** Loop CPU was
1.2-2.3 s of that, so every other request queues behind it (it is the loop-lateness spike above). It is the worst
single request in the suite, with an unbounded payload. Target for workstream C (compute and encode off the loop) and
D (payload budget).

*Large baseline: not run in this pass* (free disk under 6 GiB; the large fixture is about 1.1 GB plus a copy per
run). Run `nexus bench run --scale large` when space allows. A fresh fixture now has its deferred payload indexes and
search backfill built before it is saved; before, first startup spent over 7 minutes on that maintenance and grew the
DB from 1.1 GB to 1.5 GB.

*Real `nexus.db` size report* (read-only `mode=ro`, aggregates only): 400,174 pages x 4 KiB = 1.64 GB, of which
139,259 free pages = 544 MiB (33%). `auto_vacuum` is 0 (none), so the file never shrinks; retention's reclaim step
only works on incremental-vacuum databases, and offline `nexus db compact --apply` is the only way down. The DB is
young: the oldest session, message and turn are 17.9 days old, events go back 5.5 days. Canonical growth is about
90 MB/day (about 1.1 GB live).

| Table | Live MiB | Rows |
| --- | --- | --- |
| message | 554 | 128,032 |
| turn_tool | 232 | 62,075 |
| session_snapshot | 43 | 904 |
| event | 37 | 48,099 |
| pending_input | 33 | 224 |
| turn_request | 32 | 62,082 |
| event_body | 15 | 24,018 |
| turn_attempt | 10 | |

Every index is 5 MiB or less. **Retention is not the cause:** diagnostics are small (events 37 MiB, wire bodies
15 MiB, `log.retention` events 7 MB). The growth is canonical rows retention deliberately keeps. Turn pruning (30
days, 200 per session) has not triggered yet: no turn is older than 30 days and the most in one session is 71.

Where message bytes go (497 MB of `parts`): user messages 145 MB across 3,922, of which 226 carry inline `data:image`
base64 totalling 141 MB (the other user messages total 4.7 MB), so embedded images are about 29% of message bytes;
assistant messages 172 MB across 63,015 (avg 2.9 KB), all with provider `raw` replay state; tool messages 180 MB. 278
messages exceed 64 KiB (149 MB total), 99 exceed 256 KiB and 33 exceed 1 MiB; one session holds 50 MB of messages.
Duplication: tool output is stored twice (`turn_tool.output` 159 MB and the tool message parts 180 MB), and
`turn_tool.arguments` (33 MB: bash 12.6, patch 7.4, write 7.2) repeats the assistant message's tool-call arguments.
`session_snapshot` is about 48 KB per session (42 MB total, 41 MB for archived sessions). 871 of 904 sessions are
archived and hold 493 MB of messages. `event_body` is 23 MB raw, 11 MB stored.

Recommended fixes, by size of win:
1. Move image bytes out of `message.parts` into the artifact store and serve them lazily by reference (about 141 MB;
   also shrinks the 200-message pages the TUI opens, R4).
2. Keep assistant `raw` replay state (172 MB) out of message and turn pages and the open bundle; clients never use it.
3. Store tool output once (reference `turn_tool.output` from the tool message, or drop one copy); same for
   `turn_tool.arguments`.
4. Share the `session_snapshot` between sessions with the same agent (48 KB x 904).
5. Offline or idle compaction, or `auto_vacuum=incremental`, to recover 544 MiB (33%).
6. Compress or archive the 493 MB of messages in archived sessions (zstd blob or a cold table).
7. Give retention a canonical-growth budget: today only diagnostics are bounded, so about 90 MB/day grows without limit.

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
- [x] JSON responses and SSE frames encode with `orjson` (or `msgspec`), output byte-compatible where clients care.
- [x] Large responses (`/context`, `/snapshot`, inspector, export) encode off the loop.
- [x] Remaining synchronous file walks and subprocess calls on request paths move off the loop (`tools/walk.py`,
  `files.search_files`, the few `subprocess.run`).
- [x] The stall watchdog joins the test suite (R1).
- [x] The timing-flaky tests seen 11-17 (`test_children`, `test_manager_service`, kernel-start tests in
  `test_server`) made deterministic.

Landed (C): orjson via `nexus/jsonenc.py`; `ajson` (thread) for large bodies; `search_files`, `list_directory`,
`read_file`, `connected()` and provider usage off the loop (E replaces `search_files` with an index). Deflake: `tests/waiting.py`
`until` (60 s ceiling), `Harness.flush()`. Still on the loop: `connected()` inside sync `create_session` (child
sessions), `workers/agent._record` `ps` (ZMX), recovery `ps` probes, CLI-side `daemon.py`/`pair.py`.

Stalls the suite watchdog found (default 100 ms; 1633 tests):
- Fixed: `test_manager_live::test_two_real_peers...` was 16.8 s of blocking wait (now a 0.5 s step, marked);
  `files` search/list/read stalls (moved to threads); `test_server` project-git and anonymous-server stalls (gone
  after the moves and `until`).
- Marked `blocks_loop` (26 tests, reasons in the marks): Manager tests (`test_manager_live/stage/authority/store/
  redteam/service`) are synchronous SQLite seeding on the loop, belong to workstream B; `test_git_ops` pull
  (real git subprocess, 595 ms), `test_bash_jobs` pipeline failure (232 ms), `test_inputs_integration` recovery
  (220 ms), `test_terminals_http`, `test_client_view`, `test_export` (store reads, B), `test_zmx_workers` control
  expansion (114 ms), `test_children` fallback, `test_voice_api` partial query. Remove each mark when its
  cause is fixed; an unmarked 100 ms stall now fails the test.
- After merging A-E (11-17): the models.dev catalog parse/encode and the MCP SDK's first import moved to threads
  (they stalled whichever test reached them first, and every first request in production); `test_bench` is marked
  (it drives `/context`, which still builds the whole request body on the loop); seeding in `test_store_threads`
  and `test_open_bundle` moved to the writer or is marked. With B merged, 12 of 245 Manager tests still stall, so their
  blanket mark stays.

**Gate:** a week of use with no stall ≥ 250 ms; trivial routes p95 < 20 ms during a streaming turn on the fixture.

### Phase 2 — Read paths by design (workstream D; API addition, both clients)

- [x] `GET /api/session/{id}/open` (API 1.6, 11-17): one bounded bundle (session row, starting context, newest page of
  messages and the turn summaries covering them, usage totals, pending waits). Replaces the TUI's four parallel requests and
  up to nine sequential turn pages; NSH changed in the same change and logs the same timing with `debug.timing`.
  Older history pages messages with their turns (`message?before=&turns=1`).
- [ ] Turn summaries as a read model, written when a turn step closes (R3). **Left open:** a summary is assembled from
  `turn`, `turn_request`, `turn_attempt` and `turn_tool`, written at about 25 sites in `store.py` (and the live partial
  overlays in `runtime.py`); a stored JSON column needs invalidation at every one, plus pruning, startup recovery and the
  backfill, and workstream B is rewriting those paths into the writer thread. After `/open`, reads are bounded (about 100
  turns, three indexed queries, the new `turn_session_seq` index), so the read model is no longer the open bottleneck. Do it
  after B lands, as a write-through in the writer, with a test that stored and assembled summaries are equal.
- [x] Per-route payload budgets (R4) as tests on a 3,000-turn fixture built in the test (`/open`, older page, `/context?summary=1`)
  and a statement-count test; `/context` sends a summary (`?summary=1`, a few bytes instead of ~522 KB) in both clients.
  Core still builds the whole request body to estimate tokens when no measured figure exists; caching that is for Phase 1.
- [x] Visible changes (what loads when) have a porting-ledger row.

**Gate:** open p95 < 100 ms on the fixture, independent of history length; no route over budget.

### Phase 3 — PyO3 for proven hot spots (workstream E)

- [x] First candidate: the `@` file index and search (workstream E, 2026-11-17). **Decision: no PyO3 module.** A
  pure-Python per-root index meets the budget with margin; R7 is not met, so `nexus_native` is not built.
  Reproduce with `cd nexus-core && uv run python scripts/bench_file_search.py --dirs --profile` (synthetic repos
  are cached under `$TMPDIR/nexus-bench-files`; `--sizes`, `--repo`, `--rounds`).

  Method: 12 keystroke-like queries (`s`, `sr`, `src`, `src/`, `src/ma`, `src/mai`, `main`, `comp`, `index.ts`,
  a no-match, `test_`, `util`) x 3 rounds, `limit=30`, `directories=true`, in-process on one Apple-silicon Mac, warm
  filesystem cache. Synthetic repos are git-initialised (all files `git add`ed), depth 2 to 7, plus an ignored
  `node_modules` of 20% extra files. "Cold" is the first query after dropping every cache.

  | Repo | Before cold | Before warm p50 / p95 | After cold | After warm p50 / p95 |
  | --- | --- | --- | --- | --- |
  | synthetic 10k | 574 ms | 507 / 586 ms | 119 ms | 0.1 / 0.9 ms |
  | synthetic 100k | 3185 ms | 2811 / 3080 ms | 887 ms | 0.1 / 4.8 ms |
  | this repo (310 tracked) | 134 ms | 89 / 94 ms | 28 ms | 0.2 / 0.3 ms |

  Budget: warm p95 < 20 ms on 100k (4.8 ms: met); first query on a cold root < 1 s (0.89 s: met, with little
  margin).

  Profile before (warm, 100k): 100% of the time was the per-query walk, and it ran on the event loop. About a fifth in
  `Path.resolve()` per entry (an `lstat` storm), a quarter in `Path.relative_to`, a third in `PurePosixPath.parts`
  checks and path-object construction, the rest in `scandir`, sorting and the `visible` set lookups. The walk stops at 50,000 visited
  entries, so a 100k repo was only half searchable. The matching and ranking were never the cost.

  Profile after (warm): the whole sweep is 4 ms of generator passes over the index; the only real cost is cold. Cold
  100k splits as `git ls-files --cached --others --exclude-standard` 500 to 630 ms (git's own stat of 100k files),
  the bounded walk 215 ms, building and pre-sorting the index 23 ms, parsing the Git list 20 ms. Git is the floor
  for a pure-Python or a Rust design that keeps Git's ignore semantics; only an `ignore`-crate walker (its own
  `.gitignore` engine, parallel) would remove most of it, and then ignore rules would differ from Git's (global
  excludes, `info/exclude`, tracked-but-ignored files), which the existing tests treat as the contract.

  What changed (all in `nexus-core/nexus/files.py`, same request/response contract, same ranking, same
  confinement): `search_files` now answers from an in-memory `_Index` per root (at most 8 roots). It is built off
  the event loop (Git async, walk and sort in threads, single-flight per root), rebuilt before answering after
  `mutate_path` or a watcher `files.changed`/`git.changed` (`files.invalidate_index`, called from
  `file_watch.py`), and refreshed in the background once 3 s old (stale-while-revalidate: that query is answered
  from the older index, so the staleness bound is unchanged from the old 3 s Git cache). A query is three filtered
  passes (basename prefix, basename substring, path substring) over rows pre-sorted by the rank tie-break (depth,
  directories first, path), each cut at `limit`; `_rank` stays as the specification and
  `tests/test_file_search_index.py` checks the index against it. The per-entry `resolve()` is dropped: symlinks are
  never listed or entered, so every reached entry is already inside the resolved root. Watchers are leased and only
  run while a client has the file panel open, so the TTL, not the watcher, is the general invalidation.

  Risks and proposals (not done, need the user): (1) the 50,000-visited-entry cap is kept, so a repo over 50k
  visible entries is still partly searchable (before and after); with an index the cap could rise to 200k at a
  memory cost of about 100 bytes per entry, a behaviour change needing a ledger row. (2) A fuzzy matcher (`nucleo`)
  stays a deliberate ranking change. (3) Revisit R7 only if a real repo shows a cold first query over 1 s with
  Git's cost removed from the picture, or the index memory matters; if a module is ever built, per-platform wheels
  (macOS arm64/x64, Linux x64/arm64), `install.sh`, `nexus doctor` and a pure-Python fallback are required.
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
