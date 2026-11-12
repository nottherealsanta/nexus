# 44. Tooling and prompts from a review of real sessions

User request (2026-11-12): "go through the last few sessions and see if any tooling or prompts can be improved",
then "do all the fixes after more careful investigation for each, and mark this decision somewhere so we can get back
and improve if needed". This file is that record: what the sessions showed, what changed and why, what was left
alone, and how to measure again.

## Method

Read-only queries on `~/.nexus/nexus.db` (`session`, `message`, `turn`, `turn_tool`), sessions updated in the two
days before 2026-11-12 14:00: about 65 user sessions, 5,700 tool calls, across `copilot/claude-sonnet-5.5`,
`codex|copilot/gpt-6.1-terra`, `copilot/gpt-6-luna`, `codex|copilot/gpt-6.1-sol` and `opencode/deepseek-v4.1-flash`.
Some measurements use 7 or 14 days where two days were too few. No live daemon, provider or session was touched.

## What the sessions showed

| # | Finding | Evidence |
| --- | --- | --- |
| A | Edits went through the shell, not `edit`/`patch`/`write` | ~650 shell writes (`python3 … write_text`, `cat >`, `sed -i`) against ~480 edit-tool calls; a user asked "i dont see any edit tool, how did you remove" |
| B | A turn that died on an unexpected exception left tool calls without results; every later prompt got HTTP 400 | "Image Editing Request" (`ses_1a1cd54815b36cee45b`): full disk, `messages.14: tool_use ids were found without tool_result blocks`; 9 such turns in a week (6 on 11-12); no traceback in `daemon.log` |
| C | A parent saw only "Sub agent … failed: copilot: HTTP 400" and resumed the child again and again | 8× `model_not_supported` for `copilot/gpt-6.1-terra`; one child resumed three times. Root cause (fallback changed the child's model permanently) is plan [34](34-network-errors-no-fallback.md) |
| D | `cmd \| tail` reported `[exit code 0]` for failures | 7 days: 280 exit-0 outputs with "N failed", 95 with cargo errors, 26 with `command not found` (`npx nsh`, `timeout`), nearly all piped |
| E | Guessed paths were the most common tool error | `nexus-nsh/src/App.tsx` (is `nexus-nsh/App.tsx`), `nexus/loop/types.py`, `tests/test_terminal_api.py`, `PLAN.md` … |
| F | One failing call in a `code` cell's `asyncio.gather` lost every sibling's result | 14 days: 70 cells stopped on an uncaught tool error, 58 had completed calls whose output was discarded; Monty's `gather` has no `return_exceptions` (2 attempts failed) |
| G | `grep`/`glob`/`ls` refused paths outside the workspace though `read` allows them | 24 refusals in a week, mostly `~/repos/nexus-old` (AGENTS.md's reference); then 26 bash calls on nexus-old |
| H | 35% of bash calls began `cd <workspace> &&` | 1,152 of 3,257, though the description said not to |
| I | Agents hand-built NSH test commands | 188 calls resolving a Playwright browser, 113 adding an llvm `PATH`, `npx nsh` twice; Chrome has been installed since 11-08 and `scripts/nsh-web.mjs` finds llvm-ar |
| J | Smaller tool frictions | `web_fetch` refused JSON (PyPI API); `web_search` down told the model how to start Docker; a no-op entry failed a multi-edit (4×); reading a PNG inside `code` gave a confusing error (3×); screenshot skills loaded 7× vs 18 ad-hoc `/tmp` Playwright scripts |

## Decisions and changes (2026-11-12)

- [x] **A. Shell edits** — one line in `builtin_agents/build.md` (landed by the session "Why Build Agents Skip Edit
  Tool", not this one): change files with the editing tools, never by writing them from the shell. Prompt only; a
  `bash` guard that refuses file-writing scripts was considered and **not** done (intrusive, easy to evade). Revisit
  if the measurement below does not move.
- [x] **B. Unexpected turn failures** (`runtime.py` `_run`): a new `except Exception` branch logs the traceback, closes
  the turn's calls (`_close_dangling_calls(failure=…)`: "Nexus failed during this turn (OSError: …)"; a running sub
  agent is *not* stopped and the text says so), ends the turn `failed` with `kind: "internal"` and the exception
  text, and holds queued inputs like an interrupt. Repairing at the *next* prompt was rejected: once a user message
  follows the dangling calls, `continuation.reconcile` (rightly) refuses to rewrite history. If the closing writes
  also fail, the old fallback and startup recovery still apply. Test:
  `test_server.py::test_unexpected_failure_closes_tool_calls_so_the_next_prompt_is_valid`.
- [x] **C. Child failure report** (`runtime.failure_advice`): the report adds the provider's own reason from the
  response body (`… failed: copilot: HTTP 400 (model_not_supported: The requested model is not supported.).`) and,
  for 400/401/402/403/404/413/422, that resuming on the same model will most likely fail the same way; for 429, wait
  for the reset or use another model. `self.errors` and clients' `session.error` text are unchanged; the full error is
  kept in `self.error_details`. Tests in `test_children.py`. Still open in plan 34: fallbacks must not permanently
  change the model, and fallbacks should be validated against the live catalog. **`~/.nexus/config.toml` still lists
  `copilot/gpt-6.1-terra` as a `build` fallback**; not edited here (user state).
- [x] **D. Hidden pipeline failures** (`tools/bash.py`): an `EXIT` trap, prepended on the command's own first line so
  bash's line numbers do not move, records the last pipeline's `PIPESTATUS`. A zero exit then reads `[exit code 0 ·
  pipeline exit codes 1 0]`, plus `· command not found: X` when bash printed that anywhere. 141 (SIGPIPE) is
  ignored. **No `pipefail`**: it would turn every `grep | head` into a false failure, and the exit code itself is
  never changed. Foreground only: background jobs (`bash_jobs.py`, being edited by another session today) are a
  follow-up. Tests in `test_bash_jobs.py`.
- [x] **E. Likely paths** (`tools/suggest.py`): `read`, `grep`, `ls`, `edit` and `patch` add `; did you mean …?` (up to
  three) to "does not exist", from the same Git-aware walk as `glob` (same base name or stem, else a close spelling,
  ranked by whole-path similarity; outside the workspace only the parent directory). Checked against the real
  guesses above; `edit`/`patch` check before taking the file lock. Tests in `test_tools.py`.
- [x] **F. Failed cells keep evidence** (`kernels.py`): the completed calls' outputs go into the kernel variable
  `last_outputs[N]` (bounded at 300,000 characters) and the error says so; a call cut off by the failure reads
  "cancelled when the cell stopped at an uncaught error" instead of "interrupted or timed out". The `code`
  description now names the missing `return_exceptions`, gives a `safe()` wrapper verified in Monty, mentions
  `last_outputs`, and says images need a direct `read`. Returning errors as strings instead of raising was
  rejected: it would silently change what every tool returns inside a cell. Test in `test_kernels.py`.
- [x] **G. Read-only tools outside the workspace** (`grep`, `glob`, `ls`, via `walk.search_base`): allowed like `read`;
  results are absolute paths; the permission key was already absolute, so `"grep(/Users/*)" = "deny"` still gates
  it; symlinks out of a walk are still not followed. Changes old-Nexus behavior: porting-ledger row. Tests in
  `test_search_tools.py` (the three "refuses outside" asserts became positive checks).
- [x] **H. `cd` and `timeout`** — `bash` description: every call starts fresh in the workspace, `cd` does not carry over,
  never `cd` to the workspace itself; do not wrap commands in `timeout` (missing on macOS). Also dropped its stale
  "Users can control background PTYs" (plan 41 made jobs pipes).
- [x] **I. NSH tests** — `AGENTS.md` (loaded by every agent here) and `docs/development.md` give the one command
  (`node scripts/nsh-web.mjs test --target web <file>`), say no `--browser`/llvm `PATH` is needed, forbid `npx nsh`,
  and point visual checks at the two screenshot skills. Deliberately **not** in `build.md`: that prompt ships to every
  project.
- [x] **J.** `web_fetch` accepts JSON (`application/json`, `text/json`, `+json`), re-indented for paging.
  `web_search`'s unreachable error now says it will keep failing, not to start the service or retry, and to use
  `web_fetch` or tell the user; the user's start options follow (Core still never starts it, porting ledger). A no-op
  entry inside an `edits` list must still match but is skipped with a note; a lone no-op is still an error.

## Not done, and why

- The one poisoned session (`ses_1a1cd54815b36cee45b`) stays as it is: its dangling calls are already followed by a user
  message, and repairing that would rewrite history. Start a new session from it if needed.
- Plan 34's items (fallback scope, live-catalog validation, waiting on network errors) belong there.
- Background-job pipeline status (D) waits for the concurrent `bash_jobs.py` work.

## Measure again

Run after a week of use (read-only; adjust the date):

```sql
-- A: shell writes vs edit tools
select sum(name in ('edit','patch','write')) edit_tools,
       sum(name='bash' and (arguments like '%write_text(%' or arguments like '%sed -i%' or arguments like '%cat > %')) shell_writes
from turn_tool where started > strftime('%s','2026-11-13');
-- B: turns that ended without a recorded outcome, and provider 400s for missing tool results
select count(*) from turn where error like '%without a recorded outcome%' and started > strftime('%s','2026-11-13');
select count(*) from turn where error like '%tool_use%without%tool_result%' and started > strftime('%s','2026-11-13');
-- D: exit-0 outputs that name a hidden failure, and how many now carry the note
select count(*), sum(output like '[exit code 0 · %') from turn_tool
where name='bash' and output like '[exit code 0%' and (output like '% failed%' or output like '%command not found%')
  and started > strftime('%s','2026-11-13');
-- E/F: missing paths and failed code cells that kept outputs
select count(*), sum(output like '%did you mean%') from turn_tool where output like '%does not exist%' and started > strftime('%s','2026-11-13');
select count(*), sum(output like '%last_outputs%') from turn_tool where name='code' and is_error=1 and started > strftime('%s','2026-11-13');
-- G/H/I
select count(*) from turn_tool where output like '%is outside the workspace%' and started > strftime('%s','2026-11-13');
select count(*) from turn_tool t join session s on s.id=t.session_id where t.name='bash'
  and t.arguments like '%"command":%"cd ' || s.directory || ' &&%' and t.started > strftime('%s','2026-11-13');
select count(*) from turn_tool where name='bash' and (arguments like '%ms-playwright%' or arguments like '%npx nsh%')
  and started > strftime('%s','2026-11-13');
```

Baselines from these queries over the two days before 2026-11-12 14:00: 939 bash calls starting `cd <workspace> &&`
(the 1,152 above also counted `;`), 46 "does not exist" results with no suggestion.

If a number does not move: A → consider the `bash` guard; H → consider stripping a leading `cd <workspace> &&` in the
tool; I → wrap the command in `scripts/nexus`.
