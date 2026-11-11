# Harness workflow audit and fixes — 2026-11-10

## Scope and preservation

User requested reviewing all projects' prior sessions, especially code/fork workflow,
then approved implementing the recommendations and, if needed, real Copilot GPT-6 Luna checks.

The read-only history scan covered 482 prior sessions (2026-10-30 through 2026-11-10),
37,864 top-level tool execution records, 583 code calls, 273 named-agent launches and
14 fork sessions. Nine registered projects had history (including the Manager); three
had no sessions. Audit activity was excluded. Retained canonical records and sampled
conversations were inspected; compacted diagnostic events do not support a complete
historical count of nested calls. Historical error counts are not current defect rates.

Existing UI, session-status, btw and ZMX working-tree changes were preserved. No real
home migration, daemon restart, deployment or commit was performed. Tests used scratch
homes. Live checks copied only the Copilot credential into a private temporary home;
no credentials or authentication headers were printed or retained here.

## Implemented

- Default Python kernels are session-private, preventing unrelated children from
  serializing or overwriting each other's notebooks. Named agents may deliberately
  share an explicit kernel id; forks cannot borrow another session's kernel.
- Idle Monty heaps are dumped in memory and restored into fresh checkouts between
  cells. This resets per-checkout suspension accounting without source/tool replay.
  Host nested calls remain capped at 64 per cell, with a 1024-suspension protocol
  allowance. Time, memory and recursion limits remain. Forced cancellation/resource
  failure discards state; failed state transfer removes the kernel and says not to replay.
- Direct and nested task tools use the same pre-hook, modified-input approval,
  execution and post-hook pipeline. Nested hook events remain live, and hook notices
  enter visible model context. Caller-bound execution survives runtime binding refresh.
- Post-hook expiry preserves already-completed results. Cancellation during actual
  work warns that effects may exist; it does not suggest blindly retrying.
- Ordinary fork turns have 20 model steps, 40 total tool attempts (including the
  outer code call and nested attempts), and a 180-second tool deadline. Tool schemas
  disappear on exhaustion, and a visible notice asks for a partial report. Provider
  waiting counts toward the deadline; final response generation is not cut off.
  Btw retains its separate persistent 10-call cap and inherits the time bound.
- Forks refuse job termination and background launches. User extensions require
  frozen boolean `READ_ONLY = True` metadata; missing/old metadata fails closed.
  Like MCP hints, this declaration is not a sandbox. Ordinary fork shell access still
  relies partly on instructions; btw's stricter shell allowlist remains unchanged.
- Foreground shell calls default to 120 seconds, with explicit timeouts up to 600
  seconds. Servers and longer work use managed background jobs; those retain optional
  timeouts and are not silently killed after 120 seconds.
- Removed stale read-coverage instructions; clarified restricted Python, pre-bound
  tools, selective evidence output and partial-failure handling. Delegation advice
  is conditional on available tools. Interrupted objectives stay paused unless
  resumption fits the user's latest intent. Validation is focused and bounded.
- A null-safe model-field check fixes a session-update crash encountered during
  integration with concurrent ZMX changes; it does not change the intended ZMX guard.

## Offline verification

Before the fixes, controlled probes reproduced:

1. A direct fake tool was blocked by a pre-hook, but its nested code equivalent ran.
2. After 64 cells with four nested no-op calls each, the 65th cell failed with
   `suspension limit 256 exceeded`; retry failed again, with no kernel-deleted notice.
3. The audit fork could see a parent's Python variable in the same kernel. No live
   parent state was mutated or deliberately destroyed.

Regression coverage now includes per-cell suspension reset, saved tool handles after
rotation, 64-call parallel batches and recovery after a capped cell, private defaults,
explicit named-child sharing, forbidden fork borrowing, cancellation isolation,
nested hook block/modify/allow and notices, completed effects with slow post-hooks,
failed checkpoint/restore without replay, nested fork call/time budgets, extension
acceptance/refusal and frozen source, foreground timeout defaults and unchanged
background timeout semantics.

