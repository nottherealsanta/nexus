# Tools

`nexus/tools/` holds the twelve task tools (read, grep, glob, ls, bash, edit,
patch, write, web_search, web_fetch, ask, code) and the two companion tools for background bash. They act on the workspace, except `ask`, which asks the user a
question and returns the answer as data. Changing Nexus itself (session,
workspace, worktree, model) is never a task tool; it is the control plane
(PLAN.md, "Task tools vs. control"). The workspace controls in this document are
control tools: they are defined in `nexus/workspace.py`, executed by the daemon,
and are not part of `DEFAULT_TOOLS` or `nexus/tools/`.

`DEFAULT_TOOLS = (read, grep, glob, ls, bash, edit, patch, write, web_search, web_fetch, ask, code)`.
`BASH_JOB_TOOLS = (bash_output, bash_kill)` is granted with `bash` (unless
disabled individually), because a background command needs a way to be read and
stopped. Agents may also name a job tool explicitly; disabling `bash` removes
the automatic pair. A `Tool` is a `ToolSpec` (name, description and JSON schema,
what the model sees) plus `async run(args, ctx)` that returns text or raises
`ToolError`. The spec is the only place a tool is described: the system prompt
does not list tools, so each tool is described once, in the request, and a
disabled tool is described nowhere. `ToolContext` holds the `workspace` path, the model call id, the session's file
guard and whether the result is model-visible.

| File | Role |
| --- | --- |
| `tools/base.py` | `Tool`, `ToolContext`, `ToolError`, `resolve()`, `clip()` |
| `tools/read.py`, `bash.py`, `edit.py`, `patch.py`, `write.py` | Workspace tools |
| `tools/web_search.py`, `web_fetch.py`, `_html_markdown.py`, `net.py` | Web tools, the HTML-to-Markdown converter and the public-only HTTP client |
| `tools/grep.py`, `glob.py`, `ls.py`, `walk.py`, `_grep_scan.py` | Search and listing tools, the shared walk and the fallback scan worker |
| `tools/ask.py`, `questions.py` | `ask` spec and the validated question/reply contract |
| `tools/code.py`, `kernels.py` | Code spec and daemon-owned Monty kernels |
| `observations.py` | Per-session read observations and the read-before-edit guard |
| `bash_jobs.py` | Daemon-owned background bash jobs |
| `workspace.py`, `worktrees.py` | Workspace control tools, directory moves and Git worktree operations |

## Permission rules

`allow`, `ask` or `deny` per tool and per pattern, from
`[tools.permission]` in the config file and from an agent file's `permission:`
map (`nexus/permissions.py`, adapted from old Nexus). A rule is `tool` (every
call) or `tool(pattern)`:

```toml
[tools.permission]
bash = "ask"                       # every command asks, even in an allow session
"bash(git status*)" = "allow"      # except this one: no prompt, even in an ask session
"bash(git push*)" = "deny"         # never runs; the model gets an error naming the rule
"read(*.env)" = "deny"
"edit(src/*)" = "allow"
web_fetch = "ask"
"web_fetch(https://docs.python.org/*)" = "allow"
```

