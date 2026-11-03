# Why the TUI sometimes takes a long time to load

Investigation: 2026-11-03, Nexus 0.10, `main` at `66b4564e`, Apple silicon (M-series), macOS.

Question: launching the TUI sometimes takes a long time. Is it rendering, the client, or Core?

Short answer: rendering is not the bottleneck. The slow launches are cold Core starts
waiting on daemon startup, dominated by a full scan of the event log during recovery.
A fixed terminal-capability probe adds up to one second on every launch.

All numbers below are measurements on this machine. Cold-start and history tests used a
consistent copy of the real database in a scratch home and a separate port; the real
`~/.nexus` and its running daemon were not modified.

## Environment

| | |
| --- | --- |
| Database | `~/.nexus/nexus.db`, **1.5 GB** |
| Events | **1,369,055** |
| Messages | 46,587 |
| Sessions | 260 |
| Turns | 700 |
| Running daemon | ~6.9 h uptime, started from older code than the checkout |
| `nexus doctor` | `FAIL daemon … runs other code than the checkout` |

## Summary

| Scenario | Observed | Cause |
| --- | --- | --- |
| Warm Core, `--once` | ~10 ms | Normal fast path |
| Terminal launch, no terminal reply | first pixels 6 ms, next frame at **1.012 s** | `Picker::from_query_stdio()` 1 s timeout |
| Cold Core, first start on current code | **14.7 s** | one-off migrations + recovery |
| Cold Core, subsequent start | **1.9 s** | recovery still scans the event log |
| First open of a session (cold) | 214 ms | cold I/O + Core reads |
| Switching back to a cached view | 0.6 ms | cache hit |
| Re-reading that history | 17 ms | warm Core |
| TUI frame at 1,000 messages | 0.35 ms mean | not a factor |

## 1. Cold Core start (dominant)

When no daemon answers, the TUI starts Core through the CLI
(`nexus-tui/src/discovery.rs`, `start_core`) and shows the launch splash until Core
responds. The TUI waits up to 25 s; Core's `daemon start` waits up to 15 s.

On a consistent copy of the real 1.5 GB database:

| Run | Command | Time |
| --- | --- | --- |
| 1 | `NEXUS10_HOME=… NEXUS_PORT=4801 uv run --project nexus-core nexus daemon start` | **14.74 s** |
| 2 | same, immediately after stop | **1.89 s** |

The first run paid in-place schema migrations and backfills because the running
daemon is older code; the second reflects a normal cold start of a large database.

Breakdown of `make_runtime()` (`Store()` + `Runtime.recover()`) on a fresh copy:

| Phase | Time |
| --- | --- |
| `Store()` — open, in-place migrations, log head | 0.3 s |
| `Runtime.recover()` | 5.8–9.5 s |
| → `Recovery.run()` | 5.7 s |
| → `_close_lost_bash_jobs()` | **4.5 s** |
| → → `Store.open_bash_jobs()` | **4.5 s** |
| → per-turn `_recover()` for 5 unfinished turns | 1.2 s |
| `build_app()` | 0.03 s |

### Root cause: `open_bash_jobs()` scans the whole event table

`nexus-core/nexus/store.py:2777`:

```sql
select session_id, type, json_extract(data, '$.id') as job
from event
where type in ('bash.started', 'bash.finished') and session_id is not null
```

The event table has:

- `event_session (session_id, id)`
- `event_lifecycle (session_id, type, id)`
- `event_turn`, `event_wire_order`, `event_wire_expiry`

No index has `type` as its leading column, so SQLite scans all 1.37 M events and runs
`json_extract` on every matching row. This runs on every daemon start, before Core
accepts requests, and therefore on every cold TUI launch.

Why it feels intermittent:

- It only happens when Core is not already running (stopped, crashed, restarted).
- The first start after a code change also pays the one-off migrations, which is why
  one launch can be ~15 s and later ones ~2 s.
- `nexus doctor` currently reports that the running daemon executes different code
  from the checkout, so the next restart will take the migration hit.

## 2. Terminal capability probe (fixed ~1 s)

After painting the splash, `nexus-tui/src/main.rs:513` calls:

```rust
let picker = ratatui_image::picker::Picker::from_query_stdio()
    .unwrap_or_else(|_| Picker::from_fontsize((10, 20)));
```

