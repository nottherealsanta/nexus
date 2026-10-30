"""`patch`: a multi-file change in the Codex `apply_patch` format.

The format GPT models are trained on: `*** Begin Patch` … `*** End Patch` with
`Add File`, `Delete File` and `Update File` (optionally `Move to`) sections and
`@@ [anchor]` context hunks. Every operation is checked before any file is
written, so a patch applies completely or not at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, resolve
from .edit import MAX_PATCH_CHARS, _line_span


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


def _apply_hunks(path: str, text: str, hunks: list[_Hunk]) -> tuple[str, list[tuple[int, int]]]:
    """Apply hunks, also returning the original line spans they changed."""
    lines = text.split("\n")
    trailing_newline = text.endswith("\n")
    if trailing_newline:
        lines.pop()
    original_lines = len(lines)
    original_offsets = [0]
    for original_line in lines:
        original_offsets.append(original_offsets[-1] + len(original_line) + 1)
    cursor = 0
    shift = 0
    affected: list[tuple[int, int]] = []
    for number, hunk in enumerate(hunks, 1):
        anchor_at = -1
        if hunk.anchor:
            at = _find(lines, [hunk.anchor], cursor)
            if at < 0:
                raise ToolError(f"{path}: hunk {number}: anchor {hunk.anchor!r} not found")
            anchor_at = at
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
        original_at = at - shift
        if hunk.old:
            first, last = original_at + 1, original_at + len(hunk.old)
            if first <= last and original_lines:
                affected.append((max(1, first), min(original_lines, last)))
        elif original_lines:
            if hunk.end_of_file:
                line = original_lines
            elif hunk.anchor:
                line = min(max(anchor_at - shift + 1, 1), original_lines)
            else:
                line = min(max(original_at + 1, 1), original_lines)
            affected.append((line, line))
        lines[at : at + len(hunk.old)] = hunk.new
        shift += len(hunk.new) - len(hunk.old)
        cursor = at + len(hunk.new)
    # Patch matching uses LF-delimited lines; read uses splitlines(), which also
    # recognizes Unicode separators. Translate coverage back to read's numbering.
    visible_spans = [
        _line_span(text, original_offsets[first - 1], min(original_offsets[last], len(text)))
        for first, last in affected if first <= last
    ]
    return "\n".join(lines) + ("\n" if trailing_newline or not lines else ""), visible_spans


async def _apply_patch(text: Any, ctx: ToolContext) -> str:
    if not isinstance(text, str):
        raise ToolError("patch must be a string")
    ops = parse_patch(text)
    guard = ctx.files
    if guard is None:  # a bare tool context (unit use), not a Nexus session
        return _apply_patch_now(ops, ctx, None)
    async with guard.mutation():
        return _apply_patch_now(ops, ctx, guard)


def _apply_patch_now(ops: list[_Op], ctx: ToolContext, guard: Any) -> str:
    writes: dict[Path, str | None] = {}  # None deletes
    created: dict[Path, str] = {}
    forget: set[Path] = set()
    summary: list[str] = []
    for op in ops:
        path = resolve(ctx, op.path)
        shown = str(path) if ctx.cwd_changed else op.path
        if op.kind == "add":
            if path.exists():
                raise ToolError(f"Add File {shown}: already exists")
            writes[path] = "\n".join(op.lines) + "\n"
            created[path] = shown
            summary.append(f"added {shown} ({len(op.lines)} lines)")
        elif op.kind == "delete":
            if not path.is_file():
                raise ToolError(f"Delete File {shown}: does not exist")
            if guard is not None:
                guard.require(shown, path, complete=True)
            writes[path] = None
            forget.add(path)
            summary.append(f"deleted {shown}")
        else:
            if not path.is_file():
                raise ToolError(f"Update File {shown}: does not exist")
            updated, affected = _apply_hunks(shown, path.read_text(newline=""), op.hunks)
            if op.move_to:
                # Moving is a whole-file operation: it needs a complete observation.
                if guard is not None:
                    guard.require(shown, path, complete=True)
                destination = resolve(ctx, op.move_to)
                destination_shown = str(destination) if ctx.cwd_changed else op.move_to
                if destination.exists():
                    raise ToolError(f"Move to {destination_shown}: already exists")
                writes[path] = None
                writes[destination] = updated
                created[destination] = destination_shown
                forget.update({path, destination})
                summary.append(f"updated {shown} → {destination_shown} ({len(op.hunks)} hunks)")
            else:
                if guard is not None:
                    if affected:
                        guard.require(shown, path, affected=affected)
                    else:  # a pure insertion into an empty file
                        guard.require(shown, path, complete=True)
                writes[path] = updated
                forget.add(path)
                summary.append(f"updated {shown} ({len(op.hunks)} hunks)")
    for path, display in created.items():
        if path.exists():  # appeared while the rest of the patch was validated
            raise ToolError(f"Add File {display}: destination appeared while the patch was being validated")
    forget.update(created)
    for path, content in writes.items():
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path in created:
                with path.open("x") as destination:
                    destination.write(content)
            else:
                path.write_text(content)
    if guard is not None:
        guard.forget(forget)
    return "\n".join(summary)


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    return await _apply_patch(args.get("patch"), ctx)


TOOL = Tool(
    ToolSpec(
        name="patch",
        description=(
            "Change several places or several files at once, in apply_patch format. A patch is checked fully "
            "before anything is written, so it applies completely or not at all. Each file section is one of:\n"
            "*** Begin Patch\n"
            "*** Update File: path\n@@ optional anchor line\n context\n-removed\n+added\n"
            "*** Add File: path\n+line\n"
            "*** Delete File: path\n"
            "*** End Patch\n"
            "The lines a hunk changes (and a deleted or moved file in full) must have been read in this session; "
            "after any change or external edit, read again before the next change."
        ),
        parameters={
            "type": "object",
            "properties": {"patch": {"type": "string", "description": "The complete patch text."}},
            "required": ["patch"],
            "additionalProperties": False,
        },
    ),
    run,
)
