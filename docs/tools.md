# Tools

`nexus/tools/` holds the six task tools and the two companion tools for
background bash. They act on the workspace, except `ask`, which asks the user a
question and returns the answer as data. Changing Nexus itself (session,
workspace, worktree, model) is never a tool (PLAN.md, "Task tools vs. control").

`DEFAULT_TOOLS = (read, bash, edit, write, ask, code)`.
`BASH_JOB_TOOLS = (bash_output, bash_kill)` is granted with `bash` (unless
disabled individually), because a background command needs a way to be read and
stopped. Agents may also name a job tool explicitly; disabling `bash` removes
the automatic pair. A `Tool` is a `ToolSpec` (name, description and JSON schema,
what the model sees) plus `async run(args, ctx)` that returns text or raises
`ToolError`, plus `guidance`: short system-prompt advice rendered only when the
tool is available. `ToolContext` holds the `workspace` path, the model call id, the session's file
guard and whether the result is model-visible.

| File | Role |
| --- | --- |
| `tools/base.py` | `Tool`, `ToolContext`, `ToolError`, `resolve()`, `clip()` |
| `tools/read.py`, `bash.py`, `edit.py`, `write.py` | Workspace tools |
| `tools/ask.py`, `questions.py` | `ask` spec and the validated question/reply contract |
| `tools/code.py`, `kernels.py` | Code spec and daemon-owned Monty kernels |
| `observations.py` | Per-session read observations and the read-before-edit guard |
| `bash_jobs.py` | Daemon-owned background bash jobs |

## Shared rules

**Workspace confinement** (`resolve`). `path` must be a non-empty string.
Relative paths are relative to the workspace. The path is resolved (symlinks
followed) and must be the workspace or inside it, otherwise `ToolError("... is
outside the workspace ...")`. `read`, `edit` and `write` all use it. `bash` is
not sandboxed: it starts in the workspace but can reach anything the user can
(PLAN.md, known gaps).

**Clipping** (`clip`). Output over 30,000 characters (`MAX_OUTPUT_CHARS`) keeps
the first and last 15,000 and puts this line between them:
`[… clipped N of TOTAL characters …]`. Clipping is stated in the output the model
sees, never silent.

