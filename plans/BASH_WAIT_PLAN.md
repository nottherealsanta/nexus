# Long-running bash commands: yield instead of kill, wait until exit

> **Status on 2026-09-30:** Implemented (options A + B, steps 1-8). Verified with the `bash-wait` mock scenario in the web client.
> Option D (completion notifications) is deferred to its own phase.

## Problem

Session `session-c6d02bdc` ("read @docs/release.md and commit and push") ran the
full suite with `bash {command: "pytest -q", run_in_background: true,
timeout_s: 30}`, then called `bash {action: "wait", job_id, wait_s: 30}` eight
times in about 20 seconds. Each call was a full model round trip (~6k input
tokens). The model then ended the turn with "The full test suite is still
running", leaving an orphaned job whose result nobody saw.

Causes:

1. **`wait` returns on any output, from offset 0.** `_wait_for_output`
   (`nexus/tools/builtin/bash_output.py`) returns as soon as there are bytes
   past the given offset. The model never passed `stdout_offset`, so it
   defaulted to 0. Once pytest printed its first dots, every `wait` returned
   immediately. `wait` is also capped at 30s and never means "until exit".
2. **Foreground commands are killed at the timeout.** The default is
   `tools.bash_timeout_s = 120`. The model has no "keep running, tell me later"
   path.
3. **Long `timeout_s` values are silently cut.** `ToolManager._timeout_for`
   (`nexus/tools/manager.py`) stops every `bash` call at
   `tools.bash_timeout_s + BASH_TIMEOUT_GRACE_S` (125s), whatever `timeout_s`
   the model passed. The resulting cancellation kills the process group.
4. **Background jobs never notify the loop.** Polling is the only option.
   (Out of scope here; see option D.)

## Options considered

| # | Idea | Decision |
|---|---|---|
| A | Foreground `run` blocks for a soft window, then **yields** a still-running job to the background instead of killing it | **Do** |
| B | `wait` waits until the job exits (not just for new output), with a larger cap and a per-job read cursor | **Do** |
| C | A per-call flag such as `wait_for_exit` / `expect_long` meaning "don't return until done" | Covered by A + B; relies on the model predicting duration, which it guessed badly here |
| D | Inject a `job.completed` note into the next model call, or wake the session | Later phase: touches `core/loop.py` and the durable log |

## Design

### Config (`nexus/config/schema.py`, `nexus/config/layers.py`)

- `tools.bash_yield_s = 120`: how long a foreground `run` blocks before
  yielding.
- `tools.bash_max_s = 3600`: hard runtime limit for any job, foreground or
  background. Configurable, so everything stays bounded.
- `tools.bash_timeout_s`: kept as a deprecated alias for `bash_yield_s` so
  existing settings keep working.

### A: `run` yields instead of killing (`bash.py`, `_jobs.await_job`)

- A foreground command waits until the first of: exit, the yield window, or
  cancel.
- **Yield:** the job keeps running and is marked as a background job. The
  result reports `status: running`, the `job_id`, "moved to background after
  Ns", and the output so far. The context note tells the model to call
  `action=wait`.
- A per-call `timeout_s` is a hard kill limit, capped at `bash_max_s`. If it is
  shorter than the yield window, the command is still killed at `timeout_s`
  (today's behavior).
- Cancel (Esc) during the blocking window kills the process group (today's
  behavior).

### B: `wait` waits for the job to exit (`bash.py`, `bash_output._read_job_output`)

- New `until: "exit" | "output"`, default `"exit"` on the unified `bash` tool.
- `wait_s` becomes optional. With `until=exit` it may be as long as the job's
  remaining lifetime (bounded by `bash_max_s`). The 30s cap stays for
  `until=output`.
- **Per-job read cursor:** the job remembers how much stdout and stderr it has
  already returned, so `wait`/`status` without offsets return only new output.
  Explicit offsets still override the cursor.
- Cancel during `wait` stops waiting but leaves the job running, since the
  model started or yielded it into the background on purpose.

### Manager backstop (`nexus/tools/manager.py:_timeout_for`)

- The `bash` backstop becomes `bash_max_s + BASH_TIMEOUT_GRACE_S`. The tool
  enforces its own yield and kill limits; the manager is only a last resort.

### Output shape (`_jobs.format_job_output`)

- Truncate as head + tail instead of head only. Test runners and builds print
  their summary at the end.

### Live progress (`bash.py`)

- While `run` or `wait` is blocking, emit throttled `tool.progress` events: the
  last output line, at most one every 2s, capped at about 300 per call. The
  reducer (`_on_tool_progress`), the TUI and the web client already render
  progress for running tools, so no surface code changes and the TUI and web
  stay in sync.

### Model guidance (the `bash` `SPEC` description and field descriptions)

- "Run long commands (test suites, builds) in the foreground. If a command is
  still running after the yield window you get a job_id; call `action=wait`
  once, and it returns when the job exits. Don't poll."
- Update `nexus/devtools/mock/scenarios/stress.py` and `tool_marathon.py` to
  match.

## Defaults decided

1. The legacy `bash_output` tool keeps `until=output` semantics for
   compatibility. Only the unified `bash` tool gets the new defaults.
2. Jobs still running at turn end are out of scope (option D). A yielded run's
   result always names its job_id, so it is never lost silently.

## Steps

Each step is independently testable and can be its own commit.

1. **Manager backstop:** derive the `bash` backstop from `bash_max_s`. Test that
   a 600s `timeout_s` is no longer cut at 125s.
2. **Config:** add `bash_yield_s` and `bash_max_s` and make `bash_timeout_s` an
   alias. Add schema and layer tests.
3. **B, `wait`:** add `until`, the optional/raised `wait_s`, and the read
   cursor. Tests: `until=exit` returns only after exit; the cursor never repeats
   output; `until=output` keeps today's behavior; cancel during `wait` leaves the
   job alive; `bash_output` is unchanged.
4. **A, yield:** add soft-window yield in `run` and a hard `timeout_s` capped at
   `bash_max_s`. Tests: yield keeps the process alive and returns a job_id;
   `timeout_s` below/above the yield window and above `bash_max_s`; cancel
   during the blocking window kills the group.
5. **Output and progress:** add head + tail truncation and throttled
   `tool.progress`. Tests for truncation, the throttle and the cap.
6. **Guidance:** update the `SPEC` description and the mock scenarios.
7. **Regression:** a `ScriptedProvider` scenario with a slow, chatty command
   that asserts at most 2 model calls (vs. 9+ today).
8. **Docs:** update the tools section of `docs/core.md` and the README config
   table.

Test homes: `tests/test_builtin_bash_actions.py`, `tests/test_builtin_shell.py`,
the manager tests, and the config tests.
