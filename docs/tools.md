# Tools

`nexus/tools/` holds the four task tools. They act on the workspace. Changing
Nexus itself (session, workspace, worktree, model) is never a tool (PLAN.md,
"Task tools vs. control").

`DEFAULT_TOOLS = (read, bash, edit, write)`. A `Tool` is a `ToolSpec` (name,
description and JSON schema, what the model sees) plus `async run(args, ctx)`
that returns text or raises `ToolError`. `ToolContext` holds the `workspace` path.

| File | Role |
| --- | --- |
| `tools/base.py` | `Tool`, `ToolContext`, `ToolError`, `resolve()`, `clip()` |
| `tools/read.py`, `bash.py`, `edit.py`, `write.py` | One tool each |

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

**Approval.** Tools do not ask. The loop calls an `approve` callback before each
call ([loop.md](loop.md#approval)). The daemon auto-approves only `read`; `bash`,
`edit` and `write` ask unless the session's permission is `allow` (`--yes`).

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
* The whole output is also subject to the 30,000-character clip.
* File bytes are decoded as UTF-8 with replacement.

## bash

Run a command with `/bin/bash -c` in the workspace.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `command` (required) | string | | Non-empty |
| `timeout` | integer | 120 | Seconds, capped at 600 |

* stdout and stderr are combined; stdin is `/dev/null`.
* The output starts with `[exit code N]`, or `[exit code N, no output]`, or
  `[killed after Ns timeout]` when the timeout hit. On timeout the whole process
  group is sent SIGKILL (the command runs in its own session).
* A non-zero exit code is a normal result, not an error result. The output is
  decoded as UTF-8 with replacement and clipped at 30,000 characters.

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
does not exist. Result: `edited <path> (N replacement[s])`.

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
description tells the model to prefer `edit` for existing files.