**Errors.** `ToolError` text goes back to the model as an error result. See
[loop.md](loop.md#tool-execution) for the other exceptions the loop catches.

**Approval.** Tools do not ask for permission. The loop calls an `approve` callback before each
call ([loop.md](loop.md#approval)). The daemon auto-approves `read`, `bash_output`,
`bash_kill`, `ask`, `edit`, `write` and the outer `code` call; `edit` and `write`
are approval-free inside the session directory by fixed policy, with no setting
to change that. `bash` (including `background`) still asks unless the session's
permission is `allow` (`--yes`). `ask` is not an approval: its answers are data
for the model, never a grant. Path confinement and read freshness still apply to
every in-directory edit.

**Read observations and freshness.** A direct `read` whose result enters the
model's context records an observation for that session:
`nexus/observations.py`, table `file_observation`. An observation holds the
resolved absolute path, a `sha256` content revision (never mtime alone), the
total line count, the line ranges that reached context, clipped line numbers,
whether the whole output was clipped, and the read tool call id. Observations
are stored in SQLite and survive a daemon restart; an old observation is never
rebuilt from the file's current contents. Reads are staged until their successful
tool results are saved into context. A read and edit in the same provider tool
batch cannot establish new coverage for that edit; the model must first receive
the read result. Completed reads recovered on interruption can establish
coverage when their results are saved; unreported reads never do.

Before changing an existing file, the tool asks the session's guard whether the
current revision is covered:

* **Replacement edit and patch hunks** need coverage of the original lines they
  change. A pure insertion needs its anchor line (or the last line when
  appended, or a complete read of an empty file).
* **Whole-file overwrite, deletion and move** need complete coverage: every line,
  or a read of an empty file. Several windows add up only while they share one
  revision.
* A line clipped at 2,000 characters is not covered. An output clipped at 30,000
  characters establishes no coverage at all; read a smaller window.
* A missing observation, a different revision, or uncovered lines raises a
  `ToolError` naming the file and requiring a fresh read.
* A directory listing is not a file read. A read by another session does not
  count. A read inside a `code` kernel is retained by the kernel, not shown to
  the model, so it records nothing; the nested edit still needs a direct read.

After a successful edit, patch or write, affected observations are dropped, so
the next change needs a fresh read of the new revision. Creating a new file
needs no read, but an existing destination is an overwrite (or a refused
`Add File`). Nexus file mutations are serialized by one in-process lock per
daemon; freshness is checked again inside it immediately before writing. A
multi-file patch validates every affected file before writing any. New-file
writes use exclusive creation so a destination appearing at write time is not
overwritten. Replacement edits preserve existing newline bytes; patch coverage
uses the same visible line numbering as `read`, including Unicode separators.

**Enforcement boundary.** This protects the `edit` and `write` tools, direct and
nested. Unrestricted `bash` can still write files without those tools, so the
invariant is not a filesystem sandbox; full enforcement needs a sandbox or
restricted command execution (PLAN.md, known gaps). External processes can also
change a file between the in-process check and the write; the hash detects a
change up to that point, but the lock does not control other processes.

**Session snapshot.** Each session stores the descriptions, JSON schemas and
guidance of the tools it was created with ([agents.md](agents.md#session-snapshots)).
Execution is rebound by name through the registry on each turn. A tool disabled
at creation is absent from the stored definitions, the provider request and the
system prompt. A later switch removes or adds the definition for subsequent
turns and appends a visible `[Nexus notice: tool availability changed]` message;
the frozen system prompt is not rewritten, and a running call keeps the tools it
started with. Explicit updates are rebound before each subsequent model/tool
call, including calls later in the same turn. Nested code cells see exactly the effective task tools, so a
disabled tool is not callable there either.

## read

Read a text file with line numbers, or list a directory.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `path` (required) | string | | File or directory |
| `offset` | integer | 1 | First line, 1-based |
| `limit` | integer | 2000 | Number of lines |

* `offset` and `limit` below 1 raise `ToolError`, and so does an `offset` past the
  last line of a non-empty file.
* A directory returns `"<path> is a directory (N entries):"` and the sorted
  names, directories with a trailing `/`.
* A missing path raises an error. A file with a NUL byte in its first 8192 bytes
  is refused as binary. An empty file returns `"<path> is empty"`.
* Lines are `{number:>6}<TAB>{text}`. A line over 2000 characters is cut and ends
  with `[… line clipped at 2000 of N characters]`.
* A footer `[lines A–B of N]` is added when the window is not the whole file.
* The whole output is also subject to the 30,000-character clip. A clipped
  output establishes no freshness coverage: the clip and any clipped lines are
  recorded, but no line range from that call authorizes an edit.
* File bytes are decoded as UTF-8 with replacement.
* A direct read records an observation for this session (above). A directory
  listing does not, and neither does a read inside a `code` cell.

## bash

Run a command with `/bin/bash -c` in the workspace.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `command` (required) | string | | Non-empty |
| `timeout` | number | none | Optional seconds; omitted means no time limit |
| `background` | boolean | false | Return a job id at once instead of waiting |

* stdout and stderr are combined; stdin is `/dev/null`.
* There is **no default time limit**. A foreground call waits until the command
  exits, the caller passes `timeout`, or the call is cancelled. Cancelling it
  (an interrupted turn, or a code cell's `timeout_s`) sends SIGKILL to the whole
  process group, because the command runs in its own session.
* The output starts with `[exit code N]`, or `[exit code N, no output]`, or
  `[killed after Ns timeout]`. A non-zero exit code is a normal result, not an
  error result. Output is decoded as UTF-8 with replacement and clipped at
  30,000 characters.
* `background: true` returns `started background job job_…` immediately; see
  below.
* Inside a `code` cell, the cell's own `timeout_s` still bounds a foreground
  call (and kills it); a background job outlives the cell.

## Background bash jobs

A background job runs in the caller's directory in its own process group,
independent of the turn and of any code cell. It is owned by the session tree
that started it: inside a child session, `bash_output` and `bash_kill` see the
tree's jobs, not another tree's.

* `bash_output {job_id, offset?, limit?}` returns status (`running`, `done`,
  `timeout`, `killed`, `error`), exit code, duration and output. `offset` and
  `limit` page through it; `limit` defaults to and is capped at 30,000
  characters. Output beyond 1,000,000 characters per job is dropped and the
  count is announced.
* `bash_kill {job_id}` kills the process group. A finished job is unchanged.
* When a job finishes, the runtime publishes `bash.finished` and adds a notice
  to the starting session's context, like a `wait: false` child: immediately if
  the session is idle (that starts a turn), or before its next model call if it
  is busy. The notice carries a 4,000-character tail and the job id for
  `bash_output`. A job killed because its session or the daemon is going away
  is silent.
* Concurrent launches share one admission lock, so they cannot exceed the live
  limit; shutdown waits for an in-flight launch before collecting processes.
  Explicit timeouts kill the process group even if the shell has exited while
  a descendant still holds its output pipe. Output decoding preserves UTF-8
  characters split across pipe reads.
* At most 16 jobs run at once and 64 are retained (running plus finished) for
  output; the oldest finished job is evicted first. They do not survive a
  daemon restart. Deleting a session or project kills its jobs; interrupting a
  turn does not.
* `GET /api/bash`, `GET /api/bash/{id}` and `DELETE /api/bash/{id}` inspect and
  kill jobs from any client. The inspector lists them beside kernels.

`bash` itself asks for approval like before; the job starts only after that
approval. Reading or killing a job does not ask, and `background` does not skip
the start approval. The job id is not a capability boundary for API clients (the
daemon token is), but the tools refuse another tree's job id.

## edit

Change existing files. The mode depends on the arguments: if `patch` is present
and not `null`, it is a patch; otherwise a replacement.

### Replacement

| Parameter | Type | Meaning |
| --- | --- | --- |
| `path` | string | Existing file |
| `old_string` | string | Non-empty text to find |
| `new_string` | string | Replacement; must differ from `old_string` |
| `replace_all` | boolean | Replace every occurrence |

`old_string` must occur exactly once unless `replace_all` is true. Errors: not
found, occurs N times (add context or set `replace_all`), identical strings, file
does not exist. Result: `edited <path> (N replacement[s])`. Before writing, the
session needs coverage of every original line an occurrence touches; with
`replace_all` that means all occurrences. No approval is requested.

### Patch (`apply_patch` format)

| Parameter | Type | Meaning |
| --- | --- | --- |
| `patch` | string | The patch text; use instead of the three replacement parameters |

This is the Codex `apply_patch` format that GPT models are trained on.

```
*** Begin Patch
*** Update File: src/app.py
@@ def greet(name):
     """Say hello."""
-    return "hi " + name
+    return f"hello {name}"
*** Add File: notes/todo.txt
+first line
+second line
*** Delete File: old.txt
*** End Patch
```

Format:

* Starts with `*** Begin Patch` and ends with `*** End Patch` (surrounding blank
  lines are ignored). Blank lines between operations are ignored.
* `*** Add File: <path>`: every following line starts with `+`; the file ends
  with a newline. Fails if the file exists.
* `*** Delete File: <path>`: fails if the file does not exist.
* `*** Update File: <path>`, optionally followed by `*** Move to: <new path>`
  (fails if the target exists). Then hunks:
  * `@@ <anchor>` starts a hunk. The anchor line is optional and is searched
    for first, from the end of the previous hunk. The first hunk may omit `@@`.
  * Hunk lines start with a space (context), `-` (remove) or `+` (add). An empty
    line counts as empty context.
  * `*** End of File` after a hunk anchors the hunk at the end of the file.
* Matching tries an exact comparison, then ignoring trailing whitespace, then
  ignoring leading and trailing whitespace. A hunk with no old lines is inserted
  after its anchor, or appended at the end of the file with no anchor or with
  `*** End of File`.
* An Update with no hunks is allowed only with `Move to`.
* Every operation is validated against the files first, and writes happen only
  if all pass, so a patch applies completely or not at all (a failure while
  writing is not covered). Parent directories are created. Patches over
  1,000,000 characters are refused.
* Freshness is checked per operation before any write: updates and hunks need
  coverage of the original lines they change; `Delete File` and every `Move to`
  need complete coverage; `Add File` needs no read but fails if the destination
  exists (and is rechecked before writing). A stale file rejects the whole patch.
* A successful patch drops the session's observations for every path it
  changed, so the next change needs a fresh read. No approval is requested.
* Result: one line per operation, e.g. `updated src/app.py (1 hunks)`,
  `added notes/todo.txt (2 lines)`, `deleted old.txt`,
  `updated a.py → b.py (1 hunks)`.
* All paths, including `Move to`, go through the workspace check. Operations are
  checked against the files as they are on disk, not against earlier operations
  of the same patch.

## write

Create a file or replace its whole content.

| Parameter | Type | Meaning |
| --- | --- | --- |
| `path` (required) | string | Created with parent directories |
| `content` (required) | string | The complete file |

Result: `created <path> (N lines, B bytes)` or `overwrote <path> (...)`. The
description tells the model to prefer `edit` for existing files. Creating a new
file needs no read. Overwriting an existing file needs a complete observation of
its current revision from this session; otherwise the tool refuses and asks for
a fresh read. A destination that appears while a create is in progress is
refused. After writing, the observation is dropped. No approval is requested.

## ask

`ask {questions: [...]}` asks the user one or more questions and waits for the
answers. It is a task tool, not a control tool and not an approval.

```jsonc
// arguments
{"questions": [
  {"id": "db", "question": "Which database?",
   "options": [
     {"id": "pg", "label": "PostgreSQL", "preview": "Version 16 and extensions"},
     {"id": "lite", "label": "SQLite"}
   ],
   "multiple": false,
   "allow_free_text": false},
  {"id": "notes", "question": "Anything else?"}   // no options: free text
]}

// reply submitted through Core
{"answers": [
  {"question_id": "db", "selected": ["pg"]},
  {"question_id": "notes", "text": "Keep migrations small."}
]}
```

The `ask` tool's `parameters` is the question JSON Schema. The reply is one
object; every question appears exactly once:

```jsonc
{"type": "object", "additionalProperties": false, "required": ["answers"],
 "properties": {"answers": {"type": "array", "minItems": 1, "items": {
   "type": "object", "additionalProperties": false, "required": ["question_id"],
   "properties": {"question_id": {"type": "string"},
                  "selected": {"type": "array", "items": {"type": "string"}},
                  "text": {"type": "string", "maxLength": 2000}}}}}}
```

| Question field | Meaning |
| --- | --- |
| `id` (required) | Stable within the request: 1-64 characters `[A-Za-z0-9_.:-]`, starting with a letter or digit. Duplicates are rejected. |
| `question` (required) | Non-empty text, at most 2,000 characters. |
| `options` | Up to 10 `{id, label, preview?}` entries; option IDs are unique within the question, labels are at most 100 characters and previews 1,000. Omit or use `[]` for free text. |
| `multiple` | Boolean, default `false`. `true` allows several selected option IDs and requires options. |
| `allow_free_text` | Boolean. Default `true` only when there are no options. With options, `true` lets the answer include a free-text note in addition to selections. |

Limits are 5 questions per call and 2,000 characters per answer text. Tool
arguments are validated completely before a pending request exists; a malformed
request is an ordinary tool error, so the model can correct itself.

Every question must be answered. `selected` holds option IDs (single-select
allows at most one; multi-select at least one unless free text is also allowed),
and `text` is the free-text answer. Unknown question or option IDs, duplicate or
missing answers, invalid selection counts, text where none is allowed and
oversized values are rejected before the wait resolves.

The wait has no tool-specific timeout: it ends with an answer, or with
cancellation when the turn is interrupted, its session or project is deleted, the
daemon shuts down, or the enclosing `code` cell times out or is cancelled. A
cancelled question publishes `question.replied` with `outcome: "cancelled"` and a
reason; a late answer cannot resume the work (409 while the session exists, 404
once it is deleted). Each request resolves exactly once, so duplicate or
conflicting replies create no second result or event. Recent resolutions are
remembered in a bounded tombstone set; an id older than that gets 404 and still
changes nothing.

The model-visible result is one JSON object keyed by question id. Its size is
bounded by the question/answer limits; generic text clipping is not applied to
this structured result, so valid answers remain complete, parseable JSON:

```json
{"db": {"selected": ["pg"], "labels": ["PostgreSQL"], "text": null}}
```

It is subject to the shared 30,000-character clip as a defensive backstop; the
validated limits keep a normal reply well below it.

`question.asked`/`question.replied` are stored session events and are attributed
to the asking session and tool call. Endpoints:
`GET /api/question`, `GET /api/session/{id}/question`, and
`POST /api/session/{id}/question/{qid}` ([api.md](api.md#endpoints)). The CLI
shows the questions, collects answers and submits them; the inspector shows the
request, reply or cancellation, and pending status. Child questions belong to the
child session and appear on its parent's descendant event stream.

Granted like any task tool: `ask` is in `DEFAULT_TOOLS`, so an agent that omits
`tools` gets it, while an explicit list or a session switch controls it. A session
snapshot stored before `ask` existed does not gain it. Inside `code`, `ask` is
available only when the caller has it, and questions it creates are attributed to
the nested call id.

## code

`code {code, kernel_id?, timeout_s?}` runs Python in a persistent Monty 1.1.0
sandbox. Omit `kernel_id` to use the session tree's main kernel (created on
first use); pass a live `krn_…` to share another kernel. Variables persist
between cells, including across sessions. Kernels survive session deletion,
but not daemon shutdown. At most eight kernels are live, with one cell at a
time per kernel. Waiting for a busy kernel counts toward the timeout.

```python
import asyncio
results = await asyncio.gather(read(path="README.md"), bash(command="git status --short"))
print(results[1])
len(results[0])
```

Only the caller's granted task tools are available inside cells; `code` and
control tools such as `session` are excluded. Each function takes keyword
arguments and returns the usual text result. It runs in the caller's directory
and uses the caller's approval policy. Saved tool references cannot retain a
previous caller's permissions. `code` itself asks no approval; nested calls do.
A failed or denied tool raises catchable `RuntimeError`. Each finished nested
call emits `code.call` with arguments, output, error, kernel, cell, index and
parent call id; a call stopped by a timeout or interrupt has an error and no
output. `code.cell` records the finished cell, source, stdout and value. These
events are durable in the calling session, even when intermediate output is
omitted from the model's context.

Results label the kernel id, cell number, duration and call count, followed by
stdout, the final expression's repr and a summary of nested calls. Errors show
the exception's last line. Stdout and results are clipped with explicit notices.

The whole-cell timeout defaults to 300 seconds (greater than zero, maximum
600), including tool calls. A timeout or interrupt cancels the cell's nested
calls before the result is reported: a running command is killed, a pending
approval is withdrawn, and no further call can change the workspace afterwards.
Monty separately bounds compute to ten seconds per feed, memory to 64 MiB,
recursion to 200 and suspensions to 256. Nested calls are capped at 64 per cell.
No mounts, filesystem or OS handler are provided.

Ordinary Python errors preserve the kernel. **Interrupts, active-cell host
timeouts, worker crashes, and resource-limit errors discard the kernel and its
variables**, with a `kernel.deleted` notice; the next default call creates a new
one. Monty cancellation closes its worker checkout. A timed-out queued cell
leaves the active kernel alone. Daemon shutdown stops any running cell, then
closes every worker and the pool. This is persistent notebook-style execution,
with no state recovery after forced cancellation or restart.

`GET /api/kernel`, `GET /api/kernel/{id}` and `DELETE /api/kernel/{id}` inspect
and delete kernels; deleting a busy kernel returns 409. Metadata retains the
last 100 cells and announces omitted cells. Variable inspection tracks assigned
names (up to 200) and returns type and repr previews (200 characters); Monty
has no `globals()`/`dir()`. This is an approximate listing, not a full Python
namespace inspector. Full cell history remains in session event logs.

Version 1 runs Monty's worker pool from the daemon; workers inherit its
environment. A scrubbed helper process remains required before 0.10.0.

### Live parent/child smoke test

From `nexus-core/` with Codex signed in:

```sh
uv run nexus run --agent orchestrate --model codex/gpt-6.1-terra "$(cat ../artifacts/shared_kernel_smoke.txt)"
```

The built-in orchestrator has `code` alongside read and bash. The prompt asks
it to store a dictionary, launch a build child that receives only the variable
name (not its contents), and verify the child's changes in a third cell. All
three code results should show the same `krn_…` and successive cell numbers.
The child computes 42 and sets `child_seen` to true. No files or bash are used
for communication. This sequence was verified live against Codex; the evidence
is in `artifacts/shared_kernel_live_proof.json`.