* **Key.** The pattern is matched against the call's key: `bash`, the command;
  `read`, `grep`, `glob`, `ls`, `edit`, `patch`, `write`, the path (relative
  to the session directory with `..` and symlinks resolved, or absolute when
  outside it; a missing `grep`/`glob`/`ls` path is the workspace, `.`); an
  `patch` has one key per file it touches (an `edit(...)` rule governs `patch` too); `web_fetch`, the URL without
  credentials, query or fragment and with a lowercased host; `web_search`, the
  query with spaces collapsed. Every other tool has no key, so only a bare
  `tool` rule matches it. `tool` may be a glob (`mcp__github__*`). In a pattern
  `*` matches any characters including `/`, `?` one character, `\` escapes;
  matching is case sensitive.
* **Order.** Any matching `deny`, from either source, wins. Otherwise the last
  matching rule in `[tools.permission]` (file order) decides `allow` or `ask`.
  The agent's rules (`ask` and `deny` only) add an `ask`. A call no rule
  matches is decided as before: the session's `permission`, and the tools that
  never ask. For a patch, a `deny` or `ask` pattern matches if *any* file does,
  and an `allow` pattern only if *every* file does.
* **bash.** A compound command (`;`, `&&`, `|`, a newline, `$(…)`, backticks,
  an unclosed quote) is never matched by an `allow` pattern. `deny` and `ask`
  patterns are tested on the whole command and on each part, so
  `"bash(rm -rf*)" = "deny"` also stops `cd build && rm -rf .`.
* **Where it applies.** Every tool call, including calls nested in a `code`
  cell. A denied call is an error result: `bash was refused by permission rule
  `bash(git push*)` (config); do not retry it…`. A rule that made a call ask adds
  `rule` to `permission.asked`.
* **Live and frozen.** Config rules are re-read when the file changes, so an
  edit applies to running sessions at their next call. Agent rules are part of
  the session's frozen agent snapshot.
* **Errors.** A rule that does not parse is skipped and listed in the config
  `errors` (`tools.permission: …`); the valid rules still apply, so a typo never
  drops a `deny`. A write through `PATCH /api/config` with a bad rule is
  refused. A bad agent rule makes the agent file invalid (listed in `problems`).
* **Limits.** This is a guard against mistakes, not a sandbox. A rule on `read`
  does not stop `bash` from running `cat`. Rule tables per tool
  (`[tools.permission.bash]`) are not supported, because TOML orders sub-tables
  after plain keys and would change which rule is last.

## Shared rules

**Workspace confinement** (`resolve`). `path` must be a non-empty string.
Relative paths are relative to the workspace. The path is resolved (symlinks
followed) and must be the workspace or inside it, otherwise `ToolError("... is
outside the workspace ...")`. `read`, `edit` and `write` all use it, except that `read` may
read any path outside the workspace (allowed by default; the key is the absolute path, so a rule such as
`"read(/Users/*)" = "ask"` gates it). `bash` is
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
`bash_kill`, `ask`, `edit`, `write`, the outer `code` call, the `session` and
`message_session` and `escalate` control tools and every workspace control tool; `edit` and `write`
are approval-free inside the session directory by fixed policy, with no setting
to change that. `bash` (including `background`) still asks unless the session's
permission is `allow` (`--yes`), which is the default for new sessions. `ask`
is not an approval: its answers are data for the model, never a grant. Path confinement and read freshness still apply to
every in-directory edit. Activation and moving do not ask a question. [Permission rules](#permission-rules)
can make any of these ask or refuse, and let `bash` or the web tools run without asking.

<a id="read-observations-and-freshness"></a>
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
of the tools it was created with ([agents.md](agents.md#session-snapshots)).
Execution is rebound by name through the registry on each turn. A tool disabled
at creation is absent from the stored definitions, the provider request and the
system prompt. A later switch removes or adds the definition for subsequent
turns and appends a visible `[Nexus notice: tool availability changed]` message;
the frozen system prompt is not rewritten, and a running call keeps the tools it
started with. Explicit updates are rebound before each subsequent model/tool
call, including calls later in the same turn. Nested code cells see exactly the effective task tools, so a
disabled tool is not callable there either.

## Workspace control tools (on demand)

Ordinary new sessions start with one lightweight control entry point,
`expose_workspace_tools`. The detailed controls (`change_session_directory` and
`worktree`) are absent from the request schemas and from the system prompt until
the model asks for them. They are control tools, never task tools, and they
never ask for approval or an `ask` question.

| Tool | Arguments | Effect |
| --- | --- | --- |
| `expose_workspace_tools` | none | Activate the two tools below for this session |
| `change_session_directory` | `directory` | Move where this session's tools run |
| `worktree` | `action` (`list`/`create`/`delete`), `path?`, `branch?`, `base?` | List, create or delete Git worktrees |

### Activation

- **New sessions** get the entry point automatically, whether root or child,
  unless `disabled_tools` contains it. An agent's `tools` list does not grant or
  remove it. `expose_workspace_tools` is not in `DEFAULT_TOOLS`; it is added by
  the session snapshot.
- **Existing sessions** keep their stored snapshot and do not silently gain the
  entry point or the two tools. `POST /api/session/{id}/workspace-tools`
  activates them explicitly (also for a reconstructed old session).
- The first call sets `workspace.activated` in the session snapshot, adds both
  definitions to the stored tool definitions and returns both **descriptions,
  JSON schemas** as its model-visible result. The next provider
  request carries both schemas. Printing definitions alone would not make them
  callable.
- Activation is **idempotent**: a second call changes nothing and returns one
  short sentence. It is persisted with the snapshot, so it survives turns and
  daemon restarts.
- Activation does **not** rewrite the frozen system prompt. The entry point's own
  guidance is frozen there; the tool result is the forward guidance for the two
  new tools.
- Children activate independently. A parent's activation does not reach a child,
  and a child's does not reach the parent.
- `disabled_tools` can switch any of the three off individually. Activation
  never re-enables a disabled tool.
- The control tools are never bound inside `code`: a cell can call only the
  caller's task tools. `session`, `message_session`, `expose_workspace_tools`,
  `change_session_directory` and `worktree` are all excluded
  (`agents.NEXUS_CONTROL_TOOLS`).

### `change_session_directory {directory}`

Moves the session to an **existing directory**. It does not create a worktree,
create directories, or move files. Relative paths resolve from the current
session directory; the result stores the resolved absolute path. A directory
that is the current one is a successful no-op ("already works in …") with no
event.

A successful change:

- persists the new resolved directory on the session; the project stays the same
  (project identity is separate from where the session runs);
- reads `<new directory>/AGENTS.md`, else the nearest parent's up to the Git root (no nested search) and
  includes its labelled `# Instructions file` section in the announcement, with
  `Status: loaded`, `not found`, `unreadable` or `too large` and the error or
  limit stated;
- announces the previous and current absolute paths, the current branch and
  repository when they exist, and says that later calls resolve from the new
  directory;
- leaves earlier messages, the frozen system prompt and the instructions file
  recorded at session start unchanged (the snapshot keeps the starting
  directory, the original instruction file, and the last 20 moves);
- emits `session.directory_changed` with from/to, branch, repository/worktree,
  instruction-file status and the frozen directory, then `session.updated`.

**Timing.** The change is never applied inside a running tool call. A tool call
keeps the `ToolContext.workspace` it started with. When the model calls
`change_session_directory`, the validated move is queued and applied at the next
tool/model dispatch boundary (or when the turn ends); the next direct and nested
task calls then resolve from the new directory. A client request while a turn is
running gets **202** with `directory_change.status: "pending"`; the pending
target appears in session JSON. It is applied at the next boundary, or when the
turn ends. A second pending request for the same session gets 409. While idle,
the API applies the move immediately and returns **200**.

A client-driven move inserts the same announcement as a user message: now if the
session is idle, or before the next model request if a turn is running. A
tool-driven move normally uses its tool result as the announcement. If applied
at turn teardown without a saved result, Core adds forward context instead.
Destination instructions are reread when a pending move applies; if they differ
from a tool's earlier result, a forward notice carries the actual instructions.

Children created after the move inherit the new directory; existing children
keep theirs. File observations are keyed by resolved absolute path, so reading
`a.txt` in the old directory never authorizes changing a different `a.txt` in
the destination.

**Reporting while moved.** While the live directory differs from the frozen
prompt's starting directory, `bash` results begin with `[cwd: <directory>]`, and
`read`, `edit` and `write` report resolved absolute paths in their results and
errors. A file read also starts with `[path: <absolute path>]`. This keeps the
difference visible without rewriting the cached prompt.

### `worktree {action, path?, branch?, base?}`

All operations use Git subprocesses with argument arrays (never an interpolated
shell), against the repository of the session's current directory. Paths from
Git's `worktree list --porcelain` decide what exists; a requested path is never
trusted. Relative paths resolve against the main checkout's root.

- **`list`** returns the repository, main checkout and each worktree's path,
  head, branch, `main`/`detached`/`bare`/`locked`/`prunable` flags and
  working-tree state (`dirty`, `changes`). It emits `worktree.listed`.
- **`create`** creates a worktree at `path`, or returns an existing registered
  one (`reused: true`). `branch` reuses that branch when it exists, otherwise
  creates it; `base` is allowed only for a new branch and defaults to `HEAD`.
  The worktree is created or reused and its path returned; **the session
  directory does not change** — call `change_session_directory` to work there.
  Uncommitted changes in the original checkout are untouched. A path that exists
  but is not a registered worktree of this repository is refused, never reused
  or replaced, and the parent directory must exist. Emits `worktree.created`.
- **`delete`** removes a registered worktree with `git worktree remove` (no
  `--force`). It refuses the main checkout, a bare, locked, missing/prunable or
  dirty worktree, and a path used by any session (all projects), a project, or
  targeted by a pending directory change. Change those sessions' directories or
  remove those projects first. Branches are never deleted. Emits
  `worktree.deleted`.
- Create and delete run under one daemon lock, so conflicting operations are
  serialized and delete revalidates the real state after waiting.
- Git failures are reported with Git's own message. A session outside a Git
  repository gets a clear error. Worktree tool calls never change the session.

API and CLI: `GET`/`POST /api/session/{id}/worktrees`,
`POST /api/session/{id}/directory`,
`POST /api/session/{id}/workspace-tools`, and
`nexus workspace expose|directory|list|create|delete --session …`
([api.md](api.md#endpoints), [daemon.md](daemon.md#workspace-controls)).

## read

Read a text file with line numbers, view an image file, or list a directory.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `path` (required) | string | | File or directory |
| `offset` | integer | 1 | First line, 1-based |
| `limit` | integer | 2000 | Number of lines |

* `offset` and `limit` below 1 raise `ToolError`, and so does an `offset` past the
  last line of a non-empty file.
* A directory returns `"<path> is a directory (N entries):"` and the sorted
  names, directories with a trailing `/`.
* An image file (PNG, JPEG, GIF or WebP, recognised by its bytes) is stored as a session artifact and returned to the
  model as an image beside a one-line text, like an MCP image ([llm-providers.md](llm-providers.md)). A model without
  image input gets the text and a "not sent" note. It records no observation, and inside a `code` cell it is an error.
* A missing path raises an error. A file with a NUL byte in its first 8192 bytes
  is refused as binary. An empty file returns `"<path> is empty"`.
* Lines are `{number:>6}<TAB>{text}`. A line over 2000 characters is cut and ends
  with `[… line clipped at 2000 of N characters]`.
* A footer `[lines A–B of N]` is added when the window is not the whole file.
* After a directory move, the result starts with `[path: <absolute path>]` and
  errors/listings use the absolute path ([tools.md](tools.md#workspace-control-tools-on-demand)).
* The whole output is also subject to the 30,000-character clip. A clipped
  output establishes no freshness coverage: the clip and any clipped lines are
  recorded, but no line range from that call authorizes an edit.
* File bytes are decoded as UTF-8 with replacement.
* A direct read records an observation for this session (above). A directory
  listing does not, and neither does a read inside a `code` cell.
* Startup recovery restores a read's observation only when it puts that read's
  saved result back into conversation history. A read whose result is unknown
  grants no coverage, so recovery cannot authorize an edit from content the
  model never received ([daemon.md](daemon.md#crash-recovery)).

## grep, glob, ls

Adapted from old Nexus. All three are read-only, confined to the session
directory (a path outside it, or a symlink out of it, is refused or skipped),
auto-approved, and record **no** read observation: a match or a listing is not
a read, so a file still has to be `read` before `edit`. Output is clipped to the
30,000-character rule, and every cut says so in the result.

The walk (`tools/walk.py`) is what Git would show in a work tree (tracked and
untracked, minus `.gitignore`d, recomputed on every call so a file written a
moment ago is found) and a plain walk elsewhere. Symlinks are never followed or
listed, `.git` is never entered, hidden (dot-prefixed) entries are skipped
unless `include_hidden`, and the walk stops at 50,000 entries (announced).

### `grep {pattern, path?, glob?, regex?, case_insensitive?, include_hidden?, max_matches?}`

`path` is a directory or a single file (default: the workspace); a missing path
is an error. Lines are `path:line:text`, sorted by path, with paths relative to
the workspace (so they can go straight into `read`). `regex` defaults to true; `max_matches`
defaults to 100 and is capped at 2000, after which `[showing the first N
matches; there are more …]` is added. A line over 2000 characters is cut and
marked. `no matches` is the empty result. Files over 2 MiB and binary files
are skipped. A search over 20 s is stopped with an error.

With `rg` on PATH the search is `rg` (Rust regex syntax, which cannot
backtrack catastrophically). Without it, a Python `re` scan runs in a
killable subprocess (`_grep_scan.py`, started with `-I`) over the same file list,
so a runaway pattern is killed, not run in the daemon.

### `glob {pattern, path?, include_hidden?}`

A shell glob relative to `path`; `**` spans directories and a trailing `/` matches
directories only. Returns up to 200 sorted paths (directories end in `/`); more
adds `[showing 200 of N matches …]`. At most 4 `**` segments and 1024 characters.

### `ls {path?, include_hidden?}`

One directory as it is on disk (it does not apply `.gitignore`). Directories end
in `/`, a symlink in `@` (`@ -> target` only when the target is inside the
workspace). At most 1000 entries, then a clip notice. An empty directory returns
`(empty directory)`.

## web_search

`web_search {query, limit?, page?}` queries the SearXNG at `[web] search_url`
([config.md](config.md)), the service in `nexus-search/` (Docker Compose or Apple
`container`; see its README). Core never starts a container: when the service is
down the error says how to start it. Only JSON is read (`json` must be an
enabled format in SearXNG's `settings.yml`, which the shipped one does).
`query` is at most 1000 characters, `limit` 1 to 10, `page` 1 or 2, the response
at most 512,000 bytes, the call 15 s.

The result is a header (`Search query`, `Provider`) and the results between
`<<< BEGIN UNTRUSTED SEARCH RESULTS >>>` and `<<< END … >>>`: title, URL,
snippet (1200 characters), source engines and date. Control characters are
removed, text that imitates the markers is defused, duplicate URLs are dropped,
and only `http(s)` URLs without credentials are kept. Links are never fetched
by this tool. Results over the output limit end with `[results clipped]`. HTTP
429, 403, other statuses, a timeout, an unreachable service and a non-SearXNG
body each give a distinct error.

## web_fetch

`web_fetch {url, offset?, limit?, timeout_s?}` fetches a public page and returns
Markdown between `<<< BEGIN UNTRUSTED WEB CONTENT >>>` and `<<< END … >>>`,
after a header with `Source`, `Final URL`, `HTTP status`, `Content-Type` and
`Title`. The header never contains credentials, query or fragment. HTML is
converted offline by `_html_markdown.py` (scripts, styles, forms and navigation
are dropped; limits on input, nodes, depth and output); `text/plain` and
Markdown are returned as they are; PDFs and other types are refused.
`offset`/`limit` select Markdown lines (1-based). The output is cut to the
30,000-character limit with `[showing lines A–B of N; use offset=… to read on]`,
a single line longer than the budget is cut and says so, and a download stopped
at 2,000,000 bytes says so. A status of 400 or more is an error.

`nexus/net.py` enforces the network policy for every hop:

* http or https only; no credentials, spaces or control characters in the URL;
* the host is resolved and **every** address must be globally routable.
  Loopback, private, link-local (including cloud metadata), unique-local,
  multicast, unspecified and IPv4-mapped forms are refused;
* the connection is made to the checked address, with the original `Host` and
  TLS server name, so the name cannot resolve differently afterwards;
* redirects (up to 5) are followed only within the same origin and re-checked at
  each hop; a redirect to another origin is an error naming it, and the model
  may fetch that URL itself;
* no proxy or environment settings are read.

Both web tools reach the network, so they are **not** auto-approved: a session
with permission `ask` prompts for each call, unless a [permission rule](#permission-rules) says otherwise.
The default agents without a `tools` list (`build`, `orchestrate`, `quick`, `plan`) and `scout`
have them.

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
that started it: inside a sub agent, `bash_output` and `bash_kill` see the
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
  is busy. The notice begins `→ Background Shell Done: <command>`, then a line
  with the job id, status, exit code and any dropped-character count, a
  4,000-character output tail (`[… earlier output omitted …]` when clipped) and
  a `bash_output(job_id=…)` hint. It is model context like any notice; the TUI
  and NSH render it as a context notice, not a user-message card, and transcript
  export treats it as a notice. A job killed because its session or the daemon
  is going away is silent.
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

`edit` and `patch` are two tools. `edit` replaces exact text in one file;
`patch` changes several places or files at once. `edit` takes no `patch`
argument (it says to use the `patch` tool), and `multiedit` no longer exists:
several replacements in one file are a patch with several hunks.

### `edit`

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

### `patch` (`apply_patch` format)

| Parameter | Type | Meaning |
| --- | --- | --- |
| `patch` (required) | string | The patch text |

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
answers. It is a task tool, not a control tool and not an approval. In the TUI,
use up/down and Enter to choose an option; for multiple choices, use Space to
toggle each option before Enter. The question card does not show an elapsed timer.

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
daemon crash also closes it: startup recovery publishes `question.replied` with
`outcome: "cancelled"`, so the old id cannot resume the turn. A cancelled
question publishes `question.replied` with `outcome: "cancelled"` and a
reason; a late answer cannot resume the work (409 while the session exists, 404
once it is deleted). Each request resolves exactly once, so duplicate or
conflicting replies create no second result or event. Recent resolutions are
remembered in a bounded tombstone set; an id older than that gets 404 and still
changes nothing. If the answer was durable before the crash, recovery restores
that answer as the call's result instead of inventing a cancellation.

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
sub agent and appear on its parent's descendant event stream.

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
with no state recovery after forced cancellation or restart. A daemon crash
loses every kernel; startup recovery says so for a recovered turn that used
`code`, and preserves the durable `code.call`/`code.cell` evidence without
claiming a successful call or recreating variables.

`GET /api/kernel`, `GET /api/kernel/{id}` and `DELETE /api/kernel/{id}` inspect
and delete kernels; deleting a busy kernel returns 409. Metadata retains the
last 100 cells and announces omitted cells. Variable inspection tracks assigned
names (up to 200) and returns type and repr previews (200 characters); Monty
has no `globals()`/`dir()`. This is an approximate listing, not a full Python
namespace inspector. Full cell history remains in session event logs.

The pool is started with `binary_path` set to a per-daemon launcher script
(`nexus-monty-*` in the system temp dir, mode 0700, removed at shutdown). It
`cd`s into an empty directory and runs the worker under `env -i`, so workers
never see the daemon's API keys, tokens or working directory. The worker still
has the daemon's uid and no OS-level sandbox (no seccomp or `sandbox-exec`);
containment rests on Monty having no filesystem, network or OS handler.

### Live parent/child smoke test

From `nexus-core/` with Codex signed in:

```sh
uv run nexus run --agent orchestrate --model codex/gpt-6.1-terra "$(cat ../artifacts/shared_kernel_smoke.txt)"
```

The built-in orchestrator has every task tool `build` has, including `code`. The prompt asks
it to store a dictionary, launch a build child that receives only the variable
name (not its contents), and verify the child's changes in a third cell. All
three code results should show the same `krn_…` and successive cell numbers.
The child computes 42 and sets `child_seen` to true. No files or bash are used
for communication. This sequence was verified live against Codex; the evidence
is in `artifacts/shared_kernel_live_proof.json`.


Workspace-control delivery details: activation describes enabled tools only and
labels switched-off tools unavailable. Worktree deletion includes users in
subdirectories and daemon-owned background jobs; a deletion reservation blocks
new session/project users and directory moves until removal finishes. An
unavailable working-tree status is a refusal, not evidence of a clean tree.
Git subprocess cancellation kills and reaps the process group before releasing
the operation lock. Git's NUL-delimited worktree metadata preserves unusual path
characters. External processes and other daemons remain outside these checks.

## User tools

A user tool is a Python file in `~/.nexus/tools/` or `<project>/.agents/tools/`
(the project's replaces a global one of the same name). `nexus/extensions.py`.

```python
NAME = "hello"                # optional; the file name by default (a-z, 0-9, _)
DESCRIPTION = "Greet someone."
PARAMETERS = {"type": "object", "properties": {"who": {"type": "string"}}, "required": ["who"]}
GUIDANCE = "Use to say hello."   # optional; appended to the description
TIMEOUT = 30                     # optional seconds, at most 600 (default 60)

def run(args):                   # or `async def`; returns text
    return f"Hello, {args['who']}!"
```

- **The daemon never runs the file.** Metadata is read with `ast.literal_eval`,
  so `DESCRIPTION` and the others must be literals. A call runs in a child
  Python (`-I`, a scrubbed environment of `PATH`, `HOME`, `LANG`, `TMPDIR`
  plus `NEXUS_WORKSPACE`, the session directory as cwd, killed as a process
  group on timeout or cancel). This is process quarantine, not a sandbox: the
  tool can do what the user can. Printed output is kept off the result channel.
- **Granting:** an agent with no `tools:` list gets every user tool; list a
  name to grant it to a restricted agent. A name may not shadow a built-in tool
  or start with `mcp__`. Approval follows the session's permission setting and
  the permission rules (`my_tool(...)` keys are not defined; use `my_tool` alone).
- **Frozen per session:** a new session's snapshot stores each granted tool's
  source (`snapshot.extensions`). A running session keeps running that code. If
  the file differs on disk, the result ends with a line saying so, so the
  timeline shows it. A new session uses the current file; an agent switch
  rebuilds the snapshot from the current files.
- **Errors:** an invalid file is a `problems` row in `GET /api/extension` and an
  `extension.failed` event. A tool that raises, times out or dies returns an
  error result; it never fails the turn.
- **Switches:** user tools appear in the starting-context tools grid and take
  per-session switches. A `[tools] disabled` entry in config is validated
  against built-ins and global user tools only.

## Skills and the `skill` tool

A skill is a folder `<name>/SKILL.md` in `<project>/.agents/skills/` or
`~/.agents/skills/` (the project's wins a name clash, and the clash is a
`shadowed` problem). `nexus/skills/`.

```markdown
---
name: deploy                 # optional; the folder name by default
description: Ship the app. Use when asked to deploy.   # required
allowed-tools: Read Grep     # parsed and shown; not enforced yet (see below)
---
# Deploy
Instructions…  (bundled files: scripts/…, references/…)
```

- **Lenient frontmatter.** Plain, quoted, folded (`>`) and literal (`|`) scalars,
  flow and block lists, one level of nested mapping and comments are read.
  Unknown keys (`license`, `metadata`…) are kept and listed as warnings, never
  rejected. A skill is turned away only for no frontmatter block, no
  description, an invalid name, more than 256 KiB, or invalid UTF-8; the file
  is a `problems` row (`kind: "error"`).
- **Progressive disclosure.** The `skill` tool's description lists
  `` `name`: description `` lines for the skills the session started with. `skill {name}`
  returns the body inside `<skill …>`, plus the bundled files it may read;
  `skill {name, resource}` returns one file under `scripts/` or `references/`
  as text. Nothing is run. Resources resolve fail-closed: no absolute paths,
  `..`, `~`, backslashes, other folders, links out of the skill, directories,
  binaries, or files over 128 KiB.
- **The tool exists only when a skill did.** A session whose start found no
  enabled skill has no `skill` tool and gets no skill notices (start a new
  session). An agent with a `tools:` list gets it by naming `skill`.
- **Frozen index, live body.** The body is read when the tool is called, so an edit is
  loaded as it is now (the result says the file changed if it differs from the session's
  record). A skill that is added or changed after the session started is **not** injected
  and not loadable in it (`skill` says "no skill named"): only a new session has it, and
  clients tell the user with `skills.changed`. The system prompt is never rewritten.
- **Switches.** `skill:<name>` entries in `disabled_tools`, from the context header (both
  clients). Only skills the session started with can be switched; one that was off at the start
  is `locked` and a PATCH that turns it on returns 400 ("start a new session"). A mid-session
  switch injects one JSON notice, visible in the timeline, and `skill` refuses a switched-off
  skill: `{"skill_unavailable": {"name": "x"}}`, `{"skill_available": {"name": "x"}}`. The
  session JSON carries `skills: [{name, description, scope, state: on|off, locked}]`.
- **API.** `GET /api/skill` (`?project=` or `?directory=`) lists
  `{skills: [{name, description, scope, path, sha256, allowed_tools, warnings}],
  problems: [{path, error, kind}]}`; `GET /api/skill/{name}` adds `body`,
  `extra` and `files`. `skills.changed` (live only) says a `SKILL.md`, a skill
  folder or an install changed.
- **Not yet:** `allowed-tools` narrowing while a skill is active (the result
  says it is not enforced), and the `npx skills` import.

### `/learn`: installing skills

`/learn <source>` (TUI and NSH composer, `skills.learn`) takes three steps: a modal for
**This project** (`<project>/.agents/skills/`) or **Every project** (`~/.agents/skills/`);
the fetch (`POST /api/skill/preview`); then a checklist of the skills found (Select all,
Select none, Invert; installed ones are tagged, and ticking one replaces it) and
`POST /api/skill/install`. If the source named skills (`owner/repo@name`, `--skill name`) and
none of them is installed, the checklist is skipped. It copies files; there is no npx and
nothing in a skill is run. `nexus/skills/install.py`.

| Source | Meaning |
| --- | --- |
| `owner/repo`, `https://github.com/owner/repo` | every skill in the repository |
| `owner/repo@name`, `… --skill name` | one skill (`--skill` takes several names) |
| `owner/repo/path/to/skills`, `…/tree/<ref>/<path>` | only that folder (and a branch or tag) |
| `git@host:o/r.git`, `https://host/o/r.git` | any Git remote |
| `./folder`, `../x`, `/abs`, `~/x`, `file:///abs` | a local folder; a bare `a/b` is always GitHub |

A leading `npx skills add` (or `skills add`) and the options `-g/--global`, `-y`,
`-a <agent>` are accepted; any other option is refused. A remote source is a shallow
`git clone` into a temporary folder (120 s limit, no prompts, only https, ssh and git; needs
`git`). `preview` keeps that folder for 10 minutes under a `fetch_id` and returns
`{fetch_id, source, scope, requested, problems, found: [{name, description, installed,
warnings}]}`; `install` takes `{fetch_id, skills, scope, project?, overwrite?}` and releases the
fetch (a failed install keeps it). `{source, …}` without a `fetch_id` fetches and installs in one
step (every skill, or the ones the source names). Every folder holding a `SKILL.md` below the
source (five levels deep) is a skill; unusable ones come back in `problems`. The copy leaves
out `.git`, symbolic links and special files, stops at 2000 files or 20 MiB, and goes through a
staging folder, so a failure leaves nothing half-installed. An installed skill is skipped
unless `overwrite` is true, which replaces the whole folder (local edits are lost; the result
says `replaced`). Verified against `anthropics/skills@frontend-design` on 2026-10-29. The
reply is `{installed: [{name, description, path, files, warnings, replaced}], skipped, problems,
available, scope}`, and `skills.changed` is published.
