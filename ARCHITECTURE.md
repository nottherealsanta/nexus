# Core and clients

Status: design note (2026-10-28). It states how Nexus Core and its clients
divide the work, what "several clients on one session" must mean, and where
Rust comes into Core to keep it fast. Nothing here is built yet unless it says
so; the steps at the end are proposals for PLAN.md.

## The idea in one paragraph

Nexus Core is the one authority. It owns every piece of Nexus state and every
answer about the workspace: sessions, the timeline, turns, waits, and also the
file list behind an `@` completion, the file tree, Git status and diffs. The
clients (CLI, TUI, NSH in the browser and on the desktop, the inspector) are
views. They ask Core and render what Core says. Two clients looking at the same
session see the same thing, updated live, because they read the same event log.
Python is not allowed to be the reason a client feels slow: the hot read paths
move into a Rust engine inside Core, behind the same HTTP API.

```
            ┌──────────┐ ┌──────────┐ ┌──────────────┐ ┌───────────┐
 clients    │   CLI    │ │   TUI    │ │ NSH web/app  │ │ inspector │
 (views)    │ (Python) │ │  (Rust)  │ │    (TSX)     │ │  (page)   │
            └────┬─────┘ └────┬─────┘ └──────┬───────┘ └─────┬─────┘
                 │  HTTP /api/* (queries, mutations) + one SSE stream
            ┌────┴────────────┴──────────────┴───────────────┴─────┐
 Core       │ HTTP server (server.py)                               │
 (daemon,   │   runtime: projects, sessions, turns, waits, control  │
  one per   │   event bus + durable log (bus.py, eventlog.py)       │
  NEXUS10   │   loop, providers, task tools (Python)                │
  _HOME)    │   engine (Rust, in-process): file index, search,      │
            │     walk, Git status, watcher            ← proposed   │
            └───────────────────────────┬──────────────────────────┘
                                        │
                           SQLite, workspace files, Git, providers
```

## Rules

1. **Core owns state and workspace knowledge.** No client reads Nexus state
   files, walks the workspace, runs Git, or reads a project file on its own,
   even when it runs on the same machine. Reasons:
   - NSH may run in a browser on another device; it cannot touch the disk.
   - The two main clients must give the same answer (same `@` ranking, same
     ignored files, same Git view). One implementation guarantees that.
   - Confinement (no symlinks out, no hidden paths, the session's directory as
     root) lives in one place.
   - What the client shows about files is what the agent's tools would see.
2. **Clients hold only view state.** Scroll position, open panels, focus,
   folded items, the composer draft, completion popups, theme. All of it may be
   thrown away at any time; a client can always rebuild from
   `GET /api/snapshot` plus the event stream.
3. **Data a client ships with may be ranked in the client.** The shared
   manifest (`interface/manifest.toml`) is client data, so ranking `/` commands
   locally is fine. Anything that comes from the workspace or from Nexus state
   comes from Core. This is the line: *where does the data live?*
4. **Every change goes through Core and comes back as an event.** A client
   that sends a prompt does not draw the user message itself as the truth; it
   draws the `message.added` it receives, exactly like every other client. A
   pending marker for "sent, not yet confirmed" is view state and is replaced
   by the event.
5. **One timeline, shared.** The timeline is the agent's context (AGENTS.md,
   PLAN.md Principle 7). Because it is one durable, ordered log in Core, every
   client that renders it renders the same context in the same order.

## Three kinds of traffic

Everything between a client and Core is one of three kinds. Naming the kind of
each endpoint decides how it is built, cached and tested.

| Kind | What | How it travels | Shared between clients? | Examples |
| --- | --- | --- | --- | --- |
| **State** | Nexus state and the timeline | Mutations over HTTP; results arrive as **durable events** on `GET /api/event`; `GET /api/snapshot` to rebuild | Yes, by construction | prompt, interrupt, approvals and answers, model/agent changes, archive, `message.added`, deltas, `session.status` |
| **Queries** | Read-only questions about the workspace or Nexus, asked by one view | Plain HTTP request and response; nothing is logged | No: they serve one user's view at one moment | `@` file search, directory listing, file read, Git status and diff, worktree list, context preview, model catalog |
| **Hints** | "Something you may be showing has changed" | Live events on the same stream, coalesced; proposed: **not** stored | Yes | proposed `files.changed`, `git.changed` |