`ratatui-image 8.1.1` sends a device-attributes/Kitty query and waits up to
**1 second** for a reply (`picker.rs`, `query_with_timeout(..., Duration::from_secs(1))`).

In a pty that does not answer:

```
0.006 s  first output (alternate screen + splash)
1.012 s  next frame
```

The splash is painted before the query, so the screen is not blank, but the logo is
frozen for that second. Terminals that answer (Ghostty, kitty, iTerm) are fast; tmux,
screen, or an unusual `TERM` may burn the second on every launch.

Note: `term.rs` already avoids a similar 2-second `supports_keyboard_enhancement`
query by pushing the flags unconditionally. The image picker query has no equivalent
fast path.

## 3. Launch path

`~/.local/bin/nexus` is a symlink to `scripts/nexus`. Its `tui` command uses
`cargo run --release`, not the built binary:

```sh
cargo run --quiet --release -- "$@"
```

Consequences:

- A no-op invocation costs ~0.4 s (measured 0.36–0.49 s over six runs).
- After editing sources/dependencies, a release rebuild is minutes.
- Core is started through `uv run --project nexus-core nexus`, so a stale lock or
  venv triggers a `uv` sync before Core starts.

One launch measured 46.75 s with only 0.09 s user CPU (so it was blocked, not
compiling), while other `uv`/`pytest` jobs were running on the machine; immediate
reruns were 0.36–0.49 s. Treat that as environmental contention, not a code path.

## 4. Session open vs. launch

Separate from launch, the first open of a session on the large database is slower
than later reads (`nexus-tui --bench 120x40`):

```
messages 1
history_ms 214.2
first_frame_ms 216.8
frame_ms mean 0.35 p95 0.39 max 1.00 (n=400)
switch_away_ms 5.9
switch_back_first_frame_ms 0.6
switch_back_history_ms 17.0
```

- First open: 214 ms (cold page cache, Core reads, history load).
- Warm `/api/session/<id>/turn` calls: 3–17 ms; the first after idle was 81 ms.
- Warm `/api/session/<id>/message?limit=200`: 11–17 ms for ~190 KB.
- The TUI's own frame cost is not the issue (0.35 ms mean at this history size; see
  `TUI_PERFORMANCE_PLAN.md` for the render work already landed).

Other startup calls measured:

| Call | First (cold) | Warm |
| --- | --- | --- |
| `GET /api/model` | 211 ms | 2 ms |
| `GET /api/kernel` | 95 ms | 95 ms |
| `GET /api/snapshot?message_limit=200` | 16 ms | 16 ms |
| `GET /api/config` | 1 ms | 1 ms |
| `GET /api/agent` | 7 ms | 7 ms |

These are small; they add up to a few hundred milliseconds, not seconds.

## Recommendations

1. **Remove the full event scan from recovery.** Add a partial index keyed by type,
   for example:

   ```sql
   create index if not exists event_bash on event(type, session_id, id)
     where type in ('bash.started', 'bash.finished');
   ```

   Create it alongside the other indexes in `_migrate_payload_storage` /
   `_migration_schema`. `open_bash_jobs()` can then read only bash lifecycle rows
   instead of 1.37 M events. Expected: seconds → milliseconds on every cold start.

2. **Restart Core once at a convenient time** after the index lands, to pay the
   pending migrations and clear the `nexus doctor` code-mismatch warning.

3. **Make the terminal probe non-blocking or cached.** Either skip
   `Picker::from_query_stdio()` when the environment says it will not answer, cache
   the result, or reduce the timeout. This removes the fixed ~1 s stall.

4. **Avoid `cargo run` for routine launches** if it keeps biting: install/copy the
   release binary and exec it (the Core CLI's `nexus tui` already looks for
   `nexus-tui` on `PATH` or `~/.local/bin`).

## Method notes

- Cold-start and recovery numbers came from `sqlite3` backup copies of
  `~/.nexus/nexus.db` in `mktemp` homes, with `NEXUS_PORT` set to unused ports.
  The copies and scratch homes were removed afterwards.
- Per-frame and history numbers came from the release binary's `--bench` mode and
  the built-in replay benchmark.
- Terminal timing came from a pty harness that records output arrival times; a pty
  does not answer the capability query, which is exactly the worst case.
- Import/startup breakdown came from timing `Store()`, `Runtime()`, `recover()` and
  `build_app()` directly, and from `cProfile` around `recover()`.
