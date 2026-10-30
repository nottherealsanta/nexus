"""`grep`: content search under the workspace.

Adapted from old Nexus nexus/tools/builtin/grep.py. Uses `rg` when it is on
PATH (its regex engine cannot backtrack catastrophically) and otherwise a
killable Python scan over the same `.gitignore`-aware file list. Matches are
`path:line:text` with paths relative to the workspace. A grep is a search, not a
read: it records no read observation, so a file still has to be read before it
is edited.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, clip, resolve
from .walk import glob_match, pattern_error, walk

DEFAULT_MATCHES = 100
MAX_MATCHES = 2000
MAX_PATTERN_CHARS = 4096
MAX_LINE_CHARS = 2000
TIMEOUT = 20.0


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern or "\x00" in pattern:
        raise ToolError("pattern must be a non-empty string")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ToolError(f"pattern is too long ({len(pattern)} characters; limit {MAX_PATTERN_CHARS})")
    file_glob = args.get("glob")
    if file_glob is not None and not isinstance(file_glob, str):
        raise ToolError("glob must be a string")
    if file_glob is not None and (problem := pattern_error(file_glob)):
        raise ToolError(f"glob {problem}")
    limit = args.get("max_matches", DEFAULT_MATCHES)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ToolError("max_matches must be a positive integer")
    limit = min(limit, MAX_MATCHES)
    regex = args.get("regex", True) is not False
    if regex:
        try:
            re.compile(pattern)
        except re.error as exc:
            if not shutil.which("rg"):  # with rg the pattern is rg syntax, so only rg can judge it
                raise ToolError(f"invalid pattern: {exc}") from exc
    root = resolve(ctx, args.get("path") or ".")  # a directory, or a single file
    if not root.exists():
        raise ToolError(f"{args.get('path')} does not exist")
    workspace = ctx.workspace.resolve()
    options = {"regex": regex, "ignore_case": bool(args.get("case_insensitive")),
               "hidden": bool(args.get("include_hidden")), "glob": file_glob}
    try:
        if shutil.which("rg"):
            matches, more = await asyncio.wait_for(_rg(workspace, root, pattern, limit, **options), TIMEOUT)
        else:
            matches, more = await asyncio.wait_for(_python(workspace, root, pattern, limit, **options), TIMEOUT)
    except TimeoutError as exc:
        raise ToolError(f"the search took longer than {TIMEOUT:g}s and was stopped; simplify the pattern "
                        "or narrow `path` and `glob`") from exc
    lines = []
    for rel, number, text in matches:
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + f" [… line clipped at {MAX_LINE_CHARS} characters]"
        lines.append(f"{rel}:{number}:{text}")
    if more:
        lines.append(f"[showing the first {len(matches)} matches; there are more. Narrow the pattern, path or glob, "
                     "or raise max_matches]")
    elif not matches:
        lines.append("no matches")
    prefix = f"[workspace: {workspace}]\n" if ctx.cwd_changed else ""
    return clip(prefix + "\n".join(lines))


async def _rg(workspace: Path, root: Path, pattern: str, limit: int, *, regex: bool, ignore_case: bool,
              hidden: bool, glob: str | None) -> tuple[list[tuple[str, int, str]], bool]:
    command = ["rg", "--null", "--no-heading", "--line-number", "--color", "never", "--sort", "path",
               "--max-filesize", "2M", "--max-columns", str(MAX_LINE_CHARS), "--max-columns-preview", "--no-messages", "--with-filename"]
    if not regex:
        command.append("--fixed-strings")
    if ignore_case:
        command.append("--ignore-case")
    if hidden:
        command += ["--hidden", "--glob", "!.git"]
    if glob:
        command += ["--glob", glob]
    relative = root.relative_to(workspace).as_posix()
    command += ["--regexp", pattern, "--", relative]
    process = await asyncio.create_subprocess_exec(
        *command, cwd=workspace, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    assert process.stdout is not None and process.stderr is not None
    matches: list[tuple[str, int, str]] = []
    more = False
    try:
        async for raw in process.stdout:
            path, _, rest = raw.decode("utf-8", "replace").rstrip("\n").partition("\0")
            number, _, text = rest.partition(":")
            if not number.isdigit():
                continue
            if len(matches) >= limit:
                more = True
                break
            matches.append((path.removeprefix("./"), int(number), text))
        if more:
            process.kill()
        error = (await process.stderr.read()).decode("utf-8", "replace").strip()
        code = await process.wait()
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    if code == 2 and not matches:
        raise ToolError(f"rg: {error or 'search failed'}")
    return matches, more


async def _python(workspace: Path, root: Path, pattern: str, limit: int, *, regex: bool, ignore_case: bool,
                  hidden: bool, glob: str | None) -> tuple[list[tuple[str, int, str]], bool]:
    if root.is_file():
        names = [root.relative_to(workspace).as_posix()]
    else:
        entries, _ = await walk(workspace, root, include_hidden=hidden)
        prefix = root.relative_to(workspace).as_posix()
        prefix = "" if prefix == "." else prefix + "/"
        names = [rel for rel, is_dir in entries
                 if not is_dir and (glob is None or glob_match(rel.removeprefix(prefix), glob)
                                    or ("/" not in glob and glob_match(rel.rsplit("/", 1)[-1], glob)))]
    request = json.dumps({"pattern": pattern, "regex": regex, "ignore_case": ignore_case,
                          "files": names, "limit": limit})
    process = await asyncio.create_subprocess_exec(
        str(Path(sys.executable).absolute()), "-I", str(Path(__file__).with_name("_grep_scan.py")), cwd=workspace,
        env={"PATH": os.defpath},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    try:
        out, err = await process.communicate(request.encode())
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    if process.returncode != 0:
        raise ToolError(f"search worker failed: {err.decode('utf-8', 'replace')[:200]}")
    response = json.loads(out)
    if "error" in response:
        raise ToolError(response["error"])
    return [(rel, number, text) for rel, number, text in response["matches"]], bool(response["stopped"])


TOOL = Tool(
    ToolSpec(
        name="grep",
        description="Search file contents for text or a regular expression. Returns `path:line:text` matches, "
                    "sorted by path. Respects .gitignore; skips binary and hidden files. A match is not a read: read the "
                    "file before you change it.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text or regular expression."},
                "path": {"type": "string", "description": "Directory or single file to search. Default: the workspace."},
                "glob": {"type": "string", "description": "Only search files matching this glob, e.g. `*.py`."},
                "regex": {"type": "boolean", "description": "Treat pattern as a regular expression. Default true."},
                "case_insensitive": {"type": "boolean", "description": "Default false."},
                "include_hidden": {"type": "boolean", "description": "Search dot-prefixed files. Default false."},
                "max_matches": {"type": "integer", "description": f"Default {DEFAULT_MATCHES}, at most {MAX_MATCHES}."},
            },
            "required": ["pattern"],
        },
    ),
    run,
)