- **State** is already in good shape: durable ids, cursors, replay-to-live,
  idempotency keys, revisions and resync exist (Step 11).
- **Queries** are where speed matters most: a completion popup sends one per
  keystroke. They must be fast and cancellable, and a stale reply must be
  harmless (the TUI already drops replies for an old token).
- **Hints** are what is missing for "live" outside the timeline. Today a
  second client's Files tab or Git panel does not update when the agent (or
  the first client) changes a file; it only refreshes when asked. A hint
  carries no content, only "re-query this root/path", so a missed hint costs
  one refetch, never wrong data. On reconnect a client re-runs its open
  queries. Whether hints must be stored is an open question (see below): the
  rule "wire events are never dropped" is about context, and hints are not
  context.

## Several clients on one session

Concretely, with the TUI and NSH both open on `ses_…`:

| Situation | Behavior |
| --- | --- |
| A turn streams | Both receive the same `text_delta`/`reasoning_delta`/tool events from the log and render the same timeline. |
| One client sends a prompt | Both see `session.status` busy and the `message.added`; the sender's draft clears when its request is accepted. |
| A prompt from the other while busy | Today 409. With steer and queue (Step 12a) the queued prompt is state, so both clients show it. |
| An approval or question is pending | Both show the waiting card. The first reply wins; the other client receives `permission.replied`/`question.replied` and dismisses its card (already true for the CLI). A late reply gets 409. |
| Model, agent, tools, directory change | A `session.updated` (or the matching event) reaches both; edits carry `expected_revision`, so a stale settings page gets 409 instead of overwriting. |
| Interrupt | Either can interrupt; both see `session.error` `interrupted` and idle. |
| A client disconnects | It reconnects from its last applied cursor; on a cursor error it resyncs from `/api/snapshot`. It never resends a mutation without its idempotency key. |
| Files change | Proposed: both get `files.changed` and refresh any open tree, file view or diff. |
| Drafts, scroll, open panels | Per client. Not shared. |

Gaps to close for this table to hold:

- **Archive events** are stored only in the root's log, so a client watching
  just a child misses them (PLAN.md Known gaps).
- **Pending directory moves** live in memory; a crash loses them.
- **File and Git changes** have no event at all (the hints above).
- **No multi-client test.** Nothing checks that two subscribers reduce the same
  stream to the same state. One should: two clients, one session, a scripted
  turn with an approval, compare reduced state at the end.
- **Presence** ("NSH is also viewing this session", "the user is typing in the
  TUI") does not exist. Not needed for correctness; listed as an open question.

## Speed: Rust inside Core

### Where Python is the limit, and where it is not

| Path | Bound by | Python OK? |
| --- | --- | --- |
| Provider streaming, the loop | network, the model | yes |
| Tools (bash, edit, read) | the process or disk | yes |
| SQLite writes, event fan-out | SQLite, a few events per token | yes for now; measure under several clients |
| `@` search (`files.search_files`) | a Python walk of up to 50,000 entries, `git ls-files` parsed into sets, per keystroke (Git list cached 3 s) | **no**: tens to hundreds of ms on a large repo, and it holds the event loop |
| Directory listing with Git status | a `git status` subprocess per call | borderline |
| Git status and diff | subprocess plus parsing | borderline |
| Token counts for the starting context (N10) | tokenizer in Python | likely no |

The rule: keep the agent loop and the control plane in Python, where they are
easy to change; move the read paths that a UI calls per keystroke or per frame
into Rust.

### Shape: an in-process engine, not a second server

Options considered:

| Option | For | Against |
| --- | --- | --- |
| **A. Rust extension module** (`nexus-engine`, PyO3 + maturin, imported as `nexus_engine`) called from the Python handlers | one process, one port, one token, one API; the GIL is released during work; built by uv | a native build step in `nexus-core` |
| B. Rust sidecar that answers queries and proxies the rest to Python | queries never touch Python at all | two processes to supervise, two auth paths, split routing, harder to keep "one authority" |
| C. Rewrite Core in Rust | everything fast | throws away working, tested Core; no measured need |

**Proposal: A.** The HTTP contract does not change, so clients cannot tell.
If measurement later shows the Python HTTP layer itself is the limit for
queries, B can come after A with the same engine crate.

### What the engine does

1. **A warm file index per root.** Built once per workspace root (project
   path, session directory, worktree) with the `ignore` crate (ripgrep's
   walker: honors `.gitignore`, parallel, skips hidden and symlinks), then kept
   current with a filesystem watcher (`notify`). The same confinement rules as
   today: relative POSIX paths, no `..`, no control characters, nothing that
   resolves outside the root. Indexes are dropped after a period with no use.
2. **Search over the index.** `@` queries never walk the disk; they match the
   in-memory list. Keep today's ranking (basename prefix, basename, path;
   shallower first; directories ahead of files) as the contract, and consider a
   fuzzy matcher (`nucleo`) as a later, deliberate ranking change with a
   ledger row.
