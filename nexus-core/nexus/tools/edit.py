"""`edit`: ordered, atomic replacements in one file, with conservative matching tiers."""
from __future__ import annotations

from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, resolve

# Matching tiers only normalize typography and line endings, never indentation.
_TYPOGRAPHY = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-"})


def _normalized(text: str, typography: bool, whitespace: bool) -> tuple[str, list[tuple[int, int]]]:
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        start = i
        char = text[i]
        if whitespace and char == "\r" and text[i:i + 2] == "\r\n":
            char = "\n"
            i += 1
        if whitespace and char == "\n":
            while chars and chars[-1] in (" ", "\t"):
                chars.pop()
                spans.pop()
        chars.append(char.translate(_TYPOGRAPHY) if typography else char)
        spans.append((start, i + 1))
        i += 1
    if whitespace:
        while chars and chars[-1] in (" ", "\t"):
            chars.pop()
            spans.pop()
    return "".join(chars), spans


def _matches(text: str, old: str) -> tuple[list[tuple[int, int]], bool]:
    exact: list[tuple[int, int]] = []
    at = 0
    while (at := text.find(old, at)) >= 0:
        exact.append((at, at + len(old)))
        at += len(old)
    if exact:
        return exact, False
    for typography, whitespace in ((True, False), (True, True)):
        haystack, spans = _normalized(text, typography, whitespace)
        needle, _ = _normalized(old, typography, whitespace)
        if not needle:
            continue
        found: list[tuple[int, int]] = []
        at = 0
        while (at := haystack.find(needle, at)) >= 0:
            found.append((spans[at][0], spans[at + len(needle) - 1][1]))
            at += len(needle)
        if found:
            return found, typography or whitespace
    return [], False


def _replace_now(display: str, path: Any, edits: list[dict[str, Any]]) -> str:
    if not path.is_file():
        raise ToolError(f"{display} does not exist")
    text = path.read_text(newline="")
    total = 0
    normalized = False
    for edit in edits:
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise ToolError("old_string (non-empty) and new_string must be strings")
        if old == new:
            raise ToolError("old_string and new_string are identical")
        matches, fuzzy = _matches(text, old)
        if not matches:
            raise ToolError(f"old_string was not found in {display}")
        if len(matches) > 1 and not edit.get("replace_all"):
            raise ToolError(f"old_string occurs {len(matches)} times in {display}; add context or set replace_all")
        # Preserve CRLF files when a fallback matched a prompt using LF line endings.
        if fuzzy and "\r\n" in text and "\n" not in text.replace("\r\n", ""):
            new = new.replace("\r\n", "\n").replace("\n", "\r\n")
        for start, end in reversed(matches):
            text = text[:start] + new + text[end:]
        total += len(matches)
        normalized |= fuzzy
    # Every edit is validated in memory first: a failed later edit writes nothing.
    path.write_text(text, newline="")
    suffix = "; normalized matching" if normalized else ""
    return f"edited {display} ({total} replacement{'s' if total != 1 else ''}{suffix})"


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    if args.get("patch") is not None:
        raise ToolError("edit takes path, old_string and new_string; for a patch use the `patch` tool")
    path = resolve(ctx, args.get("path"))
    display = str(path) if ctx.cwd_changed else args["path"]
    if "edits" in args:
        if any(key in args for key in ("old_string", "new_string", "replace_all")):
            raise ToolError("use either edits or old_string/new_string, not both")
        edits = args["edits"]
        if not isinstance(edits, list) or not 1 <= len(edits) <= 64 or not all(isinstance(e, dict) for e in edits):
            raise ToolError("edits must contain between 1 and 64 replacement objects")
    else:
        edits = [args]
    if ctx.file_lock is None:
        return _replace_now(display, path, edits)
    async with ctx.file_lock:
        return _replace_now(display, path, edits)


TOOL = Tool(
    ToolSpec(
        name="edit",
        description=(
            "Replace text in one existing file. Exact matching is preferred; typography, trailing whitespace and CRLF "
            "differences are tolerated when exact text is absent. Matches must be unique unless replace_all is true. "
            "Use edits for ordered atomic replacements in one file, or patch for several files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string", "description": "The text to replace; include enough context for a unique match."},
                "new_string": {"type": "string", "description": "The replacement text."},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false."},
                "edits": {
                    "type": "array", "minItems": 1, "maxItems": 64,
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string"}, "new_string": {"type": "string"},
                            "replace_all": {"type": "boolean"},
                        },
                        "required": ["old_string", "new_string"], "additionalProperties": False,
                    },
                },
            },
            "required": ["path"],
            "anyOf": [{"required": ["old_string", "new_string"]}, {"required": ["edits"]}],
            "additionalProperties": False,
        },
    ),
    run,
)
