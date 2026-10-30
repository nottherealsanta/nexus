"""`glob`: workspace paths matching a shell pattern.

Adapted from old Nexus nexus/tools/builtin/glob.py. Respects `.gitignore` in a
Git work tree, skips hidden entries unless asked, and never follows symlinks.
"""
from __future__ import annotations

from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, clip
from .walk import glob_match, pattern_error, resolve_dir, walk

MAX_MATCHES = 200


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip("/ "):
        raise ToolError("pattern must be a non-empty string")
    if pattern.startswith("/"):
        raise ToolError("pattern must be relative to the search root")
    if (problem := pattern_error(pattern)) is not None:
        raise ToolError(f"pattern {problem}")
    root = resolve_dir(ctx, args.get("path"))
    pattern = pattern.removeprefix("./")
    dirs_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    prefix = root.relative_to(ctx.workspace.resolve()).as_posix()
    prefix = "" if prefix == "." else prefix + "/"
    entries, cut = await walk(ctx.workspace, root, include_hidden=bool(args.get("include_hidden")))
    matches = [(rel, is_dir) for rel, is_dir in entries
               if (is_dir or not dirs_only) and glob_match(rel.removeprefix(prefix), pattern)]
    shown = [rel + "/" if is_dir else rel for rel, is_dir in matches[:MAX_MATCHES]]
    lines = list(shown)
    if len(matches) > MAX_MATCHES or cut:
        counted = f"at least {len(matches)}" if cut else str(len(matches))
        lines.append(f"[showing {len(shown)} of {counted} matches"
                     f"{'; the walk stopped at ' + str(len(entries)) + ' entries' if cut else ''}; narrow the pattern or path]")
    elif not shown:
        lines.append("no matches")
    return clip("\n".join(lines))


TOOL = Tool(
    ToolSpec(
        name="glob",
        description="List files and directories whose path matches a shell glob such as `src/**/*.py`. "
                    "Paths are relative to the workspace; results are sorted. Respects .gitignore and skips hidden files.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob relative to `path`; `**` spans directories. "
                                                              "A trailing `/` matches directories only."},
                "path": {"type": "string", "description": "Directory to search under. Default: the workspace."},
                "include_hidden": {"type": "boolean", "description": "Include dot-prefixed entries. Default false."},
            },
            "required": ["pattern"],
        },
    ),
    run,
)