3. **The source of hints.** The watcher that keeps the index current is also
   what emits `files.changed` (coalesced, per root) to Python, which publishes
   it on the bus. One mechanism gives both speed and liveness.
4. **Later, if measured:** directory listing with Git status from the index
   plus `gix`; status and diffs; token counting (`tiktoken-rs`); possibly
   syntax highlighting in Core so the TUI and NSH colour a file identically
   (open question).

### Budgets

Measure first, then keep these as tests or benchmarks (numbers are proposals):

- `@` search, end to end over loopback, on a 100,000-file repo: p95 under
  20 ms with a warm index; first query on a cold root under 1 s and announced
  in the reply (`indexing: true`) rather than silently partial.
- Clipping stays visible: a reply that hit a cap says so (today the 50,000
  entry cap is silent in the response; it should not be).
- A burst of keystrokes never queues work: a newer query for the same client
  and root cancels the older one, or the older reply is ignored.

## Proposed changes (in order)

1. **Classify the API.** Add the kind (state, query, hint) to every endpoint
   in `docs/api.md`, and the rule "clients do not read the workspace" to the
   docs index. No code change.
2. **Benchmark the queries.** A small script against a large real repo:
   search, listing, Git status. Record numbers in this file.
3. **`nexus-engine` crate with the file index and search.** Lives in
   `nexus-core/` as a maturin-built module in the uv project. Swap the body of
   `files.search_files` to call it; the endpoint and its response stay the
   same, plus a `clipped`/`indexing` flag. The current Python test cases become
   the engine's contract tests.
4. **`files.changed` hint** from the engine's watcher, scoped to project or
   session directory, coalesced (for example 100 ms). TUI and NSH refresh open
   file views and Git panels on it.
5. **Multi-client test** (two subscribers, one session, same reduced state)
   and fixes for the gaps listed above (archive event scope, pending move
   durability).
6. **More engine paths** only where step 2 shows a need.

Each step adds a ledger row in docs/porting-ledger.md where it changes behavior, and updates
`docs/` in the same change.

## Not doing

- Clients walking the workspace or running Git "because it's faster locally".
- Client-to-client sync or a client acting as a server for another.
- Moving the agent loop, providers or tools to Rust without a measured reason.
- A second HTTP server or port for queries (unless option B is chosen later).

## Open questions

- **Hints durable or live-only?** Live-only is simpler and loses nothing that
  a refetch cannot recover; storing them would make the inspector show file
  activity. Either way they are not model context.
- **Presence**: do clients announce themselves (which session, which client),
  and do others show it?
- **Shared drafts**: should a draft typed in the TUI appear in NSH? Today
  drafts are view state.
- **Syntax highlighting** in Core (identical in both clients) or per client
  (native libraries, less traffic)?
- **Index memory**: how many roots stay warm (worktrees multiply them), and
  when to evict.
- **Remote Core**: once Core can serve a client on another machine, the token
  and transport story (NSH's host proxy today) needs its own note.
