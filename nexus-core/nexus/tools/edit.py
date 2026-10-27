"""`edit`: exact string replacement in one file, or a multi-file patch.

Two modes, chosen by which arguments are present:

* `path` + `old_string` + `new_string` (+ `replace_all`): `old_string` must
  occur exactly once unless `replace_all` is true.
* `patch`: the Codex `apply_patch` format that GPT models are trained on —
  `*** Begin Patch` … `*** End Patch` with `Add File`, `Delete File` and
  `Update File` (optionally `Move to`) sections and `@@ [anchor]` context hunks.
  Every operation is checked before any file is written, so a patch applies
  completely or not at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, resolve

MAX_PATCH_CHARS = 1_000_000


# -- Replacement ------------------------------------------------------------


def _replace(args: dict[str, Any], ctx: ToolContext) -> str:
    path = resolve(ctx, args.get("path"))
    old, new = args.get("old_string"), args.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or not old:
        raise ToolError("old_string (non-empty) and new_string must be strings")
    if old == new:
        raise ToolError("old_string and new_string are identical")
    if not path.is_file():
        raise ToolError(f"{args['path']} does not exist")
    text = path.read_text()
    count = text.count(old)
    if count == 0:
        raise ToolError(f"old_string was not found in {args['path']}")
    if count > 1 and not args.get("replace_all"):
        raise ToolError(f"old_string occurs {count} times in {args['path']}; add context or set replace_all")
    replaced = count if args.get("replace_all") else 1
    path.write_text(text.replace(old, new, replaced))
    return f"edited {args['path']} ({replaced} replacement{'s' if replaced != 1 else ''})"


# -- Patch ------------------------------------------------------------------


@dataclass
class _Hunk:
    anchor: str | None
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    end_of_file: bool = False


@dataclass
class _Op:
    kind: str  # add | delete | update
    path: str
    move_to: str | None = None
    lines: list[str] = field(default_factory=list)  # add: file contents
    hunks: list[_Hunk] = field(default_factory=list)


def parse_patch(text: str) -> list[_Op]:
    if len(text) > MAX_PATCH_CHARS:
        raise ToolError("patch is too large")
    lines = text.strip("\n").replace("\r\n", "\n").split("\n")
    if len(lines) < 2 or lines[0].strip() != "*** Begin Patch" or lines[-1].strip() != "*** End Patch":
        raise ToolError("patch must start with '*** Begin Patch' and end with '*** End Patch'")
    ops: list[_Op] = []
    i, end = 1, len(lines) - 1
    while i < end:
        line = lines[i]
        if line.startswith("*** Add File: "):
            op = _Op("add", line[len("*** Add File: "):].strip())
            i += 1
            while i < end and not lines[i].startswith("*** "):
                if not lines[i].startswith("+"):
                    raise ToolError(f"Add File lines must start with '+': {lines[i][:80]!r}")
                op.lines.append(lines[i][1:])
                i += 1
        elif line.startswith("*** Delete File: "):
            op = _Op("delete", line[len("*** Delete File: "):].strip())
            i += 1
        elif line.startswith("*** Update File: "):
            op = _Op("update", line[len("*** Update File: "):].strip())
            i += 1
            if i < end and lines[i].startswith("*** Move to: "):
                op.move_to = lines[i][len("*** Move to: "):].strip()
                i += 1
            hunk: _Hunk | None = None
            while i < end and (not lines[i].startswith("*** ") or lines[i] == "*** End of File"):
                current = lines[i]
                if current.startswith("@@"):
                    hunk = _Hunk(current[2:].strip() or None)
                    op.hunks.append(hunk)
                elif current == "*** End of File":
                    if hunk is None:
                        raise ToolError("'*** End of File' before any hunk")
                    hunk.end_of_file = True
                else:
                    if hunk is None:  # the first hunk may omit its @@ header
                        hunk = _Hunk(None)
                        op.hunks.append(hunk)
                    prefix, body = (current[:1], current[1:]) if current else (" ", "")
                    if prefix == " ":
                        hunk.old.append(body)
                        hunk.new.append(body)
                    elif prefix == "-":
                        hunk.old.append(body)
                    elif prefix == "+":
                        hunk.new.append(body)
                    else:
                        raise ToolError(f"hunk lines must start with ' ', '-' or '+': {current[:80]!r}")
                i += 1
            if not op.hunks and not op.move_to:
                raise ToolError(f"Update File {op.path} has no hunks")
        elif not line.strip():
            i += 1
            continue
        else:
            raise ToolError(f"unexpected patch line: {line[:80]!r}")
        ops.append(op)
    if not ops:
        raise ToolError("patch contains no operations")
    return ops


def _find(lines: list[str], block: list[str], start: int) -> int:
    """First index >= start where `block` matches, trying looser whitespace."""
    for normalize in (lambda s: s, str.rstrip, str.strip):
        wanted = [normalize(b) for b in block]
        for index in range(start, len(lines) - len(block) + 1):
            if [normalize(x) for x in lines[index : index + len(block)]] == wanted:
                return index
    return -1


def _apply_hunks(path: str, text: str, hunks: list[_Hunk]) -> str:
    lines = text.split("\n")
    trailing_newline = text.endswith("\n")
    if trailing_newline:
        lines.pop()
    cursor = 0
    for number, hunk in enumerate(hunks, 1):
        if hunk.anchor:
            at = _find(lines, [hunk.anchor], cursor)
            if at < 0:
                raise ToolError(f"{path}: hunk {number}: anchor {hunk.anchor!r} not found")
            cursor = at + 1
        if not hunk.old:
            at = len(lines) if hunk.end_of_file or not hunk.anchor else cursor
        elif hunk.end_of_file:
            at = len(lines) - len(hunk.old)
            if at < 0 or _find(lines, hunk.old, at) != at:
                raise ToolError(f"{path}: hunk {number}: context does not match the end of the file")
        else:
            at = _find(lines, hunk.old, cursor)
            if at < 0:
                raise ToolError(f"{path}: hunk {number}: context not found:\n" + "\n".join(hunk.old[:8]))
        lines[at : at + len(hunk.old)] = hunk.new
        cursor = at + len(hunk.new)
    return "\n".join(lines) + ("\n" if trailing_newline or not lines else "")


def _apply_patch(text: Any, ctx: ToolContext) -> str:
    if not isinstance(text, str):
        raise ToolError("patch must be a string")
    ops = parse_patch(text)
    writes: dict[Path, str | None] = {}  # None deletes
    summary: list[str] = []
    for op in ops:
        path = resolve(ctx, op.path)
        if op.kind == "add":
            if path.exists():
                raise ToolError(f"Add File {op.path}: already exists")
            writes[path] = "\n".join(op.lines) + "\n"
            summary.append(f"added {op.path} ({len(op.lines)} lines)")
        elif op.kind == "delete":
            if not path.is_file():
                raise ToolError(f"Delete File {op.path}: does not exist")
            writes[path] = None
            summary.append(f"deleted {op.path}")
        else:
            if not path.is_file():
                raise ToolError(f"Update File {op.path}: does not exist")
            updated = _apply_hunks(op.path, path.read_text(), op.hunks)
            if op.move_to:
                destination = resolve(ctx, op.move_to)
                if destination.exists():
                    raise ToolError(f"Move to {op.move_to}: already exists")
                writes[path] = None
                writes[destination] = updated
                summary.append(f"updated {op.path} → {op.move_to} ({len(op.hunks)} hunks)")
            else:
                writes[path] = updated
                summary.append(f"updated {op.path} ({len(op.hunks)} hunks)")
    for path, content in writes.items():
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    return "\n".join(summary)


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    if "patch" in args and args["patch"] is not None:
        return _apply_patch(args["patch"], ctx)
    return _replace(args, ctx)


TOOL = Tool(
    ToolSpec(
        name="edit",
        description=(
            "Change existing files. Either give `path`, `old_string` and `new_string` for an exact replacement "
            "(old_string must be unique unless replace_all is true), or give `patch` alone in apply_patch format:\n"
            "*** Begin Patch\n*** Update File: path\n@@ optional anchor line\n context\n-removed\n+added\n"
            "*** Add File: path\n+line\n*** Delete File: path\n*** End Patch\n"
            "A patch is checked fully before anything is written."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
                "patch": {"type": "string", "description": "apply_patch text; use instead of path/old_string/new_string."},
            },
        },
    ),
    run,
    guidance="Change existing files: an exact replacement (`old_string` must be unique unless `replace_all`), or an "
             "apply_patch patch for several changes or files at once. Prefer it over rewriting whole files.",
)