- Initial focused gate: **97 passed**.
- Expanded post-review integration gate (kernels/hooks/fork/btw/jobs/extensions/agents/
  server/snapshot/archive/Manager service): **221 passed**, 45.37 seconds.
- Ruff, focused Python compilation and `git diff --check`: passed.
- Final whole-Core run: **1400 passed, 1 skipped, 4 failed**, 179.22 seconds. All four
  failures are in concurrent `tests/test_zmx_workers.py` work, not the harness paths:
  - `test_no_import_or_registry_coupling`: transport-foundation expectation conflicts
    with the now-present production worker registration.
  - `test_success_stdin_lifetime_argv_env_cwd_and_raw_order`: expected sequence omits
    the transport's new `launch` event.
  - `test_event_consumer_failure_stops_worker`: consumer exits on the new launch event
    before the child writes the expected pid file.
  - `test_wire_is_delivered_even_if_interpretation_raises`: expected single wire event
    omits the new launch event.
  Those transport files/tests were left untouched. The entire tree is **not** marked
  green. An earlier intermediate whole-tree run also exposed the corrected null-model
  session-update crash, the intentionally changed shared-default expectation, and
  transient Manager waits; those did not recur in the final run.

### Checkpoint overhead

One isolated-process microbenchmark ran 25 equivalent tiny cells with inspection:

| Mode | Warm median per cell | Total, including cold pool start |
| --- | ---: | ---: |
| Keep one checkout (benchmark-only baseline) | 0.28 ms | 344.65 ms |
| Snapshot/restore each cell | 7.21 ms | 562.85 ms |

This measures tiny heaps on this Mac, not maximum-size heap performance. The additional
roughly 7 ms is small relative to model requests, but larger heaps remain a performance
observation item. Snapshots stay in memory, are not restart checkpoints, and are never
used to replay commands.

## Approved live Copilot check

Model: **`copilot/gpt-6-luna`**, effort **low**, real provider, same synthetic two-file
fixture. Each approach ran in a separate scratch session with no fallback, one provider
attempt, a 10-step ceiling, 16-call cap and 120-second tool budget. No shell commands,
workspace writes or external web tools were requested. Cleanup shut down temporary jobs
and kernels; no personal daemon was involved.

Fixture: README defines BASE=35; `src/settings.py` defines EXTRA=7. README also contains
irrelevant padding. Expected answer: 42, with citations to both paths. A synthetic
`forbidden.txt` is protected by a blocking pre-hook. The code variant additionally
attempts/catches that nested read. The fork variant additionally saves and rechecks
`parent_value=41`. These extra assertions mean this is a workflow smoke, not a strict
latency A/B benchmark.

| Approach | Requests, including children | Input tokens | Cached input | Output tokens | Wall time | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Direct reads | 3 | 11,879 | 7,508 | 77 | 7.33 s | 42, both paths |
| Concurrent/filtering code | 2 | 6,846 | 6,351 | 271 | 6.51 s | 42, both paths; nested read blocked by hook |
| Fork plus parent-state checks | 7 | 26,625 | 21,388 | 255 | 19.06 s | 42; parent value still 41 |

All three completed without provider/turn errors. The code variant emitted the
expected hook event. One fork was created and reported normally. Top-level tool
records are counted separately from nested code events.

**Interpretation:** code can save round trips and avoid dumping irrelevant content.
A fork is not worthwhile merely to read two files; its value is keeping a genuinely
separate investigation out of the parent context. Cached prefixes are useful, not
free or guaranteed. This small successful sample does not establish broad performance
or long-running reliability claims, and it does not verify the separate btw cache/UI work.

## Operational follow-up

No client-specific presentation was introduced: both clients consume the same existing
tool results, hook events and visible notices. UI work from other sessions was not
changed or visually re-certified here. A controlled daemon restart is needed to load
new Core code. Existing sessions keep frozen prompt/tool descriptions until explicitly
refreshed; start a new session for the updated guidance. No prompt snapshot was silently
rewritten.
